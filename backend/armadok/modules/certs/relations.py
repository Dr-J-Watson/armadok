"""Certificates on the relations map: a certificate linked to the domains it covers."""

from __future__ import annotations

from typing import Any

from armadok.core.errors import ApiError
from armadok.core.module import RelationData, RelationLink, RelationObject
from armadok.modules.certs import schemas, service
from armadok.modules.workers.routes import call_operation


async def provide(app: Any, node_id: str, viewer: Any) -> RelationData:
    """Return certificate objects, linked to the exact domain names they cover.

    Wildcard names are shown on the certificate but not linked (a link needs a domain object
    that another provider may or may not have drawn; the graph drops links to absent ends).
    """
    result = await call_operation(app, app.state.registry, node_id, "certs.list", {})
    try:
        certs = schemas.CertificateList.model_validate(result).certificates
    except ValueError:
        raise ApiError("worker.invalid_answer", 502) from None
    objects, links = [], []
    for cert in certs:
        ident = f"{cert.resolver}:{cert.serial}"
        label = cert.names[0] if cert.names else cert.serial
        objects.append(RelationObject("certificate", ident, label))
        for name in cert.names:
            if not name.startswith("*."):
                links.append(RelationLink(("certificate", ident), ("domain", name), "covers"))
    _ = service  # shared helpers live in service.py
    return RelationData(tuple(objects), tuple(links))
