"""Exact current-service ordinary-label CI evidence; fictional fixtures only.

Source-only candidate. The host wrapper owns admission, immutable image identity,
network isolation, resource limits, process teardown and framed evidence delivery.
All source/native/RECORD checks are stdlib/data-only before foreign imports.
Only then is the real selected Psycopg binary implementation imported and asserted.
"""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct
import sys
import sysconfig
import time
import unittest

ROOT = Path('/workspace')
CONFIG = Path('/opt/ordinary-label-ci')
EVIDENCE = Path('/ci-evidence')
PROJECT = (
    "catalog_cleanup.py",
    "catalog_physical_ownership.py",
    "credit_tracker.py",
    "hosted_labels.py",
    "hosted/__init__.py",
    "hosted/credit_authority.py",
    "hosted/credit_lots.py",
    "hosted/database.py",
    "hosted/intake.py",
    "hosted/ledger.py",
    "hosted/receiving_plan.py",
    "hosted/security.py",
    "hosted/settlement.py",
    "hosted/trades.py",
    "intake_label_contract.py",
    "intake_manifest.py",
    "intake_physical_identity.py",
    "pricing_review.py",
    "receiving_evidence.py",
    "roca_edition.json",
    "roca_to_square.py",
    "scanner_adapters.py",
    "square_variations.py",
    "station_cloud_journal_cas.py",
    "station_cloud.py",
    "station_edition.py",
    "station_financial_record.py",
    "station_hosted_receive.py",
    "station_http_json.py",
    "station_intake_claim.py",
    "station_intake_receiving_projection.py",
    "station_intake_receiving_registry.py",
    "station_intake_receiving_review.py",
    "station_intake_stock_action_codec.py",
    "station_intake_stock_action_registry.py",
    "station_intake_stock_action_transport.py",
    "station_intake_stock_actions.py",
    "station_label_legacy_worker.py",
    "station_label_publication.py",
    "station_paths.py",
    "station_pricing.py",
    "station_profile.py",
    "station_receiving_action_registry.py",
    "station_receiving_action_transport.py",
    "station_receiving_actions.py",
    "station_receiving_budget.py",
    "station_receiving_journal_cas.py",
    "station_receiving_label_plan.py",
    "station_receiving_label_publication.py",
    "station_receiving_label_render.py",
    "station_receiving_label_worker.py",
    "station_receiving_labels.py",
    "station_receiving_reference_fences.py",
    "station_receiving_reference_retirement.py",
    "station_receiving_registry.py",
    "station_receiving_restoration_proof.py",
    "station_receiving_restore.py",
    "station_recovery_review.py",
    "station_server.py",
    "station_service.py",
    "station_setup.py",
    "station_shared_trade.py",
    "station_store.py",
    "station_trade_recovery.py",
    "store_pricing.py",
    "test_hosted_receive.py",
    "test_shared_trade.py",
    "test_station_cloud.py",
    "test_station_label_publication.py",
    "test_receiving_label_plan.py",
    "test_receiving_label_render.py",
    "test_receiving_label_worker.py",
)
SOURCE = PROJECT + ('tools/ordinary_label_ci_evidence.py', 'tools/ordinary_label_ci_admission.json',
                    'tools/label_ci_provenance.py', 'tools/test_label_ci_provenance.py')
