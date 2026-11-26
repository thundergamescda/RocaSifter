"""Reviewed-CI candidate entry only; never execute on the owner's Windows PC.

The one parent evidence seam only redirects its original PG subprocess here.
No acceptance module, assertion, fixture or approved case ID is patched.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import sysconfig
import time

CONFIG = Path('/opt/fixed-hosted-ci')
ROOT = Path('/workspace')
EVIDENCE = Path('/ci-evidence')


def imported_sources(preflight, admission):
    project = {row['path']: row for row in admission['project_sources']}
    controls = {row['path']: row for row in admission['control_sources']}
    assets = {row['path']: row for row in admission['installed_asset_pins']}
    site = Path(sysconfig.get_path('purelib')).resolve(strict=True)
    result = {'project': {}, 'controls': {}, 'foreign_record_owned': {}, 'uncovered': []}
    for name, module in tuple(sys.modules.items()):
        value = getattr(module, '__file__', None)
        if not isinstance(value, str):
            continue
        path = Path(value).resolve(strict=True)
        if path.is_relative_to(ROOT):
            relative = path.relative_to(ROOT).as_posix()
            category, pins = 'project', project
        elif path.is_relative_to(CONFIG):
            relative = path.relative_to(CONFIG).as_posix()
            category, pins = 'controls', controls
        elif path.is_relative_to(site):
            relative = path.relative_to(site).as_posix()
            category, pins = 'foreign_record_owned', assets
        else:
            continue  # Stdlib/base identity is bound by the separate immutable image proof.
        if relative not in pins:
            result['uncovered'].append({'module': name, 'relative_path': relative, 'category': category})
            continue
        actual = preflight.digest(path, 128 * 1024 * 1024)
        if actual != {key: pins[relative][key] for key in ('bytes', 'sha256')}:
            raise ValueError('IMPORTED_PREIMAGE_CHANGED')
        entry = result[category].setdefault(relative, {**actual, 'module_names': []})
        entry['module_names'].append(name)
    if result['uncovered'] or not result['project']:
        raise ValueError('ACTUAL_IMPORT_COVERAGE')
    return result


def exact_ids(summary, expected):
    return bool(type(expected) is list and expected
                and len(set(expected)) == len(expected)
                and summary.get('expected_test_ids') == expected
                and sorted(summary.get('started_test_ids', [])) == sorted(expected)
                and sorted(summary.get('successful_test_ids', [])) == sorted(expected)
                and summary.get('tests_run') == len(expected)
                and summary.get('passed') is True
                and summary.get('skipped_count') == 0
                and summary.get('failed_count') == 0 and summary.get('error_count') == 0)


def main():
    if sys.argv[1:] not in ([], ['--postgres-child'], ['--gunicorn-child']):
        raise ValueError('ENTRY_ARGUMENTS')
    child = sys.argv[1:] == ['--postgres-child']
    gunicorn_child = sys.argv[1:] == ['--gunicorn-child']
    output = EVIDENCE / 'fixed-pg-child' if child else EVIDENCE / 'fixed-gunicorn-child' if gunicorn_child else EVIDENCE
    output.mkdir(exist_ok=True)
    sys.path.insert(0, str(CONFIG))  # Only root-owned, COPY-audited stdlib control source.
    from tools import fixed_hosted_ci_preflight as preflight
    preflight.EVIDENCE = output
    before = admission = None
    started_utc = datetime.now(timezone.utc).isoformat()
    started_wall, started_cpu = time.monotonic(), time.process_time()
    result = {'scope': 'Maintained20 graph; original549PG plus51foundationPG; unchanged99unit/6provenance/9Gunicorn cases.',
              'mode': 'postgres_child' if child else 'gunicorn_child' if gunicorn_child else 'parent', 'started_utc': started_utc,
              'passed': False, 'execution_exit_code': None, 'error_types': [],
              'admission': 'UNRUN_UNLESS_INDEPENDENTLY_REVIEWED'}
    exit_code = 1
    try:
        # Root must publish/admit the full immutable image/runtime/source graph.
        # Merely authoring or copying this Planning candidate grants no execution.
        policy = preflight.load(CONFIG / 'admission.json')
        if policy.get('execution_admission') != 'REVIEWED_FOR_DEDICATED_CI':
            raise ValueError('SOURCE_ONLY_CANDIDATE_NOT_ADMITTED')
        admission, before = preflight.preflight()
        sys.path.insert(0, str(ROOT))
        target = 'tools/test_hosted_postgres.py' if child else 'tools/hosted_ci_evidence.py'
        sys.argv = [target]  # Faithful original no-argument entry; no --modules narrowing.
        try:
            if gunicorn_child:
                from tools import fixed_hosted_gunicorn_smoke as gunicorn_smoke
                gunicorn_smoke.serve(preflight, admission, imported_sources)
            else:
                runpy.run_path(str(ROOT / target), run_name='__main__')
            exit_code = 0
        except SystemExit as stop:
            exit_code = stop.code if type(stop.code) is int else (0 if stop.code is None else 1)
            if not 0 <= exit_code <= 255:
                raise ValueError('CHILD_EXIT_CODE')
        result['execution_exit_code'] = exit_code
        contract = preflight.load(CONFIG / 'regression-contract.json')
        if child:
            summary = preflight.load(ROOT / 'Testing/oauth-receiving/postgres-latest.json', 64 * 1024 * 1024)
            if not exact_ids(summary, contract['postgres']['expected_test_ids']):
                raise ValueError('EXACT600_IDS_REQUIRED')
            if summary.get('modules') != [row['module'] for row in contract['postgres']['modules']]:
                raise ValueError('EXACT40_PG_MODULES_REQUIRED')
        elif gunicorn_child:
            workers = gunicorn_smoke.worker_reports(preflight)
            if (len(workers) != 2 or len({row.get('pid') for row in workers}) != 2
                or not all(row.get('passed') is True and row.get('parent_pid') == os.getpid()
                           for row in workers)):
                raise ValueError('TWO_GUNICORN_WORKER_PROOFS_REQUIRED')
            result['gunicorn_worker_proofs'] = workers
        else:
            for group in contract['units']['groups']:
                stem = group['historical_observation']['path'].replace('\\', '/').rsplit('/', 1)[-1]
                summary = preflight.load(EVIDENCE / stem)
                if not exact_ids(summary, group['expected_test_ids']):
                    raise ValueError('UNCHANGED99_IDS_REQUIRED')
            child_result = preflight.load(EVIDENCE / 'fixed-pg-child/fixed-entry-summary.json')
            if child_result.get('passed') is not True:
                raise ValueError('PG_CHILD_PREIMPORT_OR_IMPORT_PROOF_FAILED')
            from tools import fixed_hosted_gunicorn_smoke as gunicorn_smoke
            result['gunicorn_smoke'] = gunicorn_smoke.run(preflight, admission)
        result['actual_imports'] = imported_sources(preflight, admission)
        after = preflight.source_snapshot(admission)
        result['source_before'] = before
        result['source_after'] = after
        result['source_stable'] = before == after
        result['passed'] = bool(exit_code == 0 and result['source_stable'])
        if not result['passed']:
            exit_code = 1
    except (Exception, SystemExit) as error:
        result['error_types'].append(type(error).__name__)
        # No credential, DSN, SQL/provider response, path payload or exception repr.
        print('FIXED_ENTRY_FAILURE_TYPE=' + type(error).__name__)
        exit_code = 1
    finally:
        result['finished_utc'] = datetime.now(timezone.utc).isoformat()
        result['elapsed_monotonic'] = time.monotonic() - started_wall
        result['elapsed_process_cpu'] = time.process_time() - started_cpu
        result['exit_code'] = exit_code
        preflight.save('fixed-entry-summary.json', result)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
