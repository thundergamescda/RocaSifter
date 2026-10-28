"""Fictional package/Windows installer tests; never launch an executable or install outside temp."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

from tools import package_preview as package


def vendor_fixture(root, program):
    """A pinned wheel RECORD/SBOM fixture, separate from the copied payload."""
    records = []
    installed_root = root / 'installed-vendor'
    for index, (bundled, installed) in enumerate(package.VENDOR_SBOMS.items()):
        content = json.dumps({'bomFormat': 'CycloneDX', 'fixture_component': index}).encode()
        original = installed_root / installed
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(content)
        target = program / bundled
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

        class Record(str):
            pass

        record = Record(installed)
        record.size = len(content)
        record.hash = SimpleNamespace(mode='sha256', value=base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip('='))
        records.append(record)
    return SimpleNamespace(version='50.0.2', files=records, locate_file=lambda record: installed_root / str(record))


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'fictional.py').write_text('# fixture only; never executed\n')
        self.program = self.root / 'program'
        (self.program / '_internal/assets').mkdir(parents=True)
        (self.program / 'StockAndSleeve.exe').write_bytes(b'Fictional bytes: never execute')
        (self.program / '_internal/assets/stock-and-sleeve.ico').write_bytes(b'fictional-icon')
        (self.program / '_internal/runtime.dll').write_bytes(b'fictional-library')
        self.metadata()
        self.output = self.root / 'delivery/pilot.zip'

    def metadata(self):
        inputs = package.build_input_hashes(self.source)
        identity = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        value = {'format_version': 1, 'channel': 'private-pilot', 'build_id': identity, 'input_sha256': inputs}
        (self.program / '_internal/desktop_build.json').write_text(json.dumps(value))
        return value

    def build(self, **kwargs):
        return package.package_preview(self.program, self.output, source_root=self.source, **kwargs)

    def extracted(self):
        self.build()
        extracted = self.root / 'extracted'
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(extracted)
        return extracted

    def test_explicit_evidence_only_and_verified_output(self):
        evidence = self.root / 'evidence'
        evidence.mkdir()
        (evidence / 'automated-summary.json').write_text('{"fixture":true}')
        (evidence / 'private-merchant.log').write_text('must never be read or copied')
        result = self.build(evidence_directory=evidence, evidence=['automated-summary.json'])
        self.assertEqual(result['evidence_files'], 1)
        self.assertEqual(result['program_files'], 4)
        self.assertEqual(result['sha256'], package.digest_file(self.output))
        self.assertEqual(package.verify_archive(self.output)['program_files'], 4)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual([n for n in archive.namelist() if n.startswith('Test results/')],
                             ['Test results/automated-summary.json'])
            instructions = archive.read('READ ME FIRST.txt').decode()
            self.assertIn('invited online shared', instructions)
            self.assertIn('under development', instructions)
            self.assertNotIn('TestProfile-2.6.0', instructions)

    def test_unreviewed_or_nested_evidence_rejected(self):
        for name in ('private.log', '../automated-summary.json', 'nested/automated-summary.json'):
            with self.subTest(name=name), self.assertRaises(package.PackageError):
                self.build(evidence_directory=self.root, evidence=[name])
        self.assertFalse(self.output.exists())

    def test_stale_source_or_tampered_build_record_rejected(self):
        (self.source / 'fictional.py').write_text('# newer source\n')
        with self.assertRaisesRegex(package.PackageError, 'differ'):
            self.build()
        value = self.metadata()
        value['build_id'] = '0' * 64
        (self.program / '_internal/desktop_build.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(package.PackageError, 'identity'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_source_record_path_escape_rejected(self):
        value = {'format_version': 1, 'channel': 'private-pilot', 'input_sha256': {'../merchant.py': '0' * 64}}
        value['build_id'] = hashlib.sha256(json.dumps(value['input_sha256'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        (self.program / '_internal/desktop_build.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(package.PackageError, 'Unsafe'):
            self.build()

    def test_tampered_bundled_asset_rejected_before_archive(self):
        (self.source / 'station.js').write_text('/* fixture script, never executed */')
        self.metadata()
        (self.program / '_internal/station.js').write_text('/* changed bundled script */')
        with self.assertRaisesRegex(package.PackageError, 'bundled asset'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_exact_pinned_vendor_sboms_are_verified_and_packaged(self):
        distribution = vendor_fixture(self.root, self.program)
        with patch.object(package.metadata, 'distribution', return_value=distribution):
            result = self.build()
        self.assertEqual(result['program_files'], 6)
        with zipfile.ZipFile(self.output) as archive:
            hashes = json.loads(archive.read('ProgramSHA256.json'))
            for name, original in package.VENDOR_SBOMS.items():
                self.assertEqual(archive.read('StockAndSleeve/' + name), distribution.locate_file(original).read_bytes())
                self.assertEqual(hashes[name], package.digest_file(distribution.locate_file(original)))
        self.assertEqual(package.verify_archive(self.output)['program_files'], 6)

    def test_vendor_copy_tamper_and_matching_installed_tamper_are_rejected(self):
        distribution = vendor_fixture(self.root, self.program)
        name, installed = next(iter(package.VENDOR_SBOMS.items()))
        target, original = self.program / name, distribution.locate_file(installed)
        original_bytes = original.read_bytes()
        with patch.object(package.metadata, 'distribution', return_value=distribution):
            target.write_bytes(original_bytes + b' ')
            with self.assertRaisesRegex(package.PackageError, 'differs from'):
                self.build()
            # Comparing two copies alone is insufficient: the installed RECORD
            # must reject a changed installed source even if the bundle matches.
            original.write_bytes(original_bytes + b' ')
            with self.assertRaisesRegex(package.PackageError, 'RECORD integrity'):
                self.build()
        self.assertFalse(self.output.exists())

    def test_vendor_pair_and_installed_pinned_version_are_required(self):
        distribution = vendor_fixture(self.root, self.program)
        with patch.object(package.metadata, 'distribution', return_value=distribution):
            distribution.version = '50.0.3'
            with self.assertRaisesRegex(package.PackageError, 'pinned SBOM version'):
                self.build()
            distribution.version = '50.0.2'
            (self.program / next(iter(package.VENDOR_SBOMS))).unlink()
            with self.assertRaisesRegex(package.PackageError, 'pair is incomplete'):
                self.build()

    def test_vendor_exception_does_not_allow_other_json_or_versions(self):
        distribution = vendor_fixture(self.root, self.program)
        for name in ('_internal/cryptography-50.0.2.dist-info/sboms/customer.json',
                     '_internal/cryptography-50.0.3.dist-info/sboms/sbom.json',
                     '_internal/cryptography-50.0.2.dist-info/other/sbom.json'):
            with self.subTest(name=name):
                target = self.program / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{"fixture":"unapproved payload"}')
                with patch.object(package.metadata, 'distribution', return_value=distribution):
                    with self.assertRaisesRegex(package.PackageError, 'Unapproved JSON'):
                        self.build()
                target.unlink()
        self.assertFalse(self.output.exists())

    def test_archive_manifest_cannot_authorize_arbitrary_vendor_json(self):
        self.build()
        altered = self.root / 'unapproved-json.zip'
        name = '_internal/cryptography-50.0.2.dist-info/sboms/customer.json'
        content = b'{"fixture":"unapproved despite a matching manifest hash"}'
        with zipfile.ZipFile(self.output) as original, zipfile.ZipFile(altered, 'w') as target:
            for existing in original.namelist():
                data = original.read(existing)
                if existing == 'ProgramSHA256.json':
                    value = json.loads(data)
                    value[name] = hashlib.sha256(content).hexdigest()
                    data = json.dumps(value).encode()
                target.writestr(existing, data)
            target.writestr('StockAndSleeve/' + name, content)
        with self.assertRaisesRegex(package.PackageError, 'Unapproved JSON'):
            package.verify_archive(altered)

    def test_nested_case_insensitive_data_and_credentials_rejected(self):
        for name in ('_internal/nested/STATION_DATA/customer.json', '_internal/MerchantData/accounts.json',
                     '_internal/nested/.ENV.production', '_internal/nested/cloud.pending',
                     '_internal/nested/ledger.SQLITE3', '_internal/nested/receipt.csv',
                     '_internal/nested/customer.json', '_internal/credentials.json'):
            with self.subTest(name=name):
                file = self.program / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('fictional forbidden payload')
                with self.assertRaises(package.PackageError):
                    self.build()
                file.unlink()
                # Empty forbidden directories must also be rejected, so remove owned parents.
                while file.parent != self.program / '_internal' and not any(file.parent.iterdir()):
                    directory = file.parent
                    directory.rmdir()
                    file = directory
        self.assertFalse(self.output.exists())

    def test_windows_aliases_and_path_escape_rejected(self):
        for name in ('/outside', '../outside', 'a/../outside', 'C:/outside', 'a\\outside',
                     'a//outside', 'a/CON.txt', 'a/file.', 'a/file ', 'a/file:stream'):
            with self.subTest(name=name), self.assertRaises(package.PackageError):
                package.safe_relative(name)

    def test_program_mutation_while_copying_rejected(self):
        original = zipfile.ZipFile.write
        changed = False

        def change_after_copy(archive, file, *args, **kwargs):
            nonlocal changed
            result = original(archive, file, *args, **kwargs)
            if Path(file).name == 'StockAndSleeve.exe' and not changed:
                changed = True
                Path(file).write_bytes(b'changed during packaging')
            return result

        with patch.object(zipfile.ZipFile, 'write', change_after_copy):
            with self.assertRaisesRegex(package.PackageError, 'changed during'):
                self.build()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.output.parent.glob('*.tmp')))

    def test_tampered_archive_and_extra_payload_rejected(self):
        self.build()
        for extra in (False, True):
            altered = self.root / ('altered-' + str(extra) + '.zip')
            with zipfile.ZipFile(self.output) as original, zipfile.ZipFile(altered, 'w') as target:
                for name in original.namelist():
                    data = original.read(name)
                    if not extra and name == 'StockAndSleeve/StockAndSleeve.exe':
                        data = b'tampered after packaging'
                    target.writestr(name, data)
                if extra:
                    target.writestr('StockAndSleeve/_internal/station_data/customer.json', 'forbidden')
            with self.subTest(extra=extra), self.assertRaises(package.PackageError):
                package.verify_archive(altered)

    def test_existing_artifact_is_preserved(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b'previous reviewed output')
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertEqual(self.output.read_bytes(), b'previous reviewed output')


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows installer helpers')
class WindowsInstallerTests(unittest.TestCase):
    setUp = PackageTests.setUp
    metadata = PackageTests.metadata
    build = PackageTests.build
    extracted = PackageTests.extracted
    def powershell(self, body, *, success=True):
        helper = package.ROOT / 'tools/pilot_installer.ps1'
        harness = self.root / ('harness-' + str(len(list(self.root.glob('harness-*')))) + '.ps1')
        harness.write_text("$ErrorActionPreference='Stop'\n. '" + str(helper).replace("'", "''") + "'\n" + body, encoding='utf-8-sig')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
                                capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    @staticmethod
    def literal(path):
        return "'" + str(path).replace("'", "''") + "'"

    def test_installer_rejects_tampered_payload_before_install(self):
        extracted = self.extracted()
        (extracted / 'StockAndSleeve/StockAndSleeve.exe').write_bytes(b'tampered')
        result = self.powershell('Install-SharedPilot -PackageRoot ' + self.literal(extracted) + ' -ValidateOnly', success=False)
        self.assertIn('Package verification failed', result.stderr)
        self.assertFalse((self.root / 'profiles').exists())

    def test_installer_accepts_exact_sbom_pair_and_rejects_tamper_or_extra_json(self):
        distribution = vendor_fixture(self.root, self.program)
        with patch.object(package.metadata, 'distribution', return_value=distribution):
            extracted = self.extracted()
        call = 'Install-SharedPilot -PackageRoot ' + self.literal(extracted) + ' -ValidateOnly | ConvertTo-Json'
        verified = json.loads(self.powershell(call).stdout)
        self.assertTrue(verified['verified'])
        name = next(iter(package.VENDOR_SBOMS))
        target = extracted / 'StockAndSleeve' / name
        original = target.read_bytes()
        target.write_bytes(original + b' ')
        result = self.powershell(call, success=False)
        self.assertIn('Package verification failed', result.stderr)
        target.write_bytes(original)
        extra = target.parent / 'customer.json'
        extra.write_text('{"fixture":"never install"}')
        result = self.powershell(call, success=False)
        self.assertIn('Unapproved JSON data', result.stderr)

    def test_installer_post_copy_hash_check_rejects_changed_copy(self):
        extracted = self.extracted()
        destination = self.root / 'copy'
        body = "$script:originalCheck=${function:Test-PilotPayload}\nfunction Test-PilotPayload($Root,$Expected) {\n"
        body += 'if ($Root -eq ' + self.literal(destination) + ") { [IO.File]::WriteAllText((Join-Path $Root 'StockAndSleeve.exe'),'changed after copy') }\n"
        body += '& $script:originalCheck $Root $Expected\n}\n'
        body += '$expected=Get-Content -LiteralPath ' + self.literal(extracted / 'ProgramSHA256.json') + ' -Raw | ConvertFrom-Json\n'
        body += 'Copy-PilotPayload ' + self.literal(extracted / 'StockAndSleeve') + ' ' + self.literal(destination) + ' $expected'
        result = self.powershell(body, success=False)
        self.assertIn('Package verification failed', result.stderr)
        self.assertFalse((self.root / 'Start Pilot.vbs').exists())

    def test_installer_rejects_path_escape_and_nested_unlisted_data(self):
        extracted = self.extracted()
        record = extracted / 'ProgramSHA256.json'
        expected = json.loads(record.read_text())
        expected['../outside.exe'] = expected.pop('StockAndSleeve.exe')
        record.write_text(json.dumps(expected))
        result = self.powershell('Install-SharedPilot -PackageRoot ' + self.literal(extracted) + ' -ValidateOnly', success=False)
        self.assertIn('Unsafe package path', result.stderr)
        nested = extracted / 'StockAndSleeve/_internal/nested/customer.json'
        nested.parent.mkdir()
        nested.write_text('{"fictional":"must not install"}')
        result = self.powershell('Install-SharedPilot -PackageRoot ' + self.literal(extracted) + ' -ValidateOnly', success=False)
        self.assertIn('Unapproved JSON data', result.stderr)

    def test_junction_payload_rejected_without_following_target(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'fictional.txt').write_text('outside selected program')
        junction = self.program / '_internal/linked'
        self.powershell('New-Item -ItemType Junction -Path ' + self.literal(junction) + ' -Target ' + self.literal(outside) + ' | Out-Null')
        with self.assertRaisesRegex(package.PackageError, 'junction'):
            self.build()
        body = 'Get-PilotFiles ' + self.literal(self.program)
        result = self.powershell(body, success=False)
        self.assertIn('junctions', result.stderr)

    def test_unique_profiles_ports_start_stop_and_preserved_shortcuts(self):
        extracted = self.extracted()
        program_base, profile_base = self.root / 'installed', self.root / 'pilot-profiles'
        folders = [self.root / 'desktop', self.root / 'start-menu']
        preserved = {}
        for folder in folders:
            folder.mkdir()
            for name in ('Stock & Sleeve.lnk', 'Stock & Sleeve Test.lnk',
                         'Stock & Sleeve Pilot.lnk', 'Stop Stock & Sleeve Pilot.lnk'):
                file = folder / name
                file.write_bytes(('unrelated existing ' + name).encode())
                preserved[file] = file.read_bytes()
        call = ('Install-SharedPilot -PackageRoot ' + self.literal(extracted) + ' -ProgramBase ' + self.literal(program_base)
                + ' -ProfileBase ' + self.literal(profile_base) + ' -ShortcutFolders @('
                + ','.join(self.literal(p) for p in folders) + ')')
        body = '$first=' + call + '\n$second=' + call + '\n'
        body += '$s=New-Object -ComObject WScript.Shell\n$l=$s.CreateShortcut($first.shortcuts[0])\n'
        body += '@{first=$first;second=$second;link=@{target=$l.TargetPath;args=$l.Arguments;icon=$l.IconLocation}} | ConvertTo-Json -Depth 8'
        result = json.loads(self.powershell(body).stdout)
        first, second = result['first'], result['second']
        self.assertNotEqual(first['profile'], second['profile'])
        self.assertNotEqual(first['program'], second['program'])
        self.assertNotEqual(first['port'], second['port'])
        self.assertNotIn(first['port'], (8765, 8876))
        self.assertEqual(json.loads((Path(first['profile']) / 'pilot-install.json').read_text(encoding='utf-8-sig'))['port'], first['port'])
        start, stop = Path(first['start']).read_text(encoding='utf-16'), Path(first['stop']).read_text(encoding='utf-16')
        for content in (start, stop):
            self.assertIn(first['profile'], content)
            self.assertIn('ROCA_STATION_PORT")="' + str(first['port']), content)
        self.assertNotIn('--stop', start)
        self.assertIn('--stop', stop)
        self.assertIn(first['start'], result['link']['args'])
        self.assertIn('stock-and-sleeve.ico', result['link']['icon'])
        for file, original in preserved.items():
            self.assertEqual(file.read_bytes(), original)
        self.assertTrue(all('(' + first['tag'] + ')' in p for p in first['shortcuts']))

    def test_port_selection_skips_active_and_reserved_ports(self):
        with socket.socket() as occupied, socket.socket() as available, socket.socket() as another:
            for listener in (occupied, available, another):
                listener.bind(('127.0.0.1', 0))
            used, free, next_free = occupied.getsockname()[1], available.getsockname()[1], another.getsockname()[1]
            available.close()
            another.close()
            body = '$a=Reserve-PilotPort ' + self.literal(self.root / 'ports') + ' -Candidates @(' + str(used) + ',' + str(free) + ')\n'
            body += '$b=Reserve-PilotPort ' + self.literal(self.root / 'ports') + ' -Candidates @(' + str(free) + ',' + str(next_free) + ')\n'
            body += '@($a,$b) | ConvertTo-Json'
            self.assertEqual(json.loads(self.powershell(body).stdout), [free, next_free])
            self.assertFalse((self.root / 'ports/.ports' / str(used)).exists())


if __name__ == '__main__':
    unittest.main()