SERVICE_IDS = (
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_csv_receipt_actual_pair_exact_skus_and_restart_same_job",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_manual_raw_and_slab_actual_range_reprint_preserves_full_marks_and_quantities",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_legacy_actual_pair_preserves_existing_numeric_barcode_contract",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_partial_link_failure_is_prepared_not_downloadable_and_restart_completes_pair",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_lost_commit_ack_recovers_published_pair_and_never_cleans_ambiguous_files",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_normal_final_pair_read_revalidates_result_authority_and_keeps_committed_pair",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_lost_ack_final_pair_read_revalidates_result_authority_and_keeps_committed_pair",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_active_job_is_busy_and_expired_lease_preserves_unknown_old_attempt",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_exact_receipt_cas_denies_intervening_edit_without_replacing_it",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_current_receiving_source_change_during_render_denies_publication",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_actor_session_and_profile_changes_after_actual_render_deny_publication",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_managed_download_checks_actual_pair_and_fresh_receiving_authority",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_partial_private_copy_failure_cleans_only_owned_attempt_and_keeps_prior_pair",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_delivery_bookend_rechecks_source_and_session_after_actual_file_read",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_cleanup_refuses_foreign_content_replacement_identity_and_symlink",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_range_and_job_identity_conflicts_never_overwrite_a_prior_output",
    "test_station_label_publication.OrdinaryReceiptLabelPublicationTests.test_actual_http_routes_preserve_origin_session_and_managed_file_gates",
)
REGRESSION_IDS = (
    "test_receiving_label_plan.ReceivingLabelPlanTests.test_partial_range_crosses_sorted_rows_without_changing_full_quantities",
    "test_receiving_label_plan.ReceivingLabelPlanTests.test_reordered_rows_and_unprinted_mutable_fields_preserve_print_plan",
    "test_receiving_label_plan.ReceivingLabelPlanTests.test_legal_million_copy_plan_is_compact_and_last_part_is_exact",
    "test_receiving_label_plan.ReceivingLabelPlanTests.test_adaptive_profile_budget_bounds_every_worst_case_part",
    "test_receiving_label_plan.ReceivingLabelPlanTests.test_nonwhole_bool_negative_zero_and_overrun_ranges_reject",
    "test_receiving_label_plan.ReceivingLabelPlanTests.test_profile_is_explicit_and_frozen_without_default_or_override",
    "test_receiving_label_plan.ReceivingLabelPlanTests.test_full_mark_types_quantities_source_and_retail_binding_reject",
    "test_receiving_label_plan.ReceivingLabelPlanTests.test_duplicate_review_ids_and_cross_operation_or_terminal_marks_reject",
    "test_receiving_label_plan.ReceivingLabelPlanTests.test_per_row_total_caps_and_unknown_count_ids_reject",
    "test_receiving_label_plan.ReceivingLabelPlanTests.test_rehashed_plan_tampering_and_invalid_part_indices_reject",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_actual_pdf_and_zpl_exact_sku_across_layouts_and_dpis",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_pdf_human_image_and_zpl_gfa_are_exact_same_raster",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_partial_copy_counts_preserve_original_full_cards_and_marks",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_unprinted_metadata_and_worker_projection_do_not_change_basis",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_unsupported_printed_glyph_bidi_shaping_and_controls_write_nothing",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_only_layouts_that_print_details_require_their_glyphs",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_visible_clipping_reports_ellipsis_without_changing_sku_or_identity",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_pinned_font_mismatch_and_frozen_barcode_geometry_fail_before_writes",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_existing_destinations_and_second_file_failure_preserve_older_files",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_partial_zpl_write_and_pdf_byte_overflow_remove_only_own_pair",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_thousand_copy_part_is_actual_bounded_pdf_with_one_zpl_row",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_maximum_distinct_row_part_measures_metadata_and_adaptive_zpl_bound",
    "test_receiving_label_render.ReceivingLabelRenderTests.test_actual_widest_profile_adaptive_part_is_bounded_without_dropping_rows",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_actual_pair_private_full_quantities_unchanged_and_no_parent_raster",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_actual_preflight_only_matches_pair_basis_and_removes_attempt",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_two_actual_attempts_are_unique_and_cleanup_cannot_remove_other_pair",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_actual_unsupported_font_text_fails_child_without_half_pair",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_invalid_full_mark_or_copy_range_creates_no_attempt_or_child",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_bad_deadline_or_input_byte_limit_creates_no_attempt",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_relative_traversal_file_and_simulated_reparse_roots_reject",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_timeout_stops_only_exact_owned_child_and_cleans_partial",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_partial_exit_oversize_and_metadata_failures_leave_no_half_pair",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_cancellation_propagates_after_stopping_owned_child_and_cleaning_attempt",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_preset_operator_event_denies_preflight_and_render_before_files_or_spawn",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_active_operator_event_stops_exact_child_for_both_modes",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_magic_hash_copy_type_basis_type_input_hash_and_zpl_count_are_verified",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_cleanup_requires_owned_identity_and_never_recurses_into_unknown_content",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_cleanup_refuses_simulated_reparse_and_retains_existing_private_file",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_source_frozen_cli_shape_and_minimal_environment_without_runtime_execution",
    "test_receiving_label_worker.ReceivingLabelWorkerTests.test_child_cli_rejects_mismatched_input_and_duplicate_json_without_initializing_app",
)
GUARD_IDS = tuple('tools.test_label_ci_provenance.ProvenanceTests.' + name for name in (
    'test_valid_owner_matches_both_wheel_and_installed_records',
    'test_same_site_root_does_not_confer_distribution_ownership',
    'test_missing_wheel_or_installed_record_membership_is_refused',
    'test_record_hash_and_size_mismatches_are_refused',
    'test_unsafe_or_symlink_escape_is_refused',
    'test_distribution_identity_and_duplicate_records_are_refused',
))
LIMIT = 4 * 1024 * 1024


