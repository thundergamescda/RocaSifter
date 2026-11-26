"""Real temporary ZIP/RECORD and installed dist-info fixtures; no import mocks.

Separate six-case stdlib selection. These are not six additional label cases.
"""
import copy
import csv
import hashlib
from importlib.metadata import PathDistribution
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools import label_ci_provenance as provenance


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='label-provenance-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = self.root / 'site'
        self.site.mkdir()
        self.serial = 0

    def fixture(self, name='owner-a'):
        self.serial += 1
        package = name.replace('-', '_')
        info = package + '-1.0.dist-info'
        payload = b'actual byte-valid fixture asset'
        files = {package + '/asset.bin': payload,
                 info + '/METADATA': ('Metadata-Version: 2.1\nName: ' + name + '\nVersion: 1.0\n').encode(),
                 info + '/WHEEL': b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
        record = info + '/RECORD'
        output = io.StringIO(newline='')
        writer = csv.writer(output, lineterminator='\n')
        for path, raw in files.items():
            writer.writerow((path, provenance.record_hash(raw), len(raw)))
        writer.writerow((record, '', ''))
        files[record] = output.getvalue().encode()
        archive_path = self.root / (name + '-' + str(self.serial) + '.whl')
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path, raw in files.items():
                archive.writestr(path, raw)
        for path, raw in files.items():
            installed = self.site / path
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(raw)
        raw = archive_path.read_bytes()
        expected = {'name': name, 'version': '1.0', 'bytes': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest(), 'record_path': record,
                    'record_sha256': hashlib.sha256(files[record]).hexdigest(),
                    'metadata_path': info + '/METADATA'}
        self.last_archive, self.last_expected = archive_path, expected
        wheel = provenance.read_wheel(archive_path, expected)
        distribution = PathDistribution(self.site / info)
        pin = {'distribution': name, 'version': '1.0', 'path': package + '/asset.bin',
               'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
               'wheel_record_sha256': wheel.record_sha256}
        return wheel, distribution, pin

    def installed(self, wheel, distribution):
        return provenance.read_install(distribution, self.site, wheel)

    def rewrite_record(self, wheel, change):
        path = self.site / wheel.record_path
        rows = list(csv.reader(io.StringIO(path.read_text())))
        change(rows)
        with path.open('w', encoding='utf-8', newline='') as stream:
            csv.writer(stream, lineterminator='\n').writerows(rows)

    def test_valid_owner_matches_both_wheel_and_installed_records(self):
        for newline in ('LF', 'CRLF'):
            with self.subTest(installed_csv_newline=newline):
                wheel, distribution, pin = self.fixture()
                record = self.site / wheel.record_path
                if newline == 'CRLF':
                    record.write_bytes(record.read_bytes().replace(b'\n', b'\r\n'))
                actual = provenance.verify_asset(pin, wheel, self.installed(wheel, distribution))
                self.assertEqual((actual['distribution'], actual['version'], actual['sha256']),
                                 ('owner-a', '1.0', pin['sha256']))
                self.assertTrue(actual['record_ownership_verified'])
                self.assertEqual(actual['installed_record_sha256'], hashlib.sha256(record.read_bytes()).hexdigest())

    def test_same_site_root_does_not_confer_distribution_ownership(self):
        wheel, distribution, pin = self.fixture()
        _, _, other = self.fixture('owner-b')
        wrong = pin | {'path': other['path']}
        # The old common-root locate/hash check accepts these exact bytes.
        self.assertEqual(hashlib.sha256(Path(distribution.locate_file(other['path'])).read_bytes()).hexdigest(), pin['sha256'])
        with self.assertRaisesRegex(provenance.ProvenanceError, 'ASSET_RECORD_MEMBERSHIP'):
            provenance.verify_asset(wrong, wheel, self.installed(wheel, distribution))

    def test_missing_wheel_or_installed_record_membership_is_refused(self):
        wheel, distribution, pin = self.fixture()
        with self.subTest(record='wheel'):
            other = 'unrecorded/asset.bin'
            (self.site / other).parent.mkdir()
            (self.site / other).write_bytes((self.site / pin['path']).read_bytes())
            with self.assertRaisesRegex(provenance.ProvenanceError, 'ASSET_RECORD_MEMBERSHIP'):
                provenance.verify_asset(pin | {'path': other}, wheel, self.installed(wheel, distribution))
        with self.subTest(record='installed'):
            self.rewrite_record(wheel, lambda rows: rows.__setitem__(slice(None), [r for r in rows if r[0] != pin['path']]))
            with self.assertRaisesRegex(provenance.ProvenanceError, 'ASSET_RECORD_MEMBERSHIP'):
                provenance.verify_asset(pin, wheel, self.installed(wheel, distribution))

    def test_record_hash_and_size_mismatches_are_refused(self):
        for field in ('hash', 'size'):
            with self.subTest(field=field):
                wheel, distribution, pin = self.fixture()
                def changed(rows):
                    row = next(row for row in rows if row[0] == pin['path'])
                    row[1 if field == 'hash' else 2] = provenance.record_hash(b'wrong') if field == 'hash' else str(pin['bytes'] + 1)
                self.rewrite_record(wheel, changed)
                with self.assertRaisesRegex(provenance.ProvenanceError, 'ASSET_RECORD_HASH_OR_SIZE'):
                    provenance.verify_asset(pin, wheel, self.installed(wheel, distribution))
        for field in ('hash', 'size'):
            with self.subTest(wheel_record=field):
                wheel, _, pin = self.fixture()
                with zipfile.ZipFile(self.last_archive) as archive:
                    files = {name: archive.read(name) for name in archive.namelist()}
                rows = list(csv.reader(io.StringIO(files[wheel.record_path].decode())))
                row = next(row for row in rows if row[0] == pin['path'])
                row[1 if field == 'hash' else 2] = provenance.record_hash(b'wrong') if field == 'hash' else str(pin['bytes'] + 1)
                output = io.StringIO(newline='')
                csv.writer(output, lineterminator='\n').writerows(rows)
                files[wheel.record_path] = output.getvalue().encode()
                with zipfile.ZipFile(self.last_archive, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    for name, raw in files.items():
                        archive.writestr(name, raw)
                raw = self.last_archive.read_bytes()
                expected = self.last_expected | {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                    'record_sha256': hashlib.sha256(files[wheel.record_path]).hexdigest()}
                with self.assertRaisesRegex(provenance.ProvenanceError, 'WHEEL_RECORD_CONTENT'):
                    provenance.read_wheel(self.last_archive, expected)

    def test_unsafe_or_symlink_escape_is_refused(self):
        wheel, distribution, pin = self.fixture()
        installed = self.installed(wheel, distribution)
        for path in ('../outside', '/outside', 'owner_a/../outside', 'owner_a\\asset.bin'):
            with self.subTest(path=path), self.assertRaisesRegex(provenance.ProvenanceError, 'UNSAFE_ASSET_PATH'):
                provenance.verify_asset(pin | {'path': path}, wheel, installed)
        outside = self.root / 'outside.bin'
        source = self.site / pin['path']
        outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
        with self.assertRaisesRegex(provenance.ProvenanceError, 'UNSAFE_RESOLVED_PATH'):
            provenance.verify_asset(pin, wheel, installed)

    def test_distribution_identity_and_duplicate_records_are_refused(self):
        wheel, distribution, pin = self.fixture()
        installed = self.installed(wheel, distribution)
        for change in ({'distribution': 'owner-b'}, {'version': '2.0'}):
            with self.subTest(change=change), self.assertRaisesRegex(provenance.ProvenanceError, 'ASSET_OWNER_IDENTITY'):
                provenance.verify_asset(pin | change, wheel, installed)
        self.rewrite_record(wheel, lambda rows: rows.append(copy.copy(rows[0])))
        with self.assertRaisesRegex(provenance.ProvenanceError, 'INSTALLED_RECORD_MEMBERSHIP'):
            self.installed(wheel, distribution)
