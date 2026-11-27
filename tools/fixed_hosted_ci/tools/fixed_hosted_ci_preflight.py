"""Candidate-only stdlib admission before any application or foreign import.

Not executed or admitted by authoring this file. The host owns exact image,
filtered COPY, internal database network, resource and cleanup evidence. Six
provenance fixtures remain separate from the unchanged549 PostgreSQL/99 units.
"""
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import io
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

CONFIG = Path('/opt/fixed-hosted-ci')
ROOT = Path('/workspace')
EVIDENCE = Path('/ci-evidence')
MAX_SOURCE = 4 * 1024 * 1024
MAX_REPORT = 4 * 1024 * 1024
GUARD_IDS = tuple('tools.test_label_ci_provenance.ProvenanceTests.' + name for name in (
    'test_valid_owner_matches_both_wheel_and_installed_records',
    'test_same_site_root_does_not_confer_distribution_ownership',
    'test_missing_wheel_or_installed_record_membership_is_refused',
    'test_record_hash_and_size_mismatches_are_refused',
    'test_unsafe_or_symlink_escape_is_refused',
    'test_distribution_identity_and_duplicate_records_are_refused',
))


def regular(path, limit=MAX_SOURCE):
    item = path.lstat()
    if not stat.S_ISREG(item.st_mode) or item.st_size > limit:
        raise ValueError('REGULAR_FILE_BOUND')
    raw = path.read_bytes()
    if len(raw) != item.st_size:
        raise ValueError('FILE_CHANGED')
    return raw


def digest(path, limit=MAX_SOURCE):
    raw = regular(path, limit)
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('JSON_DUPLICATE_KEY')
        result[key] = value
    return result


def load(path, limit=MAX_REPORT):
    return json.loads(regular(path, limit).decode('utf-8'), object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('JSON_NONFINITE')))


def save(name, value):
    if not re.fullmatch(r'fixed-[a-z0-9-]{1,60}\.json', name):
        raise ValueError('REPORT_NAME')
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    if len(raw) > MAX_REPORT:
        raise ValueError('REPORT_BOUND')
    with (EVIDENCE / name).open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def source_snapshot(admission):
    rows = admission['project_sources']
    if len(rows) != 285 or len({row['path'] for row in rows}) != 285:
        raise ValueError('PUBLIC_SOURCE_SELECTION')
    expected = {row['path'] for row in rows}
    actual = set()
    for path in ROOT.rglob('*'):
        relative = path.relative_to(ROOT)
        if relative.parts[0] == 'Testing':
            continue  # Private mutable runner results; never an import root.
        if path.is_symlink():
            raise ValueError('SOURCE_SYMLINK')
        if path.is_file():
            actual.add(relative.as_posix())
    if actual != expected:
        raise ValueError('PUBLIC_SOURCE_EXACT_FILES')
    result = {}
    for row in rows:
        path = ROOT / row['path']
        value = digest(path)
        if value != {key: row[key] for key in ('bytes', 'sha256')} or os.access(path, os.W_OK):
            raise ValueError('PUBLIC_SOURCE_PIN_OR_WRITABLE')
        result[row['path']] = value
    return result


def context_proof(admission):
    manifest_sha = os.environ.get('SS_FIXED_HOSTED_CONTEXT_SHA256', '')
    raw = regular(CONFIG / 'context-audit.json')
    report = json.loads(raw, object_pairs_hook=unique_object)
    expected = admission['context_paths']
    if (not re.fullmatch('[a-f0-9]{64}', manifest_sha)
        or report.get('format') != 'stock-and-sleeve:fixed-hosted-context-audit:v1'
        or report.get('source_sha') != os.environ.get('SS_CI_SOURCE_SHA')
        or report.get('expected_manifest_sha256') != manifest_sha
        or [row.get('path') for row in report.get('files', [])] != expected
        or report.get('successful') is not True
        or report.get('foreign_install_started') is not False
        or report.get('application_or_foreign_import') is not False):
        raise ValueError('FILTERED_CONTEXT_PROOF')
    by_name = {row['path']: row for row in report['files']}
    if len(by_name) != len(expected):
        raise ValueError('FILTERED_CONTEXT_DUPLICATES')
    if digest(CONFIG / 'admission.json') != {
            key: by_name['tools/fixed_hosted_ci/admission.json'][key] for key in ('bytes', 'sha256')}:
        raise ValueError('CONTEXT_ADMISSION_PREIMAGE')
    for row in admission['control_sources']:
        if digest(CONFIG / row['path']) != {key: row[key] for key in ('bytes', 'sha256')}:
            raise ValueError('CONTROL_SOURCE_PIN')
    save('fixed-context-audit.json', report)
    return {'source_sha': report['source_sha'], 'manifest_sha256': manifest_sha,
            'audit_sha256': hashlib.sha256(raw).hexdigest(), 'file_count': len(expected),
            'before_foreign_install': True}


class GuardResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started_ids = []
        self.successful_ids = []
        self.timings = []

    def startTest(self, test):
        self.started_ids.append(test.id())
        self.start_wall = time.monotonic()
        self.start_cpu = time.process_time()
        self.start_utc = datetime.now(timezone.utc).isoformat()
        super().startTest(test)

    def addSuccess(self, test):
        self.successful_ids.append(test.id())
        super().addSuccess(test)

    def stopTest(self, test):
        self.timings.append({'id': test.id(), 'started_utc': self.start_utc,
                             'finished_utc': datetime.now(timezone.utc).isoformat(),
                             'elapsed_monotonic': time.monotonic() - self.start_wall,
                             'elapsed_process_cpu': time.process_time() - self.start_cpu})
        super().stopTest(test)


def case_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from case_ids(item)
        else:
            yield item.id()


def provenance_guards():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName('tools.test_label_ci_provenance')
    expected = tuple(case_ids(suite))
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2, resultclass=GuardResult).run(suite)
    report = {'scope': 'Six separate stdlib provenance guards; not549PG/99unit acceptance.',
              'expected_test_ids': list(GUARD_IDS), 'discovered_test_ids': list(expected),
              'started_test_ids': result.started_ids, 'successful_test_ids': result.successful_ids,
              'case_timing': result.timings, 'tests_run': result.testsRun,
              'failure_count': len(result.failures), 'error_count': len(result.errors),
              'skip_count': len(result.skipped), 'loader_error_count': len(loader.errors),
              'expected_failure_count': len(result.expectedFailures),
              'unexpected_success_count': len(result.unexpectedSuccesses),
              'log_sha256': hashlib.sha256(output.getvalue().encode()).hexdigest()}
    report['passed'] = bool(set(expected) == set(GUARD_IDS) and len(expected) == 6
                            and sorted(result.started_ids) == sorted(GUARD_IDS)
                            and sorted(result.successful_ids) == sorted(GUARD_IDS)
                            and result.testsRun == 6 and result.wasSuccessful()
                            and not any(report[key] for key in ('failure_count', 'error_count',
                                'skip_count', 'loader_error_count', 'expected_failure_count',
                                'unexpected_success_count')))
    save('fixed-provenance-guards.json', report)
    if not report['passed']:
        raise ValueError('PROVENANCE_GUARD_FAILURE')
    return report