def regular(path, limit):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError('REGULAR_FILE_BOUND')
    value = path.read_bytes()
    if len(value) != info.st_size:
        raise ValueError('FILE_CHANGED')
    return value


def digest(path, limit=128 * 1024 * 1024):
    value = regular(path, limit)
    return {'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}


def write(name, value):
    if name not in {'ordinary-summary.json', 'source-before.json', 'source-after.json',
                    'dependency-preflight.json', 'service-summary.json', 'service-results.json',
                    'regression-summary.json', 'regression-results.json',
                    'guard-summary.json', 'guard-results.json'}:
        raise ValueError('EVIDENCE_NAME')
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    if len(raw) > LIMIT:
        raise ValueError('EVIDENCE_BOUND')
    with (EVIDENCE / name).open('xb') as stream:
        stream.write(raw)


def sources(admission):
    actual = tuple(sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file()))
    if actual != tuple(sorted(SOURCE)) or any(p.is_symlink() for p in ROOT.rglob('*')):
        raise ValueError('SOURCE_SCOPE')
    expected = admission['expected_project_sources']
    if tuple(row['path'] for row in expected) != PROJECT:
        raise ValueError('PROJECT_SELECTION')
    result = {name: digest(ROOT / name, LIMIT) for name in SOURCE}
    for row in expected:
        if result[row['path']] != {k: row[k] for k in ('bytes', 'sha256')}:
            raise ValueError('PROJECT_PREIMAGE')
    if any(os.access(ROOT / name, os.W_OK) for name in SOURCE):
        raise ValueError('WRITABLE_SOURCE')
    return result


