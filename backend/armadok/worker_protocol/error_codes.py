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
        "worker.history_failed",
        # Git (see `worker/git_ops.py`).
        "worker.git_missing",
        "worker.terminal_not_configured",
        "worker.git_auth",
        "worker.git_branch",
        "worker.git_not_found",
        "worker.git_failed",
        "worker.git_dirty",
        "worker.git_not_repo",
        "worker.git_unsafe_tree",
        "worker.git_address_not_allowed",
        "worker.metrics_not_configured",
        "worker.metrics_unreachable",
        "worker.accesslog_not_configured",
        "worker.accesslog_unreadable",
        # Volume backups (see `worker/backup_ops.py`).
        "worker.backups_not_configured",
        "worker.backup_failed",
        "worker.restore_failed",
        # Availability probes: why a probe got no answer (see `worker/uptime_ops.py`).
        "worker.probe_dns",
        "worker.probe_refused",
        "worker.probe_timeout",
        "worker.probe_tls",
        "worker.probe_failed",
        "worker.probe_address_not_allowed",
    }
)
