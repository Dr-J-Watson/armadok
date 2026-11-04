from armadok.modules.traefik import coherence, parse


def summaries(**files):
    return [parse.summarize(path, text) for path, text in files.items()]


def codes(found):
    return sorted((i["code"], i["subject"]) for i in found)


GOOD = """
http:
  routers:
    blog:
      rule: Host(`blog.example.com`)
      service: blog
      middlewares: [secure]
  services:
    blog:
      loadBalancer:
        servers:
          - url: http://blog:80
  middlewares:
    secure:
      headers:
        frameDeny: true
"""


def test_a_consistent_setup_has_nothing_to_report():
    assert coherence.analyse(summaries(**{"a.yml": GOOD})) == []


def test_broken_references_are_errors_and_other_providers_are_not_blamed():
    text = """
http:
  routers:
    a:
      rule: Host(`a.example.com`)
      service: gone
      middlewares: [missing, from-docker@docker]
    b:
      rule: Host(`b.example.com`)
      service: whoami@docker
    c:
      service: gone
    d:
      rule: Host(`d.example.com`)
"""
    found = codes(coherence.analyse(summaries(**{"a.yml": text})))
    assert ("router.service_missing", "a@file") in found
    assert ("router.middleware_missing", "a@file") in found
    assert ("router.no_rule", "c@file") in found
    assert ("router.no_service", "d@file") in found
    assert not any(subject == "b@file" for _, subject in found)  # @docker is not ours to check
    assert not any("docker" in subject for _, subject in found)
    assert all(i["severity"] == "error" for i in coherence.analyse(summaries(**{"a.yml": text})))


def test_chain_loops_are_found_once_and_missing_members_are_reported():
    text = """
http:
  middlewares:
    one:
      chain:
        middlewares: [two]
    two:
      chain:
        middlewares: [three, ghost]
    three:
      chain:
        middlewares: [one]
    fine:
      headers:
        frameDeny: true
"""
    found = coherence.analyse(summaries(**{"m.yml": text}))
    loops = [i for i in found if i["code"] == "chain.loop"]
    assert len(loops) == 1 and loops[0]["detail"] == "one@file → two@file → three@file → one@file"
    assert ("chain.middleware_missing", "two@file") in codes(found)


def test_the_same_name_in_two_files_and_the_same_site_twice_are_flagged():
    other = GOOD.replace("blog:\n      rule", "blog2:\n      rule", 1)
    found = coherence.analyse(summaries(**{"a.yml": GOOD, "b.yml": GOOD}))
    assert ("duplicate.router", "blog@file") in codes(found)
    assert ("duplicate.service", "blog@file") in codes(found)
    twice = coherence.analyse(summaries(**{"a.yml": GOOD, "b.yml": other}))
    assert any(i["code"] == "router.duplicate_rule" for i in twice)


def test_unused_services_and_middlewares_are_only_information_and_sorted_last():
    text = GOOD + "    spare:\n      compress: {}\n"
    text = text.replace(
        "  middlewares:\n    secure:",
        "  middlewares:\n    unused:\n      compress: {}\n    secure:",
    )
    found = coherence.analyse(summaries(**{"a.yml": text}))
    assert ("middleware.unused", "unused@file") in codes(found)
    assert {i["severity"] for i in found} == {"info"}


def test_a_file_that_cannot_be_read_does_not_break_the_check():
    found = coherence.analyse(summaries(**{"bad.yml": "http: [unclosed", "a.yml": GOOD}))
    assert found == []
