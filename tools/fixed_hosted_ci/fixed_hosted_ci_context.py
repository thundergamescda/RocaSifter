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
    if len(expected) != 303 or len({row['path'] for row in expected}) != len(expected):
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
    # Five exact reviewed foundation files are additions, never replacements
    # for the immutable279 public source preimages or their old case bodies.
    foundations = admission['foundation_sources']
    foundation_names = (
        'hosted/intake_label_policy_store.py', 'hosted/label_postgres_store.py',
        'intake_label_policy_evidence.py', 'test_label_policy_store_postgres.py',
        'test_label_postgres_store_postgres.py',
    )
    if (type(foundations) is not list or len(foundations) != 5
        or [row['path'] for row in foundations] != list(foundation_names)
        or set(foundation_names) & {row['path'] for row in project}):
        raise ValueError('EXACT_FIVE_FOUNDATION_ADDITIONS')
    by_name = {row['path']: row for row in checked}
    for row in foundations:
        if row != by_name.get(row['path']):
            raise ValueError('FOUNDATION_CONTEXT_PREIMAGE')
        save(Path('/runtime-source') / safe(row['path']), raw(INPUT / row['path']))
    overlay = raw(INPUT / (CONTROL_PREFIX + 'runtime-overlay/hosted_ci_evidence.py'))
    target = Path('/runtime-source/tools/hosted_ci_evidence.py')
    original = target.read_bytes()
    old = b"process = subprocess.run([sys.executable, 'tools/test_hosted_postgres.py'],"
    new = b"process = subprocess.run([sys.executable, '-I', '-S', '-B', '/opt/fixed-hosted-ci/fixed_hosted_ci_entry.py', '--postgres-child'],"
    old_version, new_version = b'version != 180006', b'version != 180007'
    old_count, new_count = b'POSTGRES_EXPECTED_COUNT = 549', b'POSTGRES_EXPECTED_COUNT = 600'
    if (original.count(old) != 1 or original.count(old_version) != 1 or original.count(old_count) != 1
        or original.replace(old, new).replace(old_version, new_version).replace(old_count, new_count) != overlay
        or overlay.replace(new_count, old_count).replace(new_version, old_version).replace(new, old) != original):
        raise ValueError('EXACT_THREE_EVIDENCE_FORWARD_INVERSE_OVERLAYS')
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
              'runtime_evidence_literal_changes': 3, 'foundation_addition_count': 5}
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
