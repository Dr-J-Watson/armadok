"""API errors carrying a stable code.

The server never sends sentences: the interface translates `code` (fr / en).
A missing right is a 403; an object the caller may not see is reported as a
plain 404 so its existence is not revealed.
"""

from __future__ import annotations


class ApiError(Exception):
    """Base class: an HTTP status and a stable, translatable error code."""

    status_code = 400
    code = "common.bad_request"

    def __init__(self, code: str | None = None) -> None:
        """Override the default code when a more specific one applies."""
        super().__init__(code or self.code)
        self.code = code or self.code


class Unauthenticated(ApiError):
    """No valid credentials were presented."""

    status_code = 401
    code = "auth.required"


class MissingRight(ApiError):
    """The caller can see the object but lacks the right for this action."""

    status_code = 403
    code = "rights.missing"


class NotFound(ApiError):
    """The object does not exist, or the caller is not allowed to know it does."""

    status_code = 404
    code = "common.not_found"
