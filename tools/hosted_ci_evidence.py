"""Linux hosted API unit and SQL evidence, never Windows/provider acceptance.

Invoked only inside the owned CI image/network by run_hosted_ci.sh. The unchanged
PostgreSQL runner selects its full ALLOWED suite in a fresh child process. Seven
companion unit modules run all 92 approved cases, including no-database Flask
startup and fictional capture orchestration; these are not SQL, browser, printer
or provider acceptance.
This helper adds copied-source preservation and an actual test-client TLS probe.
"""
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import unittest


ROOT = Path('/workspace')
EVIDENCE = Path('/ci-evidence')
REPORT_LIMIT = 64 * 1024 * 1024
POSTGRES_EXPECTED_COUNT = 532
IDENTITY_UNIT_MODULE = 'test_hosted_store_identity'
IDENTITY_UNIT_IDS = (
    IDENTITY_UNIT_MODULE + '.StoreIdentityContract.test_authenticated_me_projects_resolved_actor_not_query_or_cookie_hints',
    IDENTITY_UNIT_MODULE + '.StoreIdentityContract.test_full_session_helper_exposes_same_canonical_uuid_for_both_auth_paths',
    IDENTITY_UNIT_MODULE + '.StoreIdentityContract.test_http_full_session_and_challenge_envelopes_remain_distinct',
    IDENTITY_UNIT_MODULE + '.StoreIdentityContract.test_password_only_mfa_challenge_has_no_tenant_or_session_authority',
    IDENTITY_UNIT_MODULE + '.StoreIdentityContract.test_unauthenticated_me_exposes_no_store_id',
)
PROVIDER_UNIT_MODULE = 'test_hosted_provider_ids'
PROVIDER_UNIT_IDS = (
    PROVIDER_UNIT_MODULE + '.ProviderIDTests.test_application_request_key_validator_remains_strict',
    PROVIDER_UNIT_MODULE + '.ProviderIDTests.test_controls_empty_dot_segments_and_overlong_values_are_rejected',
    PROVIDER_UNIT_MODULE + '.ProviderIDTests.test_symbols_length_case_and_percent_text_are_preserved',
    PROVIDER_UNIT_MODULE + '.SquareOpaqueIDTests.test_catalog_latest_or_invalid_versions_are_rejected_before_any_provider_call',
    PROVIDER_UNIT_MODULE + '.SquareOpaqueIDTests.test_catalog_read_pins_exact_version_and_quotes_opaque_id',
    PROVIDER_UNIT_MODULE + '.SquareOpaqueIDTests.test_invalid_resource_id_never_obtains_a_token_or_calls_provider',
    PROVIDER_UNIT_MODULE + '.SquareOpaqueIDTests.test_invalid_signed_event_ids_are_rejected_before_database_access',
    PROVIDER_UNIT_MODULE + '.SquareOpaqueIDTests.test_retrieve_quotes_one_opaque_segment_without_decoding_or_changing_host',
    PROVIDER_UNIT_MODULE + '.SquareOpaqueIDTests.test_signed_webhook_preserves_opaque_event_merchant_and_resource_ids',
    PROVIDER_UNIT_MODULE + '.SquareOpaqueIDTests.test_token_response_preserves_only_valid_opaque_merchant_id',
)
STATUS_UNIT_MODULE = 'test_hosted_square_status'
STATUS_UNIT_IDS = (
    STATUS_UNIT_MODULE + '.SquareStatusTests.test_absent_connection_is_explicit_and_does_not_expose_credential_values',
    STATUS_UNIT_MODULE + '.SquareStatusTests.test_only_matching_nonrevoked_environment_reports_connected',
    STATUS_UNIT_MODULE + '.SquareStatusTests.test_other_environment_never_reports_a_ready_connection_or_merchant',
    STATUS_UNIT_MODULE + '.SquareStatusTests.test_revoked_connection_is_not_connected',
    STATUS_UNIT_MODULE + '.SquareStatusTests.test_saved_connection_never_reports_requested_scopes_as_granted',
)
REVOCATION_UNIT_MODULE = 'test_square_revocation_guards'
REVOCATION_UNIT_IDS = (
    'test_square_revocation_guards.ReconciliationHoldGuardTests.test_heartbeat_takes_job_lock_before_connection_and_preserves_hold',
    'test_square_revocation_guards.ReconciliationHoldGuardTests.test_hold_does_not_permanently_exception_or_exhaust_job',
    'test_square_revocation_guards.ReconciliationHoldGuardTests.test_reconnect_before_finish_does_not_misclassify_old_authority_failure',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_begin_pins_committed_cursor_and_current_generation_before_returning_url',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_conflicting_event_id_denies_even_unknown_or_already_blocked',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_first_notice_preserves_tokens_jobs_and_credit',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_initial_absent_generation_is_legitimate_only_with_captured_cursor',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_insert_conflict_reads_and_compares_the_winning_receipt',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_introspection_cannot_return_authority_after_generation_only_change',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_invalid_signature_does_not_open_a_transaction',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_legacy_missing_cursor_rejects_before_token_exchange',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_missing_or_blank_application_identity_fails_controlled_before_database',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_notice_after_flow_began_rejects_late_callback_without_storing_tokens',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_other_completed_flow_invalidates_generation_without_notice',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_other_environment_connection_is_not_held_by_this_notice',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_pending_blocks_token_use_without_refresh_or_provider_calls',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_pending_status_does_not_claim_current_provider_revocation',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_receiving_final_guard_checks_internal_generation_and_pending',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_resource_events_arriving_during_hold_are_queued_unchanged',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_same_signed_event_replay_has_no_repeated_effect',
    'test_square_revocation_guards.SquareRevocationGuardTests.test_unknown_merchant_notice_is_durable_without_inventing_a_connection',
)
CLEANUP_UNIT_MODULE = 'test_hosted_cleanup_square'
CLEANUP_UNIT_IDS = (
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_changed_catalog_version_or_same_version_payload_blocks_current_coverage',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_complete_read_brackets_canonical_context_and_keeps_all_trust_limits',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_count_matrix_bound_does_not_allocate_an_unbounded_missing_list',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_duplicate_or_malformed_later_row_cannot_erase_a_valid_positive_prefix',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_empty_catalog_is_bracketed_without_an_unfiltered_inventory_request',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_expired_attempt_returns_incomplete_without_contacting_provider',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_later_count_page_failure_retains_positive_rows_and_unknown_pairs',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_location_status_currency_or_membership_change_blocks_current_coverage',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_missing_count_is_explicit_partial_coverage_after_successful_bookends',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_missing_timestamps_and_composed_estimates_are_retained_without_complete_coverage',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_one_shared_budget_covers_both_context_bookends_and_counts',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_unknown_expected_matrix_is_not_reported_as_no_missing_counts',
    'test_hosted_cleanup_square.CleanupBracketedCollectionTests.test_wrong_estimate_flag_types_are_not_validated_count_evidence',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_access_requests_only_inventory_catalog_and_merchant_read_grants',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_all_count_pages_are_consumed_and_missing_pairs_remain_unknown',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_catalog_bad_types_versions_deleted_and_foreign_merchant_are_rejected',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_catalog_consumes_every_page_and_preserves_nested_variations',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_catalog_duplicate_top_level_and_conflicting_mirrors_are_rejected',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_catalog_record_budget_includes_returned_objects_and_nested_rows',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_catalog_requires_parent_closure_and_consistent_child_binding',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_collection_has_explicit_nonatomic_nondeletion_results_and_no_secret',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_collection_retains_unknown_count_coverage_even_with_complete_pagination',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_collection_shares_resource_budget_across_phases',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_count_batches_cover_every_requested_pair_without_dropping_empty_pages',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_count_pair_budget_is_checked_before_large_missing_output_or_requests',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_duplicate_count_tuple_is_rejected_even_when_identical',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_empty_catalog_does_not_request_unfiltered_inventory',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_excessively_nested_catalog_metadata_fails_with_sanitized_read_error',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_exhausting_page_or_byte_budget_never_returns_a_partial_catalog',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_expired_collection_budget_does_not_start_a_provider_read',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_falsey_nontext_and_repeated_cursors_never_end_a_paged_read',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_inactive_and_other_currency_locations_are_not_filtered_out',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_invalid_or_duplicate_requested_ids_fail_before_provider_contact',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_invalid_or_foreign_locations_fail_without_partial_coverage',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_invalid_resource_settings_are_rejected_before_any_read',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_malformed_quantities_and_unusable_timestamps_are_rejected',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_malformed_response_fields_and_nonfinite_values_are_incomplete',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_missing_null_or_empty_text_cursor_end_only_after_all_rows_checked',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_missing_optional_count_timestamp_is_preserved_without_invented_freshness',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_negative_and_positive_stock_are_retained_as_evidence_not_zeroed',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_opaque_count_identifiers_are_preserved_without_becoming_url_paths',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_provider_errors_and_redirects_are_sanitized_incomplete_reads_without_retry',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_transport_is_fixed_read_shapes_no_redirects_and_closes_every_response',
    'test_hosted_cleanup_square.CleanupReadCollectionTests.test_unrequested_count_ids_locations_states_and_types_are_rejected',
)
RECOVERY_STARTUP_UNIT_MODULE = 'test_hosted_staff_password_recovery'
RECOVERY_STARTUP_UNIT_IDS = (
    RECOVERY_STARTUP_UNIT_MODULE + '.UnconfiguredStaffRecoveryStartup.test_unconfigured_app_keeps_public_pages_health_and_explicit_not_ready_apis',
)
CAPTURE_PRIVACY_UNIT_MODULE = 'test_hosted_browser_capture_privacy'
CAPTURE_PRIVACY_UNIT_IDS = (
    CAPTURE_PRIVACY_UNIT_MODULE + '.HostedBrowserCapturePrivacyTests.test_capture_error_propagates_without_retry_or_publishing_image',
    CAPTURE_PRIVACY_UNIT_MODULE + '.HostedBrowserCapturePrivacyTests.test_exact_sensitive_subtrees_hidden_only_during_existing_masked_capture',
    CAPTURE_PRIVACY_UNIT_MODULE + '.HostedBrowserCapturePrivacyTests.test_failed_hidden_check_restores_before_refusing_capture',
    CAPTURE_PRIVACY_UNIT_MODULE + '.HostedBrowserCapturePrivacyTests.test_prepare_bounds_or_context_failure_is_fixed_and_never_captures',
    CAPTURE_PRIVACY_UNIT_MODULE + '.HostedBrowserCapturePrivacyTests.test_restore_document_or_dispose_failure_discards_captured_image',
    CAPTURE_PRIVACY_UNIT_MODULE + '.HostedBrowserCapturePrivacyTests.test_unknown_or_css_injecting_extra_selector_refused_before_capture',
)
UNIT_GROUPS = (
    ('unit_identity', IDENTITY_UNIT_MODULE, IDENTITY_UNIT_IDS, 'unit-identity',
     'Five hosted server API projection unit cases with fictional database responses; not SQL acceptance.'),
    ('unit_provider_ids', PROVIDER_UNIT_MODULE, PROVIDER_UNIT_IDS, 'unit-provider-ids',
     'Ten hosted provider-ID, URL, signed-event and token-response unit cases with fictional database/transport; not live provider or SQL acceptance.'),
    ('unit_square_status', STATUS_UNIT_MODULE, STATUS_UNIT_IDS, 'unit-square-status',
     'Five hosted Square status DTO unit cases with fictional database responses; not SQL or live authorization acceptance.'),
    ('unit_revocation_guards', REVOCATION_UNIT_MODULE, REVOCATION_UNIT_IDS, 'unit-revocation-guards',
     'Twenty-one hosted revocation and reconciliation method-guard units with recording SQL and fictional signatures; not real SQL or provider acceptance.'),
    ('unit_cleanup_square', CLEANUP_UNIT_MODULE, CLEANUP_UNIT_IDS, 'unit-cleanup-square',
     'Forty-four hosted read-facade, pagination, bracket and partial-evidence units with fictional streamed responses; not live Square, SQL, continuity or deletion acceptance.'),
    ('unit_staff_recovery_startup', RECOVERY_STARTUP_UNIT_MODULE, RECOVERY_STARTUP_UNIT_IDS, 'unit-staff-recovery-startup',
     'One real Flask public-page, health, readiness and API startup unit with no configured database; not PostgreSQL or account-recovery acceptance.'),
    ('unit_capture_privacy', CAPTURE_PRIVACY_UNIT_MODULE, CAPTURE_PRIVACY_UNIT_IDS, 'unit-capture-privacy',
     'Six CaseContext capture-orchestration units using fictional page/handle objects and the real runner module; not browser, DOM, CSP, pixel or hardware acceptance.'),
)


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            value.update(chunk)
    return value.hexdigest()