def elf_versions(raw):
    """Read ELF64/amd64 exported version names without loading the library."""
    if len(raw) < 64 or raw[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', raw, 18)[0] != 62:
        raise ValueError('ELF_ARCHITECTURE')
    offset = struct.unpack_from('<Q', raw, 40)[0]
    width, count = struct.unpack_from('<HH', raw, 58)
    if width != 64 or count > 4096 or offset + width * count > len(raw):
        raise ValueError('ELF_SECTION_BOUND')

    def string(start):
        end = raw.find(b'\0', start, start + 4096)
        if not 0 <= start < len(raw) or end < 0:
            raise ValueError('ELF_STRING_BOUND')
        return raw[start:end].decode('ascii')

    exported = set()
    for index in range(count):
        section = offset + index * width
        if struct.unpack_from('<I', raw, section + 4)[0] != 0x6ffffffd:
            continue
        start, size = struct.unpack_from('<QQ', raw, section + 24)
        link = struct.unpack_from('<I', raw, section + 40)[0]
        if start + size > len(raw) or link >= count:
            raise ValueError('ELF_VERSION_BOUND')
        strings = struct.unpack_from('<Q', raw, offset + link * width + 24)[0]
        cursor = start
        for _ in range(4096):
            if cursor + 20 > start + size:
                raise ValueError('ELF_VERSION_ENTRY')
            auxiliary, following = struct.unpack_from('<II', raw, cursor + 12)
            if cursor + auxiliary + 8 > start + size:
                raise ValueError('ELF_VERSION_AUXILIARY')
            name = struct.unpack_from('<I', raw, cursor + auxiliary)[0]
            exported.add(string(strings + name))
            if following == 0:
                break
            if following < 20:
                raise ValueError('ELF_VERSION_PROGRESS')
            cursor += following
        else:
            raise ValueError('ELF_VERSION_LIMIT')
    return exported


def base_native(admission):
    result = []
    for requirement in admission['base_symbol_requirements']:
        soname = requirement['soname']
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+', soname):
            raise ValueError('SONAME')
        candidates = []
        for directory in ('/lib/x86_64-linux-gnu', '/usr/lib/x86_64-linux-gnu', '/lib64'):
            path = Path(directory) / soname
            if path.exists():
                resolved = path.resolve(strict=True)
                if not (resolved.is_relative_to('/usr/lib') or resolved.is_relative_to('/lib')):
                    raise ValueError('BASE_LIBRARY_PATH')
                candidates.append(resolved)
        candidates = sorted(set(candidates))
        if len(candidates) != 1:
            raise ValueError('BASE_LIBRARY_MISSING_OR_AMBIGUOUS')
        raw = regular(candidates[0], 128 * 1024 * 1024)
        exported = elf_versions(raw)
        if not set(requirement['required_versions']) <= exported:
            raise ValueError('BASE_SYMBOL_VERSION_MISSING')
        result.append({'soname': soname, 'path': str(candidates[0]), 'bytes': len(raw),
                       'sha256': hashlib.sha256(raw).hexdigest(),
                       'required_versions': requirement['required_versions'],
                       'exported_version_count': len(exported)})
    return result


def context_proof(admission):
    expected_paths = tuple(admission['expected_context_paths'])
    if len(expected_paths) != 81 or len(set(expected_paths)) != 81:
        raise ValueError('CONTEXT_PROOF_EXPECTED_SELECTION')
    raw = regular(CONFIG / 'context-audit.json', 65536)
    proof = json.loads(raw)
    manifest_sha = os.environ.get('SS_ORDINARY_CONTEXT_MANIFEST_SHA256', '')
    if (re.fullmatch(r'[a-f0-9]{64}', manifest_sha) is None
        or proof.get('format') != 'stock-and-sleeve:ordinary-label-filtered-context:v1'
        or proof.get('source_sha') != os.environ.get('SS_ORDINARY_LABEL_SOURCE_SHA')
        or proof.get('expected_manifest_sha256') != manifest_sha
        or proof.get('successful') is not True or proof.get('exact_names_and_hashes') is not True
        or proof.get('foreign_install_started') is not False
        or proof.get('application_or_foreign_import') is not False
        or type(proof.get('file_count')) is not int or proof['file_count'] != 81
        or proof.get('directories') != ['hosted', 'tools']
        or proof.get('required_absent') != {'hosted/app.py': True, 'tools/run_hosted_browser_ci.sh': True}
        or type(proof.get('files')) is not list):
        raise ValueError('CONTEXT_PROOF_SCHEMA_OR_BINDING')
    files = proof['files']
    if (len(files) != 81 or tuple(row.get('path') for row in files) != expected_paths
        or any(set(row) != {'path', 'bytes', 'sha256'} or type(row['bytes']) is not int
               or not 0 <= row['bytes'] <= LIMIT or type(row['sha256']) is not str
               or re.fullmatch(r'[a-f0-9]{64}', row['sha256']) is None for row in files)):
        raise ValueError('CONTEXT_PROOF_EXACT_FILES')
    by_name = {row['path']: row for row in files}
    for name in SOURCE:
        if name not in by_name or digest(ROOT / name, LIMIT) != {
                key: by_name[name][key] for key in ('bytes', 'sha256')}:
            raise ValueError('CONTEXT_TO_RUNTIME_SOURCE_MISMATCH')
    return {'file_count': 81, 'runtime_files_bound': len(SOURCE),
            'source_sha': proof['source_sha'], 'expected_manifest_sha256': manifest_sha,
            'proof_sha256': hashlib.sha256(raw).hexdigest(), 'successful': True}


def preflight(admission):
    if (sys.version_info[:3] != (3, 12, 16) or sys.implementation.name != 'cpython'
        or sys.platform != 'linux' or platform.machine() != 'x86_64' or os.getuid() != 10001):
        raise ValueError('RUNTIME_PLATFORM')
    libc = os.confstr('CS_GNU_LIBC_VERSION')
    if not libc or not libc.startswith('glibc ') or tuple(map(int, libc.split()[1].split('.'))) < (2, 34):
        raise ValueError('GLIBC_VERSION')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    interfaces = {line.split(':', 1)[0].strip() for line in Path('/proc/net/dev').read_text().splitlines() if ':' in line}
    if interfaces != {'lo'} or int(status['CapEff'].strip(), 16) != 0 or status['NoNewPrivs'].strip() != '1':
        raise ValueError('CONTAINER_BOUNDARY')
    context_checked = context_proof(admission)
    native = base_native(admission)  # Must precede package import/test execution.
    site = Path(sysconfig.get_path('purelib'))
    if (not sys.flags.no_site or not sys.flags.isolated or not sys.flags.ignore_environment
        or not sys.flags.dont_write_bytecode or list(site.glob('*.pth'))):
        raise ValueError('SITE_STARTUP_HOOK_REFUSED')
    # This reviewed project module and its six real stdlib fixture tests run
    # while site startup remains suppressed, before any foreign import.
    from tools import label_ci_provenance as provenance
    guard_report = run_guard_suite(admission)
    wheels = admission['wheels']
    if len(wheels) != 16 or len({provenance.canonical(r['name']) for r in wheels}) != 16:
        raise ValueError('WHEEL_SELECTION')
    if {p.name for p in (CONFIG / 'wheels').iterdir()} != {r['filename'] for r in wheels}:
        raise ValueError('WHEEL_SCOPE')
    packages = []
    verified = {}
    installs = {}
    installed_distributions = list(metadata.distributions(path=[str(site)]))
    for wheel in wheels:
        if wheel['yanked'] or wheel['extras']:
            raise ValueError('WHEEL_PIN')
        record = provenance.read_wheel(CONFIG / 'wheels' / wheel['filename'], wheel)
        candidates = [dist for dist in installed_distributions
                      if provenance.canonical(dist.metadata['Name']) == record.name]
        if len(candidates) != 1:
            raise ValueError('INSTALLED_DISTRIBUTION_DUPLICATE_OR_MISSING')
        installed = provenance.read_install(candidates[0], site, record)
        verified[record.name] = record
        installs[record.name] = installed
        packages.append({'name': record.name, 'version': record.version,
                         'bytes': wheel['bytes'], 'sha256': wheel['sha256'],
                         'wheel_record_sha256': record.record_sha256,
                         'installed_record_sha256': installed.record_sha256})
    assets = []
    for pin in admission['installed_asset_pins']:
        name = provenance.canonical(pin['distribution'])
        assets.append(provenance.verify_asset(pin, verified[name], installs[name]))
    expected_assets = {(name, path) for name, record in verified.items()
                       for path in record.rows if path != record.record_path}
    if len({path for _, path in expected_assets}) != len(expected_assets):
        raise ValueError('WHEEL_ASSET_OWNER_COLLISION')
    actual_assets = [(row['distribution'], row['path']) for row in assets]
    if len(actual_assets) != len(set(actual_assets)) or set(actual_assets) != expected_assets:
        raise ValueError('ASSET_OWNERSHIP_SCOPE')
    pdfium_version = json.loads(provenance.raw_file(site / 'pypdfium2_raw/version.json', LIMIT))
    if pdfium_version != admission['pdfium_build'] or pdfium_version['flags']:
        raise ValueError('PDFIUM_BUILD_OR_FEATURES')
    native_paths = admission['native_asset_paths']
    if len(native_paths) != 46 or len(set(native_paths)) != 46:
        raise ValueError('NATIVE_ASSET_SELECTION')
    native_assets = []
    for name in native_paths:
        raw = provenance.raw_file(site / name)
        if (len(raw) < 64 or raw[:6] != b'\x7fELF\x02\x01'
            or struct.unpack_from('<H', raw, 18)[0] != 62):
            raise ValueError('NATIVE_ASSET_ARCHITECTURE')
        native_assets.append({'path': name, 'bytes': len(raw),
                              'sha256': hashlib.sha256(raw).hexdigest()})
    # The actual site location is enabled only after native checks, independent
    # wheel/installed ownership, every file pin and guard regression success.
    sys.path.append(str(site))
    if os.environ.get('PSYCOPG_IMPL') != 'binary':
        raise ValueError('BINARY_IMPLEMENTATION_SELECTION')
    import psycopg
    from psycopg import pq
    if psycopg.__version__ != '3.3.9' or pq.__impl__ != 'binary':
        raise ValueError('BINARY_IMPLEMENTATION_REQUIRED')
    return {'platform': sys.platform, 'python': platform.python_version(), 'machine': platform.machine(),
            'uid': os.getuid(), 'glibc': libc, 'network_interfaces': sorted(interfaces),
            'filtered_build_context': context_checked,
            'base_native_data': native, 'native_assets': native_assets,
            'wheels': packages, 'installed_assets': assets,
            'psycopg_version': psycopg.__version__, 'psycopg_implementation': pq.__impl__,
            'libpq_version': pq.version(),
            'pdfium_build': pdfium_version, 'provenance_guards': guard_report,
            'native_preflight': 'ELF data/symbol and RECORD checks precede first foreign import; no ldd',
            'test_only_decoder_graph': True, 'test_only_fixed_driver_graph': True}


class BoundedLog:
    def __init__(self, stream):
        self.stream, self.bytes = stream, 0

    def write(self, text):
        size = len(text.encode('utf-8'))
        if self.bytes + size > LIMIT:
            raise ValueError('TEST_LOG_BOUND')
        self.bytes += size
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()


class Results(unittest.TextTestResult):
    event_name = 'case-events.jsonl'
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = {}
        self.events = (EVIDENCE / self.event_name).open('x', encoding='utf-8', newline='\n')

    def event(self, row):
        self.events.write(json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n')
        self.events.flush()

    def startTest(self, test):
        if test.id() in self.rows:
            raise ValueError('DUPLICATE_TEST_START')
        self.rows[test.id()] = {'id': test.id(), 'started': True, 'status': 'RUNNING', 'subtests': 0}
        self.event(self.rows[test.id()])
        super().startTest(test)

    def stopTest(self, test):
        self.event(self.rows[test.id()])
        super().stopTest(test)

    def stopTestRun(self):
        self.events.close()
        super().stopTestRun()

    def addSuccess(self, test):
        self.rows[test.id()]['status'] = 'PASS'
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.rows[test.id()]['status'] = 'FAIL'
        super().addFailure(test, err)

    def addError(self, test, err):
        self.rows[test.id()]['status'] = 'ERROR'
        super().addError(test, err)

    def addSubTest(self, test, subtest, err):
        self.rows[test.id()]['subtests'] += 1
        if err is not None:
            self.rows[test.id()]['status'] = 'SUBTEST_FAILURE'
        super().addSubTest(test, subtest, err)

    def addSkip(self, test, reason):
        self.rows[test.id()]['status'] = 'SKIP_REFUSED'
        super().addSkip(test, reason)


class GuardResults(Results):
    event_name = 'guard-events.jsonl'


class RegressionResults(Results):
    event_name = 'regression-events.jsonl'


class ServiceResults(Results):
    event_name = 'service-events.jsonl'


def run_guard_suite(admission):
    if tuple(admission['guard_test_ids']) != GUARD_IDS or admission['guard_test_count'] != 6:
        raise ValueError('GUARD_SELECTION')
    with (EVIDENCE / 'guard-tests.log').open('x', encoding='utf-8', newline='\n') as stream:
        log = BoundedLog(stream)
        with redirect_stdout(log), redirect_stderr(log):
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromNames(GUARD_IDS)
            for error in loader.errors:
                log.write(error)
            if sorted(test.id() for test in tests(suite)) != sorted(GUARD_IDS):
                raise ValueError('GUARD_DISCOVERY')
            result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=GuardResults).run(suite)
    successful = (result.wasSuccessful() and result.testsRun == 6 and set(result.rows) == set(GUARD_IDS)
                  and all(row['status'] == 'PASS' for row in result.rows.values())
                  and not result.skipped and not result.expectedFailures and not result.unexpectedSuccesses
                  and not list(Path('/tmp/ordinary-label-tests').iterdir()))
    report = {'scope': 'Six stdlib RECORD provenance fixture regressions; not label cases',
              'expected_count': 6, 'tests_run': result.testsRun, 'successful': successful}
    write('guard-results.json', [result.rows[key] for key in sorted(result.rows)])
    write('guard-summary.json', report)
    if not successful:
        raise ValueError('PROVENANCE_GUARD_REGRESSION_FAILURE')
    return report


def tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from tests(item)
        else:
            yield item


def cleanup_state():
    worker = sys.modules.get('station_receiving_label_worker')
    owned = len(worker._OWNED) if worker is not None else None
    temporary = list(Path('/tmp/ordinary-label-tests').iterdir())
    processes = []
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal() or int(path.name) in (1, os.getpid()):
            continue
        try:
            row = next(s for s in (path / 'status').read_text().splitlines() if s.startswith('Uid:'))
            if int(row.split()[1]) == os.getuid():
                processes.append(int(path.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return {'worker_owned_attempts': owned, 'remaining_temp_entries': len(temporary),
            'other_uid10001_pids': sorted(processes),
            'complete': owned == 0 and not temporary and not processes}


def run_group(admission, group, expected, result_class):
    if (tuple(admission[group + '_test_ids']) != expected
        or admission[group + '_test_count'] != len(expected)):
        raise ValueError('EXACT_GROUP_SELECTION')
    with (EVIDENCE / (group + '-tests.log')).open('x', encoding='utf-8', newline='\n') as stream:
        log = BoundedLog(stream)
        with redirect_stdout(log), redirect_stderr(log):
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromNames(expected)
            for error in loader.errors:
                log.write(error)
            if sorted(test.id() for test in tests(suite)) != sorted(expected):
                raise ValueError('EXACT_GROUP_DISCOVERY')
            result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=result_class).run(suite)
    cleanup = cleanup_state()
    successful = (result.wasSuccessful() and result.testsRun == len(expected)
        and set(result.rows) == set(expected) and all(r['status'] == 'PASS' for r in result.rows.values())
        and not result.skipped and not result.expectedFailures and not result.unexpectedSuccesses
        and cleanup['complete'])
    report = {'group': group, 'expected_count': len(expected), 'tests_run': result.testsRun,
              'successful': successful, 'cleanup': cleanup}
    write(group + '-results.json', [result.rows[key] for key in sorted(result.rows)])
    write(group + '-summary.json', report)
    return report


def main():
    summary = {'format': 'stock-and-sleeve:ordinary-label-ci:v1',
               'scope': 'Owned Linux real-service17, unchanged portable regression40, separate provenance6',
               'source_sha': os.environ.get('SS_ORDINARY_LABEL_SOURCE_SHA'),
               'started': False, 'successful': False, 'status': 'PREFLIGHT_FAILURE',
               'service_expected': 17, 'regression_expected': 40, 'separate_guards_expected': 6,
               'total_selected': 63, 'windows_acceptance': False, 'hardware_acceptance': False,
               'desktop_browser_acceptance': False, 'hosted_or_provider_acceptance': False}
    before = None
    started = time.monotonic()
    try:
        if len(sys.argv) != 1 or not re.fullmatch(r'[a-f0-9]{40}', summary['source_sha'] or ''):
            raise ValueError('FIXED_INVOCATION')
        admission = json.loads(regular(ROOT / 'tools/ordinary_label_ci_admission.json', LIMIT))
        if admission['execution_hold'] is not None:
            raise ValueError('EXECUTION_HOLD')
        if len(PROJECT) != 72 or len(SERVICE_IDS) != 17 or len(REGRESSION_IDS) != 40:
            raise ValueError('FIXED_SOURCE_AND_TEST_COUNTS')
        Path('/tmp/ordinary-label-tests').mkdir(mode=0o700)
        before = sources(admission)
        write('source-before.json', before)
        sys.path.insert(0, str(ROOT))
        checked = preflight(admission)
        write('dependency-preflight.json', checked)
        summary['guard_successful'] = checked['provenance_guards']['successful']
        summary['started'] = True
        # Original40 are byte-exact regressions on this combined native graph.
        # Service17 are a separate real SQLite/worker/loopback fixture group.
        regression = run_group(admission, 'regression', REGRESSION_IDS, RegressionResults)
        service = run_group(admission, 'service', SERVICE_IDS, ServiceResults)
        summary['regression'] = regression
        summary['service'] = service
        summary['cleanup'] = cleanup_state()
        summary['successful'] = (summary['guard_successful'] and regression['successful']
                                  and service['successful'] and summary['cleanup']['complete'])
        summary['status'] = 'TEST_FAILURE'
    except Exception as error:
        summary['successful'] = False
        summary['failure_type'] = type(error).__name__
        code = str(error)
        summary['failure_code'] = code if re.fullmatch(r'[A-Z_]{1,100}', code) else 'UNCLASSIFIED_FAILURE'
        summary['status'] = 'PREFLIGHT_OR_RUNNER_FAILURE'
    finally:
        try:
            if before is not None:
                after = sources(admission)
                write('source-after.json', after)
                summary['source_stable'] = before == after
                summary['successful'] = summary['successful'] and before == after
        except Exception:
            summary['source_stable'] = False
            summary['successful'] = False
        summary['duration_seconds'] = round(time.monotonic() - started, 3)
        summary['completed_utc'] = datetime.now(timezone.utc).isoformat()
        if summary['successful']:
            summary['status'] = 'PASS'
        write('ordinary-summary.json', summary)
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
