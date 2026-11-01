"""Reviewed legal texts are build inputs, not a general package-data exception."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import zipfile

from distribution_notices import validate_notice_payload,REVIEWED_MANIFEST_SHA256
import test_package_preview as fixtures
from tools.build_desktop import build_command

ROOT=Path(__file__).resolve().parent


class ReviewedNoticePackagingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.PackageTests('test_explicit_evidence_only_and_verified_output')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root
        self.reviewed=validate_notice_payload(ROOT/'third_party_notices')
        for relative,source in self.reviewed.items():
            for folder in (self.fixture.source/'third_party_notices',self.fixture.program/'_internal/third_party_notices'):
                target=folder/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        self.fixture.metadata()

    def test_complete_exact_notice_set_is_bound_to_build_and_verified_archive(self):
        inputs=fixtures.package.build_input_hashes(self.fixture.source)
        self.assertEqual({name.removeprefix('third_party_notices/') for name in inputs if name.startswith('third_party_notices/')},set(self.reviewed))
        self.assertEqual(inputs['third_party_notices/source-manifest.json'],REVIEWED_MANIFEST_SHA256)
        result=self.fixture.build()
        self.assertEqual(result['program_files'],146)
        self.assertEqual(fixtures.package.verify_archive(self.fixture.output)['program_files'],146)
        with zipfile.ZipFile(self.fixture.output) as archive:
            names={name.removeprefix('StockAndSleeve/') for name in archive.namelist() if name.startswith('StockAndSleeve/')}
            self.assertEqual({name.removeprefix('_internal/third_party_notices/') for name in names if name.startswith('_internal/third_party_notices/')},set(self.reviewed))
            self.assertFalse(any('LOCAL_PREPARATION_PROVENANCE' in name for name in archive.namelist()))
        self.assertTrue(any(value.endswith(':third_party_notices') for value in build_command()))

    def test_missing_or_changed_bundled_notice_rejects_before_output(self):
        relative=next(name for name in self.reviewed if name!='source-manifest.json')
        path=self.fixture.program/'_internal/third_party_notices'/relative;original=path.read_bytes()
        for tamper in ('missing','bytes'):
            with self.subTest(tamper=tamper):
                if tamper=='missing':path.unlink()
                else:path.write_bytes(original+b'\nFictional corruption')
                with self.assertRaisesRegex(fixtures.package.PackageError,'notices'):self.fixture.build()
                self.assertFalse(self.fixture.output.exists());path.write_bytes(original)

    def test_extra_notice_text_is_not_read_or_authorized_by_manifest_exception(self):
        unknown=self.fixture.program/'_internal/third_party_notices/private-neighbor.txt'
        unknown.write_text('FICTIONAL-SENTINEL-NEVER-READ',encoding='utf-8')
        original=Path.read_bytes
        def guarded(path):
            if path==unknown:raise AssertionError('Unapproved neighbor content was read')
            return original(path)
        with patch.object(Path,'read_bytes',guarded),self.assertRaisesRegex(fixtures.package.PackageError,'notices'):
            self.fixture.build()
        self.assertFalse(self.fixture.output.exists())
        unknown.unlink()
        unknown.with_suffix('.json').write_text('{"fictional_private_neighbor":true}',encoding='utf-8')
        with self.assertRaisesRegex(fixtures.package.PackageError,'Unapproved JSON'):self.fixture.build()
        self.assertFalse(self.fixture.output.exists())

    def test_source_notice_corruption_cannot_relabel_an_old_build_as_verified(self):
        relative=next(name for name in self.reviewed if name!='source-manifest.json')
        path=self.fixture.source/'third_party_notices'/relative
        path.write_bytes(path.read_bytes()+b'\nFictional source corruption')
        with self.assertRaisesRegex(fixtures.package.PackageError,'notices'):self.fixture.build()
        self.assertFalse(self.fixture.output.exists())

    def test_archive_rejects_changed_notice_even_when_its_hash_map_is_rewritten(self):
        self.fixture.build();altered=self.root/'changed-notice.zip'
        relative=next(name for name in self.reviewed if name!='source-manifest.json')
        payload_name='_internal/third_party_notices/'+relative
        with zipfile.ZipFile(self.fixture.output) as source,zipfile.ZipFile(altered,'w') as target:
            bad=source.read('StockAndSleeve/'+payload_name)+b'\nFictional changed archived licence'
            for name in source.namelist():
                data=source.read(name)
                if name=='StockAndSleeve/'+payload_name:data=bad
                elif name=='ProgramSHA256.json':
                    value=json.loads(data);value[payload_name]=hashlib.sha256(bad).hexdigest();data=json.dumps(value).encode()
                target.writestr(name,data)
        with self.assertRaisesRegex(fixtures.package.PackageError,'notices'):fixtures.package.verify_archive(altered)

    @unittest.skipUnless(os.name=='nt' and shutil.which('powershell.exe'),'Windows notice installer integration')
    def test_windows_installer_accepts_the_exact_source_bound_notice_payload(self):
        extracted=self.fixture.extracted()
        literal=fixtures.WindowsInstallerTests.literal
        result=fixtures.WindowsInstallerTests.powershell(self,'Install-SharedPilot -PackageRoot '+literal(extracted)+' -ValidateOnly | ConvertTo-Json')
        self.assertTrue(json.loads(result.stdout)['verified'])

    @unittest.skipUnless(os.name=='nt' and shutil.which('powershell.exe'),'Windows notice installer integration')
    def test_windows_installer_does_not_let_a_forged_hash_map_relabel_notice_changes(self):
        extracted=self.fixture.extracted();record=extracted/'ProgramSHA256.json'
        expected=json.loads(record.read_text());literal=fixtures.WindowsInstallerTests.literal
        call='Install-SharedPilot -PackageRoot '+literal(extracted)+' -ValidateOnly'
        relative=next(name for name in self.reviewed if name!='source-manifest.json')
        name='_internal/third_party_notices/'+relative;path=extracted/'StockAndSleeve'/name;original=path.read_bytes()
        path.write_bytes(original+b'\nFictional changed licence')
        changed=dict(expected);changed[name]=hashlib.sha256(path.read_bytes()).hexdigest();record.write_text(json.dumps(changed))
        result=fixtures.WindowsInstallerTests.powershell(self,call,success=False)
        self.assertIn('third-party notice',result.stderr)
        path.write_bytes(original)
        name='_internal/third_party_notices/private-neighbor.txt';path=extracted/'StockAndSleeve'/name
        path.write_text('FICTIONAL-NOT-A-LICENCE',encoding='utf-8')
        changed=dict(expected);changed[name]=hashlib.sha256(path.read_bytes()).hexdigest();record.write_text(json.dumps(changed))
        result=fixtures.WindowsInstallerTests.powershell(self,call,success=False)
        self.assertIn('third-party notice',result.stderr)

    @unittest.skipUnless(os.name=='nt' and shutil.which('powershell.exe'),'Windows notice installer integration')
    def test_windows_installer_rejects_encrypted_intake_and_receiving_journals_even_in_hash_map(self):
        extracted=self.fixture.extracted();record=extracted/'ProgramSHA256.json'
        expected=json.loads(record.read_text());literal=fixtures.WindowsInstallerTests.literal
        for journal in ('cloud.intake','cloud.receiving'):
            with self.subTest(journal=journal):
                name='_internal/'+journal;path=extracted/'StockAndSleeve'/name;path.write_bytes(b'FICTIONAL-ENCRYPTED-JOURNAL')
                changed=dict(expected);changed[name]=hashlib.sha256(path.read_bytes()).hexdigest();record.write_text(json.dumps(changed))
                result=fixtures.WindowsInstallerTests.powershell(self,'Install-SharedPilot -PackageRoot '+literal(extracted)+' -ValidateOnly',success=False)
                self.assertIn('Merchant or credential data',result.stderr)
                path.unlink();record.write_text(json.dumps(expected))


if __name__=='__main__':unittest.main(verbosity=2)