def save(name, value):
    (EVIDENCE / name).write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def walk_error(error):
    raise error


def snapshot():
    result = {}
    total = 0
    for directory, names, files in os.walk(ROOT, followlinks=False, onerror=walk_error):
        parent = Path(directory)
        relative = parent.relative_to(ROOT)
        kept = []
        for name in sorted(names):
            if name in ('.git', '__pycache__') or (relative == Path('.') and name == 'Testing'):
                continue
            path = parent / name
            if path.is_symlink():
                raise ValueError('source_symlink')
            kept.append(name)
        names[:] = kept
        for name in sorted(files):
            path = parent / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError('source_nonregular_file')
            total += path.stat().st_size
            if total > REPORT_LIMIT or len(result) >= 10000:
                raise ValueError('source_manifest_bound')
            result[path.relative_to(ROOT).as_posix()] = digest(path)
    if not result:
        raise ValueError('source_manifest_empty')
    return result


def scrub(text):
    url = os.environ.get('DATABASE_URL', '')
    if url:
        text = text.replace(url, '[redacted fixture database]')
        password = url.split(':', 2)[2].split('@', 1)[0]
        text = text.replace(password, '[redacted fixture password]')
    return text


def read_text(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > REPORT_LIMIT:
        raise ValueError('report_file_invalid')
    return path.read_text(encoding='utf-8')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('report_duplicate_key')
        result[key] = value
    return result


def read_json(path):
    return json.loads(read_text(path), object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('report_nonfinite')))


