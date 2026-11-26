"""Exact D1 policy-evidence checksums and closed immutable comparison data.

Integrity verification does not establish genuine acceptance, owner authority,
current permission, compatibility, retention or a usable policy. Historical and
disabled evidence can be valid data. No business-document or current-clock
interpretation belongs to this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import hashlib
import hmac

__all__ = (
    "PolicyEvidence",
    "LabelPolicyEvidenceError",
    "document_hash",
    "notice_hash",
    "evidence_hash",
    "verify_evidence",
)

_MESSAGE = "Shared label policy evidence requires a fresh review."
_ERROR_ARGS = (_MESSAGE,)
_CODE = "INTAKE_LABEL_POLICY_EVIDENCE_INVALID"
_FORMAT = "ss_label_policy_document_utf8_v1"
_HEX = "0123456789abcdef"
_MAX_BODY_BYTES = 65_536
_MAX_REVISION = 9_223_372_036_854_775_807
_DOCUMENT_DOMAIN = b"ss.label.policy.document.v1"
_NOTICE_DOMAIN = b"ss.label.policy.notice.v1"
_EVIDENCE_DOMAIN = b"ss.label.policy.evidence.v1"


class LabelPolicyEvidenceError(Exception):
    """Fixed outward error with no rejected evidence in its payload."""

    __slots__ = ()

    def __init__(self):
        super().__init__(_MESSAGE)

    @property
    def code(self):
        return _CODE

    @property
    def args(self):
        return _ERROR_ARGS

    def __str__(self):
        return _MESSAGE

    def __repr__(self):
        return "LabelPolicyEvidenceError()"


def _safe(function):
    @wraps(function)
    def owned(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            pass
        # Leave the owned handler before constructing a fresh outward error.
        # Arbitrary caller chains and traceback-local erasure are not promised.
        raise LabelPolicyEvidenceError() from None

    return owned


def _require(condition):
    if not condition:
        raise ValueError("Invalid policy evidence structure.")


def _body_bytes(value):
    _require(type(value) is str and 1 <= len(value) <= _MAX_BODY_BYTES)
    _require("\x00" not in value)
    body = value.encode("utf-8", "strict")
    _require(1 <= len(body) <= _MAX_BODY_BYTES)
    return body


def _uuid(value):
    _require(type(value) is str and len(value) == 36)
    _require(all(
        character == "-" if index in (8, 13, 18, 23) else character in _HEX
        for index, character in enumerate(value)
    ))


def _hash_text(value):
    _require(type(value) is str and len(value) == 64)
    _require(all(character in _HEX for character in value))


def _instant(value):
    _require(type(value) is str and len(value) == 27)
    _require(
        value[4] == "-" and value[7] == "-" and value[10] == "T"
        and value[13] == ":" and value[16] == ":"
        and value[19] == "." and value[26] == "Z"
    )
    parts = (
        value[0:4], value[5:7], value[8:10], value[11:13],
        value[14:16], value[17:19], value[20:26],
    )
    _require(all("0" <= character <= "9" for part in parts for character in part))
    return datetime(*(int(part) for part in parts), tzinfo=timezone.utc)


def _envelope_atoms(
    *, envelope_version, tenant_id, revision, document_format,
    document_hash, notice_hash, accepted_enabled, accepted_by,
    accepted_at, evidence_use_expires_at,
):
    _require(type(envelope_version) is int and envelope_version == 1)
    _uuid(tenant_id)
    _require(type(revision) is int and 1 <= revision <= _MAX_REVISION)
    _require(type(document_format) is str and document_format == _FORMAT)
    _hash_text(document_hash)
    _hash_text(notice_hash)
    _require(type(accepted_enabled) is bool)
    _uuid(accepted_by)
    accepted = _instant(accepted_at)
    expires = _instant(evidence_use_expires_at)
    _require(expires > accepted)
    return (
        str(envelope_version).encode("ascii"),
        tenant_id.encode("ascii"),
        str(revision).encode("ascii"),
        document_format.encode("ascii"),
        document_hash.encode("ascii"),
        notice_hash.encode("ascii"),
        b"true" if accepted_enabled else b"false",
        accepted_by.encode("ascii"),
        accepted_at.encode("ascii"),
        evidence_use_expires_at.encode("ascii"),
    )


def _digest(domain, atoms):
    digest = hashlib.sha256(domain + b"\x00")
    for atom in atoms:
        digest.update(str(len(atom)).encode("ascii") + b":" + atom)
    return digest.hexdigest()


@_safe
def document_hash(body):
    """Hash exact bounded document bytes in the D1 document domain."""
    return _digest(_DOCUMENT_DOMAIN, (_body_bytes(body),))


@_safe
def notice_hash(body):
    """Hash exact bounded notice bytes in its separate D1 domain."""
    return _digest(_NOTICE_DOMAIN, (_body_bytes(body),))


@_safe
def evidence_hash(
    *, envelope_version, tenant_id, revision, document_format,
    document_hash, notice_hash, accepted_enabled, accepted_by,
    accepted_at, evidence_use_expires_at,
):
    """Hash the ten exact envelope facts; never grant policy authority."""
    atoms = _envelope_atoms(
        envelope_version=envelope_version, tenant_id=tenant_id,
        revision=revision, document_format=document_format,
        document_hash=document_hash, notice_hash=notice_hash,
        accepted_enabled=accepted_enabled, accepted_by=accepted_by,
        accepted_at=accepted_at, evidence_use_expires_at=evidence_use_expires_at,
    )
    return _digest(_EVIDENCE_DOMAIN, atoms)


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class PolicyEvidence:
    tenant_id: str
    revision: int
    policy_hash: str
    envelope_version: int
    document_format: str
    document_body: str
    document_hash: str
    notice_body: str
    notice_hash: str
    accepted_enabled: bool
    accepted_by: str
    accepted_at: str
    evidence_use_expires_at: str

    @_safe
    def __post_init__(self):
        _validate_evidence(self)

    def __repr__(self):
        return "<PolicyEvidence>"

    def __str__(self):
        return "<PolicyEvidence>"


def _validate_evidence(value):
    _require(type(value) is PolicyEvidence)
    _hash_text(value.policy_hash)
    _body_bytes(value.document_body)
    _body_bytes(value.notice_body)
    _envelope_atoms(
        envelope_version=value.envelope_version, tenant_id=value.tenant_id,
        revision=value.revision, document_format=value.document_format,
        document_hash=value.document_hash, notice_hash=value.notice_hash,
        accepted_enabled=value.accepted_enabled, accepted_by=value.accepted_by,
        accepted_at=value.accepted_at,
        evidence_use_expires_at=value.evidence_use_expires_at,
    )


@_safe
def verify_evidence(value):
    """Return the same exact record only when all three checksums match."""
    _validate_evidence(value)
    actual_document = document_hash(value.document_body)
    actual_notice = notice_hash(value.notice_body)
    actual_evidence = evidence_hash(
        envelope_version=value.envelope_version, tenant_id=value.tenant_id,
        revision=value.revision, document_format=value.document_format,
        document_hash=actual_document, notice_hash=actual_notice,
        accepted_enabled=value.accepted_enabled, accepted_by=value.accepted_by,
        accepted_at=value.accepted_at,
        evidence_use_expires_at=value.evidence_use_expires_at,
    )
    document_matches = hmac.compare_digest(actual_document, value.document_hash)
    notice_matches = hmac.compare_digest(actual_notice, value.notice_hash)
    evidence_matches = hmac.compare_digest(actual_evidence, value.policy_hash)
    _require(document_matches and notice_matches and evidence_matches)
    return value