def preflight():
    """No foreign package is imported until this function's final block."""
    admission = load(CONFIG / 'admission.json')
    if (sys.version_info[:3] != (3, 12, 16) or sys.implementation.name != 'cpython'
        or sys.platform != 'linux' or platform.machine() != 'x86_64' or os.getuid() != 10001
        or not sys.flags.no_site or not sys.flags.isolated or not sys.flags.ignore_environment
        or not sys.flags.dont_write_bytecode):
        raise ValueError('RUNTIME_IDENTITY_OR_STARTUP_FLAGS')
    if not re.fullmatch('[a-f0-9]{40}', os.environ.get('SS_CI_SOURCE_SHA', '')):
        raise ValueError('PUBLIC_CHECKOUT_IDENTITY')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    interfaces = {line.split(':', 1)[0].strip() for line in Path('/proc/net/dev').read_text().splitlines() if ':' in line}
    # Unlike ordinary-label network-none, real PG needs the host-verified
    # --internal Docker network. The host must bind/capture that network's ID.
    if (not {'lo'} <= interfaces or not interfaces <= {'lo', 'eth0'}
        or os.environ.get('SS_FIXED_HOSTED_INTERNAL_NETWORK') != '1'
        or int(status['CapEff'].strip(), 16) != 0 or status['NoNewPrivs'].strip() != '1'):
        raise ValueError('CONTAINER_NETWORK_OR_PRIVILEGE_BOUNDARY')
    if any(name in os.environ for name in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME')):
        raise ValueError('AMBIENT_LOADER_OVERRIDE')
    source = source_snapshot(admission)
    context = context_proof(admission)
    libc = os.confstr('CS_GNU_LIBC_VERSION')
    if not libc or not libc.startswith('glibc ') or tuple(map(int, libc.split()[1].split('.'))) < (2, 34):
        raise ValueError('GLIBC_REQUIREMENT')
    from tools import fixed_hosted_native_data
    base = fixed_hosted_native_data.base_native(admission)
    site = Path(sysconfig.get_path('purelib')).resolve(strict=True)
    if list(site.rglob('*.pth')):
        raise ValueError('STARTUP_HOOK_REFUSED')
    from tools import label_ci_provenance as provenance
    guard = provenance_guards()
    wheels = admission['wheels']
    if (len(wheels) != 20 or len({provenance.canonical(row['name']) for row in wheels}) != 20
        or {p.name for p in (CONFIG / 'wheels').iterdir()} != {row['filename'] for row in wheels}):
        raise ValueError('EXACT20_ARCHIVES')
    distributions = list(metadata.distributions(path=[str(site)]))
    allowed = {provenance.canonical(row['name']) for row in wheels} | {'pip'}
    if {provenance.canonical(dist.metadata['Name']) for dist in distributions} != allowed:
        raise ValueError('UNEXPECTED_INSTALLED_DISTRIBUTION')
    verified = {}
    installs = {}
    packages = []
    for pin in wheels:
        expected_extras = ['binary', 'pool'] if provenance.canonical(pin['name']) == 'psycopg' else []
        if pin['yanked'] or (pin['extras'] or []) != expected_extras:
            raise ValueError('WHEEL_SELECTION_POLICY')
        wheel = provenance.read_wheel(CONFIG / 'wheels' / pin['filename'], pin)
        owners = [dist for dist in distributions if provenance.canonical(dist.metadata['Name']) == wheel.name]
        if len(owners) != 1:
            raise ValueError('DISTRIBUTION_OWNERSHIP')
        installed = provenance.read_install(owners[0], site, wheel)
        verified[wheel.name] = wheel
        installs[wheel.name] = installed
        packages.append({'name': wheel.name, 'version': wheel.version,
                         'wheel_sha256': pin['sha256'], 'wheel_record_sha256': wheel.record_sha256,
                         'installed_record_sha256': installed.record_sha256})
    assets = [provenance.verify_asset(pin, verified[provenance.canonical(pin['distribution'])],
                                     installs[provenance.canonical(pin['distribution'])])
              for pin in admission['installed_asset_pins']]
    expected_assets = {(name, path) for name, wheel in verified.items()
                       for path in wheel.rows if path != wheel.record_path}
    actual_assets = [(row['distribution'], row['path']) for row in assets]
    if (len(expected_assets) != 658 or len({path for _, path in expected_assets}) != 658
        or len(actual_assets) != 658 or len(set(actual_assets)) != 658
        or set(actual_assets) != expected_assets):
        raise ValueError('EXACT658_INSTALLED_RECORD_ASSETS')
    native = []
    for pin in admission['native_assets']:
        raw = provenance.raw_file(site / pin['path'])
        if (len(raw) != pin['bytes'] or hashlib.sha256(raw).hexdigest() != pin['sha256']
            or raw[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', raw, 18)[0] != 62):
            raise ValueError('NATIVE_ARCHITECTURE_OR_PREIMAGE')
        native.append({'path': pin['path'], 'bytes': len(raw), 'sha256': pin['sha256']})
    if len(native) != 21 or len({row['path'] for row in native}) != 21:
        raise ValueError('EXACT21_NATIVE_ASSETS')
    # Exact installed bytes bind reviewed ELF RPATH/NEEDED/bundled-version data;
    # each required base export has independently been read above without ldd.
    report = {'format': 'stock-and-sleeve:fixed-hosted-preimport-proof:v1',
              'passed': True, 'foreign_import_started': False,
              'runtime': {'python': platform.python_version(), 'machine': platform.machine(),
                          'glibc': libc, 'uid': os.getuid(), 'interfaces': sorted(interfaces)},
              'context': context, 'source_count': len(source), 'wheels': packages,
              'installed_assets': assets, 'native_assets': native, 'base_exports': base,
              'provenance_guards': guard,
              'native_analysis_sha256': admission['native_analysis_sha256'],
              'active_metadata_graph_sha256': admission['active_metadata_graph_sha256']}
    save('fixed-preimport-proof.json', report)
    # Site startup remains disabled. Enable only the verified roots after proof.
    sys.path.append(str(site))
    if os.environ.get('PSYCOPG_IMPL') != 'binary':
        raise ValueError('EXPLICIT_BINARY_DRIVER_REQUIRED')
    import psycopg
    from psycopg import pq
    if psycopg.__version__ != '3.3.9' or pq.__impl__ != 'binary':
        raise ValueError('FIXED_BINARY_DRIVER_REQUIRED')
    save('fixed-driver-runtime.json', {'version': psycopg.__version__,
         'implementation': pq.__impl__, 'libpq_version': pq.version(),
         'preimport_proof_sha256': digest(EVIDENCE / 'fixed-preimport-proof.json')['sha256']})
    return admission, source