def run_hosted_unit(module, approved_ids, stem, scope):
    """Run the full approved module; ordinary unit failures never skip the PG child."""
    if not any((module, approved_ids, stem, scope) == group[1:] for group in UNIT_GROUPS):
        raise ValueError('unapproved_hosted_unit_group')
    expected_count = len(approved_ids)
    if expected_count < 1 or len(set(approved_ids)) != expected_count:
        raise ValueError('invalid_hosted_unit_ids')
    started = now()
    buffer = io.StringIO()
    loader = result = None
    expected = []
    runner_error = None
    with redirect_stdout(buffer), redirect_stderr(buffer):
        try:
            # Fixed reviewed code root only; do not accept a caller import path.
            sys.path.insert(0, str(ROOT))
            from tools.test_hosted_postgres import EvidenceResult, test_ids
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromName(module)
            expected = list(test_ids(suite))
            result = unittest.TextTestRunner(stream=buffer, verbosity=2,
                                            resultclass=EvidenceResult).run(suite)
        except (Exception, SystemExit) as error:
            runner_error = type(error).__name__
            print('Hosted unit runner failed with type=' + runner_error)
    log = scrub(buffer.getvalue())
    payload = log.encode('utf-8')
    if len(payload) > REPORT_LIMIT:
        raise ValueError('hosted_unit_log_bound')
    (EVIDENCE / (stem + '-tests.log')).write_bytes(payload)
    rows = result.results if result is not None else []
    started_ids = result.started_ids if result is not None else []
    passed_ids = [row['id'] for row in rows if row['status'] == 'passed']
    loader_errors = [scrub(error) for error in loader.errors] if loader is not None else []
    counts = {'skipped_count': len(result.skipped) if result is not None else 0,
              'failed_count': len(result.failures) if result is not None else 0,
              'error_count': len(result.errors) if result is not None else 0,
              'expected_failure_count': len(result.expectedFailures) if result is not None else 0,
              'unexpected_success_count': len(result.unexpectedSuccesses) if result is not None else 0}
    coverage = bool(expected == list(approved_ids) and len(set(expected)) == expected_count and
                    result is not None and result.testsRun == expected_count and
                    Counter(started_ids) == Counter(approved_ids) and
                    Counter(passed_ids) == Counter(approved_ids) and
                    len(rows) == expected_count and all(row['status'] == 'passed' for row in rows))
    was_successful = bool(result is not None and result.wasSuccessful())
    passed = bool(coverage and was_successful and not any(counts.values()) and
                  not loader_errors and runner_error is None)
    summary = {'scope': scope,
               'module': module, 'source_sha': os.environ.get('SS_CI_SOURCE_SHA'),
               'started_utc': started, 'finished_utc': now(),
               'approved_test_ids': list(approved_ids), 'expected_test_ids': expected,
               'expected_count': expected_count, 'discovered_count': len(expected),
               'tests_run': result.testsRun if result is not None else 0,
               'started_test_ids': started_ids, 'successful_test_ids': passed_ids,
               'case_results': rows, 'was_successful': was_successful,
               'expected_case_coverage_complete': coverage,
               'loader_error_count': len(loader_errors), 'loader_errors': loader_errors,
               'runner_error_type': runner_error, **counts,
               'log_sha256': hashlib.sha256(payload).hexdigest(),
               'log_hash_convention': 'SHA-256 of delivered sanitized UTF-8 log bytes, LF line endings, no BOM',
               'passed': passed}
    save(stem + '-summary.json', summary)
    print(log, end='' if log.endswith('\n') else '\n')
    print(scrub(json.dumps(summary, indent=2, allow_nan=False)))
    return summary


