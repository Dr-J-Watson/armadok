"""First-start setup: one-time install code and creation of the super-administrator."""

from __future__ import annotations

import hmac
import logging
import secrets
import sqlite3

from armadok.core.db import Migration
from armadok.core.grants import add_grant
from armadok.core.rights import Grant
from armadok.modules.users import repository
from armadok.modules.users.security import token_hash

log = logging.getLogger("armadok.setup")

# Platform rights of the first account. It gets no right on any node: an administrator has
# no implicit access to other people's nodes (it can create its own and then owns them).
SUPERADMIN_RIGHTS = (
    "users.read",
    "users.create",
    "users.update",
    "users.reset_password",
    "users.reset_2fa",
    "grants.manage",
    "audit.read",
    "nodes.create",
    "hooks.manage",
    "metrics.read",
    "service_accounts.manage",
    "nodes.admin",
    "emergency.use",
    "state_backup.manage",
    "vulns.read",
    "vulns.manage",
    "notifications.admin",
)


SUPERADMIN_MIGRATION = Migration(
    "users-0003-superadmin-rights-applied",
    "CREATE TABLE superadmin_rights_applied (right TEXT PRIMARY KEY);",
)


def ensure_superadmin_rights(
    conn: sqlite3.Connection, settings: object = None, registry: object = None
) -> None:
    """Give the installation's administrators the platform rights added since setup, once each.

    The first account gets its platform rights when it is created, so a right introduced by a
    later version would otherwise reach nobody on an existing installation (and nobody can
    grant what they do not hold). Each right is handed out once: if an administrator later
    takes it away, it is not given back at the next start.
    """
    done = {r["right"] for r in conn.execute("SELECT right FROM superadmin_rights_applied")}
    missing = [r for r in SUPERADMIN_RIGHTS if r not in done]
    if not missing:
        return
    admins = [
        row["principal_id"]
        for row in conn.execute(
            "SELECT DISTINCT g.principal_id FROM grants g JOIN users u ON u.id = g.principal_id"
            " WHERE g.granted_by = 'setup' AND g.right = 'users.read' AND u.is_active = 1"
        )
    ]
    for admin in admins:
        held = {
            (g["right"], g["scope"])
            for g in conn.execute(
                "SELECT right, scope FROM grants WHERE principal_id = ?", (admin,)
            )
        }
        for right in missing:
            if (right, "*") not in held:
                add_grant(conn, admin, Grant(right), granted_by="setup", commit=False)
    conn.executemany(
        "INSERT OR IGNORE INTO superadmin_rights_applied (right) VALUES (?)",
        [(r,) for r in missing],
    )
    conn.commit()


def ensure_install_code(
    conn: sqlite3.Connection, settings: object = None, registry: object = None
) -> None:
    """On a fresh install, create a one-time code and print it in the logs.

    Whoever can read the container logs proves they control the server. Does nothing once
    an account exists. Only a hash is stored; the code is shown nowhere else.
    """
    if repository.count_users(conn) > 0 or repository.get_setup_code_hash(conn):
        return
    code = secrets.token_urlsafe(12)
    repository.store_setup_code(conn, token_hash(code))
    log.warning("Armadok first start. Install code (use it once in the setup page): %s", code)


def run_setup(
    conn: sqlite3.Connection,
    code: str,
    username: str,
    password_hash: str,
    baseline: tuple[str, ...] = (),
) -> dict | None:
    """Burn the install code and create the super-administrator, all or nothing.

    Returns the user, or None if the code is wrong. Raises `SetupClosed` if an account
    already exists. Everything happens in one write transaction: if any step fails, the
    code is not burned and no half-created account remains.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if repository.count_users(conn) > 0:
            raise SetupClosed
        stored = repository.get_setup_code_hash(conn)
        if stored is None or not hmac.compare_digest(stored, token_hash(code)):
            conn.rollback()
            return None
        if not repository.delete_setup_code(conn, stored):
            conn.rollback()
            return None
        user = repository.create_user(conn, username, password_hash, commit=False)
        for right in (*SUPERADMIN_RIGHTS, *baseline):
            add_grant(conn, user["id"], Grant(right), granted_by="setup", commit=False)
        conn.executemany(
            "INSERT OR IGNORE INTO superadmin_rights_applied (right) VALUES (?)",
            [(r,) for r in SUPERADMIN_RIGHTS],
        )
        conn.commit()
        return user
    except BaseException:
        conn.rollback()
        raise


class SetupClosed(Exception):
    """Raised when setup is attempted after an account already exists."""
