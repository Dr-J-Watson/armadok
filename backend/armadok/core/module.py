"""Declarative description of a feature module.

A feature (docker, traefik, dns, ...) is a package that exposes one `Module`
object listing everything it contributes. The core reads these objects through
the registry and never needs to know a feature by name.

Every field except `name` is optional: a module declares only what it provides.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
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
    # Sensitive rights (terminal, reading secrets, managing rights or keys) are never
    # available to API keys and are flagged in red in the interface.
    sensitive: bool = False
    # Baseline rights are granted to every new account (e.g. managing one's own API keys).
    baseline: bool = False
    # Rights the creator of a node receives on it (scope `node:<id>`), whichever module owns them.
    node_owner: bool = False


@dataclass(frozen=True)
class RelationObject:
    """One object on the relations map (a container, a network, a router...).

    `type` and `id` identify it within a node; `label` is what the interface shows.
    """

    type: str
    id: str
    label: str


@dataclass(frozen=True)
class RelationLink:
    """A directed link between two objects, e.g. container --attached--> network.

    The link is stored once and read in both directions: each end sees it.
    """

    source: tuple[str, str]
    target: tuple[str, str]
    kind: str


@dataclass(frozen=True)
class RelationData:
    """What a provider knows about a node: its objects and the links between them."""

    objects: tuple[RelationObject, ...] = ()
    links: tuple[RelationLink, ...] = ()


@dataclass(frozen=True)
class RelationProvider:
    """Tells the relations service how to discover objects and links on a node.

    `provide(app, node_id, viewer)` is async and receives the `Principal` looking at the map, so
    a provider that enriches its answer with another module's data can check the viewer's
    right to that data. `right` is what the viewer needs on the node to see this provider's
    objects; `node_types` limits it to the machines it applies to.
    """

    name: str
    right: str
    node_types: tuple[str, ...]
    provide: Callable[[Any, str, Any], Awaitable[RelationData]]


@dataclass(frozen=True)
class WorkerOp:
    """An operation a worker may be asked to run.

    `node_types` lists the node types it makes sense for ("network", "docker", "full"); the
    hub refuses to send it to any other kind of node.
    """

    name: str
    node_types: tuple[str, ...]


@dataclass(frozen=True)
class NotificationEvent:
    """A kind of notification a module can raise.

    `right` is what a person needs on the node to be told about it (so nobody learns about an
    object they cannot see). `templates` hold the title and the body per language, with
    `{name}` placeholders filled from the event's parameters; they never contain secrets.
    """

    name: str
    severity: str  # "info", "warning" or "critical"
    right: str | None
    templates: dict[str, tuple[str, str]]  # language -> (title, body)
    # Minimum seconds between two notifications of this event for the same node: a worker that
    # keeps reconnecting must not become a stream of messages.
    cooldown: int = 0


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
    relations: tuple[RelationProvider, ...] = ()
    search: tuple[Any, ...] = ()
    notification_events: tuple[NotificationEvent, ...] = ()
    # Long-running jobs started with the application (async functions taking the app).
    tasks: tuple[Callable[[Any], Awaitable[None]], ...] = ()
    widgets: tuple[Any, ...] = ()
    nav: tuple[Any, ...] = ()
    i18n_namespaces: tuple[str, ...] = ()
    migrations: tuple[Any, ...] = ()
    worker_ops: tuple[WorkerOp, ...] = ()
    settings: tuple[Any, ...] = ()
    providers: tuple[Provider, ...] = ()
    # Turns a request into a `Principal`, or None if the credentials are not its kind.
    # Event listeners: pairs of (event name, async handler(app, **payload)).
    listeners: tuple[tuple[str, Callable[..., Any]], ...] = ()
    authenticator: Callable[[Any], Any] | None = None
    # Run once after migrations at start-up, with the connection, the settings and the registry
    # (e.g. generate a one-time install code, create the local node).
    startup: tuple[Callable[[sqlite3.Connection, Any, Any], None], ...] = ()