def database_probe():
    # This dependency import occurs only in the intended Linux CI image.
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL'], sslmode='require', connect_timeout=10) as db:
        row = db.execute('SELECT ssl,version,cipher,bits FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()
        version = db.info.server_version
        schemas = [item[0] for item in db.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' "
            "AND nspname <> 'information_schema' ORDER BY nspname").fetchall()]
        public_tables = [item[0] for item in db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()]
    value = {'ssl': bool(row and row[0] is True), 'tls_version': row[1] if row else None,
             'cipher': row[2] if row else None, 'bits': row[3] if row else None,
             'server_version_num': version, 'schemas': schemas, 'public_tables': public_tables}
    if not value['ssl'] or version != 180006 or schemas != ['public'] or public_tables:
        raise ValueError('fixture_tls_version_or_schema')
    return value


def deliver_runner():
    summary = read_json(ROOT / 'Testing/oauth-receiving/postgres-latest.json')
    run = Path(summary['run_directory']).resolve()
    runs = (ROOT / 'Testing/oauth-receiving/postgres-runs').resolve()
    if run.parent != runs or not re.fullmatch(r'[0-9]{8}T[0-9]{12}Z', run.name):
        raise ValueError('runner_report_path')
    log = run / 'postgres-tests.log'
    raw_log = read_text(log)
    if digest(log) != summary.get('log_sha256'):
        raise ValueError('runner_log_hash')
    delivered = scrub(raw_log)
    if delivered != raw_log:
        raise ValueError('runner_log_unscrubbed')
    save('postgres-summary.json', summary)
    (EVIDENCE / 'postgres-tests.log').write_text(delivered, encoding='utf-8')
    print(delivered, end='' if delivered.endswith('\n') else '\n')
    print(scrub(json.dumps(summary, indent=2, allow_nan=False)))
    return summary


