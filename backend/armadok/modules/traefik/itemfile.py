"""Edit one item (a router, a service, a middleware) of a Traefik file without touching the rest.

The files are written by people: they hold comments, a chosen order, quoting. So nothing here
re-dumps a whole file. An item is found by its position (YAML marks give the lines of each key),
and only its lines are replaced, switched off or removed.

*Switched off* means every line of the item is commented with the marker `#~ ` (right after the
indentation). Traefik sees comments, so the item no longer exists for it; the marker tells this
code that it was switched off here and can be put back exactly as it was. References to a
switched-off middleware (list entries of routers and chains) are switched off with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from armadok.modules.traefik import parse

MARK = "#~ "
SECTIONS = (("http", "routers"), ("http", "services"), ("http", "middlewares"))
_MARKED = re.compile(r"^(\s*)#~ ?(.*)$")


class ItemError(ValueError):
    """Raised with a short code when an item cannot be found or an edit is not possible."""


@dataclass(frozen=True)
class Span:
    """Where an item lives: its lines (end excluded), indentation, and whether it is off."""

    proto: str
    kind: str
    name: str
    start: int
    end: int
    indent: str
    disabled: bool


def unmark(text: str) -> str:
    """Return the text with every switched-off line written back as active (same line numbers)."""
    return "\n".join(_MARKED.sub(r"\1\2", line) for line in text.split("\n"))


def _compose(text: str) -> Any:
    try:
        return yaml.compose(text, Loader=parse._NoAliasLoader)  # noqa: SLF001
    except (yaml.YAMLError, RecursionError, ValueError) as error:
        raise ItemError("invalid_yaml") from error


def _get(mapping: Any, key: str) -> Any:
    if not isinstance(mapping, yaml.MappingNode):
        return None
    for k, v in mapping.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def _last_line(lines: list[str], start: int, end_mark_line: int, end_mark_column: int) -> int:
    """First line after an item: the node's end, minus trailing blank and comment lines."""
    # A block ends where the next token starts (indentation only before it on that line): that
    # line is not ours. A value that ends mid-line (a flow list) owns the whole line.
    before = lines[end_mark_line][:end_mark_column] if end_mark_line < len(lines) else ""
    end = end_mark_line + (0 if not before.strip() else 1)
    end = min(end, len(lines))
    while end > start + 1 and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1
    return end


def scan(text: str) -> list[Span]:
    """Find every item of the known sections, switched off or not."""
    lines = text.split("\n")
    active = unmark(text).split("\n")  # switched-off lines count as content, not as comments
    root = _compose("\n".join(active))
    spans: list[Span] = []
    if root is None:
        return spans
    for proto, kind in SECTIONS:
        section = _get(_get(root, proto), kind)
        if not isinstance(section, yaml.MappingNode):
            continue
        for key, value in section.value:
            if not isinstance(key, yaml.ScalarNode):
                continue
            start = key.start_mark.line
            end = _last_line(active, start, value.end_mark.line, value.end_mark.column)
            body = [ln for ln in lines[start:end] if ln.strip()]
            off = bool(body) and all(_MARKED.match(ln) for ln in body)
            indent = re.match(r"\s*", _MARKED.sub(r"\1\2", lines[start])).group(0)  # type: ignore[union-attr]
            spans.append(Span(proto, kind, key.value, start, end, indent, off))
    return spans


def find(text: str, proto: str, kind: str, name: str) -> Span:
    """Return the span of one item or raise `ItemError("not_found")`."""
    for span in scan(text):
        if (span.proto, span.kind, span.name) == (proto, kind, name):
            return span
    raise ItemError("not_found")


def _mark(line: str) -> str:
    if not line.strip() or _MARKED.match(line):
        return line
    indent = re.match(r"\s*", line).group(0)  # type: ignore[union-attr]
    return f"{indent}{MARK}{line[len(indent) :]}"


def _unmark_line(line: str) -> str:
    return _MARKED.sub(r"\1\2", line)


def set_enabled(text: str, span: Span, enabled: bool) -> str:
    """Switch an item off (comment its lines) or on (write them back)."""
    lines = text.split("\n")
    change = _unmark_line if enabled else _mark
    lines[span.start : span.end] = [change(line) for line in lines[span.start : span.end]]
    return "\n".join(lines)


def references(text: str, name: str) -> tuple[list[int], list[str]]:
    """Line numbers of the list entries naming a middleware, and the style problems found.

    Looks at `middlewares:` lists of routers and chains. A one-line list (`[a, b]`) cannot be
    edited line by line: it is reported, so that nobody believes the reference is gone.
    """
    root = _compose(unmark(text))
    wanted = {name, f"{name}@file"}
    lines: list[int] = []
    flow: list[str] = []
    http = _get(root, "http") if root is not None else None

    def visit(node: Any, parent_key: str) -> None:
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                visit(v, k.value if isinstance(k, yaml.ScalarNode) else "")
        elif isinstance(node, yaml.SequenceNode) and parent_key == "middlewares":
            for item in node.value:
                if isinstance(item, yaml.ScalarNode) and item.value in wanted:
                    if node.flow_style:
                        flow.append(item.value)
                    else:
                        lines.append(item.start_mark.line)

    if http is not None:
        for kind in ("routers", "middlewares"):
            visit(_get(http, kind), kind)
    return lines, flow


