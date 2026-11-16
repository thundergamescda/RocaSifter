"""Linux hosted API unit and SQL evidence, never Windows/provider acceptance.

Invoked only inside the owned CI image/network by run_hosted_ci.sh. The unchanged
PostgreSQL runner selects its full ALLOWED suite in a fresh child process. Five
companion server API unit cases use fictional database responses, not real SQL.
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
POSTGRES_EXPECTED_COUNT = 370
IDENTITY_UNIT_MODULE = 'test_hosted_store_identity'
IDENTITY_UNIT_IDS = (
    IDENTITY_UNIT_MODULE + '.StoreIdentityContract.test_authenticated_me_projects_resolved_actor_not_query_or_cookie_hints',
    IDENTITY_UNIT_MODULE + '.StoreIdentityContract.test_full_session_helper_exposes_same_canonical_uuid_for_both_auth_paths',
    IDENTITY_UNIT_MODULE + '.StoreIdentityContract.test_http_full_session_and_challenge_envelopes_remain_distinct',
    IDENTITY_UNIT_MODULE + '.StoreIdentityContract.test_password_only_mfa_challenge_has_no_tenant_or_session_authority',
    IDENTITY_UNIT_MODULE + '.StoreIdentityContract.test_unauthenticated_me_exposes_no_store_id',
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


def run_identity_unit():
    """Run the full approved module; ordinary unit failures never skip the PG child."""
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
            suite = loader.loadTestsFromName(IDENTITY_UNIT_MODULE)
            expected = list(test_ids(suite))
            result = unittest.TextTestRunner(stream=buffer, verbosity=2,
                                            resultclass=EvidenceResult).run(suite)
        except (Exception, SystemExit) as error:
            runner_error = type(error).__name__
            print('Identity unit runner failed with type=' + runner_error)
    log = scrub(buffer.getvalue())
    payload = log.encode('utf-8')
    if len(payload) > REPORT_LIMIT:
        raise ValueError('unit_identity_log_bound')
    (EVIDENCE / 'unit-identity-tests.log').write_bytes(payload)
    rows = result.results if result is not None else []
    started_ids = result.started_ids if result is not None else []
    passed_ids = [row['id'] for row in rows if row['status'] == 'passed']
    loader_errors = [scrub(error) for error in loader.errors] if loader is not None else []
    counts = {'skipped_count': len(result.skipped) if result is not None else 0,
              'failed_count': len(result.failures) if result is not None else 0,
              'error_count': len(result.errors) if result is not None else 0,
              'expected_failure_count': len(result.expectedFailures) if result is not None else 0,
              'unexpected_success_count': len(result.unexpectedSuccesses) if result is not None else 0}
    coverage = bool(expected == list(IDENTITY_UNIT_IDS) and len(set(expected)) == 5 and
                    result is not None and result.testsRun == 5 and
                    Counter(started_ids) == Counter(IDENTITY_UNIT_IDS) and
                    Counter(passed_ids) == Counter(IDENTITY_UNIT_IDS) and
                    len(rows) == 5 and all(row['status'] == 'passed' for row in rows))
    was_successful = bool(result is not None and result.wasSuccessful())
    passed = bool(coverage and was_successful and not any(counts.values()) and
                  not loader_errors and runner_error is None)
    summary = {'scope': 'Five hosted server API projection unit cases with fictional database responses; not SQL acceptance.',
               'module': IDENTITY_UNIT_MODULE, 'source_sha': os.environ.get('SS_CI_SOURCE_SHA'),
               'started_utc': started, 'finished_utc': now(),
               'approved_test_ids': list(IDENTITY_UNIT_IDS), 'expected_test_ids': expected,
               'expected_count': 5, 'discovered_count': len(expected),
               'tests_run': result.testsRun if result is not None else 0,
               'started_test_ids': started_ids, 'successful_test_ids': passed_ids,
               'case_results': rows, 'was_successful': was_successful,
               'expected_case_coverage_complete': coverage,
               'loader_error_count': len(loader_errors), 'loader_errors': loader_errors,
               'runner_error_type': runner_error, **counts,
               'log_sha256': hashlib.sha256(payload).hexdigest(),
               'log_hash_convention': 'SHA-256 of delivered sanitized UTF-8 log bytes, LF line endings, no BOM',
               'passed': passed}
    save('unit-identity-summary.json', summary)
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
              'unit_identity': {'passed': False, 'state': 'not_started'}}
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
        try:
            result['unit_identity'] = run_identity_unit()
        except (Exception, SystemExit) as error:
            # Even loader/report failures must retain the independent full PG run.
            result['unit_identity'] = {'passed': False, 'runner_error_type': type(error).__name__}
            result['error_types'].append(type(error).__name__)
            result['error_codes'].append('unit_identity_evidence_failed')
        if result['unit_identity'].get('passed') is not True:
            result['error_codes'].append('unit_identity_failed')
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
                                result.get('unit_identity', {}).get('passed') is True and
                                result.get('tls_before', {}).get('ssl') is True and
                                result.get('tls_after', {}).get('ssl') is True and
                                result.get('postgres') and result['runner_exit_code'] == 0)
        result['finished_utc'] = now()
        save('hosted-ci-summary.json', result)
        print(scrub(json.dumps(result, indent=2, allow_nan=False)))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
