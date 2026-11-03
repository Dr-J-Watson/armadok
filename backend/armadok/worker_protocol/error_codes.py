"""Error codes a worker may report to the hub.

A worker is untrusted: the hub passes on only codes from this list (anything else becomes
`worker.error`), so a compromised worker cannot invent a code that the interface or an API
client would treat as something else, such as a 403 or a 409.
"""

from __future__ import annotations

WORKER_ERROR_CODES = frozenset(
    {
        "worker.unknown_op",
        "worker.op_failed",
        "worker.too_large",
        "worker.docker_unavailable",
        "worker.docker_error",
        "worker.projects_not_configured",
        "worker.traefik_not_configured",
        "worker.acme_not_configured",
        "worker.acme_invalid",
        "worker.project_busy",
        "worker.compose_failed",
        "worker.timeout",
        "worker.busy",
        "worker.invalid_path",
        "worker.not_found",
        "worker.forbidden",
        "worker.conflict",
        "worker.not_text",
    }
)
