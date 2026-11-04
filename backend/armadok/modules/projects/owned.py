"""What the ownership module needs to know about projects: which ones exist."""

from __future__ import annotations

from fastapi import Request

from armadok.core.registry import Registry
from armadok.modules.projects import schemas
from armadok.modules.workers.access import ask


async def project_ids(request: Request, registry: Registry, node_id: str) -> list[str]:
    """Return the names of the project folders on the machine (the worker's answer, checked)."""
    answer = await ask(request, registry, node_id, "projects.list", {}, schemas.ProjectList)
    return [project.name for project in answer.projects]
