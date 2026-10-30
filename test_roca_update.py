"""Update filesystem transactions, preserved merchant data and process boundaries."""
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import roca_update as update
import roca_to_square as core


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'Existing Roca'
        self.root.mkdir()
        with socket.socket() as connection:
            connection.bind(('127.0.0.1',0))
            self.port = connection.getsockname()[1]
        self.old = f'APP_VERSION = "2.0.3"\nPORT = {self.port}\n'.encode()
        (self.root/'roca_to_square.py').write_bytes(self.old)
        (self.root/'station.js').write_bytes(b'// Original application')
        self.business = {'station_data/station.sqlite3':b'original receiving journal and receipt records',
                         'station_data/square.secret':b'test-only encrypted credential',
                         'outputs/existing-label.pdf':b'%PDF-existing labels',
                         'roca_settings.json':b'{"operator":"retained"}',
                         '.env':b'LOCAL_SETTING=test-only',
                         'incoming-cards.csv':b'original user export'}
        for name,content in self.business.items():
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
        self.archive=Path(self.temp.name)/'release.zip'
        self.commit='a'*40
        self.make_archive()

    def tearDown(self):
        self.temp.cleanup()

    def make_archive(self, extras=None, version='2.0.4', omit=None):
        with zipfile.ZipFile(self.archive,'w') as bundle:
            bundle.writestr('RocaSifter-revision/','')
            for name in sorted(update.REQUIRED):
                if name == omit:
                    continue
                content='# New application\n' if name.endswith('.py') else 'new application'
                if name=='roca_to_square.py':
                    content=f'APP_VERSION = "{version}"\nPORT = {self.port}\n'
                bundle.writestr('RocaSifter-revision/'+name,content)
            for name,content in (extras or {}).items():
                # ZipInfo preserves intentionally invalid backslashes on Windows.
                info=zipfile.ZipInfo(name if name.startswith('/') else 'RocaSifter-revision/'+name)
                info.filename=info.orig_filename
                bundle.writestr(info,content)

    def run_update(self, **kwargs):
        with patch.object(update,'download_release',return_value=(self.archive,self.commit)), \
             patch.object(update,'install_dependencies') as install, \
             patch.object(update,'validate_application') as validate, \
             patch.object(update,'start_updated_station',return_value=True) as restart:
            result=update.perform_update(self.root,**kwargs)
        return result,install,validate,restart

    def assert_business_preserved(self):
        for name,content in self.business.items():
            self.assertEqual((self.root/name).read_bytes(),content,name)

    def test_success_backs_up_existing_data_and_releases_lock_before_reopening(self):
        opened=[]
        def restart(root,version,port):
            self.assertFalse(update.update_in_progress(root))
            self.assertFalse(update.port_open(port))
            opened.append(version)
            return True
        with patch.object(update,'download_release',return_value=(self.archive,self.commit)), \
             patch.object(update,'install_dependencies'),patch.object(update,'validate_application'), \
             patch.object(update,'start_updated_station',side_effect=restart):
            result=update.perform_update(self.root)
        self.assertEqual(opened,['2.0.4'])
        self.assertTrue(result['reopened'])
        self.assert_business_preserved()
        backup=Path(result['backup'])
        self.assertFalse((backup/'files/station_data/update.lock').exists())
        self.assertEqual((backup/'files/roca_to_square.py').read_bytes(),self.old)
        for name,content in self.business.items():
            self.assertEqual((backup/'files'/name).read_bytes(),content)
        self.assertEqual(update.literal_setting(self.root/'roca_to_square.py','APP_VERSION'),'2.0.4')
        self.assertEqual(json.loads((backup/'update.json').read_text())['status'],'installed')

    def test_current_desktop_source_updates_using_its_isolated_port_override(self):
        # Use the actual current source, not the legacy literal-PORT fixture.
        self.old=Path(core.__file__).read_bytes()
        (self.root/'roca_to_square.py').write_bytes(self.old)
        self.make_archive(version='2.6.1')
        with patch.dict(os.environ,{'ROCA_STATION_PORT':str(self.port)}):
            result,_,_,restart=self.run_update()
        self.assertEqual(result['version'],'2.6.1')
        restart.assert_called_once_with(self.root,'2.6.1',self.port)
        self.assertEqual((Path(result['backup'])/'files/roca_to_square.py').read_bytes(),self.old)
        self.assert_business_preserved()

    def test_port_reader_matches_current_launcher_default_and_override(self):
        path=Path(core.__file__)
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(update.station_port(path),8765)
        with patch.dict(os.environ,{'ROCA_STATION_PORT':'8876'}):
            self.assertEqual(update.station_port(path),8876)
        # A legacy literal port must not be redirected by the newer override.
        with patch.dict(os.environ,{'ROCA_STATION_PORT':'8876'}):
            self.assertEqual(update.station_port(self.root/'roca_to_square.py'),self.port)
        for invalid in ('0','65536','not-a-port'):
            with self.subTest(override=invalid),patch.dict(os.environ,{'ROCA_STATION_PORT':invalid}):
                with self.assertRaisesRegex(update.UpdateError,'invalid local port'):
                    update.station_port(path)

    def test_port_reader_rejects_executable_source_without_executing_it(self):
        marker=Path(self.temp.name)/'must-not-be-written'
        path=self.root/'roca_to_square.py'
        path.write_text('PORT = __import__("pathlib").Path('+repr(str(marker))+').write_text("bad")\n')
        with self.assertRaisesRegex(update.UpdateError,'unsupported local port'):
            update.station_port(path)
        self.assertFalse(marker.exists())

    def test_update_archive_cannot_replace_local_data_or_environment(self):
        self.make_archive({name:b'wrong remote content' for name in [*self.business,'.venv/Scripts/python.exe','.git/config','backups/previous/private.txt']})
        self.run_update()
        self.assert_business_preserved()
        self.assertFalse((self.root/'.venv/Scripts/python.exe').exists())
        self.assertFalse((self.root/'.git/config').exists())
        self.assertFalse((self.root/'backups/previous/private.txt').exists())

    def test_failed_install_restores_app_files_and_removes_new_app_files(self):
        calls=[]
        def validate(root,payload,log):
            calls.append(payload)
            if len(calls)==2:
                raise update.UpdateError('Simulated installed import failure')
        with patch.object(update,'download_release',return_value=(self.archive,self.commit)), \
             patch.object(update,'install_dependencies'),patch.object(update,'validate_application',side_effect=validate), \
             patch.object(update,'start_updated_station') as restart:
            with self.assertRaisesRegex(update.UpdateError,'Previous application files were restored'):
                update.perform_update(self.root)
        self.assertEqual((self.root/'roca_to_square.py').read_bytes(),self.old)
        self.assertEqual((self.root/'station.js').read_bytes(),b'// Original application')
        self.assertFalse((self.root/'station_service.py').exists())
        self.assert_business_preserved()
        self.assertFalse(update.update_in_progress(self.root))
        restart.assert_not_called()

    def test_dependency_failure_keeps_old_app_and_full_backup(self):
        with patch.object(update,'download_release',return_value=(self.archive,self.commit)), \
             patch.object(update,'install_dependencies',side_effect=update.UpdateError('Package unavailable')):
            with self.assertRaisesRegex(update.UpdateError,'Package unavailable'):
                update.perform_update(self.root)
        self.assertEqual((self.root/'roca_to_square.py').read_bytes(),self.old)
        self.assert_business_preserved()
        self.assertEqual(len(list((self.root/'backups').glob('*/files/roca_to_square.py'))),1)

    def test_bad_archives_are_rejected_before_stopping_station(self):
        for bad_path in ('../outside.txt','/absolute.txt','C:/outside.txt','station_data./square.secret','folder\\outside.txt','NUL.txt'):
            self.make_archive({bad_path:'bad path'})
            with self.subTest(path=bad_path),patch.object(update,'download_release',return_value=(self.archive,self.commit)), \
                 patch.object(update,'stop_existing_station') as stop:
                with self.assertRaises(update.UpdateError):
                    update.perform_update(self.root)
                stop.assert_not_called()
        self.assert_business_preserved()

    def test_incomplete_and_broken_releases_are_rejected_before_installation(self):
        for kind in ('missing','syntax'):
            if kind=='missing':
                self.make_archive(omit='station_server.py')
            else:
                self.make_archive({'broken.py':'def broken('})
            with self.subTest(kind=kind),patch.object(update,'download_release',return_value=(self.archive,self.commit)), \
                 patch.object(update,'install_dependencies') as install:
                with self.assertRaises((update.UpdateError,SyntaxError)):
                    update.perform_update(self.root)
                install.assert_not_called()
        self.assertEqual((self.root/'roca_to_square.py').read_bytes(),self.old)

    def test_cloud_and_scanner_runtime_dependencies_are_required_before_stopping(self):
        for missing in ('station_cloud.py','station_shared_trade.py','scanner_adapters.py','catalog_cleanup.py','credit_tracker.py','cloud.js','cloud.css','intake.js','import.js','import.css','shared-trade.js','shared-trade.css','intake_manifest.py','station_intake_claim.py','intake-claim.js','intake-claim.css'):
            self.make_archive(omit=missing)
            with self.subTest(missing=missing),patch.object(update,'download_release',return_value=(self.archive,self.commit)), \
                 patch.object(update,'stop_existing_station') as stop:
                with self.assertRaisesRegex(update.UpdateError,'missing required application files'):
                    update.perform_update(self.root)
                stop.assert_not_called()
        self.assert_business_preserved()

    def test_same_revision_reopens_without_installing_again(self):
        self.run_update()
        result,install,validate,restart=self.run_update()
        self.assertTrue(result['current'])
        self.assertIsNone(result['backup'])
        install.assert_not_called();validate.assert_not_called();restart.assert_called_once()

    def test_cannot_downgrade_newer_station(self):
        self.make_archive(version='2.0.2')
        with self.assertRaisesRegex(update.UpdateError,'newer'):
            self.run_update()
        self.assertEqual((self.root/'roca_to_square.py').read_bytes(),self.old)

    def test_update_lock_blocks_another_update_and_new_station_start(self):
        with update.UpdateLock(self.root):
            self.assertTrue(update.update_in_progress(self.root))
            with self.assertRaisesRegex(update.UpdateError,'already running'):
                with update.UpdateLock(self.root):
                    pass
            with patch.object(core,'APP_DIR',str(self.root)),patch.object(core,'HTTPServer') as server:
                with self.assertRaisesRegex(RuntimeError,'being updated'):
                    core.launch_desktop_station()
                server.assert_not_called()
        self.assertFalse(update.update_in_progress(self.root))

    def test_legacy_running_port_times_out_without_changing_files(self):
        messages=[]
        with patch.object(update,'port_open',return_value=True),patch.object(update,'local_json',side_effect=OSError('Legacy station')):
            with self.assertRaisesRegex(update.UpdateError,'still running'):
                update.stop_existing_station(self.root,self.port,lambda value,text:messages.append(text),timeout=0)
        self.assertTrue(any('older Roca command window' in text for text in messages))
        self.assertEqual((self.root/'roca_to_square.py').read_bytes(),self.old)

    def test_repository_download_pins_exact_main_revision(self):
        calls=[]
        def fetch(url,target=None,headers=None):
            calls.append(url)
            if target is None:
                return ('003d'+self.commit+' refs/heads/main\n0000').encode()
            Path(target).write_bytes(b'archive')
        with patch.object(update,'fetch',side_effect=fetch):
            _,commit=update.download_release(Path(self.temp.name))
        self.assertEqual(commit,self.commit)
        self.assertEqual(calls[-1],f'https://codeload.github.com/{update.REPOSITORY}/zip/{self.commit}')


if __name__=='__main__':
    unittest.main()
