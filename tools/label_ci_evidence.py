"""Exact portable-label CI evidence; no app, provider or device acceptance.

Source-only candidate. The host wrapper owns admission, immutable image identity,
network isolation, resource limits, process teardown and framed evidence delivery.
All preflight below is stdlib/data-only and precedes foreign-package imports.
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
CONFIG = Path('/opt/label-ci')
EVIDENCE = Path('/ci-evidence')
PROJECT = (
    'hosted_labels.py', 'station_receiving_label_plan.py',
    'station_receiving_label_render.py', 'station_receiving_label_worker.py',
    'test_receiving_label_plan.py', 'test_receiving_label_render.py',
    'test_receiving_label_worker.py',
)
SOURCE = PROJECT + ('tools/label_ci_evidence.py', 'tools/label_ci_admission.json',
                    'tools/label_ci_provenance.py', 'tools/test_label_ci_provenance.py')
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
    if name not in {'label-summary.json', 'source-before.json', 'source-after.json',
                    'dependency-preflight.json', 'case-results.json',
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


def preflight(admission):
    if (sys.version_info[:3] != (3, 12, 16) or sys.implementation.name != 'cpython'
        or sys.platform != 'linux' or platform.machine() != 'x86_64' or os.getuid() != 10001):
        raise ValueError('RUNTIME_PLATFORM')
    libc = os.confstr('CS_GNU_LIBC_VERSION')
    if not libc or not libc.startswith('glibc ') or tuple(map(int, libc.split()[1].split('.'))) < (2, 28):
        raise ValueError('GLIBC_VERSION')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    interfaces = {line.split(':', 1)[0].strip() for line in Path('/proc/net/dev').read_text().splitlines() if ':' in line}
    if interfaces != {'lo'} or int(status['CapEff'].strip(), 16) != 0 or status['NoNewPrivs'].strip() != '1':
        raise ValueError('CONTAINER_BOUNDARY')
    native = base_native(admission)  # Must precede package import/test execution.
    site = Path(sysconfig.get_path('purelib'))
    if not sys.flags.no_site or list(site.glob('*.pth')):
        raise ValueError('SITE_STARTUP_HOOK_REFUSED')
    # This reviewed project module and its six real stdlib fixture tests run
    # while site startup remains suppressed, before any foreign import.
    from tools import label_ci_provenance as provenance
    guard_report = run_guard_suite(admission)
    wheels = admission['wheels']
    if len(wheels) != 5 or len({provenance.canonical(r['name']) for r in wheels}) != 5:
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
    # The actual site location is enabled only after native checks, independent
    # wheel/installed ownership, every file pin and guard regression success.
    sys.path.append(str(site))
    return {'platform': sys.platform, 'python': platform.python_version(), 'machine': platform.machine(),
            'uid': os.getuid(), 'glibc': libc, 'network_interfaces': sorted(interfaces),
            'base_native_data': native, 'wheels': packages, 'installed_assets': assets,
            'pdfium_build': pdfium_version, 'provenance_guards': guard_report,
            'native_preflight': 'ELF data/symbol comparison only; no ldd or package import',
            'test_only_decoder_graph': True}


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
                  and not list(Path('/tmp/label-tests').iterdir()))
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
    temporary = list(Path('/tmp/label-tests').iterdir())
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


def main():
    summary = {'format': 'stock-and-sleeve:portable-label-ci:v1', 'scope': 'Linux renderer/worker only',
               'source_sha': os.environ.get('SS_LABEL_SOURCE_SHA'), 'started': False, 'successful': False,
               'expected_count': 40, 'status': 'PREFLIGHT_FAILURE', 'hardware_acceptance': False,
               'separate_guard_count': 6, 'guard_successful': False,
               'windows_acceptance': False, 'ordinary_receipt_publication_acceptance': False}
    before = None
    result = None
    started = time.monotonic()
    try:
        if len(sys.argv) != 1 or not re.fullmatch(r'[a-f0-9]{40}', summary['source_sha'] or ''):
            raise ValueError('FIXED_INVOCATION')
        admission = json.loads(regular(ROOT / 'tools/label_ci_admission.json', LIMIT))
        if admission['execution_hold'] is not None:
            raise ValueError('EXECUTION_HOLD')
        Path('/tmp/label-tests').mkdir(mode=0o700)
        before = sources(admission)
        write('source-before.json', before)
        sys.path.insert(0, str(ROOT))
        checked = preflight(admission)
        write('dependency-preflight.json', checked)
        summary['guard_successful'] = checked['provenance_guards']['successful']
        expected = admission['expected_test_ids']
        if len(expected) != 40 or len(set(expected)) != 40 or admission['expected_test_count'] != 40:
            raise ValueError('TEST_SELECTION')
        with (EVIDENCE / 'label-tests.log').open('x', encoding='utf-8', newline='\n') as stream:
            log = BoundedLog(stream)
            with redirect_stdout(log), redirect_stderr(log):
                loader = unittest.TestLoader()
                suite = loader.loadTestsFromNames(expected)
                for error in loader.errors:
                    log.write(error)
                if sorted(t.id() for t in tests(suite)) != sorted(expected):
                    raise ValueError('DISCOVERY_MISMATCH')
                summary['started'] = True
                result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=Results).run(suite)
        summary['status'] = 'TEST_FAILURE'
        summary['tests_run'] = result.testsRun
        summary['cleanup'] = cleanup_state()
        write('case-results.json', [result.rows[key] for key in sorted(result.rows)])
        summary['successful'] = (result.wasSuccessful() and result.testsRun == 40
            and set(result.rows) == set(expected) and all(r['status'] == 'PASS' for r in result.rows.values())
            and not result.skipped and not result.expectedFailures and not result.unexpectedSuccesses
            and summary['cleanup']['complete'])
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
        write('label-summary.json', summary)
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
