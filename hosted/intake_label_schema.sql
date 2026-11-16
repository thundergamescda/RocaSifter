-- Explicit feature9 schema only; no startup migration or lifecycle permission.
-- Fixed schema token is bound by the pinned installer to public or isolated test schema.
-- Normal input/job/request mutation and all deletion remain denied.

CREATE TABLE __SS_LABEL_SCHEMA__.ss_intake_label_inputs (
  tenant_id pg_catalog.uuid CONSTRAINT ss_intake_label_inputs_tenant_id_nn_v9 NOT NULL,
  id pg_catalog.uuid CONSTRAINT ss_intake_label_inputs_id_nn_v9 NOT NULL,
  receiving_id pg_catalog.uuid CONSTRAINT ss_intake_label_inputs_receiving_id_nn_v9 NOT NULL,
  server_revision pg_catalog.int8 CONSTRAINT ss_intake_label_inputs_server_revision_nn_v9 NOT NULL,
  plan_hash pg_catalog.text CONSTRAINT ss_intake_label_inputs_plan_hash_nn_v9 NOT NULL,
  input_hash pg_catalog.text CONSTRAINT ss_intake_label_inputs_input_hash_nn_v9 NOT NULL,
  terminal_basis_hash pg_catalog.text CONSTRAINT ss_intake_label_inputs_terminal_basis_hash_nn_v9 NOT NULL,
  confirmed_rows_hash pg_catalog.text CONSTRAINT ss_intake_label_inputs_confirmed_rows_hash_nn_v9 NOT NULL,
  row_count pg_catalog.int4 CONSTRAINT ss_intake_label_inputs_row_count_nn_v9 NOT NULL,
  total_copies pg_catalog.int4 CONSTRAINT ss_intake_label_inputs_total_copies_nn_v9 NOT NULL,
  input_body pg_catalog.text,
  content_state pg_catalog.text CONSTRAINT ss_intake_label_inputs_content_state_nn_v9 NOT NULL,
  revision pg_catalog.int8 CONSTRAINT ss_intake_label_inputs_revision_nn_v9 NOT NULL,
  created_at pg_catalog.timestamptz CONSTRAINT ss_intake_label_inputs_created_at_nn_v9 NOT NULL,
  policy_hash pg_catalog.text CONSTRAINT ss_intake_label_inputs_policy_hash_nn_v9 NOT NULL,
  retention_until pg_catalog.timestamptz CONSTRAINT ss_intake_label_inputs_retention_until_nn_v9 NOT NULL,
  restricted_at pg_catalog.timestamptz,
  review_due_at pg_catalog.timestamptz,
  disposed_at pg_catalog.timestamptz,
  suppression_until pg_catalog.timestamptz,
  CONSTRAINT ss_intake_label_inputs_pk_v9 PRIMARY KEY (tenant_id,id) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_inputs_tenant_fk_v9 FOREIGN KEY (tenant_id) REFERENCES __SS_LABEL_SCHEMA__.ss_tenants (id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_inputs_source_fk_v9 FOREIGN KEY (tenant_id,receiving_id) REFERENCES __SS_LABEL_SCHEMA__.ss_receiving_operations (tenant_id,id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_inputs_hash_uq_v9 UNIQUE (tenant_id,input_hash) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_inputs_terminal_uq_v9 UNIQUE (tenant_id,terminal_basis_hash) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_inputs_identity_uq_v9 UNIQUE (tenant_id,id,receiving_id,input_hash,terminal_basis_hash) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_inputs_hashes_ck_v9 CHECK ((((octet_length(plan_hash) = 64) AND (plan_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(input_hash) = 64) AND (input_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(terminal_basis_hash) = 64) AND (terminal_basis_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(confirmed_rows_hash) = 64) AND (confirmed_rows_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(policy_hash) = 64) AND (policy_hash ~ '^[0-9a-f]{64}$'::text)))),
  CONSTRAINT ss_intake_label_inputs_counts_ck_v9 CHECK (((server_revision >= 0) AND (revision >= 1) AND ((row_count >= 1) AND (row_count <= 1000)) AND ((total_copies >= 1) AND (total_copies <= 1000000)) AND (total_copies >= row_count))),
  CONSTRAINT ss_intake_label_inputs_body_ck_v9 CHECK (((input_body IS NULL) OR ((octet_length(input_body) >= 1) AND (octet_length(input_body) <= 8388608)))),
  CONSTRAINT ss_intake_label_inputs_lifecycle_ck_v9 CHECK (((content_state = ANY (ARRAY['active'::text, 'restricted'::text, 'disposal_pending'::text, 'disposed'::text])) AND (retention_until > created_at) AND (((content_state = 'active'::text) AND (input_body IS NOT NULL) AND (restricted_at IS NULL) AND (review_due_at IS NULL) AND (disposed_at IS NULL) AND (suppression_until IS NULL)) OR ((content_state = ANY (ARRAY['restricted'::text, 'disposal_pending'::text])) AND (input_body IS NOT NULL) AND (restricted_at IS NOT NULL) AND (review_due_at IS NOT NULL) AND (disposed_at IS NULL) AND (suppression_until IS NULL)) OR ((content_state = 'disposed'::text) AND (input_body IS NULL) AND (restricted_at IS NOT NULL) AND (review_due_at IS NULL) AND (disposed_at IS NOT NULL) AND (suppression_until IS NOT NULL) AND (suppression_until > disposed_at))))),
  CONSTRAINT ss_intake_label_inputs_dates_ck_v9 CHECK (((isfinite(created_at) AND (created_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (created_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)) AND (isfinite(retention_until) AND (retention_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (retention_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)) AND ((restricted_at IS NULL) OR (isfinite(restricted_at) AND (restricted_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (restricted_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND ((review_due_at IS NULL) OR (isfinite(review_due_at) AND (review_due_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (review_due_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND ((disposed_at IS NULL) OR (isfinite(disposed_at) AND (disposed_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (disposed_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND ((suppression_until IS NULL) OR (isfinite(suppression_until) AND (suppression_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (suppression_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)))))
);

CREATE TABLE __SS_LABEL_SCHEMA__.ss_intake_label_jobs (
  tenant_id pg_catalog.uuid CONSTRAINT ss_intake_label_jobs_tenant_id_nn_v9 NOT NULL,
  id pg_catalog.uuid CONSTRAINT ss_intake_label_jobs_id_nn_v9 NOT NULL,
  receiving_id pg_catalog.uuid CONSTRAINT ss_intake_label_jobs_receiving_id_nn_v9 NOT NULL,
  input_id pg_catalog.uuid CONSTRAINT ss_intake_label_jobs_input_id_nn_v9 NOT NULL,
  input_hash pg_catalog.text CONSTRAINT ss_intake_label_jobs_input_hash_nn_v9 NOT NULL,
  terminal_basis_hash pg_catalog.text CONSTRAINT ss_intake_label_jobs_terminal_basis_hash_nn_v9 NOT NULL,
  job_basis_hash pg_catalog.text CONSTRAINT ss_intake_label_jobs_job_basis_hash_nn_v9 NOT NULL,
  renderer_build_hash pg_catalog.text CONSTRAINT ss_intake_label_jobs_renderer_build_hash_nn_v9 NOT NULL,
  purpose pg_catalog.text CONSTRAINT ss_intake_label_jobs_purpose_nn_v9 NOT NULL,
  reprint_of_job_id pg_catalog.uuid,
  copy_offset pg_catalog.int4 CONSTRAINT ss_intake_label_jobs_copy_offset_nn_v9 NOT NULL,
  copy_count pg_catalog.int4 CONSTRAINT ss_intake_label_jobs_copy_count_nn_v9 NOT NULL,
  part_size pg_catalog.int4 CONSTRAINT ss_intake_label_jobs_part_size_nn_v9 NOT NULL,
  part_count pg_catalog.int4 CONSTRAINT ss_intake_label_jobs_part_count_nn_v9 NOT NULL,
  render_plan_hash pg_catalog.text CONSTRAINT ss_intake_label_jobs_render_plan_hash_nn_v9 NOT NULL,
  created_by pg_catalog.uuid CONSTRAINT ss_intake_label_jobs_created_by_nn_v9 NOT NULL,
  created_at pg_catalog.timestamptz CONSTRAINT ss_intake_label_jobs_created_at_nn_v9 NOT NULL,
  policy_hash pg_catalog.text CONSTRAINT ss_intake_label_jobs_policy_hash_nn_v9 NOT NULL,
  retention_until pg_catalog.timestamptz CONSTRAINT ss_intake_label_jobs_retention_until_nn_v9 NOT NULL,
  CONSTRAINT ss_intake_label_jobs_pk_v9 PRIMARY KEY (tenant_id,id) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_jobs_basis_uq_v9 UNIQUE (tenant_id,job_basis_hash) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_jobs_input_identity_uq_v9 UNIQUE (tenant_id,id,input_id) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_jobs_input_fk_v9 FOREIGN KEY (tenant_id,input_id,receiving_id,input_hash,terminal_basis_hash) REFERENCES __SS_LABEL_SCHEMA__.ss_intake_label_inputs (tenant_id,id,receiving_id,input_hash,terminal_basis_hash) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_jobs_source_fk_v9 FOREIGN KEY (tenant_id,receiving_id) REFERENCES __SS_LABEL_SCHEMA__.ss_receiving_operations (tenant_id,id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_jobs_staff_fk_v9 FOREIGN KEY (tenant_id,created_by) REFERENCES __SS_LABEL_SCHEMA__.ss_staff (tenant_id,id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_jobs_parent_fk_v9 FOREIGN KEY (tenant_id,reprint_of_job_id,input_id) REFERENCES __SS_LABEL_SCHEMA__.ss_intake_label_jobs (tenant_id,id,input_id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_jobs_hashes_ck_v9 CHECK ((((octet_length(input_hash) = 64) AND (input_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(terminal_basis_hash) = 64) AND (terminal_basis_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(job_basis_hash) = 64) AND (job_basis_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(renderer_build_hash) = 64) AND (renderer_build_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(render_plan_hash) = 64) AND (render_plan_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(policy_hash) = 64) AND (policy_hash ~ '^[0-9a-f]{64}$'::text)))),
  CONSTRAINT ss_intake_label_jobs_range_ck_v9 CHECK ((((copy_offset >= 0) AND (copy_offset <= 999999)) AND ((copy_count >= 1) AND (copy_count <= 1000000)) AND ((copy_offset + copy_count) <= 1000000) AND ((part_size >= 1) AND (part_size <= 1000)) AND (part_count = (((copy_count + part_size) - 1) / part_size)))),
  CONSTRAINT ss_intake_label_jobs_parent_ck_v9 CHECK (((purpose = ANY (ARRAY['initial'::text, 'range_reprint'::text])) AND (((purpose = 'initial'::text) AND (reprint_of_job_id IS NULL)) OR ((purpose = 'range_reprint'::text) AND (reprint_of_job_id IS NOT NULL) AND (reprint_of_job_id <> id))))),
  CONSTRAINT ss_intake_label_jobs_time_ck_v9 CHECK ((retention_until > created_at)),
  CONSTRAINT ss_intake_label_jobs_dates_ck_v9 CHECK (((isfinite(created_at) AND (created_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (created_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)) AND (isfinite(retention_until) AND (retention_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (retention_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))))
);

CREATE TABLE __SS_LABEL_SCHEMA__.ss_intake_label_publications (
  tenant_id pg_catalog.uuid CONSTRAINT ss_intake_label_publications_tenant_id_nn_v9 NOT NULL,
  id pg_catalog.uuid CONSTRAINT ss_intake_label_publications_id_nn_v9 NOT NULL,
  job_id pg_catalog.uuid CONSTRAINT ss_intake_label_publications_job_id_nn_v9 NOT NULL,
  part_index pg_catalog.int4 CONSTRAINT ss_intake_label_publications_part_index_nn_v9 NOT NULL,
  part_hash pg_catalog.text CONSTRAINT ss_intake_label_publications_part_hash_nn_v9 NOT NULL,
  copy_offset pg_catalog.int4 CONSTRAINT ss_intake_label_publications_copy_offset_nn_v9 NOT NULL,
  copy_count pg_catalog.int4 CONSTRAINT ss_intake_label_publications_copy_count_nn_v9 NOT NULL,
  generation_id pg_catalog.uuid CONSTRAINT ss_intake_label_publications_generation_id_nn_v9 NOT NULL,
  state pg_catalog.text CONSTRAINT ss_intake_label_publications_state_nn_v9 NOT NULL,
  revision pg_catalog.int8 CONSTRAINT ss_intake_label_publications_revision_nn_v9 NOT NULL,
  lease_generation pg_catalog.int8 CONSTRAINT ss_intake_label_publications_lease_generation_nn_v9 NOT NULL,
  lease_token pg_catalog.uuid,
  lease_until pg_catalog.timestamptz,
  authorization_request_key pg_catalog.text,
  render_input_hash pg_catalog.text,
  render_basis_hash pg_catalog.text,
  output_metadata pg_catalog.text,
  pair_sealed_at pg_catalog.timestamptz,
  pdf_sha256 pg_catalog.text,
  pdf_bytes pg_catalog.int4,
  zpl_sha256 pg_catalog.text,
  zpl_bytes pg_catalog.int4,
  published_at pg_catalog.timestamptz,
  retention_until pg_catalog.timestamptz CONSTRAINT ss_intake_label_publications_retention_until_nn_v9 NOT NULL,
  tombstoned_at pg_catalog.timestamptz,
  policy_hash pg_catalog.text CONSTRAINT ss_intake_label_publications_policy_hash_nn_v9 NOT NULL,
  error_code pg_catalog.text,
  CONSTRAINT ss_intake_label_publications_pk_v9 PRIMARY KEY (tenant_id,id) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_publications_part_uq_v9 UNIQUE (tenant_id,job_id,part_index) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_publications_identity_uq_v9 UNIQUE (tenant_id,id,job_id) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_publications_generation_uq_v9 UNIQUE (tenant_id,generation_id) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_publications_job_fk_v9 FOREIGN KEY (tenant_id,job_id) REFERENCES __SS_LABEL_SCHEMA__.ss_intake_label_jobs (tenant_id,id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_publications_hashes_ck_v9 CHECK (((((octet_length(part_hash) = 64) AND (part_hash ~ '^[0-9a-f]{64}$'::text)) AND ((render_input_hash IS NULL) OR ((octet_length(render_input_hash) = 64) AND (render_input_hash ~ '^[0-9a-f]{64}$'::text))) AND ((render_basis_hash IS NULL) OR ((octet_length(render_basis_hash) = 64) AND (render_basis_hash ~ '^[0-9a-f]{64}$'::text))) AND ((pdf_sha256 IS NULL) OR ((octet_length(pdf_sha256) = 64) AND (pdf_sha256 ~ '^[0-9a-f]{64}$'::text))) AND ((zpl_sha256 IS NULL) OR ((octet_length(zpl_sha256) = 64) AND (zpl_sha256 ~ '^[0-9a-f]{64}$'::text))) AND ((octet_length(policy_hash) = 64) AND (policy_hash ~ '^[0-9a-f]{64}$'::text))) AND ((authorization_request_key IS NULL) OR (((octet_length(authorization_request_key) >= 1) AND (octet_length(authorization_request_key) <= 150)) AND (authorization_request_key ~ '^[A-Za-z0-9_-]{1,150}$'::text))) AND ((error_code IS NULL) OR (((octet_length(error_code) >= 1) AND (octet_length(error_code) <= 100)) AND (error_code ~ '^[A-Z][A-Z0-9_]{0,99}$'::text))))),
  CONSTRAINT ss_intake_label_publications_range_ck_v9 CHECK (((part_index >= 0) AND ((copy_offset >= 0) AND (copy_offset <= 999999)) AND ((copy_count >= 1) AND (copy_count <= 1000)) AND ((copy_offset + copy_count) <= 1000000) AND (revision >= 1) AND (lease_generation >= 0))),
  CONSTRAINT ss_intake_label_publications_state_ck_v9 CHECK (((state = ANY (ARRAY['planned'::text, 'queued'::text, 'rendering'::text, 'bytes_prepared'::text, 'publishing'::text, 'published'::text, 'authorization_required'::text, 'recovery_required'::text])) AND ((state = 'planned'::text) = (authorization_request_key IS NULL)))),
  CONSTRAINT ss_intake_label_publications_lease_ck_v9 CHECK ((((lease_token IS NULL) = (lease_until IS NULL)) AND ((state = ANY (ARRAY['rendering'::text, 'bytes_prepared'::text, 'publishing'::text])) = (lease_token IS NOT NULL)) AND ((lease_until IS NULL) OR (lease_until <= retention_until)))),
  CONSTRAINT ss_intake_label_publications_pair_ck_v9 CHECK (((((pair_sealed_at IS NULL) AND (pdf_sha256 IS NULL) AND (pdf_bytes IS NULL) AND (zpl_sha256 IS NULL) AND (zpl_bytes IS NULL)) OR ((pair_sealed_at IS NOT NULL) AND (pdf_sha256 IS NOT NULL) AND (pdf_bytes IS NOT NULL) AND (zpl_sha256 IS NOT NULL) AND (zpl_bytes IS NOT NULL) AND ((pdf_bytes >= 1) AND (pdf_bytes <= 33554432)) AND ((zpl_bytes >= 1) AND (zpl_bytes <= 33554432)))) AND (((render_input_hash IS NULL) AND (render_basis_hash IS NULL) AND (output_metadata IS NULL)) OR ((render_input_hash IS NOT NULL) AND (render_basis_hash IS NOT NULL) AND (output_metadata IS NOT NULL) AND ((octet_length(output_metadata) >= 1) AND (octet_length(output_metadata) <= 2097152)))) AND ((pair_sealed_at IS NULL) OR ((render_input_hash IS NOT NULL) AND (render_basis_hash IS NOT NULL) AND (output_metadata IS NOT NULL))) AND ((NOT (state = ANY (ARRAY['bytes_prepared'::text, 'publishing'::text, 'published'::text]))) OR (pair_sealed_at IS NOT NULL)))),
  CONSTRAINT ss_intake_label_publications_time_ck_v9 CHECK (((((state = 'published'::text) = (published_at IS NOT NULL)) AND ((state <> 'published'::text) OR ((tombstoned_at IS NULL) AND (published_at < retention_until)))) AND ((published_at IS NULL) OR (pair_sealed_at <= published_at)))),
  CONSTRAINT ss_intake_label_publications_dates_ck_v9 CHECK ((((lease_until IS NULL) OR (isfinite(lease_until) AND (lease_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (lease_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND ((pair_sealed_at IS NULL) OR (isfinite(pair_sealed_at) AND (pair_sealed_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (pair_sealed_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND ((published_at IS NULL) OR (isfinite(published_at) AND (published_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (published_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND (isfinite(retention_until) AND (retention_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (retention_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)) AND ((tombstoned_at IS NULL) OR (isfinite(tombstoned_at) AND (tombstoned_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (tombstoned_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)))))
);

CREATE TABLE __SS_LABEL_SCHEMA__.ss_intake_label_previews (
  tenant_id pg_catalog.uuid CONSTRAINT ss_intake_label_previews_tenant_id_nn_v9 NOT NULL,
  id pg_catalog.uuid CONSTRAINT ss_intake_label_previews_id_nn_v9 NOT NULL,
  job_id pg_catalog.uuid CONSTRAINT ss_intake_label_previews_job_id_nn_v9 NOT NULL,
  publication_id pg_catalog.uuid CONSTRAINT ss_intake_label_previews_publication_id_nn_v9 NOT NULL,
  created_by pg_catalog.uuid CONSTRAINT ss_intake_label_previews_created_by_nn_v9 NOT NULL,
  session_hash pg_catalog.text CONSTRAINT ss_intake_label_previews_session_hash_nn_v9 NOT NULL,
  role pg_catalog.text CONSTRAINT ss_intake_label_previews_role_nn_v9 NOT NULL,
  request_key pg_catalog.text CONSTRAINT ss_intake_label_previews_request_key_nn_v9 NOT NULL,
  selector_body pg_catalog.text CONSTRAINT ss_intake_label_previews_selector_body_nn_v9 NOT NULL,
  selector_hash pg_catalog.text CONSTRAINT ss_intake_label_previews_selector_hash_nn_v9 NOT NULL,
  input_hash pg_catalog.text CONSTRAINT ss_intake_label_previews_input_hash_nn_v9 NOT NULL,
  part_hash pg_catalog.text CONSTRAINT ss_intake_label_previews_part_hash_nn_v9 NOT NULL,
  build_hash pg_catalog.text CONSTRAINT ss_intake_label_previews_build_hash_nn_v9 NOT NULL,
  state pg_catalog.text CONSTRAINT ss_intake_label_previews_state_nn_v9 NOT NULL,
  revision pg_catalog.int8 CONSTRAINT ss_intake_label_previews_revision_nn_v9 NOT NULL,
  lease_generation pg_catalog.int8 CONSTRAINT ss_intake_label_previews_lease_generation_nn_v9 NOT NULL,
  lease_token pg_catalog.uuid,
  lease_until pg_catalog.timestamptz,
  render_basis_hash pg_catalog.text,
  overflows_body pg_catalog.text,
  clipping_required pg_catalog.bool,
  created_at pg_catalog.timestamptz CONSTRAINT ss_intake_label_previews_created_at_nn_v9 NOT NULL,
  ready_at pg_catalog.timestamptz,
  expires_at pg_catalog.timestamptz CONSTRAINT ss_intake_label_previews_expires_at_nn_v9 NOT NULL,
  error_code pg_catalog.text,
  expected_publication_revision pg_catalog.int8 CONSTRAINT ss_intake_label_previews_expected_publication_revision_nn_v9 NOT NULL,
  CONSTRAINT ss_intake_label_previews_pk_v9 PRIMARY KEY (tenant_id,id) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_previews_key_uq_v9 UNIQUE (tenant_id,request_key) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_previews_request_identity_uq_v9 UNIQUE (tenant_id,id,request_key,job_id,publication_id,expected_publication_revision) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_previews_publication_fk_v9 FOREIGN KEY (tenant_id,publication_id,job_id) REFERENCES __SS_LABEL_SCHEMA__.ss_intake_label_publications (tenant_id,id,job_id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_previews_staff_fk_v9 FOREIGN KEY (tenant_id,created_by) REFERENCES __SS_LABEL_SCHEMA__.ss_staff (tenant_id,id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_previews_hashes_ck_v9 CHECK (((((octet_length(session_hash) = 64) AND (session_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(selector_hash) = 64) AND (selector_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(input_hash) = 64) AND (input_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(part_hash) = 64) AND (part_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(build_hash) = 64) AND (build_hash ~ '^[0-9a-f]{64}$'::text)) AND ((render_basis_hash IS NULL) OR ((octet_length(render_basis_hash) = 64) AND (render_basis_hash ~ '^[0-9a-f]{64}$'::text)))) AND (((octet_length(request_key) >= 1) AND (octet_length(request_key) <= 150)) AND (request_key ~ '^[A-Za-z0-9_-]{1,150}$'::text)) AND ((error_code IS NULL) OR (((octet_length(error_code) >= 1) AND (octet_length(error_code) <= 100)) AND (error_code ~ '^[A-Z][A-Z0-9_]{0,99}$'::text))))),
  CONSTRAINT ss_intake_label_previews_body_ck_v9 CHECK ((((octet_length(selector_body) >= 1) AND (octet_length(selector_body) <= 16384)) AND ((overflows_body IS NULL) OR ((octet_length(overflows_body) >= 1) AND (octet_length(overflows_body) <= 2097152))))),
  CONSTRAINT ss_intake_label_previews_state_ck_v9 CHECK (((state = ANY (ARRAY['queued'::text, 'checking'::text, 'ready'::text, 'failed'::text, 'expired'::text])) AND (role = ANY (ARRAY['owner'::text, 'manager'::text])) AND (revision >= 1) AND (lease_generation >= 0) AND (((ready_at IS NULL) AND (render_basis_hash IS NULL) AND (overflows_body IS NULL) AND (clipping_required IS NULL)) OR ((ready_at IS NOT NULL) AND (render_basis_hash IS NOT NULL) AND (overflows_body IS NOT NULL) AND (clipping_required IS NOT NULL))) AND ((state <> 'ready'::text) OR (ready_at IS NOT NULL)))),
  CONSTRAINT ss_intake_label_previews_lease_ck_v9 CHECK ((((lease_token IS NULL) = (lease_until IS NULL)) AND ((state = 'checking'::text) = (lease_token IS NOT NULL)) AND ((lease_until IS NULL) OR (lease_until <= expires_at)))),
  CONSTRAINT ss_intake_label_previews_time_ck_v9 CHECK (((expires_at > created_at) AND (expires_at <= (created_at + '00:10:00'::interval)) AND ((ready_at IS NULL) OR ((ready_at >= created_at) AND (expires_at > ready_at) AND (expires_at <= (ready_at + '00:05:00'::interval)))))),
  CONSTRAINT ss_intake_label_previews_publication_revision_ck_v9 CHECK ((expected_publication_revision >= 1)),
  CONSTRAINT ss_intake_label_previews_dates_ck_v9 CHECK ((((lease_until IS NULL) OR (isfinite(lease_until) AND (lease_until >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (lease_until <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND (isfinite(created_at) AND (created_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (created_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone)) AND ((ready_at IS NULL) OR (isfinite(ready_at) AND (ready_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (ready_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))) AND (isfinite(expires_at) AND (expires_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (expires_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))))
);

CREATE TABLE __SS_LABEL_SCHEMA__.ss_intake_label_requests (
  tenant_id pg_catalog.uuid CONSTRAINT ss_intake_label_requests_tenant_id_nn_v9 NOT NULL,
  request_key pg_catalog.text CONSTRAINT ss_intake_label_requests_request_key_nn_v9 NOT NULL,
  kind pg_catalog.text CONSTRAINT ss_intake_label_requests_kind_nn_v9 NOT NULL,
  intent_body pg_catalog.text CONSTRAINT ss_intake_label_requests_intent_body_nn_v9 NOT NULL,
  intent_hash pg_catalog.text CONSTRAINT ss_intake_label_requests_intent_hash_nn_v9 NOT NULL,
  preview_id pg_catalog.uuid,
  job_id pg_catalog.uuid CONSTRAINT ss_intake_label_requests_job_id_nn_v9 NOT NULL,
  publication_id pg_catalog.uuid CONSTRAINT ss_intake_label_requests_publication_id_nn_v9 NOT NULL,
  reprint_of_publication_id pg_catalog.uuid,
  created_by pg_catalog.uuid CONSTRAINT ss_intake_label_requests_created_by_nn_v9 NOT NULL,
  session_hash pg_catalog.text CONSTRAINT ss_intake_label_requests_session_hash_nn_v9 NOT NULL,
  created_at pg_catalog.timestamptz CONSTRAINT ss_intake_label_requests_created_at_nn_v9 NOT NULL,
  expected_publication_revision pg_catalog.int8 CONSTRAINT ss_intake_label_requests_expected_publication_revision_nn_v9 NOT NULL,
  CONSTRAINT ss_intake_label_requests_pk_v9 PRIMARY KEY (tenant_id,request_key) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_requests_identity_uq_v9 UNIQUE (tenant_id,request_key,job_id,publication_id) NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_requests_publication_fk_v9 FOREIGN KEY (tenant_id,publication_id,job_id) REFERENCES __SS_LABEL_SCHEMA__.ss_intake_label_publications (tenant_id,id,job_id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_requests_preview_fk_v9 FOREIGN KEY (tenant_id,preview_id,request_key,job_id,publication_id,expected_publication_revision) REFERENCES __SS_LABEL_SCHEMA__.ss_intake_label_previews (tenant_id,id,request_key,job_id,publication_id,expected_publication_revision) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_requests_reprint_fk_v9 FOREIGN KEY (tenant_id,reprint_of_publication_id,job_id) REFERENCES __SS_LABEL_SCHEMA__.ss_intake_label_publications (tenant_id,id,job_id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_requests_staff_fk_v9 FOREIGN KEY (tenant_id,created_by) REFERENCES __SS_LABEL_SCHEMA__.ss_staff (tenant_id,id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE,
  CONSTRAINT ss_intake_label_requests_hashes_ck_v9 CHECK (((((octet_length(intent_hash) = 64) AND (intent_hash ~ '^[0-9a-f]{64}$'::text)) AND ((octet_length(session_hash) = 64) AND (session_hash ~ '^[0-9a-f]{64}$'::text))) AND (((octet_length(request_key) >= 1) AND (octet_length(request_key) <= 150)) AND (request_key ~ '^[A-Za-z0-9_-]{1,150}$'::text)))),
  CONSTRAINT ss_intake_label_requests_body_ck_v9 CHECK (((octet_length(intent_body) >= 1) AND (octet_length(intent_body) <= 16384))),
  CONSTRAINT ss_intake_label_requests_kind_ck_v9 CHECK (((kind = ANY (ARRAY['confirm_part'::text, 'resume_part'::text, 'reprint_exact'::text])) AND (((kind = 'reprint_exact'::text) AND (reprint_of_publication_id IS NOT NULL) AND (reprint_of_publication_id = publication_id)) OR ((kind <> 'reprint_exact'::text) AND (reprint_of_publication_id IS NULL))))),
  CONSTRAINT ss_intake_label_requests_publication_revision_ck_v9 CHECK ((expected_publication_revision >= 1)),
  CONSTRAINT ss_intake_label_requests_dates_ck_v9 CHECK (((isfinite(created_at) AND (created_at >= '0001-01-01 00:00:00+00'::timestamp with time zone) AND (created_at <= '9999-12-31 23:59:59.999999+00'::timestamp with time zone))))
);

ALTER TABLE __SS_LABEL_SCHEMA__.ss_intake_label_publications ADD CONSTRAINT ss_intake_label_publications_auth_fk_v9 FOREIGN KEY (tenant_id,authorization_request_key,job_id,id) REFERENCES __SS_LABEL_SCHEMA__.ss_intake_label_requests (tenant_id,request_key,job_id,publication_id) MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE INITIALLY IMMEDIATE;

CREATE INDEX ss_intake_label_inputs_source_idx_v9 ON __SS_LABEL_SCHEMA__.ss_intake_label_inputs USING btree (tenant_id,receiving_id,server_revision,plan_hash);
CREATE INDEX ss_intake_label_inputs_retention_idx_v9 ON __SS_LABEL_SCHEMA__.ss_intake_label_inputs USING btree (tenant_id,content_state,retention_until);
CREATE INDEX ss_intake_label_jobs_list_idx_v9 ON __SS_LABEL_SCHEMA__.ss_intake_label_jobs USING btree (tenant_id,receiving_id,created_at,id);
CREATE INDEX ss_intake_label_publications_work_idx_v9 ON __SS_LABEL_SCHEMA__.ss_intake_label_publications USING btree (tenant_id,state,lease_until,id);
CREATE INDEX ss_intake_label_previews_expiry_idx_v9 ON __SS_LABEL_SCHEMA__.ss_intake_label_previews USING btree (tenant_id,created_by,session_hash,expires_at,id);
CREATE INDEX ss_intake_label_requests_publication_idx_v9 ON __SS_LABEL_SCHEMA__.ss_intake_label_requests USING btree (tenant_id,publication_id,created_at,request_key);

CREATE FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_inputs_guard_v9() RETURNS pg_catalog.trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER PARALLEL UNSAFE
SET search_path=pg_catalog
AS $ss_label_guard$
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NEW.content_state <> 'active' OR NEW.revision <> 1 OR NEW.input_body IS NULL
     OR NEW.restricted_at IS NOT NULL OR NEW.review_due_at IS NOT NULL
     OR NEW.disposed_at IS NOT NULL OR NEW.suppression_until IS NOT NULL
     OR NEW.retention_until <= pg_catalog.clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  RETURN NEW;
END;
$ss_label_guard$;
REVOKE ALL ON FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_inputs_guard_v9() FROM PUBLIC;
CREATE TRIGGER ss_intake_label_inputs_guard_trg_v9 BEFORE INSERT OR UPDATE OR DELETE ON __SS_LABEL_SCHEMA__.ss_intake_label_inputs
FOR EACH ROW EXECUTE FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_inputs_guard_v9();

CREATE FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_jobs_guard_v9() RETURNS pg_catalog.trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER PARALLEL UNSAFE
SET search_path=pg_catalog
AS $ss_label_guard$
DECLARE
  basis __SS_LABEL_SCHEMA__.ss_intake_label_inputs%ROWTYPE;
  parent __SS_LABEL_SCHEMA__.ss_intake_label_jobs%ROWTYPE;
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  SELECT * INTO basis FROM __SS_LABEL_SCHEMA__.ss_intake_label_inputs
    WHERE tenant_id=NEW.tenant_id AND id=NEW.input_id;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF basis.content_state <> 'active' OR basis.input_body IS NULL
     OR basis.retention_until <= pg_catalog.clock_timestamp()
     OR NEW.receiving_id IS DISTINCT FROM basis.receiving_id
     OR NEW.input_hash IS DISTINCT FROM basis.input_hash
     OR NEW.terminal_basis_hash IS DISTINCT FROM basis.terminal_basis_hash
     OR NEW.copy_offset + NEW.copy_count > basis.total_copies
     OR NEW.created_at < basis.created_at
     OR NEW.retention_until > basis.retention_until OR NEW.retention_until <= pg_catalog.clock_timestamp()
     THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NEW.reprint_of_job_id IS NOT NULL THEN
    SELECT * INTO parent FROM __SS_LABEL_SCHEMA__.ss_intake_label_jobs
      WHERE tenant_id=NEW.tenant_id AND id=NEW.reprint_of_job_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    IF parent.input_id IS DISTINCT FROM NEW.input_id OR parent.id=NEW.id
       OR NEW.copy_offset < parent.copy_offset
       OR NEW.copy_offset + NEW.copy_count > parent.copy_offset + parent.copy_count
       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  END IF;
  RETURN NEW;
END;
$ss_label_guard$;
REVOKE ALL ON FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_jobs_guard_v9() FROM PUBLIC;
CREATE TRIGGER ss_intake_label_jobs_guard_trg_v9 BEFORE INSERT OR UPDATE OR DELETE ON __SS_LABEL_SCHEMA__.ss_intake_label_jobs
FOR EACH ROW EXECUTE FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_jobs_guard_v9();

CREATE FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_publications_guard_v9() RETURNS pg_catalog.trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER PARALLEL UNSAFE
SET search_path=pg_catalog
AS $ss_label_guard$
DECLARE
  job __SS_LABEL_SCHEMA__.ss_intake_label_jobs%ROWTYPE;
  request_basis RECORD;
  preview_basis RECORD;
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF TG_OP='INSERT' THEN
    IF NEW.state <> 'planned' OR NEW.revision <> 1 OR NEW.lease_generation <> 0
       OR NEW.lease_token IS NOT NULL OR NEW.lease_until IS NOT NULL
       OR NEW.authorization_request_key IS NOT NULL OR NEW.pair_sealed_at IS NOT NULL
       OR NEW.render_input_hash IS NOT NULL OR NEW.render_basis_hash IS NOT NULL
       OR NEW.output_metadata IS NOT NULL OR NEW.pdf_sha256 IS NOT NULL OR NEW.pdf_bytes IS NOT NULL
       OR NEW.zpl_sha256 IS NOT NULL OR NEW.zpl_bytes IS NOT NULL
       OR NEW.published_at IS NOT NULL OR NEW.tombstoned_at IS NOT NULL OR NEW.error_code IS NOT NULL
       OR NEW.retention_until <= pg_catalog.clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    SELECT * INTO job FROM __SS_LABEL_SCHEMA__.ss_intake_label_jobs WHERE tenant_id=NEW.tenant_id AND id=NEW.job_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    IF NEW.part_index >= job.part_count
       OR NEW.copy_offset <> job.copy_offset + NEW.part_index * job.part_size
       OR NEW.copy_count <> LEAST(job.part_size,job.copy_count - NEW.part_index * job.part_size)
       OR NEW.retention_until > job.retention_until THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    RETURN NEW;
  END IF;
  IF ROW(NEW.tenant_id,NEW.id,NEW.job_id,NEW.part_index,NEW.part_hash,NEW.copy_offset,NEW.copy_count,NEW.generation_id,NEW.retention_until,NEW.policy_hash,NEW.tombstoned_at) IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.job_id,OLD.part_index,OLD.part_hash,OLD.copy_offset,OLD.copy_count,OLD.generation_id,OLD.retention_until,OLD.policy_hash,OLD.tombstoned_at) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF OLD.state='published' OR NEW.revision <> OLD.revision + 1 THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NOT ((OLD.state='planned' AND NEW.state='queued')
       OR (OLD.state='queued' AND NEW.state IN ('rendering','authorization_required','recovery_required'))
       OR (OLD.state='rendering' AND NEW.state IN ('rendering','bytes_prepared','authorization_required','recovery_required'))
       OR (OLD.state='bytes_prepared' AND NEW.state IN ('bytes_prepared','publishing','authorization_required','recovery_required'))
       OR (OLD.state='publishing' AND NEW.state IN ('publishing','published','authorization_required','recovery_required'))
       OR (OLD.state IN ('authorization_required','recovery_required') AND NEW.state='queued')) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NEW.authorization_request_key IS DISTINCT FROM OLD.authorization_request_key
     AND NOT (NEW.state='queued' AND OLD.state IN ('planned','authorization_required','recovery_required')
              AND NEW.authorization_request_key IS NOT NULL) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NEW.state='queued' AND OLD.state IN ('authorization_required','recovery_required')
     AND NEW.authorization_request_key IS NOT DISTINCT FROM OLD.authorization_request_key THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NEW.state='queued' AND OLD.state IN ('planned','authorization_required','recovery_required') THEN
    SELECT r.tenant_id,r.request_key,r.kind,r.preview_id,r.job_id,r.publication_id,
           r.created_by,r.session_hash,r.expected_publication_revision
      INTO request_basis FROM __SS_LABEL_SCHEMA__.ss_intake_label_requests r
      WHERE r.tenant_id=OLD.tenant_id AND r.request_key=NEW.authorization_request_key;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    SELECT q.tenant_id,q.id,q.state,q.request_key,q.job_id,q.publication_id,
           q.created_by,q.session_hash,q.expected_publication_revision,q.expires_at
      INTO preview_basis FROM __SS_LABEL_SCHEMA__.ss_intake_label_previews q
      WHERE q.tenant_id=OLD.tenant_id AND q.id=request_basis.preview_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    IF request_basis.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR request_basis.job_id IS DISTINCT FROM OLD.job_id
       OR request_basis.publication_id IS DISTINCT FROM OLD.id
       OR request_basis.expected_publication_revision IS DISTINCT FROM OLD.revision
       OR preview_basis.expected_publication_revision IS DISTINCT FROM OLD.revision
       OR preview_basis.tenant_id IS DISTINCT FROM request_basis.tenant_id
       OR preview_basis.id IS DISTINCT FROM request_basis.preview_id
       OR preview_basis.request_key IS DISTINCT FROM request_basis.request_key
       OR preview_basis.job_id IS DISTINCT FROM request_basis.job_id
       OR preview_basis.publication_id IS DISTINCT FROM request_basis.publication_id
       OR preview_basis.created_by IS DISTINCT FROM request_basis.created_by
       OR preview_basis.session_hash IS DISTINCT FROM request_basis.session_hash
       OR preview_basis.state <> 'ready'
       OR preview_basis.expires_at <= pg_catalog.clock_timestamp()
       OR OLD.retention_until <= pg_catalog.clock_timestamp()
       OR (OLD.state='planned' AND request_basis.kind <> 'confirm_part')
       OR (OLD.state IN ('authorization_required','recovery_required') AND request_basis.kind <> 'resume_part')
       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  END IF;
  IF NEW.state='published' AND OLD.lease_until <= pg_catalog.clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF OLD.pair_sealed_at IS NOT NULL AND ROW(NEW.render_input_hash,NEW.render_basis_hash,NEW.output_metadata,NEW.pair_sealed_at,NEW.pdf_sha256,NEW.pdf_bytes,NEW.zpl_sha256,NEW.zpl_bytes) IS DISTINCT FROM ROW(OLD.render_input_hash,OLD.render_basis_hash,OLD.output_metadata,OLD.pair_sealed_at,OLD.pdf_sha256,OLD.pdf_bytes,OLD.zpl_sha256,OLD.zpl_bytes) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF OLD.pair_sealed_at IS NULL AND NEW.pair_sealed_at IS NOT NULL
     AND NOT (OLD.state='rendering' AND NEW.state='bytes_prepared') THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  
  IF NEW.lease_generation < OLD.lease_generation THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NEW.lease_generation <> OLD.lease_generation THEN
    IF NEW.lease_generation <> OLD.lease_generation + 1
       OR NEW.state NOT IN ('rendering','bytes_prepared','publishing')
       OR NEW.lease_token IS NULL OR NEW.lease_until IS NULL
       OR NEW.lease_token IS NOT DISTINCT FROM OLD.lease_token
       OR NEW.lease_until <= pg_catalog.clock_timestamp()
       OR (OLD.lease_until IS NOT NULL AND OLD.lease_until > pg_catalog.clock_timestamp())
       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  ELSIF NEW.state IN ('rendering','bytes_prepared','publishing') THEN
    IF OLD.lease_until IS NULL OR OLD.lease_until <= pg_catalog.clock_timestamp()
       OR NEW.lease_token IS DISTINCT FROM OLD.lease_token
       OR NEW.lease_until IS DISTINCT FROM OLD.lease_until THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  ELSIF NEW.lease_token IS NOT NULL OR NEW.lease_until IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  RETURN NEW;
END;
$ss_label_guard$;
REVOKE ALL ON FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_publications_guard_v9() FROM PUBLIC;
CREATE TRIGGER ss_intake_label_publications_guard_trg_v9 BEFORE INSERT OR UPDATE OR DELETE ON __SS_LABEL_SCHEMA__.ss_intake_label_publications
FOR EACH ROW EXECUTE FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_publications_guard_v9();

CREATE FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_previews_guard_v9() RETURNS pg_catalog.trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER PARALLEL UNSAFE
SET search_path=pg_catalog
AS $ss_label_guard$
DECLARE
  pub __SS_LABEL_SCHEMA__.ss_intake_label_publications%ROWTYPE;
  job __SS_LABEL_SCHEMA__.ss_intake_label_jobs%ROWTYPE;
  basis __SS_LABEL_SCHEMA__.ss_intake_label_inputs%ROWTYPE;
  current_publication_revision pg_catalog.int8;
  current_publication_retention pg_catalog.timestamptz;
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF TG_OP='INSERT' THEN
    IF NEW.state <> 'queued' OR NEW.revision <> 1 OR NEW.lease_generation <> 0
       OR NEW.lease_token IS NOT NULL OR NEW.lease_until IS NOT NULL
       OR NEW.ready_at IS NOT NULL OR NEW.render_basis_hash IS NOT NULL
       OR NEW.overflows_body IS NOT NULL OR NEW.clipping_required IS NOT NULL
       OR NEW.error_code IS NOT NULL OR NEW.expires_at <= pg_catalog.clock_timestamp()
       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    SELECT * INTO pub FROM __SS_LABEL_SCHEMA__.ss_intake_label_publications WHERE tenant_id=NEW.tenant_id AND id=NEW.publication_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    SELECT * INTO job FROM __SS_LABEL_SCHEMA__.ss_intake_label_jobs WHERE tenant_id=NEW.tenant_id AND id=NEW.job_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    SELECT * INTO basis FROM __SS_LABEL_SCHEMA__.ss_intake_label_inputs WHERE tenant_id=NEW.tenant_id AND id=job.input_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    IF pub.job_id IS DISTINCT FROM job.id OR NEW.input_hash IS DISTINCT FROM job.input_hash
       OR NEW.part_hash IS DISTINCT FROM pub.part_hash OR NEW.build_hash IS DISTINCT FROM job.renderer_build_hash
       OR NEW.expected_publication_revision IS DISTINCT FROM pub.revision
       OR NEW.expires_at > pub.retention_until
       OR basis.content_state <> 'active' OR basis.input_body IS NULL OR basis.retention_until <= pg_catalog.clock_timestamp()
       OR NEW.created_at < job.created_at OR NEW.expires_at > job.retention_until OR NEW.expires_at > basis.retention_until
       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    RETURN NEW;
  END IF;
  IF ROW(NEW.tenant_id,NEW.id,NEW.job_id,NEW.publication_id,NEW.created_by,NEW.session_hash,NEW.role,NEW.request_key,NEW.selector_body,NEW.selector_hash,NEW.input_hash,NEW.part_hash,NEW.build_hash,NEW.created_at,NEW.expected_publication_revision) IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.job_id,OLD.publication_id,OLD.created_by,OLD.session_hash,OLD.role,OLD.request_key,OLD.selector_body,OLD.selector_hash,OLD.input_hash,OLD.part_hash,OLD.build_hash,OLD.created_at,OLD.expected_publication_revision) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF OLD.state IN ('failed','expired') OR NEW.revision <> OLD.revision + 1 THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NOT ((OLD.state='queued' AND NEW.state IN ('checking','expired'))
       OR (OLD.state='checking' AND NEW.state IN ('checking','ready','failed','expired'))
       OR (OLD.state='ready' AND NEW.state='expired')) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NEW.expires_at IS DISTINCT FROM OLD.expires_at
     AND NOT (OLD.state='checking' AND NEW.state='ready' AND NEW.expires_at < OLD.expires_at)
     THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF OLD.ready_at IS NOT NULL AND ROW(NEW.ready_at,NEW.render_basis_hash,NEW.overflows_body,NEW.clipping_required,NEW.expires_at) IS DISTINCT FROM ROW(OLD.ready_at,OLD.render_basis_hash,OLD.overflows_body,OLD.clipping_required,OLD.expires_at) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF OLD.state='checking' AND NEW.state='ready' THEN
    SELECT p.revision,p.retention_until
      INTO current_publication_revision,current_publication_retention
      FROM __SS_LABEL_SCHEMA__.ss_intake_label_publications p
      WHERE p.tenant_id=NEW.tenant_id AND p.id=NEW.publication_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
    IF NEW.expected_publication_revision IS DISTINCT FROM current_publication_revision
       OR current_publication_retention <= pg_catalog.clock_timestamp()
       OR NEW.expires_at > current_publication_retention
       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  END IF;
  IF OLD.state='checking' AND NEW.state='ready' AND OLD.lease_until <= pg_catalog.clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NEW.ready_at IS NOT NULL AND OLD.ready_at IS NULL AND NEW.state <> 'ready' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  
  IF NEW.lease_generation < OLD.lease_generation THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF NEW.lease_generation <> OLD.lease_generation THEN
    IF NEW.lease_generation <> OLD.lease_generation + 1
       OR NEW.state NOT IN ('checking')
       OR NEW.lease_token IS NULL OR NEW.lease_until IS NULL
       OR NEW.lease_token IS NOT DISTINCT FROM OLD.lease_token
       OR NEW.lease_until <= pg_catalog.clock_timestamp()
       OR (OLD.lease_until IS NOT NULL AND OLD.lease_until > pg_catalog.clock_timestamp())
       THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  ELSIF NEW.state IN ('checking') THEN
    IF OLD.lease_until IS NULL OR OLD.lease_until <= pg_catalog.clock_timestamp()
       OR NEW.lease_token IS DISTINCT FROM OLD.lease_token
       OR NEW.lease_until IS DISTINCT FROM OLD.lease_until THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  ELSIF NEW.lease_token IS NOT NULL OR NEW.lease_until IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  RETURN NEW;
END;
$ss_label_guard$;
REVOKE ALL ON FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_previews_guard_v9() FROM PUBLIC;
CREATE TRIGGER ss_intake_label_previews_guard_trg_v9 BEFORE INSERT OR UPDATE OR DELETE ON __SS_LABEL_SCHEMA__.ss_intake_label_previews
FOR EACH ROW EXECUTE FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_previews_guard_v9();

CREATE FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_requests_guard_v9() RETURNS pg_catalog.trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER PARALLEL UNSAFE
SET search_path=pg_catalog
AS $ss_label_guard$
DECLARE
  quote __SS_LABEL_SCHEMA__.ss_intake_label_previews%ROWTYPE;
  pub __SS_LABEL_SCHEMA__.ss_intake_label_publications%ROWTYPE;
BEGIN
  IF TG_OP <> 'INSERT' OR NEW.preview_id IS NULL THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  SELECT * INTO quote FROM __SS_LABEL_SCHEMA__.ss_intake_label_previews WHERE tenant_id=NEW.tenant_id AND id=NEW.preview_id;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  SELECT * INTO pub FROM __SS_LABEL_SCHEMA__.ss_intake_label_publications WHERE tenant_id=NEW.tenant_id AND id=NEW.publication_id;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF quote.state <> 'ready' OR quote.expires_at <= pg_catalog.clock_timestamp()
     OR quote.request_key IS DISTINCT FROM NEW.request_key OR quote.job_id IS DISTINCT FROM NEW.job_id
     OR quote.publication_id IS DISTINCT FROM NEW.publication_id
     OR NEW.created_at < quote.ready_at OR NEW.created_at >= quote.expires_at
     OR quote.created_by IS DISTINCT FROM NEW.created_by OR quote.session_hash IS DISTINCT FROM NEW.session_hash
     OR NEW.expected_publication_revision IS DISTINCT FROM quote.expected_publication_revision
     OR NEW.expected_publication_revision IS DISTINCT FROM pub.revision
     THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  IF (NEW.kind='confirm_part' AND pub.state <> 'planned')
     OR (NEW.kind='resume_part' AND pub.state NOT IN ('authorization_required','recovery_required'))
     OR (NEW.kind='reprint_exact' AND (pub.state <> 'published' OR pub.tombstoned_at IS NOT NULL OR pub.retention_until <= pg_catalog.clock_timestamp()))
     THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='INTAKE_LABEL_SCHEMA_MUTATION_DENIED'; END IF;
  RETURN NEW;
END;
$ss_label_guard$;
REVOKE ALL ON FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_requests_guard_v9() FROM PUBLIC;
CREATE TRIGGER ss_intake_label_requests_guard_trg_v9 BEFORE INSERT OR UPDATE OR DELETE ON __SS_LABEL_SCHEMA__.ss_intake_label_requests
FOR EACH ROW EXECUTE FUNCTION __SS_LABEL_SCHEMA__.ss_intake_label_requests_guard_v9();

INSERT INTO __SS_LABEL_SCHEMA__.ss_schema_versions(version) VALUES (9);

