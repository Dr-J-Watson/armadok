"""The built-in roles, computed from the rights the enabled modules declare.

Each node-scoped right names a `level`; a role is the set of levels it includes, plus whatever
those rights depend on (so a role never contains a right that cannot work). Built-in roles
cannot be edited, only duplicated. Adding a right with a level adds it to the matching roles
without touching this file.
"""

from __future__ import annotations

from armadok.core.module import Right

ALL_LEVELS = ("view", "operate", "develop", "edit", "domains", "admin", "audit", "owner")

# role -> the levels it includes. `owner` is everything, including what cannot be delegated.
LEVELS_OF: dict[str, tuple[str, ...]] = {
    "viewer": ("view",),
    "operator": ("view", "operate"),
    "developer": ("view", "operate", "develop"),
    "editor": ("view", "edit"),
    "domains": ("domains",),
    "auditor": ("audit",),
    "administrator": ("view", "operate", "develop", "edit", "domains", "admin", "audit"),
    "owner": ALL_LEVELS,
}


def role_id(name: str) -> str:
    """Return the stored id of a built-in role."""
    return f"builtin-{name}"


def builtin_roles(catalogue: dict[str, Right]) -> dict[str, tuple[str, ...]]:
    """Return `{role name: rights}` for every built-in role, dependencies included."""
    roles: dict[str, tuple[str, ...]] = {}
    for name, levels in LEVELS_OF.items():
        wanted: set[str] = set()
        pending = [r.id for r in catalogue.values() if r.level in levels]
        while pending:
            right = pending.pop()
            if right in wanted or right not in catalogue:
                continue
            wanted.add(right)
            pending.extend(catalogue[right].depends_on)
        # A dependency may itself be a platform right with no level: it cannot be given per
        # node, so it is not part of a role (the role would be unusable either way).
        roles[name] = tuple(sorted(r for r in wanted if catalogue[r].level is not None))
    return roles
