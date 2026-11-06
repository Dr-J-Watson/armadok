"""Certificates Traefik obtained, read from its `acme.json` WITHOUT ever returning a key.

`acme.json` holds the private keys of every certificate (and of the ACME account). They must not
leave the node, so this reads the file, extracts only public facts from each certificate (the
names it covers, who issued it, when it is valid) and drops everything else before answering.
Nothing from the file is logged, and an unreadable or odd file gives a plain error code that
quotes none of it.

The file is named by `ARMADOK_ACME_FILE` (an absolute path). It is read, never written.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID

from armadok.worker import nodeconfig, safefs
from armadok.worker.operation import Operation, OperationError

MAX_ACME_BYTES = 4 * 1024 * 1024  # a real acme.json with hundreds of certificates is far smaller
MAX_CERTIFICATES = 1000
MAX_NAMES = 100
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _location() -> tuple[str, str]:
    """Return `(folder, file name)` of acme.json, or fail if it is not configured."""
    configured = nodeconfig.effective("acme_file")
    if not configured:
        raise OperationError("worker.acme_not_configured")
    path = Path(configured)
    if not path.is_absolute() or not _FILE_NAME.fullmatch(path.name) or path.parent == Path("/"):
        raise OperationError("worker.acme_not_configured")
    return str(path.parent), path.name


def _text(value: str | None, limit: int = 253) -> str:
    return (value or "")[:limit]


def _issuer(cert: x509.Certificate) -> str:
    """Return a short, readable issuer name (organisation and common name)."""
    parts = []
    for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
        found = cert.issuer.get_attributes_for_oid(oid)
        if found:
            parts.append(str(found[0].value))
    return _text(" / ".join(parts), 200)


def _names(cert: x509.Certificate) -> list[str]:
    """Return the DNS names the certificate covers (alternative names, else the common name)."""
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names = list(san.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        names = []
    if not names:
        names = [str(a.value) for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    return sorted({_text(n.lower()) for n in names})[:MAX_NAMES]


def _describe(resolver: str, entry: Any) -> dict[str, Any] | None:
    """Return the public facts of one acme.json certificate entry, or None if it is unusable.

    Anything odd about one entry (bad base64, bad PEM, a malformed extension) skips that entry
    only: one strange certificate must not hide all the others.
    """
    try:
        if not isinstance(entry, dict) or not isinstance(entry.get("certificate"), str):
            return None
        chain = x509.load_pem_x509_certificates(base64.b64decode(entry["certificate"]))
        if not chain:
            return None
        leaf = chain[0]
        return {
            "resolver": _text(resolver, 60),
            "names": _names(leaf),
            "issuer": _issuer(leaf),
            "not_before": leaf.not_valid_before_utc.isoformat(),
            "not_after": leaf.not_valid_after_utc.isoformat(),
            "serial": format(leaf.serial_number, "x")[:64],
        }
    except (
        ValueError,
        TypeError,
        OverflowError,
        binascii.Error,
        x509.ExtensionNotFound,
        x509.DuplicateExtension,
        x509.UnsupportedGeneralNameType,
    ):
        return None


def _collect(document: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Describe the certificates of every resolver, stopping at the limit (before any parsing)."""
    found: list[dict[str, Any]] = []
    for resolver, section in document.items():
        entries = section.get("Certificates") if isinstance(section, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if len(found) >= MAX_CERTIFICATES:
                return found, True
            described = _describe(str(resolver), entry)
            if described is not None:
                found.append(described)
    return found, False


def certs_list(args: dict[str, Any]) -> dict[str, Any]:
    """Return the certificates of every ACME resolver in acme.json, public facts only.

    `args` is ignored on purpose: nothing a caller sends can steer what is read or returned.
    """
    folder, name = _location()
    dir_fd = safefs.open_dir(folder)
    try:
        data, _ = safefs.read_regular(dir_fd, name, MAX_ACME_BYTES)
    finally:
        os.close(dir_fd)
    document: Any = None
    try:
        try:
            document = json.loads(data)
        except (ValueError, RecursionError):
            raise OperationError("worker.acme_invalid") from None
        finally:
            del data  # the raw file (with the keys) is not needed any more
        if not isinstance(document, dict):
            raise OperationError("worker.acme_invalid")
        found, truncated = _collect(document)
    finally:
        # Whatever happens, no reference to the parsed file (and the keys in it) outlives this
        # call, not even through a traceback.
        del document
    return {"certificates": found, "truncated": truncated}


OPERATIONS: dict[str, Operation] = {"certs.list": certs_list}
