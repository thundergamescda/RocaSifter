"""Exact feature9 catalog attestation and explicit operator installation only.

No import-time IO, caller transaction/pool creation, app wiring or authority.
Normal lifecycle disposal remains unavailable; this module grants no roles.
"""
from dataclasses import dataclass
from functools import wraps
import hashlib
from pathlib import Path
import re
from types import MappingProxyType

SCHEMA_VERSION = 9
# Global ledger allocation is reserved centrally in hosted/database.py and schema.sql.
# Protocol, function names and physical guards remain version 9.
MIGRATION_VERSION = 16
SQL_TOKEN = '__SS_LABEL_SCHEMA__'
SQL_TOKEN_COUNT = 66
SQL_SHA256 = '9b3dd4eaac08e3af014ea0a7ea5da4279ab30d3e76488c7b73f2e10765368462'
SQL_MAX_BYTES = 262144
CATALOG_ROW_LIMIT = 2048

@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    type_name: str
    nullable: bool

@dataclass(frozen=True, slots=True)
class ConstraintSpec:
    name: str
    kind: str
    columns: tuple
    ref_table: str | None
    ref_columns: tuple
    expression: str | None

@dataclass(frozen=True, slots=True)
class IndexSpec:
    name: str
    columns: tuple
    unique: bool
    primary: bool

@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    columns: tuple
    constraints: tuple
    indexes: tuple
    guard_source: str

@dataclass(frozen=True, slots=True)
class SchemaState:
    version: int
    state: str
    table_count: int
    operator_lifecycle_available: bool = False

