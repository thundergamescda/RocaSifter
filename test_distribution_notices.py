"""Exact notice payload safety; isolated fictional filesystem mutations only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import distribution_notices as notices


REVIEWED=Path(__file__).resolve().parent/notices.NOTICE_DIRECTORY
if not REVIEWED.is_dir():REVIEWED=Path(__file__).resolve().parents[2]/'Planning/distribution-notices/payload-v2'


class DistributionNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'notices'

    def fixture(self,*,names=('LICENSES/library/LICENSE.txt',)):
        self.root.mkdir();rows=[]
        for index,name in enumerate(names):
            value=('Fictional licence '+str(index)+'\n').encode()
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(value)
            rows.append({'path':name,'bytes':len(value),'sha256':hashlib.sha256(value).hexdigest()})
        self.rows=rows;self.write_manifest(rows)
        count_patch=patch.object(notices,'REVIEWED_FILE_COUNT',len(rows)+1)
        count_patch.start();self.addCleanup(count_patch.stop)
        return self.root

    def write_manifest(self,rows):
        raw=(json.dumps({'format_version':1,'files':rows},sort_keys=True)+'\n').encode()
        (self.root/notices.MANIFEST_NAME).write_bytes(raw)
        if hasattr(self,'pin'):self.pin.stop()
        self.pin=patch.object(notices,'REVIEWED_MANIFEST_SHA256',hashlib.sha256(raw).hexdigest());self.pin.start()
        self.addCleanup(self.pin.stop)

    def test_actual_reviewed_142_file_payload_validates_without_modification(self):
        self.assertTrue(REVIEWED.is_dir(),'The actual reviewed notice fixture must be available; do not silently skip it.')
        before={p.relative_to(REVIEWED).as_posix():(p.stat().st_mtime_ns,hashlib.sha256(p.read_bytes()).hexdigest())
                for p in REVIEWED.rglob('*') if p.is_file()}
        result=notices.validate_notice_payload(REVIEWED)
        self.assertEqual(len(result),142);self.assertEqual(list(result),sorted(result))
        self.assertEqual(set(result),set(before));self.assertIn('source-manifest.json',result)
        after={name:(p.stat().st_mtime_ns,hashlib.sha256(p.read_bytes()).hexdigest()) for name,p in result.items()}
        self.assertEqual(before,after);self.assertTrue(all(p.is_absolute() for p in result.values()))

    def test_small_trusted_fixture_has_exact_regular_file_map(self):
        root=self.fixture();result=notices.validate_notice_payload(root)
        self.assertEqual(set(result),{notices.MANIFEST_NAME,self.rows[0]['path']})
        self.assertEqual(result[self.rows[0]['path']],root/self.rows[0]['path'])

    def test_tampered_or_wrong_size_licence_is_rejected_without_repair(self):
        root=self.fixture();p=root/self.rows[0]['path'];original=p.read_bytes()
        for value in (original[:-1],original+b'x',b'X'+original[1:]):
            p.write_bytes(value)
            with self.subTest(bytes=len(value)),self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)
            self.assertEqual(p.read_bytes(),value)

    def test_unknown_filename_is_rejected_before_any_licence_or_unknown_content_read(self):
        root=self.fixture();unknown=root/'merchant-records.json';unknown.write_bytes(b'fictional must-never-read marker')
        real_read=notices._read;opened=[]
        def spy(path,limit):
            opened.append(path.relative_to(root).as_posix())
            if path==unknown:raise AssertionError('Unknown content was opened')
            return real_read(path,limit)
        with patch.object(notices,'_read',side_effect=spy),self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)
        self.assertEqual(opened,[notices.MANIFEST_NAME])

    def test_wrong_manifest_hash_is_rejected_before_directory_enumeration(self):
        root=self.fixture();(root/notices.MANIFEST_NAME).write_bytes(b'changed')
        with patch.object(Path,'iterdir',side_effect=AssertionError('Untrusted manifest enumerated a tree')):
            with self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)

    def test_missing_files_extra_empty_directories_and_file_directory_replacement_fail(self):
        root=self.fixture();p=root/self.rows[0]['path'];original=p.read_bytes();p.unlink()
        with self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)
        p.mkdir()
        with self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)
        p.rmdir();p.write_bytes(original);(root/'unknown-empty').mkdir()
        with self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)

    def test_manifest_traversal_absolute_device_json_and_aliases_are_rejected(self):
        root=self.fixture();base=self.rows[0]
        for name in ('../outside','/absolute','C:/outside','a\\b','a//b','a/./b','a/../b','a/trailing.',
                     'a/trailing ','CON','a/NUL.txt','a/data.json','a/control\x00'):
            self.write_manifest([dict(base,path=name)])
            with self.subTest(name=repr(name)),self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)
        count_patch=patch.object(notices,'REVIEWED_FILE_COUNT',3);count_patch.start();self.addCleanup(count_patch.stop)
        self.write_manifest([base,dict(base,path=base['path'].upper())])
        with self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)

    def test_manifest_counts_hash_and_size_types_are_strict(self):
        root=self.fixture();base=self.rows[0]
        for changes in ({'bytes':True},{'bytes':-1},{'sha256':'x'*64},{'sha256':0}):
            self.write_manifest([base|changes])
            with self.subTest(changes=changes),self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)
        self.write_manifest([])
        with self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)

    def test_manifest_and_directory_redirect_tags_are_rejected_before_target_reads(self):
        root=self.fixture();real=Path.lstat
        for redirected in (root,root/'LICENSES',root/notices.MANIFEST_NAME,root/self.rows[0]['path']):
            def info(path,*args,**kwargs):
                actual=real(path,*args,**kwargs)
                if path!=redirected:return actual
                return SimpleNamespace(st_mode=actual.st_mode,st_reparse_tag=0xA0000003)
            with self.subTest(path=redirected.name),patch.object(Path,'lstat',info),self.assertRaises(notices.NoticePayloadError):
                notices.validate_notice_payload(root)

    def test_symlink_like_file_mode_is_rejected(self):
        root=self.fixture();target=root/self.rows[0]['path'];real=Path.lstat
        def info(path,*args,**kwargs):
            return SimpleNamespace(st_mode=stat.S_IFLNK|0o777,st_reparse_tag=0) if path==target else real(path,*args,**kwargs)
        with patch.object(Path,'lstat',info),self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(root)

    def test_actual_directory_junction_or_symlink_is_rejected_before_manifest_read(self):
        root=self.fixture();alias=root.parent/'redirected-notices'
        if os.name=='nt':
            import _winapi
            _winapi.CreateJunction(str(root),str(alias))
        else:alias.symlink_to(root,target_is_directory=True)
        try:
            with patch.object(notices,'_read',side_effect=AssertionError('Redirected manifest opened')):
                with self.assertRaises(notices.NoticePayloadError):notices.validate_notice_payload(alias)
        finally:
            if os.name=='nt':alias.rmdir()
            else:alias.unlink()

    def test_build_environment_returns_only_public_exact_versions(self):
        manifest=json.loads((REVIEWED/notices.MANIFEST_NAME).read_text())
        with patch.object(notices.platform,'python_version',return_value=manifest['python']),patch.object(notices.metadata,'version',side_effect=manifest['reviewed_packages'].__getitem__):
            result=notices.validate_build_environment(REVIEWED)
        self.assertEqual(result,{'python':manifest['python'],'packages':dict(sorted(manifest['reviewed_packages'].items())),
            'manifest_sha256':notices.REVIEWED_MANIFEST_SHA256})
        self.assertNotIn('Users',json.dumps(result));self.assertNotIn('RECORD',json.dumps(result))

    def test_old_python_mismatched_or_missing_package_fails_closed(self):
        manifest=json.loads((REVIEWED/notices.MANIFEST_NAME).read_text())
        with patch.object(notices.platform,'python_version',return_value='3.12.14'),patch.object(notices.metadata,'version') as lookup:
            with self.assertRaisesRegex(notices.NoticePayloadError,'build environment'):notices.validate_build_environment(REVIEWED)
            lookup.assert_not_called()
        for effect in ('unreviewed-version',notices.metadata.PackageNotFoundError('fictional')):
            with self.subTest(effect=str(effect)),patch.object(notices.platform,'python_version',return_value=manifest['python']):
                with patch.object(notices.metadata,'version',**({'return_value':effect} if isinstance(effect,str) else {'side_effect':effect})):
                    with self.assertRaisesRegex(notices.NoticePayloadError,'build environment'):notices.validate_build_environment(REVIEWED)

    def test_build_environment_never_reads_package_metadata_for_untrusted_payload(self):
        root=self.fixture();(root/notices.MANIFEST_NAME).write_bytes(b'changed')
        with patch.object(notices.metadata,'version',side_effect=AssertionError('Untrusted manifest requested metadata')):
            with self.assertRaises(notices.NoticePayloadError):notices.validate_build_environment(root)


if __name__=='__main__':unittest.main()
