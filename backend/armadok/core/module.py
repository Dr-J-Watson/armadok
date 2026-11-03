"""Declarative description of a feature module.

A feature (docker, traefik, dns, ...) is a package that exposes one `Module`
object listing everything it contributes. The core reads these objects through
the registry and never needs to know a feature by name.

Every field except `name` is optional: a module declares only what it provides.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Right:
    """A fine-grained permission.

    `depends_on` lists rights that must be held for this one to be usable
    (e.g. writing records requires reading them).
    """

    id: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationSource:
    """Tells the relations service how to find the objects linked to one object type.

    `resolve(object_id)` returns the linked objects as `(type, id)` pairs.
    Rights filtering is applied by the core, never by the source.
    """

    object_type: str
    resolve: Callable[[str], list[tuple[str, str]]]


@dataclass(frozen=True)
class Provider:
    """An implementation of a provider kind (dns, git_host, notifier, scanner, template).

    `capabilities` is what the provider can do; the core checks capabilities,
    never the provider name.
    """

    kind: str
    name: str
    impl: type
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Module:
    """Everything one feature contributes to the application."""

    name: str
    # Names of modules that must be enabled for this one to work.
    requires: tuple[str, ...] = ()
    # API routes. Typed loosely so the core does not depend on the web framework here.
    routes: tuple[Any, ...] = ()
    rights: tuple[Right, ...] = ()
    audit_events: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    relations: tuple[RelationSource, ...] = ()
    search: tuple[Any, ...] = ()
    notification_events: tuple[str, ...] = ()
    widgets: tuple[Any, ...] = ()
    nav: tuple[Any, ...] = ()
    i18n_namespaces: tuple[str, ...] = ()
    migrations: tuple[Any, ...] = ()
    worker_ops: tuple[str, ...] = ()
    settings: tuple[Any, ...] = ()
    providers: tuple[Provider, ...] = ()
    # Turns a request into a `Principal` (or None). At most one module provides it.
    authenticator: Callable[[Any], Any] | None = None
    # Run once after migrations at start-up (e.g. generate a one-time install code).
    startup: tuple[Callable[[sqlite3.Connection], None], ...] = ()
