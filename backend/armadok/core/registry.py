"""Registry that collects and validates the contributions of all enabled modules.

The registry is built once at start-up from the list of enabled modules. It
fails fast: any inconsistency raises `RegistryError` so the application refuses
to start instead of misbehaving later.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import Any

from armadok.core.module import (
    Module,
    NotificationEvent,
    Provider,
    RelationProvider,
    Right,
    WorkerOp,
)


class RegistryError(Exception):
    """Raised when the set of modules is inconsistent."""


class Registry:
    """Read-only view over the validated modules.

    Build it with `Registry(modules)` or `load_registry(names)`.
    """

    def __init__(self, modules: Iterable[Module]) -> None:
        """Index the modules and validate them; raise `RegistryError` on any inconsistency."""
        self._modules: dict[str, Module] = {}
        for module in modules:
            if module.name in self._modules:
                raise RegistryError(f"duplicate module: {module.name}")
            self._modules[module.name] = module
        self._validate()

    @property
    def modules(self) -> tuple[Module, ...]:
        """Enabled modules, in the order they were given."""
        return tuple(self._modules.values())

    def rights(self) -> dict[str, Right]:
        """All declared rights, keyed by id."""
        return {r.id: r for m in self.modules for r in m.rights}

    def relation_providers(self) -> tuple[RelationProvider, ...]:
        """Return every relation provider declared by the modules."""
        return tuple(r for m in self.modules for r in m.relations)

    def notification_events(self) -> dict[str, NotificationEvent]:
        """Return the notification events modules can raise, by name."""
        return {e.name: e for m in self.modules for e in m.notification_events}

    def node_owner_rights(self) -> tuple[str, ...]:
        """Return the rights a node's creator receives on that node."""
        return tuple(r.id for r in self.rights().values() if r.node_owner)

    def worker_ops(self) -> dict[str, WorkerOp]:
        """Return the closed list of operations modules allow workers to run, by name."""
        return {o.name: o for m in self.modules for o in m.worker_ops}

    def baseline_rights(self) -> tuple[str, ...]:
        """Return the rights every new account receives."""
        return tuple(r.id for r in self.rights().values() if r.baseline)

    def audit_events(self) -> set[str]:
        """Return every audit event type declared by the modules."""
        return {e for m in self.modules for e in m.audit_events}

    def providers(self, kind: str) -> dict[str, Provider]:
        """Return the providers of one kind (e.g. "dns"), keyed by name."""
        return {p.name: p for m in self.modules for p in m.providers if p.kind == kind}

    def authenticator(self) -> Callable[[Any], Any] | None:
        """Return one authenticator trying every module's authenticator in turn, if any.

        The first that recognises the credentials wins (sessions, API keys, ...).
        """
        found = [m.authenticator for m in self.modules if m.authenticator is not None]
        if not found:
            return None

        def chained(request: Any) -> Any:
            for authenticate in found:
                principal = authenticate(request)
                if principal is not None:
                    return principal
            return None

        return chained

    def _validate(self) -> None:
        """Run every start-up check; the first failure aborts."""
        self._check_requires()
        self._check_unique("right", [r.id for m in self.modules for r in m.rights])
        self._check_unique("audit event", [e for m in self.modules for e in m.audit_events])
        self._check_unique("signal", [s for m in self.modules for s in m.signals])
        self._check_unique(
            "notification event", [e.name for m in self.modules for e in m.notification_events]
        )
        self._check_unique("worker operation", [o.name for m in self.modules for o in m.worker_ops])
        self._check_unique("relation provider", [r.name for m in self.modules for r in m.relations])
        self._check_unique(
            "provider", [f"{p.kind}/{p.name}" for m in self.modules for p in m.providers]
        )
        self._check_right_dependencies()

    def _check_requires(self) -> None:
        for module in self.modules:
            for needed in module.requires:
                if needed not in self._modules:
                    raise RegistryError(f"module {module.name!r} requires {needed!r}, not enabled")

    @staticmethod
    def _check_unique(label: str, values: list[str]) -> None:
        seen: set[str] = set()
        for value in values:
            if value in seen:
                raise RegistryError(f"duplicate {label}: {value}")
            seen.add(value)

    def _check_right_dependencies(self) -> None:
        known = self.rights()
        for right in known.values():
            for dep in right.depends_on:
                if dep not in known:
                    raise RegistryError(f"right {right.id!r} depends on unknown right {dep!r}")


def load_registry(names: Iterable[str]) -> Registry:
    """Import `armadok.modules.<name>.module` for each name and build the registry.

    Each module file must expose a `module` attribute of type `Module`.
    """
    modules: list[Module] = []
    for name in names:
        imported = importlib.import_module(f"armadok.modules.{name}.module")
        module = getattr(imported, "module", None)
        if not isinstance(module, Module):
            raise RegistryError(f"armadok.modules.{name}.module has no `module` object")
        if module.name != name:
            raise RegistryError(f"module folder {name!r} declares name {module.name!r}")
        modules.append(module)
    return Registry(modules)
