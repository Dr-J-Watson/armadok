"""Editing one Traefik item in place: comments stay, only its lines change."""

import pytest

from armadok.modules.traefik import itemfile as items

FILE = """# my proxy
http:
  routers:
    blog:
      rule: Host(`blog.example.com`)
      service: blog
      middlewares:
        - auth@file
        - lan   # keep
    shop:
      rule: Host(`shop.example.com`)
      service: shop
      middlewares: [auth@file]

  # who may come in
  middlewares:
    auth:
      basicAuth:
        users:
          - "a:b"
    lan:
      ipAllowList:
        sourceRange: ["10.0.0.0/8"]
    both:
      chain:
        middlewares:
          - auth
          - lan
"""


def span(text, name, kind="middlewares"):
    return items.find(text, "http", kind, name)


def test_every_item_is_found_with_its_own_lines():
    found = {(s.kind, s.name): s for s in items.scan(FILE)}
    assert set(found) == {
        ("routers", "blog"),
        ("routers", "shop"),
        ("middlewares", "auth"),
        ("middlewares", "lan"),
        ("middlewares", "both"),
    }
    lines = FILE.split("\n")
    lan = found[("middlewares", "lan")]
    assert lines[lan.start].strip() == "lan:" and "ipAllowList" in lines[lan.start + 1]
    assert "both" not in "\n".join(lines[lan.start : lan.end])  # the next item is not included
    assert not any(s.disabled for s in found.values())


def test_the_value_of_an_item_reads_back_dedented():
    assert (
        items.item_yaml(FILE, span(FILE, "lan")) == 'ipAllowList:\n  sourceRange: ["10.0.0.0/8"]\n'
    )


def test_replacing_an_item_leaves_everything_else_untouched():
    new = items.replace(
        FILE, span(FILE, "lan"), "ipAllowList:\n  sourceRange:\n    - 192.168.0.0/16\n"
    )
    assert "192.168.0.0/16" in new and "10.0.0.0/8" not in new
    assert "# my proxy" in new and "# who may come in" in new and "- lan   # keep" in new
    assert new.count("both:") == 1
    assert items.item_yaml(new, span(new, "lan")).startswith("ipAllowList:")


def test_switching_off_comments_only_the_item_and_switching_on_gives_it_back():
    off = items.set_enabled(FILE, span(FILE, "lan"), False)
    assert "#~ ipAllowList:" in off and "    #~ lan:" in off
    assert span(off, "lan").disabled is True
    assert items.set_enabled(off, span(off, "lan"), True) == FILE
    # Traefik sees the switched-off item as comments: the file is still valid YAML without it.
    import yaml

    assert "lan" not in yaml.safe_load(off)["http"]["middlewares"]


def test_references_are_found_in_routers_and_chains_and_flow_lists_are_reported():
    lines, flow = items.references(FILE, "lan")
    text = FILE.split("\n")
    assert sorted(text[n].strip().split(" ")[1] for n in lines) == [
        "lan",
        "lan",
    ]  # blog and the chain
    assert flow == []
    lines, flow = items.references(FILE, "auth")
    assert len(lines) == 2 and flow == [
        "auth@file"
    ]  # blog + chain in lists; shop has a one-line list


def test_references_can_be_switched_off_and_back():
    lines, _ = items.references(FILE, "lan")
    off = items.set_references(FILE, lines, False)
    assert items.references(off, "lan")[0] == lines  # still found: they are only marked
    assert items.set_references(off, lines, True) == FILE


def test_adding_an_item_to_an_existing_section_a_missing_one_and_an_empty_file():
    new = items.add(FILE, "http", "middlewares", "gzip", "compress: {}\n")
    assert "    gzip:\n      compress: {}" in new and span(new, "gzip")
    only_routers = "http:\n  routers:\n    a:\n      rule: x\n"
    got = items.add(only_routers, "http", "middlewares", "gzip", "compress: {}\n")
    assert span(got, "gzip") and "  middlewares:" in got
    fresh = items.add("", "http", "middlewares", "gzip", "compress: {}\n")
    assert fresh == "http:\n  middlewares:\n    gzip:\n      compress: {}\n"
    with pytest.raises(items.ItemError, match="exists"):
        items.add(FILE, "http", "middlewares", "lan", "compress: {}\n")


def test_removing_an_item_removes_only_it():
    new = items.remove(FILE, span(FILE, "lan"))
    assert "ipAllowList" not in new and "both:" in new and "auth:" in new


def test_a_value_that_is_not_a_mapping_is_refused():
    for bad in ("", "- a\n", "just text\n", "a: [\n"):
        with pytest.raises(items.ItemError):
            items.check_value(bad)