SCHEMA_SPEC = (
    TableSpec("ss_intake_label_inputs", (
        ColumnSpec("tenant_id", "uuid", False),
        ColumnSpec("id", "uuid", False),
        ColumnSpec("receiving_id", "uuid", False),
        ColumnSpec("server_revision", "bigint", False),
        ColumnSpec("plan_hash", "text", False),
        ColumnSpec("input_hash", "text", False),
        ColumnSpec("terminal_basis_hash", "text", False),
        ColumnSpec("confirmed_rows_hash", "text", False),
        ColumnSpec("row_count", "integer", False),
        ColumnSpec("total_copies", "integer", False),
        ColumnSpec("input_body", "text", True),
        ColumnSpec("content_state", "text", False),
        ColumnSpec("revision", "bigint", False),
        ColumnSpec("created_at", "timestamp with time zone", False),
        ColumnSpec("policy_hash", "text", False),
        ColumnSpec("retention_until", "timestamp with time zone", False),
        ColumnSpec("restricted_at", "timestamp with time zone", True),
        ColumnSpec("review_due_at", "timestamp with time zone", True),
        ColumnSpec("disposed_at", "timestamp with time zone", True),
        ColumnSpec("suppression_until", "timestamp with time zone", True),
    ), (
        ConstraintSpec("ss_intake_label_inputs_pk_v9", "p", ("tenant_id", "id"), None, (), None),
        ConstraintSpec("ss_intake_label_inputs_tenant_fk_v9", "f", ("tenant_id",), "ss_tenants", ("id",), None),
        ConstraintSpec("ss_intake_label_inputs_source_fk_v9", "f", ("tenant_id", "receiving_id"), "ss_receiving_operations", ("tenant_id", "id"), None),
        ConstraintSpec("ss_intake_label_inputs_hash_uq_v9", "u", ("tenant_id", "input_hash"), None, (), None),
        ConstraintSpec("ss_intake_label_inputs_terminal_uq_v9", "u", ("tenant_id", "terminal_basis_hash"), None, (), None),
        ConstraintSpec("ss_intake_label_inputs_identity_uq_v9", "u", ("tenant_id", "id", "receiving_id", "input_hash", "terminal_basis_hash"), None, (), None),
        ConstraintSpec("ss_intake_label_inputs_hashes_ck_v9", "c", (), None, (), "(((octet_length(plan_hash) = 64) AND (plan_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(input_hash) = 64) AND (input_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(terminal_basis_hash) = 64) AND (terminal_basis_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(confirmed_rows_hash) = 64) AND (confirmed_rows_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(policy_hash) = 64) AND (policy_hash ~ '^[0-9a-f]{64}$'::text)))"),
        ConstraintSpec("ss_intake_label_inputs_counts_ck_v9", "c", (), None, (), "((server_revision >= 0) AND (revision >= 1) AND ((row_count >= 1) AND (row_count <= 1000)) AND ((total_copies >= 1) AND (total_copies <= 1000000)) AND (total_copies >= row_count))"),
        ConstraintSpec("ss_intake_label_inputs_body_ck_v9", "c", (), None, (), "((input_body IS NULL) OR ((octet_length(input_body) >= 1) AND (octet_length(input_body) <= 8388608)))"),
        ConstraintSpec("ss_intake_label_inputs_lifecycle_ck_v9", "c", (), None, (), "((content_state = ANY (ARRAY['active'::text, 'restricted'::text, 'disposal_pending'::text, 'disposed'::text])) AND (retention_until > created_at) AND (((content_state = 'active'::text) AND (input_body IS NOT NULL) AND (restricted_at IS NULL) AND (review_due_at IS NULL) AND (disposed_at IS NULL) AND (suppression_until IS NULL)) OR ((content_state = ANY (ARRAY['restricted'::text, 'disposal_pending'::text])) AND (input_body IS NOT NULL) AND (restricted_at IS NOT NULL) AND (review_due_at IS NOT NULL) AND (disposed_at IS NULL) AND (suppression_until IS NULL)) OR ((content_state = 'disposed'::text) AND (input_body IS NULL) AND (restricted_at IS NOT NULL) AND (review_due_at IS NULL) AND (disposed_at IS NOT NULL) AND (suppression_until IS NOT NULL) AND (suppression_until > disposed_at))))"),
        ConstraintSpec("ss_intake_label_inputs_dates_ck_v9", "c", (), None, (), "((isfinite(created_at) AND (created_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (created_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)) AND (isfinite(retention_until) AND (retention_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (retention_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)) AND ((restricted_at IS NULL) OR (isfinite(restricted_at) AND (restricted_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (restricted_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND ((review_due_at IS NULL) OR (isfinite(review_due_at) AND (review_due_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (review_due_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND ((disposed_at IS NULL) OR (isfinite(disposed_at) AND (disposed_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (disposed_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND ((suppression_until IS NULL) OR (isfinite(suppression_until) AND (suppression_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (suppression_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))))"),
    ), (
        IndexSpec("ss_intake_label_inputs_pk_v9", ("tenant_id", "id"), True, True),
        IndexSpec("ss_intake_label_inputs_hash_uq_v9", ("tenant_id", "input_hash"), True, False),
        IndexSpec("ss_intake_label_inputs_terminal_uq_v9", ("tenant_id", "terminal_basis_hash"), True, False),
        IndexSpec("ss_intake_label_inputs_identity_uq_v9", ("tenant_id", "id", "receiving_id", "input_hash", "terminal_basis_hash"), True, False),
        IndexSpec("ss_intake_label_inputs_source_idx_v9", ("tenant_id", "receiving_id", "server_revision", "plan_hash"), False, False),
        IndexSpec("ss_intake_label_inputs_retention_idx_v9", ("tenant_id", "content_state", "retention_until"), False, False),
    ), "\nBEGIN\n  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NEW.content_state <> 'active' OR NEW.revision <> 1 OR NEW.input_body IS NULL\n     OR NEW.restricted_at IS NOT NULL OR NEW.review_due_at IS NOT NULL\n     OR NEW.disposed_at IS NOT NULL OR NEW.suppression_until IS NOT NULL\n     OR NEW.retention_until <= pg_catalog.clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  RETURN NEW;\nEND;\n"),
    TableSpec("ss_intake_label_jobs", (
        ColumnSpec("tenant_id", "uuid", False),
        ColumnSpec("id", "uuid", False),
        ColumnSpec("receiving_id", "uuid", False),
        ColumnSpec("input_id", "uuid", False),
        ColumnSpec("input_hash", "text", False),
        ColumnSpec("terminal_basis_hash", "text", False),
        ColumnSpec("job_basis_hash", "text", False),
        ColumnSpec("renderer_build_hash", "text", False),
        ColumnSpec("purpose", "text", False),
        ColumnSpec("reprint_of_job_id", "uuid", True),
        ColumnSpec("copy_offset", "integer", False),
        ColumnSpec("copy_count", "integer", False),
        ColumnSpec("part_size", "integer", False),
        ColumnSpec("part_count", "integer", False),
        ColumnSpec("render_plan_hash", "text", False),
        ColumnSpec("created_by", "uuid", False),
        ColumnSpec("created_at", "timestamp with time zone", False),
        ColumnSpec("policy_hash", "text", False),
        ColumnSpec("retention_until", "timestamp with time zone", False),
    ), (
        ConstraintSpec("ss_intake_label_jobs_pk_v9", "p", ("tenant_id", "id"), None, (), None),
        ConstraintSpec("ss_intake_label_jobs_basis_uq_v9", "u", ("tenant_id", "job_basis_hash"), None, (), None),
        ConstraintSpec("ss_intake_label_jobs_input_identity_uq_v9", "u", ("tenant_id", "id", "input_id"), None, (), None),
        ConstraintSpec("ss_intake_label_jobs_input_fk_v9", "f", ("tenant_id", "input_id", "receiving_id", "input_hash", "terminal_basis_hash"), "ss_intake_label_inputs", ("tenant_id", "id", "receiving_id", "input_hash", "terminal_basis_hash"), None),
        ConstraintSpec("ss_intake_label_jobs_source_fk_v9", "f", ("tenant_id", "receiving_id"), "ss_receiving_operations", ("tenant_id", "id"), None),
        ConstraintSpec("ss_intake_label_jobs_staff_fk_v9", "f", ("tenant_id", "created_by"), "ss_staff", ("tenant_id", "id"), None),
        ConstraintSpec("ss_intake_label_jobs_parent_fk_v9", "f", ("tenant_id", "reprint_of_job_id", "input_id"), "ss_intake_label_jobs", ("tenant_id", "id", "input_id"), None),
        ConstraintSpec("ss_intake_label_jobs_hashes_ck_v9", "c", (), None, (), "(((octet_length(input_hash) = 64) AND (input_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(terminal_basis_hash) = 64) AND (terminal_basis_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(job_basis_hash) = 64) AND (job_basis_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(renderer_build_hash) = 64) AND (renderer_build_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(render_plan_hash) = 64) AND (render_plan_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(policy_hash) = 64) AND (policy_hash ~ '^[0-9a-f]{64}$'::text)))"),
        ConstraintSpec("ss_intake_label_jobs_range_ck_v9", "c", (), None, (), "(((copy_offset >= 0) AND (copy_offset <= 999999)) AND ((copy_count >= 1) AND (copy_count <= 1000000)) AND ((copy_offset + copy_count) <= 1000000) AND ((part_size >= 1) AND (part_size <= 1000)) AND (part_count = (((copy_count + part_size) - 1) / part_size)))"),
        ConstraintSpec("ss_intake_label_jobs_parent_ck_v9", "c", (), None, (), "((purpose = ANY (ARRAY['initial'::text, 'range_reprint'::text])) AND (((purpose = 'initial'::text) AND (reprint_of_job_id IS NULL)) OR ((purpose = 'range_reprint'::text) AND (reprint_of_job_id IS NOT NULL) AND (reprint_of_job_id <> id))))"),
        ConstraintSpec("ss_intake_label_jobs_time_ck_v9", "c", (), None, (), "(retention_until > created_at)"),
        ConstraintSpec("ss_intake_label_jobs_dates_ck_v9", "c", (), None, (), "((isfinite(created_at) AND (created_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (created_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)) AND (isfinite(retention_until) AND (retention_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (retention_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)))"),
    ), (
        IndexSpec("ss_intake_label_jobs_pk_v9", ("tenant_id", "id"), True, True),
        IndexSpec("ss_intake_label_jobs_basis_uq_v9", ("tenant_id", "job_basis_hash"), True, False),
        IndexSpec("ss_intake_label_jobs_input_identity_uq_v9", ("tenant_id", "id", "input_id"), True, False),
        IndexSpec("ss_intake_label_jobs_list_idx_v9", ("tenant_id", "receiving_id", "created_at", "id"), False, False),
    ), "\nDECLARE\n  basis __SS_LABEL_SCHEMA__.ss_intake_label_inputs%ROWTYPE;\n  parent __SS_LABEL_SCHEMA__.ss_intake_label_jobs%ROWTYPE;\nBEGIN\n  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  SELECT * INTO basis FROM __SS_LABEL_SCHEMA__.ss_intake_label_inputs\n    WHERE tenant_id=NEW.tenant_id AND id=NEW.input_id;\n  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF basis.content_state <> 'active' OR basis.input_body IS NULL\n     OR basis.retention_until <= pg_catalog.clock_timestamp()\n     OR NEW.receiving_id IS DISTINCT FROM basis.receiving_id\n     OR NEW.input_hash IS DISTINCT FROM basis.input_hash\n     OR NEW.terminal_basis_hash IS DISTINCT FROM basis.terminal_basis_hash\n     OR NEW.copy_offset + NEW.copy_count > basis.total_copies\n     OR NEW.created_at < basis.created_at\n     OR NEW.retention_until > basis.retention_until OR NEW.retention_until <= pg_catalog.clock_timestamp()\n     THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NEW.reprint_of_job_id IS NOT NULL THEN\n    SELECT * INTO parent FROM __SS_LABEL_SCHEMA__.ss_intake_label_jobs\n      WHERE tenant_id=NEW.tenant_id AND id=NEW.reprint_of_job_id;\n    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    IF parent.input_id IS DISTINCT FROM NEW.input_id OR parent.id=NEW.id\n       OR NEW.copy_offset < parent.copy_offset\n       OR NEW.copy_offset + NEW.copy_count > parent.copy_offset + parent.copy_count\n       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  END IF;\n  RETURN NEW;\nEND;\n"),
    TableSpec("ss_intake_label_publications", (
        ColumnSpec("tenant_id", "uuid", False),
        ColumnSpec("id", "uuid", False),
        ColumnSpec("job_id", "uuid", False),
        ColumnSpec("part_index", "integer", False),
        ColumnSpec("part_hash", "text", False),
        ColumnSpec("copy_offset", "integer", False),
        ColumnSpec("copy_count", "integer", False),
        ColumnSpec("generation_id", "uuid", False),
        ColumnSpec("state", "text", False),
        ColumnSpec("revision", "bigint", False),
        ColumnSpec("lease_generation", "bigint", False),
        ColumnSpec("lease_token", "uuid", True),
        ColumnSpec("lease_until", "timestamp with time zone", True),
        ColumnSpec("authorization_request_key", "text", True),
        ColumnSpec("render_input_hash", "text", True),
        ColumnSpec("render_basis_hash", "text", True),
        ColumnSpec("output_metadata", "text", True),
        ColumnSpec("pair_sealed_at", "timestamp with time zone", True),
        ColumnSpec("pdf_sha256", "text", True),
        ColumnSpec("pdf_bytes", "integer", True),
        ColumnSpec("zpl_sha256", "text", True),
        ColumnSpec("zpl_bytes", "integer", True),
        ColumnSpec("published_at", "timestamp with time zone", True),
        ColumnSpec("retention_until", "timestamp with time zone", False),
        ColumnSpec("tombstoned_at", "timestamp with time zone", True),
        ColumnSpec("policy_hash", "text", False),
        ColumnSpec("error_code", "text", True),
    ), (
        ConstraintSpec("ss_intake_label_publications_pk_v9", "p", ("tenant_id", "id"), None, (), None),
        ConstraintSpec("ss_intake_label_publications_part_uq_v9", "u", ("tenant_id", "job_id", "part_index"), None, (), None),
        ConstraintSpec("ss_intake_label_publications_identity_uq_v9", "u", ("tenant_id", "id", "job_id"), None, (), None),
        ConstraintSpec("ss_intake_label_publications_generation_uq_v9", "u", ("tenant_id", "generation_id"), None, (), None),
        ConstraintSpec("ss_intake_label_publications_job_fk_v9", "f", ("tenant_id", "job_id"), "ss_intake_label_jobs", ("tenant_id", "id"), None),
        ConstraintSpec("ss_intake_label_publications_auth_fk_v9", "f", ("tenant_id", "authorization_request_key", "job_id", "id"), "ss_intake_label_requests", ("tenant_id", "request_key", "job_id", "publication_id"), None),
        ConstraintSpec("ss_intake_label_publications_hashes_ck_v9", "c", (), None, (), "((((octet_length(part_hash) = 64) AND (part_hash ~ '^[0-9a-f]{64}$'::text)) AND ((render_input_hash IS NULL) OR ((octet_length(render_input_hash) = 64) AND (render_input_hash ~ '^[0-9a-f]{64}$'::text))) AND ((render_basis_hash IS NULL) OR ((octet_length(render_basis_hash) = 64) AND (render_basis_hash ~ '^[0-9a-f]{64}$'::text))) AND ((pdf_sha256 IS NULL) OR ((octet_length(pdf_sha256) = 64) AND (pdf_sha256 ~ '^[0-9a-f]{64}$'::text))) AND ((zpl_sha256 IS NULL) OR ((octet_length(zpl_sha256) = 64) AND (zpl_sha256 ~ '^[0-9a-f]{64}$'::text))) AND ((octet_length(policy_hash) = 64) AND (policy_hash ~ '^[0-9a-f]{64}$'::text))) AND ((authorization_request_key IS NULL) OR (((octet_length(authorization_request_key) >= 1) AND (octet_length(authorization_request_key) <= 150)) AND (authorization_request_key ~ '^[A-Za-z0-9_-]{1,150}$'::text))) AND ((error_code IS NULL) OR (((octet_length(error_code) >= 1) AND (octet_length(error_code) <= 100)) AND (error_code ~ '^[A-Z][A-Z0-9_]{0,99}$'::text))))"),
        ConstraintSpec("ss_intake_label_publications_range_ck_v9", "c", (), None, (), "((part_index >= 0) AND ((copy_offset >= 0) AND (copy_offset <= 999999)) AND ((copy_count >= 1) AND (copy_count <= 1000)) AND ((copy_offset + copy_count) <= 1000000) AND (revision >= 1) AND (lease_generation >= 0))"),
        ConstraintSpec("ss_intake_label_publications_state_ck_v9", "c", (), None, (), "((state = ANY (ARRAY['planned'::text, 'queued'::text, 'rendering'::text, 'bytes_prepared'::text, 'publishing'::text, 'published'::text, 'authorization_required'::text, 'recovery_required'::text])) AND ((state = 'planned'::text) = (authorization_request_key IS NULL)))"),
        ConstraintSpec("ss_intake_label_publications_lease_ck_v9", "c", (), None, (), "(((lease_token IS NULL) = (lease_until IS NULL)) AND ((state = ANY (ARRAY['rendering'::text, 'bytes_prepared'::text, 'publishing'::text])) = (lease_token IS NOT NULL)) AND ((lease_until IS NULL) OR (lease_until <= retention_until)))"),
        ConstraintSpec("ss_intake_label_publications_pair_ck_v9", "c", (), None, (), "((((pair_sealed_at IS NULL) AND (pdf_sha256 IS NULL) AND (pdf_bytes IS NULL) AND (zpl_sha256 IS NULL) AND (zpl_bytes IS NULL)) OR ((pair_sealed_at IS NOT NULL) AND (pdf_sha256 IS NOT NULL) AND (pdf_bytes IS NOT NULL) AND (zpl_sha256 IS NOT NULL) AND (zpl_bytes IS NOT NULL) AND ((pdf_bytes >= 1) AND (pdf_bytes <= 33554432)) AND ((zpl_bytes >= 1) AND (zpl_bytes <= 33554432)))) AND (((render_input_hash IS NULL) AND (render_basis_hash IS NULL) AND (output_metadata IS NULL)) OR ((render_input_hash IS NOT NULL) AND (render_basis_hash IS NOT NULL) AND (output_metadata IS NOT NULL) AND ((octet_length(output_metadata) >= 1) AND (octet_length(output_metadata) <= 2097152)))) AND ((pair_sealed_at IS NULL) OR ((render_input_hash IS NOT NULL) AND (render_basis_hash IS NOT NULL) AND (output_metadata IS NOT NULL))) AND ((NOT (state = ANY (ARRAY['bytes_prepared'::text, 'publishing'::text, 'published'::text]))) OR (pair_sealed_at IS NOT NULL)))"),
        ConstraintSpec("ss_intake_label_publications_time_ck_v9", "c", (), None, (), "((((state = 'published'::text) = (published_at IS NOT NULL)) AND ((state <> 'published'::text) OR ((tombstoned_at IS NULL) AND (published_at < retention_until)))) AND ((published_at IS NULL) OR (pair_sealed_at <= published_at)))"),
        ConstraintSpec("ss_intake_label_publications_dates_ck_v9", "c", (), None, (), "(((lease_until IS NULL) OR (isfinite(lease_until) AND (lease_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (lease_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND ((pair_sealed_at IS NULL) OR (isfinite(pair_sealed_at) AND (pair_sealed_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (pair_sealed_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND ((published_at IS NULL) OR (isfinite(published_at) AND (published_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (published_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND (isfinite(retention_until) AND (retention_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (retention_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)) AND ((tombstoned_at IS NULL) OR (isfinite(tombstoned_at) AND (tombstoned_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (tombstoned_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))))"),
    ), (
        IndexSpec("ss_intake_label_publications_pk_v9", ("tenant_id", "id"), True, True),
        IndexSpec("ss_intake_label_publications_part_uq_v9", ("tenant_id", "job_id", "part_index"), True, False),
        IndexSpec("ss_intake_label_publications_identity_uq_v9", ("tenant_id", "id", "job_id"), True, False),
        IndexSpec("ss_intake_label_publications_generation_uq_v9", ("tenant_id", "generation_id"), True, False),
        IndexSpec("ss_intake_label_publications_work_idx_v9", ("tenant_id", "state", "lease_until", "id"), False, False),
    ), "\nDECLARE\n  job __SS_LABEL_SCHEMA__.ss_intake_label_jobs%ROWTYPE;\n  request_basis RECORD;\n  preview_basis RECORD;\nBEGIN\n  IF TG_OP='DELETE' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF TG_OP='INSERT' THEN\n    IF NEW.state <> 'planned' OR NEW.revision <> 1 OR NEW.lease_generation <> 0\n       OR NEW.lease_token IS NOT NULL OR NEW.lease_until IS NOT NULL\n       OR NEW.authorization_request_key IS NOT NULL OR NEW.pair_sealed_at IS NOT NULL\n       OR NEW.render_input_hash IS NOT NULL OR NEW.render_basis_hash IS NOT NULL\n       OR NEW.output_metadata IS NOT NULL OR NEW.pdf_sha256 IS NOT NULL OR NEW.pdf_bytes IS NOT NULL\n       OR NEW.zpl_sha256 IS NOT NULL OR NEW.zpl_bytes IS NOT NULL\n       OR NEW.published_at IS NOT NULL OR NEW.tombstoned_at IS NOT NULL OR NEW.error_code IS NOT NULL\n       OR NEW.retention_until <= pg_catalog.clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    SELECT * INTO job FROM __SS_LABEL_SCHEMA__.ss_intake_label_jobs WHERE tenant_id=NEW.tenant_id AND id=NEW.job_id;\n    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    IF NEW.part_index >= job.part_count\n       OR NEW.copy_offset <> job.copy_offset + NEW.part_index * job.part_size\n       OR NEW.copy_count <> LEAST(job.part_size,job.copy_count - NEW.part_index * job.part_size)\n       OR NEW.retention_until > job.retention_until THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    RETURN NEW;\n  END IF;\n  IF ROW(NEW.tenant_id,NEW.id,NEW.job_id,NEW.part_index,NEW.part_hash,NEW.copy_offset,NEW.copy_count,NEW.generation_id,NEW.retention_until,NEW.policy_hash,NEW.tombstoned_at) IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.job_id,OLD.part_index,OLD.part_hash,OLD.copy_offset,OLD.copy_count,OLD.generation_id,OLD.retention_until,OLD.policy_hash,OLD.tombstoned_at) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF OLD.state='published' OR NEW.revision <> OLD.revision + 1 THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NOT ((OLD.state='planned' AND NEW.state='queued')\n       OR (OLD.state='queued' AND NEW.state IN ('rendering','authorization_required','recovery_required'))\n       OR (OLD.state='rendering' AND NEW.state IN ('rendering','bytes_prepared','authorization_required','recovery_required'))\n       OR (OLD.state='bytes_prepared' AND NEW.state IN ('bytes_prepared','publishing','authorization_required','recovery_required'))\n       OR (OLD.state='publishing' AND NEW.state IN ('publishing','published','authorization_required','recovery_required'))\n       OR (OLD.state IN ('authorization_required','recovery_required') AND NEW.state='queued')) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NEW.authorization_request_key IS DISTINCT FROM OLD.authorization_request_key\n     AND NOT (NEW.state='queued' AND OLD.state IN ('planned','authorization_required','recovery_required')\n              AND NEW.authorization_request_key IS NOT NULL) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NEW.state='queued' AND OLD.state IN ('authorization_required','recovery_required')\n     AND NEW.authorization_request_key IS NOT DISTINCT FROM OLD.authorization_request_key THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NEW.state='queued' AND OLD.state IN ('planned','authorization_required','recovery_required') THEN\n    SELECT r.tenant_id,r.request_key,r.kind,r.preview_id,r.job_id,r.publication_id,\n           r.created_by,r.session_hash,r.expected_publication_revision\n      INTO request_basis FROM __SS_LABEL_SCHEMA__.ss_intake_label_requests r\n      WHERE r.tenant_id=OLD.tenant_id AND r.request_key=NEW.authorization_request_key;\n    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    SELECT q.tenant_id,q.id,q.state,q.request_key,q.job_id,q.publication_id,\n           q.created_by,q.session_hash,q.expected_publication_revision,q.expires_at\n      INTO preview_basis FROM __SS_LABEL_SCHEMA__.ss_intake_label_previews q\n      WHERE q.tenant_id=OLD.tenant_id AND q.id=request_basis.preview_id;\n    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    IF request_basis.tenant_id IS DISTINCT FROM OLD.tenant_id\n       OR request_basis.job_id IS DISTINCT FROM OLD.job_id\n       OR request_basis.publication_id IS DISTINCT FROM OLD.id\n       OR request_basis.expected_publication_revision IS DISTINCT FROM OLD.revision\n       OR preview_basis.expected_publication_revision IS DISTINCT FROM OLD.revision\n       OR preview_basis.tenant_id IS DISTINCT FROM request_basis.tenant_id\n       OR preview_basis.id IS DISTINCT FROM request_basis.preview_id\n       OR preview_basis.request_key IS DISTINCT FROM request_basis.request_key\n       OR preview_basis.job_id IS DISTINCT FROM request_basis.job_id\n       OR preview_basis.publication_id IS DISTINCT FROM request_basis.publication_id\n       OR preview_basis.created_by IS DISTINCT FROM request_basis.created_by\n       OR preview_basis.session_hash IS DISTINCT FROM request_basis.session_hash\n       OR preview_basis.state <> 'ready'\n       OR preview_basis.expires_at <= pg_catalog.clock_timestamp()\n       OR OLD.retention_until <= pg_catalog.clock_timestamp()\n       OR (OLD.state='planned' AND request_basis.kind <> 'confirm_part')\n       OR (OLD.state IN ('authorization_required','recovery_required') AND request_basis.kind <> 'resume_part')\n       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  END IF;\n  IF NEW.state='published' AND OLD.lease_until <= pg_catalog.clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF OLD.pair_sealed_at IS NOT NULL AND ROW(NEW.render_input_hash,NEW.render_basis_hash,NEW.output_metadata,NEW.pair_sealed_at,NEW.pdf_sha256,NEW.pdf_bytes,NEW.zpl_sha256,NEW.zpl_bytes) IS DISTINCT FROM ROW(OLD.render_input_hash,OLD.render_basis_hash,OLD.output_metadata,OLD.pair_sealed_at,OLD.pdf_sha256,OLD.pdf_bytes,OLD.zpl_sha256,OLD.zpl_bytes) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF OLD.pair_sealed_at IS NULL AND NEW.pair_sealed_at IS NOT NULL\n     AND NOT (OLD.state='rendering' AND NEW.state='bytes_prepared') THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  \n  IF NEW.lease_generation < OLD.lease_generation THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NEW.lease_generation <> OLD.lease_generation THEN\n    IF NEW.lease_generation <> OLD.lease_generation + 1\n       OR NEW.state NOT IN ('rendering','bytes_prepared','publishing')\n       OR NEW.lease_token IS NULL OR NEW.lease_until IS NULL\n       OR NEW.lease_token IS NOT DISTINCT FROM OLD.lease_token\n       OR NEW.lease_until <= pg_catalog.clock_timestamp()\n       OR (OLD.lease_until IS NOT NULL AND OLD.lease_until > pg_catalog.clock_timestamp())\n       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  ELSIF NEW.state IN ('rendering','bytes_prepared','publishing') THEN\n    IF OLD.lease_until IS NULL OR OLD.lease_until <= pg_catalog.clock_timestamp()\n       OR NEW.lease_token IS DISTINCT FROM OLD.lease_token\n       OR NEW.lease_until IS DISTINCT FROM OLD.lease_until THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  ELSIF NEW.lease_token IS NOT NULL OR NEW.lease_until IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  RETURN NEW;\nEND;\n"),
    TableSpec("ss_intake_label_previews", (
        ColumnSpec("tenant_id", "uuid", False),
        ColumnSpec("id", "uuid", False),
        ColumnSpec("job_id", "uuid", False),
        ColumnSpec("publication_id", "uuid", False),
        ColumnSpec("created_by", "uuid", False),
        ColumnSpec("session_hash", "text", False),
        ColumnSpec("role", "text", False),
        ColumnSpec("request_key", "text", False),
        ColumnSpec("selector_body", "text", False),
        ColumnSpec("selector_hash", "text", False),
        ColumnSpec("input_hash", "text", False),
        ColumnSpec("part_hash", "text", False),
        ColumnSpec("build_hash", "text", False),
        ColumnSpec("state", "text", False),
        ColumnSpec("revision", "bigint", False),
        ColumnSpec("lease_generation", "bigint", False),
        ColumnSpec("lease_token", "uuid", True),
        ColumnSpec("lease_until", "timestamp with time zone", True),
        ColumnSpec("render_basis_hash", "text", True),
        ColumnSpec("overflows_body", "text", True),
        ColumnSpec("clipping_required", "boolean", True),
        ColumnSpec("created_at", "timestamp with time zone", False),
        ColumnSpec("ready_at", "timestamp with time zone", True),
        ColumnSpec("expires_at", "timestamp with time zone", False),
        ColumnSpec("error_code", "text", True),
        ColumnSpec("expected_publication_revision", "bigint", False),
    ), (
        ConstraintSpec("ss_intake_label_previews_pk_v9", "p", ("tenant_id", "id"), None, (), None),
        ConstraintSpec("ss_intake_label_previews_key_uq_v9", "u", ("tenant_id", "request_key"), None, (), None),
        ConstraintSpec("ss_intake_label_previews_request_identity_uq_v9", "u", ("tenant_id", "id", "request_key", "job_id", "publication_id", "expected_publication_revision"), None, (), None),
        ConstraintSpec("ss_intake_label_previews_publication_fk_v9", "f", ("tenant_id", "publication_id", "job_id"), "ss_intake_label_publications", ("tenant_id", "id", "job_id"), None),
        ConstraintSpec("ss_intake_label_previews_staff_fk_v9", "f", ("tenant_id", "created_by"), "ss_staff", ("tenant_id", "id"), None),
        ConstraintSpec("ss_intake_label_previews_hashes_ck_v9", "c", (), None, (), "((((octet_length(session_hash) = 64) AND (session_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(selector_hash) = 64) AND (selector_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(input_hash) = 64) AND (input_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(part_hash) = 64) AND (part_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(build_hash) = 64) AND (build_hash ~ '^[0-9a-f]{64}$'::text)) AND ((render_basis_hash IS NULL) OR ((octet_length(render_basis_hash) = 64) AND (render_basis_hash ~ '^[0-9a-f]{64}$'::text)))) AND (((octet_length(request_key) >= 1) AND (octet_length(request_key) <= 150)) AND (request_key ~ '^[A-Za-z0-9_-]{1,150}$'::text)) AND ((error_code IS NULL) OR (((octet_length(error_code) >= 1) AND (octet_length(error_code) <= 100)) AND (error_code ~ '^[A-Z][A-Z0-9_]{0,99}$'::text))))"),
        ConstraintSpec("ss_intake_label_previews_body_ck_v9", "c", (), None, (), "(((octet_length(selector_body) >= 1) AND (octet_length(selector_body) <= 16384)) AND ((overflows_body IS NULL) OR ((octet_length(overflows_body) >= 1) AND (octet_length(overflows_body) <= 2097152))))"),
        ConstraintSpec("ss_intake_label_previews_state_ck_v9", "c", (), None, (), "((state = ANY (ARRAY['queued'::text, 'checking'::text, 'ready'::text, 'failed'::text, 'expired'::text])) AND (role = ANY (ARRAY['owner'::text, 'manager'::text])) AND (revision >= 1) AND (lease_generation >= 0) AND (((ready_at IS NULL) AND (render_basis_hash IS NULL) AND (overflows_body IS NULL) AND (clipping_required IS NULL)) OR ((ready_at IS NOT NULL) AND (render_basis_hash IS NOT NULL) AND (overflows_body IS NOT NULL) AND (clipping_required IS NOT NULL))) AND ((state <> 'ready'::text) OR (ready_at IS NOT NULL)))"),
        ConstraintSpec("ss_intake_label_previews_lease_ck_v9", "c", (), None, (), "(((lease_token IS NULL) = (lease_until IS NULL)) AND ((state = 'checking'::text) = (lease_token IS NOT NULL)) AND ((lease_until IS NULL) OR (lease_until <= expires_at)))"),
        ConstraintSpec("ss_intake_label_previews_time_ck_v9", "c", (), None, (), "((expires_at > created_at) AND (expires_at <= (created_at + '00:10:00'::interval)) AND ((ready_at IS NULL) OR ((ready_at >= created_at) AND (expires_at > ready_at) AND (expires_at <= (ready_at + '00:05:00'::interval)))))"),
        ConstraintSpec("ss_intake_label_previews_publication_revision_ck_v9", "c", (), None, (), "(expected_publication_revision >= 1)"),
        ConstraintSpec("ss_intake_label_previews_dates_ck_v9", "c", (), None, (), "(((lease_until IS NULL) OR (isfinite(lease_until) AND (lease_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (lease_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND (isfinite(created_at) AND (created_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (created_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)) AND ((ready_at IS NULL) OR (isfinite(ready_at) AND (ready_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (ready_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND (isfinite(expires_at) AND (expires_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (expires_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)))"),
    ), (
        IndexSpec("ss_intake_label_previews_pk_v9", ("tenant_id", "id"), True, True),
        IndexSpec("ss_intake_label_previews_key_uq_v9", ("tenant_id", "request_key"), True, False),
        IndexSpec("ss_intake_label_previews_request_identity_uq_v9", ("tenant_id", "id", "request_key", "job_id", "publication_id", "expected_publication_revision"), True, False),
        IndexSpec("ss_intake_label_previews_expiry_idx_v9", ("tenant_id", "created_by", "session_hash", "expires_at", "id"), False, False),
    ), "\nDECLARE\n  pub __SS_LABEL_SCHEMA__.ss_intake_label_publications%ROWTYPE;\n  job __SS_LABEL_SCHEMA__.ss_intake_label_jobs%ROWTYPE;\n  basis __SS_LABEL_SCHEMA__.ss_intake_label_inputs%ROWTYPE;\n  current_publication_revision pg_catalog.int8;\n  current_publication_retention pg_catalog.timestamptz;\nBEGIN\n  IF TG_OP='DELETE' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF TG_OP='INSERT' THEN\n    IF NEW.state <> 'queued' OR NEW.revision <> 1 OR NEW.lease_generation <> 0\n       OR NEW.lease_token IS NOT NULL OR NEW.lease_until IS NOT NULL\n       OR NEW.ready_at IS NOT NULL OR NEW.render_basis_hash IS NOT NULL\n       OR NEW.overflows_body IS NOT NULL OR NEW.clipping_required IS NOT NULL\n       OR NEW.error_code IS NOT NULL OR NEW.expires_at <= pg_catalog.clock_timestamp()\n       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    SELECT * INTO pub FROM __SS_LABEL_SCHEMA__.ss_intake_label_publications WHERE tenant_id=NEW.tenant_id AND id=NEW.publication_id;\n    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    SELECT * INTO job FROM __SS_LABEL_SCHEMA__.ss_intake_label_jobs WHERE tenant_id=NEW.tenant_id AND id=NEW.job_id;\n    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    SELECT * INTO basis FROM __SS_LABEL_SCHEMA__.ss_intake_label_inputs WHERE tenant_id=NEW.tenant_id AND id=job.input_id;\n    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    IF pub.job_id IS DISTINCT FROM job.id OR NEW.input_hash IS DISTINCT FROM job.input_hash\n       OR NEW.part_hash IS DISTINCT FROM pub.part_hash OR NEW.build_hash IS DISTINCT FROM job.renderer_build_hash\n       OR NEW.expected_publication_revision IS DISTINCT FROM pub.revision\n       OR NEW.expires_at > pub.retention_until\n       OR basis.content_state <> 'active' OR basis.input_body IS NULL OR basis.retention_until <= pg_catalog.clock_timestamp()\n       OR NEW.created_at < job.created_at OR NEW.expires_at > job.retention_until OR NEW.expires_at > basis.retention_until\n       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    RETURN NEW;\n  END IF;\n  IF ROW(NEW.tenant_id,NEW.id,NEW.job_id,NEW.publication_id,NEW.created_by,NEW.session_hash,NEW.role,NEW.request_key,NEW.selector_body,NEW.selector_hash,NEW.input_hash,NEW.part_hash,NEW.build_hash,NEW.created_at,NEW.expected_publication_revision) IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.job_id,OLD.publication_id,OLD.created_by,OLD.session_hash,OLD.role,OLD.request_key,OLD.selector_body,OLD.selector_hash,OLD.input_hash,OLD.part_hash,OLD.build_hash,OLD.created_at,OLD.expected_publication_revision) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF OLD.state IN ('failed','expired') OR NEW.revision <> OLD.revision + 1 THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NOT ((OLD.state='queued' AND NEW.state IN ('checking','expired'))\n       OR (OLD.state='checking' AND NEW.state IN ('checking','ready','failed','expired'))\n       OR (OLD.state='ready' AND NEW.state='expired')) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NEW.expires_at IS DISTINCT FROM OLD.expires_at\n     AND NOT (OLD.state='checking' AND NEW.state='ready' AND NEW.expires_at < OLD.expires_at)\n     THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF OLD.ready_at IS NOT NULL AND ROW(NEW.ready_at,NEW.render_basis_hash,NEW.overflows_body,NEW.clipping_required,NEW.expires_at) IS DISTINCT FROM ROW(OLD.ready_at,OLD.render_basis_hash,OLD.overflows_body,OLD.clipping_required,OLD.expires_at) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF OLD.state='checking' AND NEW.state='ready' THEN\n    SELECT p.revision,p.retention_until\n      INTO current_publication_revision,current_publication_retention\n      FROM __SS_LABEL_SCHEMA__.ss_intake_label_publications p\n      WHERE p.tenant_id=NEW.tenant_id AND p.id=NEW.publication_id;\n    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n    IF NEW.expected_publication_revision IS DISTINCT FROM current_publication_revision\n       OR current_publication_retention <= pg_catalog.clock_timestamp()\n       OR NEW.expires_at > current_publication_retention\n       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  END IF;\n  IF OLD.state='checking' AND NEW.state='ready' AND OLD.lease_until <= pg_catalog.clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NEW.ready_at IS NOT NULL AND OLD.ready_at IS NULL AND NEW.state <> 'ready' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  \n  IF NEW.lease_generation < OLD.lease_generation THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF NEW.lease_generation <> OLD.lease_generation THEN\n    IF NEW.lease_generation <> OLD.lease_generation + 1\n       OR NEW.state NOT IN ('checking')\n       OR NEW.lease_token IS NULL OR NEW.lease_until IS NULL\n       OR NEW.lease_token IS NOT DISTINCT FROM OLD.lease_token\n       OR NEW.lease_until <= pg_catalog.clock_timestamp()\n       OR (OLD.lease_until IS NOT NULL AND OLD.lease_until > pg_catalog.clock_timestamp())\n       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  ELSIF NEW.state IN ('checking') THEN\n    IF OLD.lease_until IS NULL OR OLD.lease_until <= pg_catalog.clock_timestamp()\n       OR NEW.lease_token IS DISTINCT FROM OLD.lease_token\n       OR NEW.lease_until IS DISTINCT FROM OLD.lease_until THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  ELSIF NEW.lease_token IS NOT NULL OR NEW.lease_until IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  RETURN NEW;\nEND;\n"),
    TableSpec("ss_intake_label_requests", (
        ColumnSpec("tenant_id", "uuid", False),
        ColumnSpec("request_key", "text", False),
        ColumnSpec("kind", "text", False),
        ColumnSpec("intent_body", "text", False),
        ColumnSpec("intent_hash", "text", False),
        ColumnSpec("preview_id", "uuid", True),
        ColumnSpec("job_id", "uuid", False),
        ColumnSpec("publication_id", "uuid", False),
        ColumnSpec("reprint_of_publication_id", "uuid", True),
        ColumnSpec("created_by", "uuid", False),
        ColumnSpec("session_hash", "text", False),
        ColumnSpec("created_at", "timestamp with time zone", False),
        ColumnSpec("expected_publication_revision", "bigint", False),
    ), (
        ConstraintSpec("ss_intake_label_requests_pk_v9", "p", ("tenant_id", "request_key"), None, (), None),
        ConstraintSpec("ss_intake_label_requests_identity_uq_v9", "u", ("tenant_id", "request_key", "job_id", "publication_id"), None, (), None),
        ConstraintSpec("ss_intake_label_requests_publication_fk_v9", "f", ("tenant_id", "publication_id", "job_id"), "ss_intake_label_publications", ("tenant_id", "id", "job_id"), None),
        ConstraintSpec("ss_intake_label_requests_preview_fk_v9", "f", ("tenant_id", "preview_id", "request_key", "job_id", "publication_id", "expected_publication_revision"), "ss_intake_label_previews", ("tenant_id", "id", "request_key", "job_id", "publication_id", "expected_publication_revision"), None),
        ConstraintSpec("ss_intake_label_requests_reprint_fk_v9", "f", ("tenant_id", "reprint_of_publication_id", "job_id"), "ss_intake_label_publications", ("tenant_id", "id", "job_id"), None),
        ConstraintSpec("ss_intake_label_requests_staff_fk_v9", "f", ("tenant_id", "created_by"), "ss_staff", ("tenant_id", "id"), None),
        ConstraintSpec("ss_intake_label_requests_hashes_ck_v9", "c", (), None, (), "((((octet_length(intent_hash) = 64) AND (intent_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(session_hash) = 64) AND (session_hash ~ '^[0-9a-f]{64}$'::text))) AND (((octet_length(request_key) >= 1) AND (octet_length(request_key) <= 150)) AND (request_key ~ '^[A-Za-z0-9_-]{1,150}$'::text)))"),
        ConstraintSpec("ss_intake_label_requests_body_ck_v9", "c", (), None, (), "((octet_length(intent_body) >= 1) AND (octet_length(intent_body) <= 16384))"),
        ConstraintSpec("ss_intake_label_requests_kind_ck_v9", "c", (), None, (), "((kind = ANY (ARRAY['confirm_part'::text, 'resume_part'::text, 'reprint_exact'::text])) AND (((kind = 'reprint_exact'::text) AND (reprint_of_publication_id IS NOT NULL) AND (reprint_of_publication_id = publication_id)) OR ((kind <> 'reprint_exact'::text) AND (reprint_of_publication_id IS NULL))))"),
        ConstraintSpec("ss_intake_label_requests_publication_revision_ck_v9", "c", (), None, (), "(expected_publication_revision >= 1)"),
        ConstraintSpec("ss_intake_label_requests_dates_ck_v9", "c", (), None, (), "((isfinite(created_at) AND (created_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (created_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)))"),
    ), (
        IndexSpec("ss_intake_label_requests_pk_v9", ("tenant_id", "request_key"), True, True),
        IndexSpec("ss_intake_label_requests_identity_uq_v9", ("tenant_id", "request_key", "job_id", "publication_id"), True, False),
        IndexSpec("ss_intake_label_requests_publication_idx_v9", ("tenant_id", "publication_id", "created_at", "request_key"), False, False),
    ), "\nDECLARE\n  quote __SS_LABEL_SCHEMA__.ss_intake_label_previews%ROWTYPE;\n  pub __SS_LABEL_SCHEMA__.ss_intake_label_publications%ROWTYPE;\nBEGIN\n  IF TG_OP <> 'INSERT' OR NEW.preview_id IS NULL THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  SELECT * INTO quote FROM __SS_LABEL_SCHEMA__.ss_intake_label_previews WHERE tenant_id=NEW.tenant_id AND id=NEW.preview_id;\n  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  SELECT * INTO pub FROM __SS_LABEL_SCHEMA__.ss_intake_label_publications WHERE tenant_id=NEW.tenant_id AND id=NEW.publication_id;\n  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF quote.state <> 'ready' OR quote.expires_at <= pg_catalog.clock_timestamp()\n     OR quote.request_key IS DISTINCT FROM NEW.request_key OR quote.job_id IS DISTINCT FROM NEW.job_id\n     OR quote.publication_id IS DISTINCT FROM NEW.publication_id\n     OR NEW.created_at < quote.ready_at OR NEW.created_at >= quote.expires_at\n     OR quote.created_by IS DISTINCT FROM NEW.created_by OR quote.session_hash IS DISTINCT FROM NEW.session_hash\n     OR NEW.expected_publication_revision IS DISTINCT FROM quote.expected_publication_revision\n     OR NEW.expected_publication_revision IS DISTINCT FROM pub.revision\n     THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  IF (NEW.kind='confirm_part' AND pub.state <> 'planned')\n     OR (NEW.kind='resume_part' AND pub.state NOT IN ('authorization_required','recovery_required'))\n     OR (NEW.kind='reprint_exact' AND (pub.state <> 'published' OR pub.tombstoned_at IS NOT NULL OR pub.retention_until <= pg_catalog.clock_timestamp()))\n     THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;\n  RETURN NEW;\nEND;\n"),
)
TABLES = tuple(spec.name for spec in SCHEMA_SPEC)
COLUMN_FIELDS = MappingProxyType({spec.name: tuple(c.name for c in spec.columns) for spec in SCHEMA_SPEC})
RAW_FIELDS = COLUMN_FIELDS
NOT_NULL_SPEC = MappingProxyType({spec.name: tuple(
    ConstraintSpec(spec.name + '_' + column.name + '_nn_v9', 'n', (column.name,), None, (), None)
    for column in spec.columns if not column.nullable) for spec in SCHEMA_SPEC})