def set_references(text: str, lines_to_change: list[int], enabled: bool) -> str:
    """Switch the given list entries off or on."""
    parts = text.split("\n")
    change = _unmark_line if enabled else _mark
    for number in lines_to_change:
        parts[number] = change(parts[number])
    return "\n".join(parts)


def item_yaml(text: str, span: Span) -> str:
    """Return the item's value as YAML text, without its key and dedented."""
    body = [_unmark_line(line) for line in text.split("\n")[span.start + 1 : span.end]]
    widths = [len(line) - len(line.lstrip()) for line in body if line.strip()]
    cut = min(widths) if widths else 0
    return "\n".join(line[cut:] if line.strip() else "" for line in body).rstrip() + "\n"


def _block(name: str, value: str, indent: str) -> list[str]:
    inner = value.rstrip("\n").split("\n")
    return [f"{indent}{name}:"] + [f"{indent}  {line}" if line.strip() else "" for line in inner]


def check_value(value_text: str) -> dict[str, Any]:
    """Return the item as a mapping, or raise `ItemError("invalid_item")`."""
    try:
        value = parse.load(value_text)
    except parse.YamlError as error:
        raise ItemError("invalid_yaml") from error
    if not isinstance(value, dict) or not value:
        raise ItemError("invalid_item")
    return value


def replace(text: str, span: Span, value_text: str) -> str:
    """Write a new value for an item, in place."""
    check_value(value_text)
    lines = text.split("\n")
    lines[span.start : span.end] = _block(span.name, value_text, span.indent)
    return "\n".join(lines)


def remove(text: str, span: Span) -> str:
    """Delete an item (and nothing else)."""
    lines = text.split("\n")
    del lines[span.start : span.end]
    return "\n".join(lines)


def add(text: str, proto: str, kind: str, name: str, value_text: str) -> str:
    """Append an item to its section, creating the section when the file lacks it."""
    check_value(value_text)
    if any((s.proto, s.kind, s.name) == (proto, kind, name) for s in scan(text)):
        raise ItemError("exists")
    lines = text.rstrip("\n").split("\n") if text.strip() else []
    active = unmark("\n".join(lines)).split("\n")
    root = _compose("\n".join(active)) if text.strip() else None
    top = _get(root, proto) if root is not None else None
    section = _get(top, kind)
    if isinstance(section, yaml.MappingNode) and section.value:
        last_key, last_value = section.value[-1]
        end = _last_line(
            lines, last_key.start_mark.line, last_value.end_mark.line, last_value.end_mark.column
        )
        indent = re.match(r"\s*", lines[last_key.start_mark.line]).group(0)  # type: ignore[union-attr]
        lines[end:end] = _block(name, value_text, indent)
    elif isinstance(top, yaml.MappingNode) and top.value:
        # The protocol exists but not this section: add it at the end of the protocol's block.
        key, value = top.value[-1]
        end = _last_line(active, key.start_mark.line, value.end_mark.line, value.end_mark.column)
        indent = re.match(r"\s*", lines[key.start_mark.line]).group(0)  # type: ignore[union-attr]
        lines[end:end] = [f"{indent}{kind}:"] + _block(name, value_text, indent + "  ")
    elif top is not None and root is not None:
        raise ItemError("unsupported_layout")
    else:
        lines += (
            ([""] if lines else []) + [f"{proto}:", f"  {kind}:"] + _block(name, value_text, "    ")
        )
    return "\n".join(lines) + "\n"


_OVERVIEW_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def overview(path: str, text: str, sha256: str | None = None) -> dict[str, Any]:
    """Like `parse.summarize`, but switched-off items are kept and flagged `disabled`.

    Traefik does not load them; the interface still has to show them so they can be switched on.
    """
    key = (path, sha256) if sha256 else None
    if key and key in _OVERVIEW_CACHE:
        return _OVERVIEW_CACHE[key]
    try:
        summary = parse._summarize(path, unmark(text))  # noqa: SLF001
        off = {(s.kind, s.name): s.disabled for s in scan(text)}
        for kind in ("routers", "services", "middlewares"):
            for item in summary[kind]:
                item["disabled"] = off.get((kind, item["name"]), False)
    except (
        parse.YamlError,
        ItemError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        RecursionError,
    ):
        summary = {"path": path, **parse._EMPTY_ERROR}  # noqa: SLF001
    if key:
        if len(_OVERVIEW_CACHE) > 256:
            _OVERVIEW_CACHE.clear()
        _OVERVIEW_CACHE[key] = summary
    return summary


MAX_DEPTH = 12


def dump(value: Any) -> str:
    """Write an item's value as block-style YAML (keys in the order given, no anchors)."""

    def depth(node: Any, level: int = 0) -> int:
        if level > MAX_DEPTH:
            raise ItemError("invalid_item")
        if isinstance(node, dict):
            return max((depth(v, level + 1) for v in node.values()), default=level)
        if isinstance(node, list):
            return max((depth(v, level + 1) for v in node), default=level)
        return level

    if not isinstance(value, dict) or not value:
        raise ItemError("invalid_item")
    depth(value)
    return yaml.safe_dump(value, default_flow_style=False, sort_keys=False, allow_unicode=True)