def validate_runner(summary):
    modules = ['hosted.integration'] + sorted(path.stem for path in ROOT.glob('test_*_postgres.py'))
    ids = summary.get('expected_test_ids')
    if (type(ids) is not list or len(ids) != POSTGRES_EXPECTED_COUNT or
            any(type(value) is not str for value in ids) or
            len(ids) != len(set(ids)) or summary.get('modules') != modules or
            summary.get('complete_module_selection') is not True or summary.get('run_kind') != 'complete' or
            summary.get('passed') is not True or summary.get('exit_code') != 0 or
            summary.get('database_configured') is not True or summary.get('tests_run') != len(ids) or
            summary.get('expected_count') != len(ids) or
            Counter(summary.get('started_test_ids', [])) != Counter(ids) or
            Counter(summary.get('successful_test_ids', [])) != Counter(ids) or
            summary.get('expected_case_coverage_complete') is not True or
            any(summary.get(field) != 0 for field in ('skipped_count', 'failed_count', 'error_count')) or
            summary.get('runner_error_type') is not None or summary.get('source_snapshot_stable') is not True or
            summary.get('changed_during_run') != [] or
            summary.get('source_sha256_at_start') != summary.get('source_sha256')):
        raise ValueError('full_postgres_acceptance_incomplete')
    results = summary.get('case_results')
    if (type(results) is not list or any(type(row) is not dict or row.get('status') != 'passed' for row in results) or
            Counter(row.get('id') for row in results) != Counter(ids)):
        raise ValueError('postgres_case_results_incomplete')
    return {'module_count': len(modules), 'expected_count': len(ids), 'tests_run': summary['tests_run']}