ERROR_CODES = frozenset('INTAKE_LABEL_SCHEMA_' + name for name in (
    'BASE_REQUIRED', 'PARTIAL', 'INVALID', 'TRANSACTION_REQUIRED',
    'INSTALL_SOURCE_CHANGED', 'INSTALL_FAILED'))

class LabelSchemaError(RuntimeError):
    def __init__(self, code='INTAKE_LABEL_SCHEMA_INVALID'):
        self.code = code if type(code) is str and code in ERROR_CODES else 'INTAKE_LABEL_SCHEMA_INVALID'
        super().__init__('Shared label schema requires reviewed operator setup or recovery.')


def _fail(suffix='INVALID'):
    raise LabelSchemaError('INTAKE_LABEL_SCHEMA_' + suffix)


def _safe(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except LabelSchemaError:
            raise
        except Exception:
            pass
        # Replace our internal failure only after leaving its handler.
        _fail()
    return invoke


def _schema_name(value):
    if type(value) is not str or not re.fullmatch(r'public|ss_test_[a-f0-9]{32}', value):
        _fail()
    return value


def _quoted_schema(value):
    return '"' + _schema_name(value) + '"'


def _bound(value, schema):
    return value.replace(SQL_TOKEN, _quoted_schema(schema))


def _rows(db, query, params=()):
    rows = db.execute(query, params).fetchall()
    if type(rows) is not list or len(rows) > CATALOG_ROW_LIMIT or any(type(row) is not dict for row in rows):
        _fail()
    return rows


def _unique(rows, key):
    result = {}
    for row in rows:
        value = row[key]
        if type(value) is not str or value in result:
            _fail()
        result[value] = row
    return result


def _yes(value):
    return value is True


def _same_names(actual, expected):
    if len(actual) != len(expected) or set(actual) != set(expected):
        _fail()


def _sql_tokens(value):
    # Preserve quoted bytes and EVERY semantic parenthesis; ignore whitespace only.
    if type(value) is not str or len(value) > 32768:
        _fail()
    tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|::|<=|>=|<>|!=|[^\s]", value)
    return tuple(token if token.startswith(("'", '"')) else token.lower() for token in tokens)


def _check_tokens(value):
    tokens = _sql_tokens(value)
    if not tokens or tokens[0] != 'check':
        _fail()
    tokens = tokens[1:]
    # Remove only redundant parentheses enclosing the ENTIRE check expression.
    while len(tokens) >= 2 and tokens[0] == '(' and tokens[-1] == ')':
        depth = 0
        encloses_all = True
        for index, token in enumerate(tokens):
            if token == '(':
                depth += 1
            elif token == ')':
                depth -= 1
                if depth == 0 and index != len(tokens) - 1:
                    encloses_all = False
                    break
                if depth < 0:
                    _fail()
        if not encloses_all or depth != 0:
            break
        tokens = tokens[1:-1]
    return tokens

_CONTEXT_FIELDS = ('schema_oid', 'schema_name', 'timezone', 'server_version_num',
                   'date_style', 'interval_style', 'replication_role',
                   'server_encoding', 'client_encoding')


def _native_context_values(context):
    if type(context) is not dict or any(name not in context for name in _CONTEXT_FIELDS):
        _fail()
    values = tuple(context[name] for name in _CONTEXT_FIELDS)
    oid, schema, zone, version, date_style, interval_style, role, server_encoding, client_encoding = values
    _schema_name(schema)
    if (type(oid) is not int or oid <= 0 or type(version) is not int or not 150000 <= version < 190000
        or type(zone) is not str or zone not in ('UTC','Etc/UTC')
        or type(date_style) is not str or not date_style.startswith('ISO,')
        or interval_style != 'postgres' or role != 'origin'
        or type(server_encoding) is not str or server_encoding != 'UTF8'
        or type(client_encoding) is not str or client_encoding != 'UTF8'):
        _fail()
    return values


_CONTEXT_SQL = """/* ss_labels:context */
SELECT n.oid::bigint AS schema_oid,n.nspname::text AS schema_name,
       pg_catalog.current_setting('TimeZone') AS timezone,
       pg_catalog.current_setting('server_version_num')::integer AS server_version_num,
       pg_catalog.current_setting('DateStyle') AS date_style,
       pg_catalog.current_setting('IntervalStyle') AS interval_style,
       pg_catalog.current_setting('session_replication_role') AS replication_role,
       pg_catalog.current_setting('server_encoding') AS server_encoding,
       pg_catalog.current_setting('client_encoding') AS client_encoding
FROM pg_catalog.pg_namespace n WHERE n.nspname=pg_catalog.current_schema() LIMIT 2"""
_OBJECTS_SQL = """/* ss_labels:objects */
SELECT c.oid::bigint AS oid,c.relname::text AS name,c.relkind::text AS kind,
 c.relpersistence::text AS persistence,c.relispartition AS partitioned,
 c.relrowsecurity AS rls,c.relforcerowsecurity AS force_rls,c.relhasrules AS has_rules,
 EXISTS(SELECT 1 FROM pg_catalog.pg_inherits inh WHERE inh.inhrelid=c.oid OR inh.inhparent=c.oid) AS has_inheritance
FROM pg_catalog.pg_class c WHERE c.relnamespace=%s
 AND (pg_catalog.left(pg_catalog.translate(c.relname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),16)='ss_intake_label_'
      OR c.relname IN ('ss_schema_versions','ss_tenants','ss_staff','ss_receiving_operations'))
ORDER BY c.relname LIMIT 2049"""
_COLUMNS_SQL = """/* ss_labels:columns */
SELECT c.relname::text AS table_name,a.attnum::integer AS position,a.attname::text AS name,
 pg_catalog.format_type(a.atttypid,a.atttypmod) AS type_name,a.attnotnull AS not_null,
 tn.nspname::text AS type_schema,ty.typtype::text AS type_kind,a.atttypmod::integer AS type_modifier,
 a.attisdropped AS dropped,a.attgenerated::text AS generated,a.attidentity::text AS identity,
 a.attislocal AS local,a.attinhcount::integer AS inheritance_count,
 (d.oid IS NOT NULL) AS has_default,
 CASE WHEN a.attcollation=0 THEN '' ELSE cn.nspname||'.'||co.collname END AS collation,
 COALESCE(co.collisdeterministic,TRUE) AS deterministic_collation
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
 JOIN pg_catalog.pg_type ty ON ty.oid=a.atttypid JOIN pg_catalog.pg_namespace tn ON tn.oid=ty.typnamespace
 LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
 LEFT JOIN pg_catalog.pg_collation co ON co.oid=a.attcollation
 LEFT JOIN pg_catalog.pg_namespace cn ON cn.oid=co.collnamespace
WHERE c.relnamespace=%s AND c.relkind='r' AND a.attnum>0
 AND (c.relname=ANY(%s) OR c.relname IN ('ss_schema_versions','ss_tenants','ss_staff','ss_receiving_operations'))
ORDER BY c.relname,a.attnum LIMIT 2049"""
_CONSTRAINTS_SQL = """/* ss_labels:constraints */
SELECT c.oid::bigint AS oid,t.relname::text AS table_name,c.conname::text AS name,c.contype::text AS kind,
 ARRAY(SELECT a.attname::text FROM pg_catalog.unnest(c.conkey) WITH ORDINALITY k(n,ord)
       JOIN pg_catalog.pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.n ORDER BY k.ord) AS columns,
 rn.nspname::text AS ref_schema,r.relname::text AS ref_table,c.confrelid::bigint AS ref_oid,
 ARRAY(SELECT a.attname::text FROM pg_catalog.unnest(c.confkey) WITH ORDINALITY k(n,ord)
       JOIN pg_catalog.pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.n ORDER BY k.ord) AS ref_columns,
 c.condeferrable AS deferrable,c.condeferred AS deferred,c.convalidated AS validated,
 c.confupdtype::text AS update_action,c.confdeltype::text AS delete_action,c.confmatchtype::text AS match_type,
 c.conislocal AS local,c.coninhcount::integer AS inheritance_count,c.connoinherit AS no_inherit,
 c.conparentid::bigint AS parent_oid,c.conindid::bigint AS index_oid,
 NULL::boolean AS enforced,NULL::boolean AS period,
 CASE WHEN c.contype IN ('p','u','f') THEN
   ix.indisunique AND ix.indimmediate AND ix.indisvalid AND ix.indisready AND ix.indislive
   AND ix.indrelid=CASE WHEN c.contype='f' THEN c.confrelid ELSE c.conrelid END
   ELSE NULL END AS supporting_index_valid,
 CASE WHEN c.contype='c' AND pg_catalog.octet_length(c.conbin::text)<=16384
      THEN pg_catalog.pg_get_constraintdef(c.oid,FALSE) END AS check_definition,
 NOT EXISTS(SELECT 1 FROM pg_catalog.pg_depend dep
     JOIN pg_catalog.pg_proc p ON dep.refclassid='pg_catalog.pg_proc'::regclass AND dep.refobjid=p.oid
     JOIN pg_catalog.pg_namespace ns ON ns.oid=p.pronamespace
     WHERE dep.classid='pg_catalog.pg_constraint'::regclass AND dep.objid=c.oid AND ns.nspname<>'pg_catalog')
 AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_depend dep
     JOIN pg_catalog.pg_operator o ON dep.refclassid='pg_catalog.pg_operator'::regclass AND dep.refobjid=o.oid
     JOIN pg_catalog.pg_namespace ns ON ns.oid=o.oprnamespace
     WHERE dep.classid='pg_catalog.pg_constraint'::regclass AND dep.objid=c.oid AND ns.nspname<>'pg_catalog')
 AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_depend dep
     JOIN pg_catalog.pg_type ty ON dep.refclassid='pg_catalog.pg_type'::regclass AND dep.refobjid=ty.oid
     JOIN pg_catalog.pg_namespace ns ON ns.oid=ty.typnamespace
     WHERE dep.classid='pg_catalog.pg_constraint'::regclass AND dep.objid=c.oid AND ns.nspname<>'pg_catalog')
 AS builtin_dependencies
FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_class t ON t.oid=c.conrelid
 LEFT JOIN pg_catalog.pg_class r ON r.oid=c.confrelid
 LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid=r.relnamespace
 LEFT JOIN pg_catalog.pg_index ix ON ix.indexrelid=c.conindid
WHERE t.relnamespace=%s
 AND (t.relname=ANY(%s) OR t.relname IN ('ss_schema_versions','ss_tenants','ss_staff','ss_receiving_operations'))
ORDER BY t.relname,c.conname LIMIT 2049"""
_CONSTRAINTS_SQL_18 = _CONSTRAINTS_SQL.replace(
    'NULL::boolean AS enforced,NULL::boolean AS period',
    'c.conenforced AS enforced,c.conperiod AS period')
_INDEXES_SQL = """/* ss_labels:indexes */
SELECT t.relname::text AS table_name,c.relname::text AS name,
 am.amname::text AS method,i.indisunique AS unique,i.indisprimary AS primary,
 i.indimmediate AS immediate,i.indisvalid AS valid,i.indisready AS ready,i.indislive AS live,
 i.indnullsnotdistinct AS nulls_not_distinct,i.indnkeyatts::integer AS key_count,i.indnatts::integer AS total_count,
 (i.indexprs IS NOT NULL) AS has_expression,(i.indpred IS NOT NULL) AS has_predicate,
 ARRAY(SELECT a.attname::text FROM pg_catalog.unnest(i.indkey) WITH ORDINALITY k(n,ord)
       JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.n ORDER BY k.ord) AS columns,
 ARRAY(SELECT ns.nspname||'.'||op.opcname FROM pg_catalog.unnest(i.indclass) WITH ORDINALITY k(n,ord)
       JOIN pg_catalog.pg_opclass op ON op.oid=k.n JOIN pg_catalog.pg_namespace ns ON ns.oid=op.opcnamespace ORDER BY k.ord) AS opclasses,
 ARRAY(SELECT CASE WHEN k.n=0 THEN '' ELSE ns.nspname||'.'||co.collname END
       FROM pg_catalog.unnest(i.indcollation) WITH ORDINALITY k(n,ord)
       LEFT JOIN pg_catalog.pg_collation co ON co.oid=k.n LEFT JOIN pg_catalog.pg_namespace ns ON ns.oid=co.collnamespace ORDER BY k.ord) AS collations,
 i.indoption::smallint[] AS options
FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class t ON t.oid=i.indrelid
 JOIN pg_catalog.pg_class c ON c.oid=i.indexrelid JOIN pg_catalog.pg_am am ON am.oid=c.relam
WHERE t.relnamespace=%s AND t.relname=ANY(%s)
ORDER BY t.relname,c.relname LIMIT 2049"""
_FUNCTIONS_SQL = """/* ss_labels:functions */
SELECT p.oid::bigint AS oid,p.proname::text AS name,l.lanname::text AS language,
 pg_catalog.format_type(p.prorettype,NULL) AS returns,rn.nspname::text AS return_schema,
 rt.typname::text AS return_type_name,p.pronargs::integer AS nargs,
 p.proretset AS returns_set,p.prosecdef AS security_definer,p.proleakproof AS leakproof,
 p.proisstrict AS strict,p.provolatile::text AS volatility,p.proparallel::text AS parallel,
 p.prokind::text AS kind,p.proconfig AS config,p.procost AS cost,p.prorows AS rows,
 (p.proargdefaults IS NULL AND p.proallargtypes IS NULL AND p.proargmodes IS NULL
  AND p.proargnames IS NULL AND p.provariadic=0 AND p.prosupport=0) AS plain_args,
 CASE WHEN pg_catalog.octet_length(p.prosrc)<=65536 THEN p.prosrc END AS source,
 (pg_catalog.octet_length(p.prosrc)<=65536) AS source_bounded,
 EXISTS(SELECT 1 FROM pg_catalog.aclexplode(COALESCE(p.proacl,pg_catalog.acldefault('f',p.proowner))) acl
        WHERE acl.grantee<>p.proowner OR acl.privilege_type<>'EXECUTE' OR acl.is_grantable) AS unexpected_acl
FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_language l ON l.oid=p.prolang
 JOIN pg_catalog.pg_type rt ON rt.oid=p.prorettype JOIN pg_catalog.pg_namespace rn ON rn.oid=rt.typnamespace
WHERE p.pronamespace=%s AND pg_catalog.left(pg_catalog.translate(p.proname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),16)='ss_intake_label_'
ORDER BY p.proname,p.oid LIMIT 2049"""
_TRIGGERS_SQL = """/* ss_labels:triggers */
SELECT c.relname::text AS table_name,t.tgname::text AS name,p.proname::text AS function_name,
 pn.nspname::text AS function_schema,t.tgfoid::bigint AS function_oid,
 t.tgtype::integer AS type,t.tgenabled::text AS enabled,t.tgnargs::integer AS nargs,
 pg_catalog.encode(t.tgargs,'hex') AS args,(t.tgqual IS NOT NULL) AS has_when,
 t.tgconstraint::bigint AS constraint_oid,t.tgdeferrable AS deferrable,t.tginitdeferred AS deferred
FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
 JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid JOIN pg_catalog.pg_namespace pn ON pn.oid=p.pronamespace
WHERE c.relnamespace=%s AND c.relname=ANY(%s) AND NOT t.tgisinternal
ORDER BY c.relname,t.tgname LIMIT 2049"""
_RI_TRIGGERS_SQL = """/* ss_labels:ri_triggers */
SELECT t.tgconstraint::bigint AS constraint_oid,t.tgrelid::bigint AS table_oid,
 p.proname::text AS function_name,pn.nspname::text AS function_schema,
 t.tgtype::integer AS type,t.tgenabled::text AS enabled,t.tgnargs::integer AS nargs,
 pg_catalog.encode(t.tgargs,'hex') AS args,(t.tgqual IS NOT NULL) AS has_when,
 t.tgdeferrable AS deferrable,t.tginitdeferred AS deferred
FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
 JOIN pg_catalog.pg_namespace pn ON pn.oid=p.pronamespace
WHERE t.tgisinternal AND t.tgconstraint IN (
 SELECT c.oid FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_class r ON r.oid=c.conrelid
 WHERE r.relnamespace=%s AND r.relname=ANY(%s) AND c.contype='f')
ORDER BY t.tgconstraint,t.tgrelid,t.tgname LIMIT 2049"""
_BASE_NAMES = ('ss_schema_versions', 'ss_tenants', 'ss_staff', 'ss_receiving_operations')
_OPCLASSES = MappingProxyType({'uuid':'pg_catalog.uuid_ops','text':'pg_catalog.text_ops',
    'integer':'pg_catalog.int4_ops','bigint':'pg_catalog.int8_ops',
    'timestamp with time zone':'pg_catalog.timestamptz_ops','boolean':'pg_catalog.bool_ops'})


def _base_ready(objects, columns, constraints, major):
    requirements = {'ss_schema_versions': ('version',), 'ss_tenants': ('id',),
                    'ss_staff': ('tenant_id', 'id'), 'ss_receiving_operations': ('tenant_id', 'id')}
    for name, keys in requirements.items():
        obj = objects.get(name)
        if (not obj or obj['kind'] != 'r' or obj['persistence'] != 'p'
            or obj['partitioned'] is not False or obj['has_inheritance'] is not False):
            _fail('BASE_REQUIRED')
        for key in keys:
            matches = [row for row in columns if row['table_name'] == name and row['name'] == key]
            expected_type = 'integer' if key == 'version' else 'uuid'
            if (len(matches) != 1 or matches[0]['type_name'] != expected_type
                or matches[0]['type_schema'] != 'pg_catalog' or matches[0]['type_kind'] != 'b'
                or matches[0]['type_modifier'] != -1
                or matches[0]['not_null'] is not True or matches[0]['dropped'] is not False):
                _fail('BASE_REQUIRED')
        unique = [row for row in constraints if row['table_name'] == name and row['kind'] in ('p', 'u')
                  and tuple(row['columns']) == keys and row['deferrable'] is False
                  and row['deferred'] is False and row['validated'] is True
                  and row['supporting_index_valid'] is True
                  and (row['enforced'] is True if major == 18 else row['enforced'] is None)]
        if not unique:
            _fail('BASE_REQUIRED')


def _check_columns(spec, rows):
    actual = [row for row in rows if row['table_name'] == spec.name]
    if len(actual) != len(spec.columns):
        _fail()
    for position, (column, row) in enumerate(zip(spec.columns, actual), 1):
        if (row['name'] != column.name or row['position'] != position or row['type_name'] != column.type_name
            or row['type_schema'] != 'pg_catalog' or row['type_kind'] != 'b' or row['type_modifier'] != -1
            or row['not_null'] is not (not column.nullable) or row['dropped'] is not False
            or row['generated'] != '' or row['identity'] != '' or row['has_default'] is not False
            or row['local'] is not True or row['inheritance_count'] != 0
            or row['collation'] != ('pg_catalog.default' if column.type_name == 'text' else '')
            or row['deterministic_collation'] is not True):
            _fail()


def _check_constraints(spec, rows, schema, objects, major):
    actual = _unique([row for row in rows if row['table_name'] == spec.name], 'name')
    expected = spec.constraints + (NOT_NULL_SPEC[spec.name] if major == 18 else ())
    _same_names(actual, tuple(c.name for c in expected))
    for constraint in expected:
        row = actual[constraint.name]
        if (row['kind'] != constraint.kind or row['deferrable'] is not False or row['deferred'] is not False
            or row['validated'] is not True or row['local'] is not True or row['inheritance_count'] != 0
            or row['parent_oid'] != 0 or row['no_inherit'] is not (constraint.kind in ('p','u','f'))):
            _fail()
        if major == 18 and (row['enforced'] is not True or row['period'] is not False):
            _fail()
        if major != 18 and (row['enforced'] is not None or row['period'] is not None):
            _fail()
        if row['builtin_dependencies'] is not True:
            _fail()
        if constraint.kind == 'c':
            if row['check_definition'] is None:
                _fail()
            if _check_tokens(row['check_definition']) != _check_tokens('CHECK (' + constraint.expression + ')'):
                _fail()
        elif tuple(row['columns']) != constraint.columns:
            _fail()
        if constraint.kind in ('p','u','f'):
            if row['supporting_index_valid'] is not True:
                _fail()
            if constraint.kind in ('p','u') and row['index_oid'] != objects[constraint.name]['oid']:
                _fail()
        elif row['supporting_index_valid'] is not None or row['index_oid'] != 0:
            _fail()
        if constraint.kind == 'f':
            if (row['ref_schema'] != schema or row['ref_table'] != constraint.ref_table
                or row['ref_oid'] != objects[constraint.ref_table]['oid']
                or tuple(row['ref_columns']) != constraint.ref_columns
                or row['update_action'] != 'r' or row['delete_action'] != 'r' or row['match_type'] != 's'):
                _fail()
        elif (row['ref_oid'] != 0 or row['ref_schema'] is not None or row['ref_table'] is not None
              or tuple(row['ref_columns']) or row['update_action'] != ' '
              or row['delete_action'] != ' ' or row['match_type'] != ' '):
            _fail()
        if constraint.kind != 'c' and row['check_definition'] is not None:
            _fail()


def _check_indexes(spec, rows):
    actual = _unique([row for row in rows if row['table_name'] == spec.name], 'name')
    _same_names(actual, tuple(i.name for i in spec.indexes))
    columns = {column.name: column for column in spec.columns}
    for index in spec.indexes:
        row = actual[index.name]
        if (row['method'] != 'btree' or row['unique'] is not index.unique or row['primary'] is not index.primary
            or any(row[key] is not True for key in ('immediate','valid','ready','live'))
            or row['nulls_not_distinct'] is not False or row['has_expression'] is not False
            or row['has_predicate'] is not False or tuple(row['columns']) != index.columns
            or row['key_count'] != len(index.columns) or row['total_count'] != len(index.columns)
            or tuple(row['options']) != (0,) * len(index.columns)
            or tuple(row['opclasses']) != tuple(_OPCLASSES[columns[name].type_name] for name in index.columns)
            or tuple(row['collations']) != tuple('pg_catalog.default' if columns[name].type_name == 'text' else '' for name in index.columns)):
            _fail()


def _check_guards(functions, triggers, schema):
    funcs = _unique(functions, 'name')
    names = tuple(spec.name + '_guard_v9' for spec in SCHEMA_SPEC)
    _same_names(funcs, names)
    trigger_map = _unique(triggers, 'name')
    _same_names(trigger_map, tuple(spec.name + '_guard_trg_v9' for spec in SCHEMA_SPEC))
    for spec in SCHEMA_SPEC:
        name = spec.name + '_guard_v9'; row = funcs[name]
        if (row['language'] != 'plpgsql' or row['returns'] != 'trigger' or row['return_schema'] != 'pg_catalog'
            or row['return_type_name'] != 'trigger' or row['nargs'] != 0
            or row['kind'] != 'f' or row['returns_set'] is not False or row['security_definer'] is not False
            or row['leakproof'] is not False or row['strict'] is not False
            or row['volatility'] != 'v' or row['parallel'] != 'u' or row['cost'] != 100 or row['rows'] != 0
            or tuple(row['config'] or ()) != ('search_path=pg_catalog',) or row['plain_args'] is not True
            or row['source_bounded'] is not True or row['unexpected_acl'] is not False
            or row['source'] != _bound(spec.guard_source, schema)):
            _fail()
        trigger = trigger_map[spec.name + '_guard_trg_v9']
        if (trigger['table_name'] != spec.name or trigger['function_name'] != name
            or trigger['function_schema'] != schema or trigger['function_oid'] != row['oid']
            or trigger['type'] != 31 or trigger['enabled'] != 'O' or trigger['nargs'] != 0
            or trigger['args'] != '' or trigger['has_when'] is not False or trigger['constraint_oid'] != 0
            or trigger['deferrable'] is not False or trigger['deferred'] is not False):
            _fail()


def _check_ri_triggers(rows, constraints, objects):
    expected = []
    for row in constraints:
        if row['table_name'] not in TABLES or row['kind'] != 'f':
            continue
        source = objects[row['table_name']]['oid']; target = row['ref_oid']; ident = row['oid']
        expected.extend(((ident,source,'RI_FKey_check_ins',5), (ident,source,'RI_FKey_check_upd',17),
                         (ident,target,'RI_FKey_restrict_del',9), (ident,target,'RI_FKey_restrict_upd',17)))
    actual = []
    for row in rows:
        if (row['function_schema'] != 'pg_catalog' or row['enabled'] != 'O' or row['nargs'] != 0
            or row['args'] != '' or row['has_when'] is not False
            or row['deferrable'] is not False or row['deferred'] is not False):
            _fail()
        actual.append((row['constraint_oid'],row['table_oid'],row['function_name'],row['type']))
    if sorted(actual) != sorted(expected):
        _fail()


def _inspect(db, *, expected_schema=None):
    contexts = _rows(db, _CONTEXT_SQL)
    if len(contexts) != 1:
        _fail('BASE_REQUIRED')
    context = _native_context_values(contexts[0])
    oid, schema, version = context[0], context[1], context[3]
    if expected_schema is not None and schema != expected_schema:
        _fail()
    major = version // 10000
    objects = _unique(_rows(db, _OBJECTS_SQL, (oid,)), 'name')
    columns = _rows(db, _COLUMNS_SQL, (oid,list(TABLES)))
    constraints = _rows(db, _CONSTRAINTS_SQL_18 if major == 18 else _CONSTRAINTS_SQL, (oid,list(TABLES)))
    _base_ready(objects, columns, constraints, major)
    versions_query = '/* ss_labels:versions */ SELECT version FROM ' + _quoted_schema(schema) + '.ss_schema_versions WHERE version IN (8,' + str(MIGRATION_VERSION) + ') ORDER BY version LIMIT 3'
    versions = _rows(db, versions_query)
    if any(set(row) != {'version'} or type(row['version']) is not int for row in versions):
        _fail()
    values = tuple(row['version'] for row in versions)
    if values not in ((8,), (8,MIGRATION_VERSION)):
        _fail('BASE_REQUIRED')
    functions = _rows(db, _FUNCTIONS_SQL, (oid,))
    family_names = set(objects) - set(_BASE_NAMES)
    if not family_names:
        if functions or values != (8,):
            _fail('PARTIAL')
        return SchemaState(9,'absent',0)
    expected_objects = set(TABLES) | {index.name for spec in SCHEMA_SPEC for index in spec.indexes}
    if family_names != expected_objects or values != (8,MIGRATION_VERSION):
        _fail('PARTIAL')
    for spec in SCHEMA_SPEC:
        obj = objects[spec.name]
        if (obj['kind'] != 'r' or obj['persistence'] != 'p' or obj['partitioned'] is not False
            or obj['has_inheritance'] is not False or obj['has_rules'] is not False
            or obj['rls'] is not False or obj['force_rls'] is not False):
            _fail()
        for index in spec.indexes:
            item = objects[index.name]
            if (item['kind'] != 'i' or item['persistence'] != 'p' or item['partitioned'] is not False
                or item['has_inheritance'] is not False or item['has_rules'] is not False):
                _fail()
        _check_columns(spec, columns)
        _check_constraints(spec, constraints, schema, objects, major)
    indexes = _rows(db, _INDEXES_SQL, (oid,list(TABLES)))
    triggers = _rows(db, _TRIGGERS_SQL, (oid,list(TABLES)))
    ri_triggers = _rows(db, _RI_TRIGGERS_SQL, (oid,list(TABLES)))
    for spec in SCHEMA_SPEC:
        _check_indexes(spec, indexes)
    _check_guards(functions,triggers,schema)
    _check_ri_triggers(ri_triggers,constraints,objects)
    return SchemaState(9,'present',5)


@_safe
def inspect(db):
    """Read-only attestation, never authority or a perpetual context lease.

    A mutating caller must recheck required selected schema, origin and other
    context on its SAME connection/transaction, not use another pool's result.
    Presence never enables unfinished operator lifecycle.
    """
    return _inspect(db)


def _read_install_sql(schema):
    failed = False; raw = None
    try:
        raw = Path(__file__).with_name('intake_label_schema.sql').read_bytes()
    except OSError:
        failed = True
    if failed or type(raw) is not bytes or len(raw) > SQL_MAX_BYTES or hashlib.sha256(raw).hexdigest() != SQL_SHA256:
        _fail('INSTALL_SOURCE_CHANGED')
    text = raw.decode('utf-8')
    if text.count(SQL_TOKEN) != SQL_TOKEN_COUNT:
        _fail('INSTALL_SOURCE_CHANGED')
    bound = _bound(text,schema)
    if SQL_TOKEN in bound:
        _fail('INSTALL_SOURCE_CHANGED')
    return bound


@_safe
def install(db):
    """Explicit operator primitive; caller owns transaction and actual authority."""
    if getattr(db,'autocommit',None) is not False:
        _fail('TRANSACTION_REQUIRED')
    db.execute('SELECT pg_catalog.pg_advisory_xact_lock(81726001)')
    contexts = _rows(db,_CONTEXT_SQL)
    if len(contexts) != 1:
        _fail()
    context = _native_context_values(contexts[0])
    schema = context[1]
    state = _inspect(db, expected_schema=schema)
    if state.state == 'present':
        return state
    sql = _read_install_sql(schema)
    db.execute('SAVEPOINT ss_intake_label_install_v9')
    failed = False; result = None
    try:
        db.execute(sql,prepare=False)
        result = _inspect(db, expected_schema=schema)
        if result.state != 'present':
            failed = True
    except Exception:
        failed = True
    if failed:
        try:
            db.execute('ROLLBACK TO SAVEPOINT ss_intake_label_install_v9')
            db.execute('RELEASE SAVEPOINT ss_intake_label_install_v9')
        except Exception:
            pass
        _fail('INSTALL_FAILED')
    db.execute('RELEASE SAVEPOINT ss_intake_label_install_v9')
    return result


