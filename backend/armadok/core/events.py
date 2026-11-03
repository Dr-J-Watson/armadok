"""Announcements between modules ("a node was deleted") without importing each other.

A module declares `listeners=(("node.deleted", handler),)`. The emitter calls `emit` and does
not know who listens. Handlers are async and receive the app plus the payload.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI


async def emit(app: FastAPI, name: str, /, **payload: Any) -> None:
    """Run every listener of `name`, in module order; an error in one stops the chain.

    `name` is positional-only so a payload may itself carry a key called `name` or `event`.
    """
    for module in app.state.registry.modules:
        for listener, handler in module.listeners:
            if listener == name:
                await handler(app, **payload)
