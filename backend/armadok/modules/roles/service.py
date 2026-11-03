"""Checks shared by everything that gives, changes or takes away roles."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from armadok.core.errors import MissingRight
from armadok.core.rights import Grant, can_grant


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """Hold the write lock from the checks to the change, so nothing slips in between."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()  # everything inside lands together, or none of it does
    except BaseException:
        conn.rollback()
        raise


def may_give(who, catalogue, rights, scope, expires_at) -> None:
    """Refuse unless `who` could give every right of the list on that scope and duration."""
    for right in rights:
        if not can_grant(who, Grant(right, scope, expires_at), catalogue):
            raise MissingRight()