def main():
    EVIDENCE.mkdir(exist_ok=True)
    result = {'scope': 'Linux hosted API unit plus PostgreSQL fixture acceptance only; no Windows/native/signing/provider acceptance.',
              'started_utc': now(), 'passed': False, 'runner_exit_code': None, 'error_types': [], 'error_codes': [],
              'source_sha': os.environ.get('SS_CI_SOURCE_SHA'), 'python': sys.version,
              **{key: {'passed': False, 'state': 'not_started'} for key, *_ in UNIT_GROUPS}}
    before = None
    try:
        if sys.platform != 'linux' or sys.version_info[:3] != (3, 12, 16):
            raise ValueError('linux_python_version')
        if not re.fullmatch(r'[0-9a-f]{40}', os.environ.get('SS_CI_SOURCE_SHA', '')):
            raise ValueError('public_source_sha_required')
        if not re.fullmatch(r'postgresql://postgres:[0-9a-f]{48}@db:5432/ss_ci_[0-9a-f]{16}\?sslmode=require',
                            os.environ.get('DATABASE_URL', '')):
            raise ValueError('owned_fixture_dsn_required')
        before = snapshot()
        save('copied-source-before.json', before)
        dependencies = {}
        for name in ('pip-bootstrap-report.json', 'pip-install-report.json', 'pip-freeze.txt'):
            source = Path('/opt/hosted-ci') / name
            read_text(source)
            shutil.copyfile(source, EVIDENCE / name)
            dependencies[name] = digest(source)
        result['dependency_report_sha256'] = dependencies
        result['tls_before'] = database_probe()
        save('tls-client-probe.json', result['tls_before'])
        for key, module, approved_ids, stem, scope in UNIT_GROUPS:
            try:
                result[key] = run_hosted_unit(module, approved_ids, stem, scope)
            except (Exception, SystemExit) as error:
                # Even loader/report failures retain the other groups and full PG run.
                result[key] = {'passed': False, 'runner_error_type': type(error).__name__}
                result['error_types'].append(type(error).__name__)
                result['error_codes'].append(key + '_evidence_failed')
            if result[key].get('passed') is not True:
                result['error_codes'].append(key + '_failed')
        with (EVIDENCE / 'runner.stdout.log').open('w', encoding='utf-8') as stdout, \
                (EVIDENCE / 'runner.stderr.log').open('w', encoding='utf-8') as stderr:
            process = subprocess.run([sys.executable, 'tools/test_hosted_postgres.py'],
                                     cwd=ROOT, stdout=stdout, stderr=stderr, check=False)
        result['runner_exit_code'] = process.returncode
        for name in ('runner.stdout.log', 'runner.stderr.log'):
            path = EVIDENCE / name
            path.write_text(scrub(read_text(path)), encoding='utf-8')
        runner_summary = deliver_runner()
        result['tls_after'] = database_probe()
        if process.returncode != 0:
            raise ValueError('postgres_runner_nonzero')
        result['postgres'] = validate_runner(runner_summary)
    except Exception as error:
        # Type only; never a DSN, credential, SQL parameter or raw provider body.
        result['error_types'].append(type(error).__name__)
        if type(error) is ValueError and error.args and re.fullmatch(r'[a-z][a-z0-9_]{0,79}', str(error.args[0])):
            result['error_codes'].append(error.args[0])
    finally:
        try:
            after = snapshot()
            save('copied-source-after.json', after)
            result['copied_source_stable'] = before is not None and before == after
            result['copied_source_file_count'] = len(after)
        except Exception as error:
            result['copied_source_stable'] = False
            result['error_types'].append(type(error).__name__)
        result['passed'] = bool(not result['error_types'] and result.get('copied_source_stable') and
                                all(result.get(key, {}).get('passed') is True for key, *_ in UNIT_GROUPS) and
                                result.get('tls_before', {}).get('ssl') is True and
                                result.get('tls_after', {}).get('ssl') is True and
                                result.get('postgres') and result['runner_exit_code'] == 0)
        result['finished_utc'] = now()
        save('hosted-ci-summary.json', result)
        print(scrub(json.dumps(result, indent=2, allow_nan=False)))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
