"""Real loopback HTTP checks for background launch, identity and graceful stopping."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import requests
import roca_to_square as core
import station_desktop
from station_runtime import RuntimeControl, read_runtime, running_status
from station_service import StationService


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = patch.object(core, 'SETTINGS_FILE', str(Path(self.directory.name)/'settings.json'))
        self.settings.start()
        self.server = core.HTTPServer(('127.0.0.1', 0), core.RocaStationHandler)
        self.server.station = StationService(core)
        self.control = self.server.runtime_control = RuntimeControl(self.server, core)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.client = requests.Session()
        self.client.trust_env = False

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.control.close()
        self.thread.join(timeout=3)
        self.client.close()
        self.settings.stop()
        self.directory.cleanup()

    def post(self, path, data, **kwargs):
        return self.client.post(self.base+path, json=data, timeout=4, **kwargs)

    def test_runtime_identity_hides_stop_secret_and_rejects_other_instances(self):
        record = read_runtime(core)
        self.assertEqual(running_status(record)['version'], core.APP_VERSION)
        public = self.client.get(self.base+'/api/runtime', timeout=3)
        self.assertNotIn(record['shutdownToken'], public.text)
        self.assertNotIn('pid', public.json())
        self.assertIsNone(running_status(dict(record, instance='another-station')))
        self.assertEqual(self.post('/api/shutdown', {'token':'incorrect'}).status_code, 403)
        self.assertEqual(self.post('/api/shutdown', {'token':'\u2603'}).status_code, 403)
        foreign = self.post('/api/shutdown', {'token':record['shutdownToken']},
                            headers={'Origin':'https://unrelated.example'})
        self.assertEqual(foreign.status_code, 403)
        self.assertFalse(self.control.stopping.is_set())

    def test_start_again_reopens_same_instance_without_another_service(self):
        with patch.object(core,'PORT',self.server.server_port), patch.object(core,'open_station_browser',return_value=True) as browser:
            core.launch_desktop_station(reuse_existing=True)
        browser.assert_called_once_with(self.base)
        self.assertEqual(read_runtime(core)['instance'], self.control.record['instance'])

    def test_old_version_is_not_reopened(self):
        self.control.record['version'] = 'older-test-release'
        with patch.object(core,'PORT',self.server.server_port), patch.object(core,'open_station_browser') as browser:
            with self.assertRaisesRegex(RuntimeError,'older Roca version'):
                core.launch_desktop_station(reuse_existing=True)
        browser.assert_not_called()

    def test_stop_waits_for_active_receiving_and_refuses_new_writes(self):
        started = threading.Event()
        release = threading.Event()
        result = []

        def receive(_):
            started.set()
            if not release.wait(4):
                raise AssertionError('Test did not release receiving')
            return {'id':'in-flight','inventory_status':'received'}

        def do_receive():
            # A separate session avoids sharing the pool with the stop request.
            with requests.Session() as client:
                client.trust_env = False
                result.append(client.post(self.base+'/api/receipts/in-flight/receive',json={},timeout=6))

        worker = threading.Thread(target=do_receive)
        with patch.object(self.server.station,'receive',side_effect=receive) as receiving:
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                response = self.post('/api/shutdown',{'token':self.control.record['shutdownToken']})
                self.assertEqual(response.status_code,202)
                self.assertTrue(self.thread.is_alive(),'Shutdown must wait for the active operation')
                self.assertTrue(self.server.station.stop_event.is_set())
                self.assertEqual(self.post('/api/import',{'csv':'must not import'}).status_code,503)
            finally:
                release.set()
                worker.join(timeout=6)
            self.thread.join(timeout=3)
            self.assertFalse(self.thread.is_alive())
            self.assertEqual(result[0].status_code,200)
            self.assertEqual(result[0].json()['receipt']['inventory_status'],'received')
            receiving.assert_called_once()

    def test_stop_shortcut_can_be_cancelled(self):
        with patch.object(station_desktop,'message',return_value=False):
            station_desktop.stop_station(core)
        self.assertFalse(self.control.stopping.is_set())

    def test_runtime_cleanup_preserves_newer_instance_record(self):
        replacement = dict(self.control.record,instance='new-instance')
        self.control.path.write_text(json.dumps(replacement),encoding='utf-8')
        self.control.close()
        self.assertEqual(read_runtime(core)['instance'],'new-instance')


class DesktopLifecycleTests(unittest.TestCase):
    def test_separate_process_reopens_and_stops_without_losing_saved_records(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = str(Path(directory)/'settings.json')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0))
                port = listener.getsockname()[1]
            script = '''import sys
import roca_to_square as core
core.SETTINGS_FILE=sys.argv[1]
core.PORT=int(sys.argv[2])
core.open_station_browser=lambda url: True
core.launch_desktop_station(reuse_existing=True)
'''
            command = [sys.executable,'-c',script,settings,str(port)]
            process = subprocess.Popen(command,cwd=Path(core.__file__).parent,
                                       stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            try:
                with patch.object(core,'SETTINGS_FILE',settings):
                    deadline = time.monotonic()+6
                    record = None
                    while time.monotonic()<deadline:
                        record=read_runtime(core)
                        if running_status(record):
                            break
                        time.sleep(.05)
                    self.assertIsNotNone(running_status(record),'Background station did not start')
                    service=StationService(core)
                    service.store.put('lifecycle-test','saved before stopping')
                    reopened=subprocess.run(command,cwd=Path(core.__file__).parent,
                                            capture_output=True,text=True,timeout=6)
                    self.assertEqual(reopened.returncode,0,reopened.stdout+reopened.stderr)
                    self.assertIsNone(process.poll(),'Original background process must keep running')
                    self.assertEqual(read_runtime(core)['instance'],record['instance'])
                    with patch.object(station_desktop,'message',return_value=True) as notice:
                        station_desktop.stop_station(core)
                    self.assertIn('has stopped',notice.call_args.args[0])
                    output,_=process.communicate(timeout=6)
                    self.assertEqual(process.returncode,0,output)
                    self.assertIsNone(read_runtime(core))
                    self.assertEqual(service.store.get('lifecycle-test'),'saved before stopping')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
