"""Pure, disabled-by-default canonical-label object-storage contract.

This module performs no environment, credential, file, network or SDK discovery.
Types and hashes establish data shape, never tenant access, renderer provenance,
durable deletion authority, provider storage or SQL publication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Callable, Protocol, Union
from uuid import UUID


MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_CONFIG_BYTES = 4096
_HEX64 = re.compile(r"[0-9a-f]{64}")
_ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")
_BUCKET = re.compile(r"[a-z][a-z0-9-]{1,61}[a-z0-9]")
_OWNER = re.compile(r"[0-9]{12}")
_REF_KEYS = frozenset((
    "version", "tenantId", "publicationId", "generationId", "kind", "sha256",
    "bytes", "contentType",
))
_REF_ATTRIBUTES = (
    "version", "tenant_id", "publication_id", "generation_id", "kind", "sha256",
    "bytes", "content_type",
)
_CONFIG_FIELDS = {
    "disabled": ("version", "enabled", "backend"),
    "r2_s3": ("version", "enabled", "backend", "configId", "bucket", "accountId",
              "jurisdiction", "credentialRef"),
    "aws_s3": ("version", "enabled", "backend", "configId", "bucket", "region",
               "expectedBucketOwner", "credentialRef"),
}
_CONFIG_ATTRIBUTES = {
    "disabled": ("version", "enabled", "backend"),
    "r2_s3": ("version", "enabled", "backend", "config_id", "bucket", "account_id",
              "jurisdiction", "credential_ref"),
    "aws_s3": ("version", "enabled", "backend", "config_id", "bucket", "region",
               "expected_bucket_owner", "credential_ref"),
}
_VALIDATION_CODES = frozenset((
    "LABEL_STORAGE_INVALID_REF", "LABEL_STORAGE_CONFIGURATION_INVALID",
    "LABEL_STORAGE_INVALID_CONTENT", "LABEL_STORAGE_DEPENDENCY_UNAVAILABLE",
))
_OPERATION_CODES = frozenset((
    "LABEL_STORAGE_DISABLED", "LABEL_STORAGE_CONFIGURATION_INVALID",
    "LABEL_STORAGE_DEPENDENCY_UNAVAILABLE", "LABEL_STORAGE_FORBIDDEN",
    "LABEL_STORAGE_NOT_FOUND", "LABEL_STORAGE_OUTCOME_UNKNOWN",
    "LABEL_STORAGE_CONFLICT", "LABEL_STORAGE_INTEGRITY_FAILED",
    "LABEL_STORAGE_RESPONSE_INVALID", "LABEL_STORAGE_RETRY_LATER",
    "LABEL_STORAGE_POLICY_BLOCKED",
))
_OPERATIONS = frozenset(("put", "stat", "get", "delete"))
_COMMIT_STATES = frozenset((
    "not_started", "not_committed", "may_have_committed", "not_applicable",
))


class LabelStorageValidationError(ValueError):
    """Fixed safe code; rejected values are not stored on exception fields."""

    def __init__(self, code: str):
        safe = code if type(code) is str and code in _VALIDATION_CODES else (
            "LABEL_STORAGE_CONFIGURATION_INVALID"
        )
        self.code = safe
        super().__init__(safe)

    def __str__(self) -> str:
        return self.code if type(self.code) is str and self.code in _VALIDATION_CODES else (
            "LABEL_STORAGE_CONFIGURATION_INVALID"
        )


def _invalid_ref() -> None:
    raise LabelStorageValidationError("LABEL_STORAGE_INVALID_REF")


def _invalid_config() -> None:
    raise LabelStorageValidationError("LABEL_STORAGE_CONFIGURATION_INVALID")


def _invalid_content() -> None:
    raise LabelStorageValidationError("LABEL_STORAGE_INVALID_CONTENT")


def _plain_text(value: object) -> bool:
    return type(value) is str and not any(
        ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value
    )


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str or len(value) != 36 or not _plain_text(value):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _sha256(value: object) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _check_ref_values(values: tuple) -> None:
    version, tenant, publication, generation, kind, digest, size, content_type = values
    if type(version) is not int or version != 1:
        _invalid_ref()
    if not all(_canonical_uuid(value) for value in (tenant, publication, generation)):
        _invalid_ref()
    if type(kind) is not str or kind not in ("pdf", "zpl") or not _sha256(digest):
        _invalid_ref()
    if type(size) is not int or not 1 <= size <= MAX_ARTIFACT_BYTES:
        _invalid_ref()
    expected_type = "application/pdf" if kind == "pdf" else "application/octet-stream"
    if type(content_type) is not str or content_type != expected_type:
        _invalid_ref()


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    version: int
    tenant_id: str
    publication_id: str
    generation_id: str
    kind: str
    sha256: str
    bytes: int
    content_type: str

    def __post_init__(self) -> None:
        if type(self) is not ArtifactRef:
            _invalid_ref()
        _check_ref_values(tuple(getattr(self, key) for key in _REF_ATTRIBUTES))


def validate_ref(value: object) -> ArtifactRef:
    """Return detached canonical shape, without granting producer or access rights."""
    if type(value) is dict:
        if not all(type(key) is str for key in value) or set(value) != _REF_KEYS:
            _invalid_ref()
        values = tuple(value[key] for key in (
            "version", "tenantId", "publicationId", "generationId", "kind", "sha256",
            "bytes", "contentType",
        ))
    elif type(value) is ArtifactRef:
        values = None
        try:
            values = tuple(getattr(value, key) for key in _REF_ATTRIBUTES)
        except AttributeError:
            pass
        if values is None:
            _invalid_ref()
    else:
        _invalid_ref()
    _check_ref_values(values)
    return ArtifactRef(*values)


def _fresh_ref(ref: ArtifactRef) -> ArtifactRef:
    if type(ref) is not ArtifactRef:
        _invalid_ref()
    return validate_ref(ref)


def artifact_ref_dict(ref: ArtifactRef) -> dict:
    fresh = _fresh_ref(ref)
    return {
        "version": fresh.version, "tenantId": fresh.tenant_id,
        "publicationId": fresh.publication_id, "generationId": fresh.generation_id,
        "kind": fresh.kind, "sha256": fresh.sha256, "bytes": fresh.bytes,
        "contentType": fresh.content_type,
    }


def object_key(ref: ArtifactRef) -> str:
    fresh = _fresh_ref(ref)
    return "canonical-labels/v1/{}/{}/{}/labels.{}".format(
        UUID(fresh.tenant_id).hex, UUID(fresh.publication_id).hex,
        UUID(fresh.generation_id).hex, fresh.kind,
    )


def _check_config_values(backend: str, values: tuple) -> None:
    version, enabled, actual_backend = values[:3]
    if type(version) is not int or version != 1 or type(enabled) is not bool:
        _invalid_config()
    if type(actual_backend) is not str or actual_backend != backend:
        _invalid_config()
    if backend == "disabled":
        if enabled is not False:
            _invalid_config()
        return
    if enabled is not True:
        _invalid_config()
    config_id, bucket = values[3:5]
    if not _canonical_uuid(config_id) or type(bucket) is not str or not _BUCKET.fullmatch(bucket):
        _invalid_config()
    if type(values[-1]) is not str or values[-1] != "label-storage-primary":
        _invalid_config()
    if backend == "r2_s3":
        account, jurisdiction = values[5:7]
        if type(account) is not str or not _ACCOUNT_ID.fullmatch(account):
            _invalid_config()
        if type(jurisdiction) is not str or jurisdiction not in ("default", "us", "eu"):
            _invalid_config()
    elif backend == "aws_s3":
        region, owner = values[5:7]
        if type(region) is not str or region not in ("us-west-2", "us-east-1"):
            _invalid_config()
        if type(owner) is not str or not _OWNER.fullmatch(owner):
            _invalid_config()
    else:
        _invalid_config()


@dataclass(frozen=True, slots=True)
class DisabledStorageConfig:
    version: int = 1
    enabled: bool = False
    backend: str = "disabled"

    def __post_init__(self) -> None:
        if type(self) is not DisabledStorageConfig:
            _invalid_config()
        _check_config_values("disabled", (self.version, self.enabled, self.backend))


@dataclass(frozen=True, slots=True)
class R2StorageConfig:
    version: int
    enabled: bool
    backend: str
    config_id: str
    bucket: str
    account_id: str
    jurisdiction: str
    credential_ref: str

    def __post_init__(self) -> None:
        if type(self) is not R2StorageConfig:
            _invalid_config()
        _check_config_values("r2_s3", tuple(getattr(self, key) for key in _CONFIG_ATTRIBUTES["r2_s3"]))


@dataclass(frozen=True, slots=True)
class AwsStorageConfig:
    version: int
    enabled: bool
    backend: str
    config_id: str
    bucket: str
    region: str
    expected_bucket_owner: str
    credential_ref: str

    def __post_init__(self) -> None:
        if type(self) is not AwsStorageConfig:
            _invalid_config()
        _check_config_values("aws_s3", tuple(getattr(self, key) for key in _CONFIG_ATTRIBUTES["aws_s3"]))


StorageConfig = Union[DisabledStorageConfig, R2StorageConfig, AwsStorageConfig]
_CONFIG_CLASSES = {
    "disabled": DisabledStorageConfig, "r2_s3": R2StorageConfig, "aws_s3": AwsStorageConfig,
}


def _fresh_config(config: StorageConfig) -> StorageConfig:
    backend = next((key for key, cls in _CONFIG_CLASSES.items() if type(config) is cls), None)
    if backend is None:
        _invalid_config()
    values = None
    try:
        values = tuple(getattr(config, key) for key in _CONFIG_ATTRIBUTES[backend])
    except AttributeError:
        pass
    if values is None:
        _invalid_config()
    _check_config_values(backend, values)
    return _CONFIG_CLASSES[backend](*values)


def _unique_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid_config()
        result[key] = value
    return result


def _reject_json_number(value: str) -> None:
    _invalid_config()


def _bounded_json_int(value: str) -> int:
    if len(value) > 16:
        _invalid_config()
    return int(value)


def parse_storage_config(raw: str | None) -> StorageConfig:
    """Parse explicit bounded configuration; never read the process environment."""
    if raw is None:
        return DisabledStorageConfig()
    if type(raw) is not str or len(raw) > MAX_CONFIG_BYTES or "\x00" in raw:
        _invalid_config()
    parse_failed = False
    value = None
    try:
        if len(raw.encode("utf-8", errors="strict")) > MAX_CONFIG_BYTES:
            _invalid_config()
        value = json.loads(
            raw, object_pairs_hook=_unique_pairs, parse_int=_bounded_json_int,
            parse_float=_reject_json_number, parse_constant=_reject_json_number,
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        parse_failed = True
    if parse_failed:
        _invalid_config()
    if type(value) is not dict or not all(type(key) is str for key in value):
        _invalid_config()
    backend = value.get("backend")
    if type(backend) is not str or backend not in _CONFIG_FIELDS:
        _invalid_config()
    if set(value) != set(_CONFIG_FIELDS[backend]):
        _invalid_config()
    values = tuple(value[key] for key in _CONFIG_FIELDS[backend])
    _check_config_values(backend, values)
    return _CONFIG_CLASSES[backend](*values)


def storage_config_dict(config: StorageConfig) -> dict:
    fresh = _fresh_config(config)
    return dict(zip(
        _CONFIG_FIELDS[fresh.backend],
        (getattr(fresh, key) for key in _CONFIG_ATTRIBUTES[fresh.backend]),
    ))


@dataclass(frozen=True, slots=True)
class PutObservation:
    outcome: str
    ref: ArtifactRef

    def __post_init__(self) -> None:
        if type(self) is not PutObservation:
            _invalid_content()
        if type(self.outcome) is not str or self.outcome not in ("created", "precondition_failed"):
            _invalid_content()
        object.__setattr__(self, "ref", _fresh_ref(self.ref))


@dataclass(frozen=True, slots=True)
class StatObservation:
    outcome: str
    ref: ArtifactRef
    bytes: int | None
    content_type: str | None
    metadata_sha256: str | None

    def __post_init__(self) -> None:
        if type(self) is not StatObservation:
            _invalid_content()
        ref = _fresh_ref(self.ref)
        if type(self.outcome) is not str or self.outcome not in ("present", "not_found_observed"):
            _invalid_content()
        if self.outcome == "present":
            if type(self.bytes) is not int or self.bytes != ref.bytes:
                _invalid_content()
            if type(self.content_type) is not str or self.content_type != ref.content_type:
                _invalid_content()
            if type(self.metadata_sha256) is not str or self.metadata_sha256 != ref.sha256:
                _invalid_content()
        elif any(value is not None for value in (self.bytes, self.content_type, self.metadata_sha256)):
            _invalid_content()
        object.__setattr__(self, "ref", ref)


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    """Complete matching bytes only; no PDF, renderer, storage or access proof."""

    ref: ArtifactRef
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self) is not VerifiedArtifact:
            _invalid_content()
        ref = _fresh_ref(self.ref)
        if type(self.content) is not bytes or len(self.content) != ref.bytes:
            _invalid_content()
        if hashlib.sha256(self.content).hexdigest() != ref.sha256:
            _invalid_content()
        object.__setattr__(self, "ref", ref)


@dataclass(frozen=True, slots=True)
class DeleteObservation:
    outcome: str
    ref: ArtifactRef

    def __post_init__(self) -> None:
        if type(self) is not DeleteObservation:
            _invalid_content()
        if type(self.outcome) is not str or self.outcome != "delete_accepted":
            _invalid_content()
        object.__setattr__(self, "ref", _fresh_ref(self.ref))


class LabelStorageError(Exception):
    """Sanitized operation error; retryable never grants mutation authority."""

    def __init__(self, code: str, operation: str, commit_state: str, retryable: bool):
        if type(self) is not LabelStorageError:
            _invalid_content()
        LabelStorageError._check(code, operation, commit_state, retryable)
        self.code = code
        self.operation = operation
        self.commit_state = commit_state
        self.retryable = retryable
        super().__init__(code)

    @staticmethod
    def _check(code: object, operation: object, commit_state: object, retryable: object) -> None:
        if type(code) is not str or code not in _OPERATION_CODES:
            _invalid_content()
        if type(operation) is not str or operation not in _OPERATIONS:
            _invalid_content()
        if type(commit_state) is not str or commit_state not in _COMMIT_STATES:
            _invalid_content()
        if type(retryable) is not bool:
            _invalid_content()

    def as_dict(self) -> dict:
        if type(self) is not LabelStorageError:
            _invalid_content()
        values = None
        try:
            values = (self.code, self.operation, self.commit_state, self.retryable)
        except AttributeError:
            pass
        if values is None:
            _invalid_content()
        LabelStorageError._check(*values)
        return dict(zip(("code", "operation", "commitState", "retryable"), values))

    def __str__(self) -> str:
        code = getattr(self, "code", None)
        return code if type(code) is str and code in _OPERATION_CODES else (
            "LABEL_STORAGE_RESPONSE_INVALID"
        )


class ObjectStore(Protocol):
    def put_if_absent(self, ref: ArtifactRef, content: bytes) -> PutObservation: ...
    def stat(self, ref: ArtifactRef) -> StatObservation: ...
    def open_verified(self, ref: ArtifactRef) -> VerifiedArtifact: ...
    def delete_tombstoned(
        self, ref: ArtifactRef, *, authorize: Callable[[ArtifactRef], None],
    ) -> DeleteObservation: ...


class DisabledAdapter:
    """Unavailable capability, with no content, callback or factory evaluation."""

    __slots__ = ()

    def put_if_absent(self, ref: ArtifactRef, content: bytes) -> PutObservation:
        raise LabelStorageError("LABEL_STORAGE_DISABLED", "put", "not_started", False)

    def stat(self, ref: ArtifactRef) -> StatObservation:
        raise LabelStorageError("LABEL_STORAGE_DISABLED", "stat", "not_started", False)

    def open_verified(self, ref: ArtifactRef) -> VerifiedArtifact:
        raise LabelStorageError("LABEL_STORAGE_DISABLED", "get", "not_started", False)

    def delete_tombstoned(
        self, ref: ArtifactRef, *, authorize: Callable[[ArtifactRef], None],
    ) -> DeleteObservation:
        raise LabelStorageError("LABEL_STORAGE_DISABLED", "delete", "not_started", False)


def build_object_store(
    config: StorageConfig, *, enabled_factory: Callable[[StorageConfig], ObjectStore] | None = None,
) -> ObjectStore:
    """Internal injection only. No SDK, credentials, fallback, cache or discovery."""
    fresh = _fresh_config(config)
    if type(fresh) is DisabledStorageConfig:
        return DisabledAdapter()
    if enabled_factory is None or not callable(enabled_factory):
        raise LabelStorageValidationError("LABEL_STORAGE_DEPENDENCY_UNAVAILABLE")
    factory_failed = False
    store = None
    try:
        store = enabled_factory(fresh)
        if not all(callable(getattr(store, name, None)) for name in (
            "put_if_absent", "stat", "open_verified", "delete_tombstoned",
        )):
            factory_failed = True
    except Exception:
        factory_failed = True
    if factory_failed:
        raise LabelStorageValidationError("LABEL_STORAGE_DEPENDENCY_UNAVAILABLE")
    return store
