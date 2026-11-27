"""Source-only proposed build audit; standard library, no application imports.

Runs before any foreign wheel download/install in an independently admitted
Docker build. Reject the entire COPY context unless every path/byte matches.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys

INPUT = Path('/context')
CONTROL_PREFIX = 'tools/fixed_hosted_ci/'


def raw(path, limit=4 * 1024 * 1024):
    item = path.lstat()
    if not stat.S_ISREG(item.st_mode) or item.st_size > limit:
        raise ValueError('CONTEXT_REGULAR_BOUND')
    content = path.read_bytes()
    if len(content) != item.st_size:
        raise ValueError('CONTEXT_CHANGED')
    return content


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('CONTEXT_JSON_DUPLICATE')
        result[key] = value
    return result


def read_json(path):
    return json.loads(raw(path).decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('CONTEXT_NONFINITE')))


def safe(name):
    value = PurePosixPath(name)
    if (type(name) is not str or not name or len(name) > 1024 or value.is_absolute()
        or str(value) != name or '..' in value.parts or '\\' in name or ':' in name
        or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError('CONTEXT_PATH')
    return name


def save(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def audit():
    manifest_name = CONTROL_PREFIX + 'context-manifest.json'
    content = raw(INPUT / manifest_name)
    wanted = os.environ.get('SS_FIXED_HOSTED_CONTEXT_SHA256', '')
    if not re.fullmatch('[a-f0-9]{64}', wanted) or hashlib.sha256(content).hexdigest() != wanted:
        raise ValueError('EXPECTED_CONTEXT_MANIFEST')
    manifest = json.loads(content, object_pairs_hook=unique)
    expected = manifest['files']
    if len(expected) != 304 or len({row['path'] for row in expected}) != len(expected):
        raise ValueError('CONTEXT_SELECTION')
    names = set()
    for path in INPUT.rglob('*'):
        if path.is_symlink():
            raise ValueError('CONTEXT_SYMLINK')
        if path.is_file():
            names.add(path.relative_to(INPUT).as_posix())
    if names != {row['path'] for row in expected} | {manifest_name}:
        raise ValueError('CONTEXT_EXACT_NAMES')
    checked = []
    for row in expected:
        if set(row) != {'path', 'bytes', 'sha256'} or type(row['bytes']) is not int:
            raise ValueError('CONTEXT_MANIFEST_SCHEMA')
        payload = raw(INPUT / safe(row['path']))
        actual = {'path': row['path'], 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        if actual != row:
            raise ValueError('CONTEXT_FILE_PREIMAGE')
        checked.append(actual)
    admission = read_json(INPUT / (CONTROL_PREFIX + 'admission.json'))
    if admission['context_paths'] != [row['path'] for row in checked]:
        raise ValueError('CONTEXT_ADMISSION_NAMES')
    # Complete context validation precedes any output or foreign acquisition.
    project = admission['public_project_sources']
    if len(project) != 279 or len({row['path'] for row in project}) != 279:
        raise ValueError('PUBLIC279_SELECTION')
    for row in project:
        save(Path('/runtime-source') / row['path'], raw(INPUT / row['path']))
    # Six reviewed foundation/test paths extend the selected279 project paths.
    # Four selected source files carry the separately reviewed ledger16 correction;
    # all pins above bind exact proposed bytes, not an unchanged279 claim.
    foundations = admission['foundation_sources']
    foundation_names = (
        'hosted/intake_label_policy_store.py', 'hosted/label_postgres_store.py',
        'intake_label_policy_evidence.py', 'test_label_policy_store_postgres.py',
        'test_label_postgres_store_postgres.py', 'test_hosted_intake_label_schema.py',
    )
    if (type(foundations) is not list or len(foundations) != 6
        or [row['path'] for row in foundations] != list(foundation_names)
        or set(foundation_names) & {row['path'] for row in project}):
        raise ValueError('EXACT_SIX_FOUNDATION_ADDITIONS')
    by_name = {row['path']: row for row in checked}
    for row in foundations:
        if row != by_name.get(row['path']):
            raise ValueError('FOUNDATION_CONTEXT_PREIMAGE')
        save(Path('/runtime-source') / safe(row['path']), raw(INPUT / row['path']))
    overlay = raw(INPUT / (CONTROL_PREFIX + 'runtime-overlay/hosted_ci_evidence.py'))
    target = Path('/runtime-source/tools/hosted_ci_evidence.py')
    original = target.read_bytes()
    changes = (
        (b'process = subprocess.run([sys.executable, \'tools/test_hosted_postgres.py\'],', b'process = subprocess.run([sys.executable, \'-I\', \'-S\', \'-B\', \'/opt/fixed-hosted-ci/fixed_hosted_ci_entry.py\', \'--postgres-child\'],'),
        (b'version != 180006', b'version != 180007'),
        (b'POSTGRES_EXPECTED_COUNT = 549', b'POSTGRES_EXPECTED_COUNT = 604'),
        (b'Eight\ncompanion unit modules run all 99 approved cases', b'Nine\ncompanion unit modules run all 126 approved cases (the original99 plus27 label-catalog guards)'),
        (b'UNIT_GROUPS = (', b'LABEL_SCHEMA_UNIT_MODULE = \'test_hosted_intake_label_schema\'\nLABEL_SCHEMA_UNIT_IDS = (\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaInstallBoundaryTests.test_absent_install_uses_exact_bundled_bytes_token_binding_and_post_attestation\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaInstallBoundaryTests.test_autocommit_denial_precedes_all_queries_files_or_ddl\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaInstallBoundaryTests.test_changed_bundle_fails_before_savepoint_or_ddl\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaInstallBoundaryTests.test_ddl_or_post_attestation_failure_rolls_back_only_attempt_and_never_returns_present\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaInstallBoundaryTests.test_exact_existing_family_is_unchanged_and_never_reads_or_executes_ddl\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaInstallBoundaryTests.test_partial_or_orphan_marker_denial_never_reads_sql_or_repairs\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_absent_family_is_read_only_and_never_reads_bundled_sql\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_base_schema_marker_and_required_key_facts_are_required\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_deferrable_unvalidated_parent_or_wrong_noinherit_constraints_deny\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_driver_failure_has_fixed_owned_error_without_cause_or_context\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_exact_column_order_type_null_default_and_collation_are_attested\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_exact_separate_pg15_16_17_catalog_layouts_attest_without_native_n_rows\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_grouped_tenant_fk_actions_reference_and_index_binding_deny_drift\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_guard_source_security_path_acl_and_trigger_bindings_are_checked\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_index_full_key_opclass_predicate_and_validity_are_checked\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_isolated_schema_qualified_guards_and_ref_oids_must_match\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_missing_changed_date_check_and_literal_case_are_not_accepted\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_missing_disabled_or_misbound_native_ri_trigger_denies_enforcement_claim\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_native_notnull_or_fabricated_enforcement_cannot_be_legacy_fallback\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_partial_and_marker_only_or_detached_families_never_become_absence\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_pg18_check_fk_enforcement_and_temporal_flags_are_explicit\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_pg18_native_notnull_names_columns_and_flags_deny_even_when_attnotnull_true\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_pg18_requires_exact_131_constraints_with_74_explicit_native_names\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_schema_spec_and_raw_orders_are_frozen_and_lifecycle_never_enabled\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_unknown_alias_or_colliding_family_namespace_is_denied\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_unsupported_schema_version_and_noncanonical_format_context_fail_closed\',\n    \'test_hosted_intake_label_schema.IntakeLabelSchemaMetadataTests.test_wrong_stored_function_namespace_or_nonbuiltin_check_dependency_denies\',\n)\nUNIT_GROUPS = ('),
        (b'     \'Seven exact-SKU read-adapter units with fictional provider responses; not PostgreSQL, browser, live Square or hardware acceptance.\'),\n)', b'     \'Seven exact-SKU read-adapter units with fictional provider responses; not PostgreSQL, browser, live Square or hardware acceptance.\'),\n    (\'unit_label_schema\', LABEL_SCHEMA_UNIT_MODULE, LABEL_SCHEMA_UNIT_IDS, \'unit-label-schema\',\n     \'Twenty-seven exact label-schema catalog and installer guards using fictional catalog rows; no SQL, consent or feature acceptance.\'),\n)'),
    )
    proposed = original
    for before, after in changes:
        if proposed.count(before) != 1:
            raise ValueError('EXACT_SIX_EVIDENCE_BEFORE')
        proposed = proposed.replace(before, after)
    restored = overlay
    for before, after in reversed(changes):
        if restored.count(after) != 1:
            raise ValueError('EXACT_SIX_EVIDENCE_AFTER')
        restored = restored.replace(after, before)
    if proposed != overlay or restored != original:
        raise ValueError('EXACT_SIX_EVIDENCE_FORWARD_INVERSE_OVERLAYS')
    temporary = target.with_suffix('.verified-overlay')
    save(temporary, overlay)
    os.replace(temporary, target)
    for row in expected:
        if row['path'].startswith(CONTROL_PREFIX):
            relative = row['path'][len(CONTROL_PREFIX):]
            save(Path('/runtime-controls') / relative, raw(INPUT / row['path']))
    save(Path('/runtime-controls/context-manifest.json'), content)
    report = {'format': 'stock-and-sleeve:fixed-hosted-context-audit:v1',
              'source_sha': os.environ.get('SS_CI_SOURCE_SHA'),
              'expected_manifest_sha256': wanted, 'files': checked, 'successful': True,
              'foreign_install_started': False, 'application_or_foreign_import': False,
              'public279_count': 279, 'runtime_evidence_overlay_files': 1,
              'runtime_evidence_literal_changes': 6, 'foundation_addition_count': 6}
    save(Path('/runtime-controls/context-audit.json'),
         (json.dumps(report, sort_keys=True, separators=(',', ':')) + '\n').encode())


if __name__ == '__main__':
    if sys.argv[1:] == ['--host-admission']:
        own_root = Path(__file__).resolve().parent
        policy = read_json(own_root / 'admission.json')
        if policy.get('execution_admission') != 'REVIEWED_FOR_DEDICATED_CI':
            raise SystemExit('SOURCE_ONLY_CANDIDATE_NO_EXECUTION_ADMISSION')
        print(hashlib.sha256(raw(own_root / 'context-manifest.json')).hexdigest())
    elif sys.argv[1:] == ['--audit']:
        audit()
    else:
        raise SystemExit('CONTEXT_FIXED_ARGUMENTS')
