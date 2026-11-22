"""Build-only audit of files actually retained by Docker's context filter.

Runs with the pinned base stdlib and -B -I -S, before every foreign install.
The expected hash ledger comes from the exact clean host checkout, outside the
context, avoiding a self-hash cycle. This file and both Docker control files are
themselves included and hashed. No application or dependency module is imported.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path('/filtered-context')
OUTPUT = Path('/context-proof/context-audit.json')
LIMIT = 4 * 1024 * 1024
MANIFEST_LIMIT = 65536
EXPECTED = (
    "catalog_cleanup.py",
    "catalog_physical_ownership.py",
    "credit_tracker.py",
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
    "hosted_labels.py",
    "intake_label_contract.py",
    "intake_manifest.py",
    "intake_physical_identity.py",
    "pricing_review.py",
    "receiving_evidence.py",
    "roca_edition.json",
    "roca_to_square.py",
    "scanner_adapters.py",
    "square_variations.py",
    "station_cloud.py",
    "station_cloud_journal_cas.py",
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
    "test_receiving_label_plan.py",
    "test_receiving_label_render.py",
    "test_receiving_label_worker.py",
    "test_shared_trade.py",
    "test_station_cloud.py",
    "test_station_label_publication.py",
    "tools/label_ci_provenance.py",
    "tools/ordinary_label_ci.Dockerfile",
    "tools/ordinary_label_ci.Dockerfile.dockerignore",
    "tools/ordinary_label_ci_admission.json",
    "tools/ordinary_label_ci_context.py",
    "tools/ordinary_label_ci_evidence.py",
    "tools/ordinary_label_ci_requirements.txt",
    "tools/ordinary_label_ci_wheel_urls.txt",
    "tools/test_label_ci_provenance.py",
)
FORBIDDEN = ('hosted/app.py', 'tools/run_hosted_browser_ci.sh')
DIRECTORIES = frozenset(('hosted', 'tools'))


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('CONTEXT_DUPLICATE_JSON_KEY')
        result[name] = value
    return result


def audit():
    if (len(sys.argv) != 1 or sys.platform != 'linux'
        or sys.version_info[:3] != (3, 12, 16) or sys.implementation.name != 'cpython'
        or not sys.flags.isolated or not sys.flags.no_site
        or not sys.flags.ignore_environment or not sys.flags.dont_write_bytecode):
        raise ValueError('CONTEXT_STDLIB_INVOCATION')
    encoded = os.environ.get('SS_ORDINARY_CONTEXT_EXPECTED_B64', '')
    if not encoded or len(encoded) > 90000 or not encoded.isascii():
        raise ValueError('CONTEXT_MANIFEST_ENCODING_BOUND')
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > MANIFEST_LIMIT:
        raise ValueError('CONTEXT_MANIFEST_BOUND')
    expected = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
    if (type(expected) is not dict or set(expected) != {'format', 'source_sha', 'files'}
        or expected['format'] != 'stock-and-sleeve:ordinary-label-context-expected:v1'
        or type(expected['source_sha']) is not str
        or re.fullmatch(r'[a-f0-9]{40}', expected['source_sha']) is None
        or type(expected['files']) is not list or len(expected['files']) != 81):
        raise ValueError('CONTEXT_MANIFEST_SCHEMA')
    by_name = {}
    for row in expected['files']:
        if (type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'}
            or type(row['path']) is not str or row['path'] not in EXPECTED
            or row['path'] in by_name or type(row['bytes']) is not int
            or not 0 <= row['bytes'] <= LIMIT or type(row['sha256']) is not str
            or re.fullmatch(r'[a-f0-9]{64}', row['sha256']) is None):
            raise ValueError('CONTEXT_EXPECTED_FILE_SCHEMA')
        by_name[row['path']] = row
    if tuple(sorted(by_name)) != EXPECTED:
        raise ValueError('CONTEXT_EXPECTED_EXACT_SELECTION')
    if not stat.S_ISDIR(ROOT.lstat().st_mode) or ROOT.is_symlink():
        raise ValueError('CONTEXT_ROOT_TYPE')
    absent = {name: not os.path.lexists(ROOT / name) for name in FORBIDDEN}
    if not all(absent.values()):
        raise ValueError('CONTEXT_FORBIDDEN_SOURCE_PRESENT')
    files = {}
    directories = set()
    pending = [ROOT]
    entries = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as listing:
            for entry in listing:
                entries += 1
                if entries > 128:
                    raise ValueError('CONTEXT_ENTRY_BOUND')
                path = Path(entry.path)
                name = path.relative_to(ROOT).as_posix()
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if name not in DIRECTORIES:
                        raise ValueError('CONTEXT_UNSELECTED_DIRECTORY')
                    directories.add(name)
                    pending.append(path)
                    continue
                if name not in by_name:
                    raise ValueError('CONTEXT_UNSELECTED_NAME')
                if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                    raise ValueError('CONTEXT_REGULAR_FILE_BOUND')
                data = path.read_bytes()
                after = path.lstat()
                if (len(data) != info.st_size or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
                    raise ValueError('CONTEXT_FILE_CHANGED')
                files[name] = {'path': name, 'bytes': len(data),
                               'sha256': hashlib.sha256(data).hexdigest()}
                if files[name] != by_name[name]:
                    raise ValueError('CONTEXT_SOURCE_HASH_MISMATCH')
    if tuple(sorted(files)) != EXPECTED or directories != DIRECTORIES:
        raise ValueError('CONTEXT_ACTUAL_EXACT_SELECTION')
    return {'format': 'stock-and-sleeve:ordinary-label-filtered-context:v1',
            'source_sha': expected['source_sha'], 'successful': True,
            'expected_manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'file_count': len(files), 'files': [files[name] for name in sorted(files)],
            'directories': sorted(directories), 'required_absent': absent,
            'exact_names_and_hashes': True, 'foreign_install_started': False,
            'application_or_foreign_import': False,
            'scope': 'Actual Docker-filtered COPY filesystem before foreign installation; not a test case'}


def main():
    try:
        result = audit()
        raw = (json.dumps(result, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw) > MANIFEST_LIMIT:
            raise ValueError('CONTEXT_REPORT_BOUND')
        OUTPUT.parent.mkdir(mode=0o755)
        with OUTPUT.open('xb') as stream:
            stream.write(raw)
        print('ORDINARY_CONTEXT_AUDIT_PASS')
        return 0
    except Exception as error:
        value = str(error)
        code = value if re.fullmatch(r'[A-Z_]{1,96}', value) else 'CONTEXT_UNCLASSIFIED_FAILURE'
        # No raw paths, source text, exception text or traceback on rejection.
        print('ORDINARY_CONTEXT_AUDIT_FAIL ' + code, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
