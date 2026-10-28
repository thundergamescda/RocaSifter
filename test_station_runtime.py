"""Real loopback HTTP checks for background launch, identity and graceful stopping."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import requests
import roca_to_square as core
import station_desktop
from station_runtime import RuntimeControl, current_build, read_runtime, running_status
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

    def test_in_app_quit_rejects_foreign_missing_and_stale_actions(self):
        data={'instance':self.control.record['instance']}
        headers={'Origin':self.base,'X-SS-User-Action':'quit'}
        self.assertEqual(self.post('/api/quit',data).status_code,403)
        self.assertEqual(self.post('/api/quit',data,headers={'Origin':self.base}).status_code,403)
        self.assertEqual(self.post('/api/quit',data,headers=headers|{'Origin':'https://other.example'}).status_code,403)
        self.assertEqual(self.post('/api/quit',{'instance':'previous-session'},headers=headers).status_code,409)
        self.assertFalse(self.control.stopping.is_set())
        self.assertEqual(self.post('/api/quit',data,headers=headers).status_code,202)
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.server.station.stop_event.is_set())

    def test_old_version_is_not_reopened(self):
        self.control.record['version'] = 'older-test-release'
        with patch.object(core,'PORT',self.server.server_port), patch.object(core,'open_station_browser') as browser:
            with self.assertRaisesRegex(RuntimeError,'older Roca version'):
                core.launch_desktop_station(reuse_existing=True)
        browser.assert_not_called()

    def test_another_build_with_same_version_is_not_silently_reopened(self):
        self.control.record['build']='package:'+'a'*64
        with patch.object(core,'PORT',self.server.server_port),patch.object(core,'open_station_browser') as browser:
            with self.assertRaisesRegex(RuntimeError,'different Stock & Sleeve build'):
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

    def test_stop_waits_for_active_cloud_work_without_holding_local_save_lock(self):
        started=threading.Event();release=threading.Event();local_access=threading.Event();result=[]
        station=self.server.station

        def create_customer(data,**_):
            with station.cloud.lock:
                started.set()
                if not station.stop_event.wait(4):raise AssertionError('Stop was not requested')
                # Teardown must not hold this lock while waiting for cloud work.
                with station.lock:local_access.set()
                if not release.wait(4):raise AssertionError('Test did not release cloud operation')
                return {'customer_id':'fictional-finished-customer'}

        def cloud_request():
            with requests.Session() as client:
                client.trust_env=False
                result.append(client.post(self.base+'/api/cloud/customer',json={'request_key':'fixture','sessionId':'a'*32},timeout=7))

        worker=threading.Thread(target=cloud_request)
        with patch.object(station.cloud,'create_customer',side_effect=create_customer) as creating:
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                response=self.post('/api/shutdown',{'token':self.control.record['shutdownToken']})
                self.assertEqual(response.status_code,202)
                self.assertTrue(local_access.wait(2),'Shutdown must release the local lock before draining cloud work')
                self.assertTrue(self.thread.is_alive(),'Stop must wait for the active cloud operation')
                self.assertEqual(self.post('/api/cloud/customer',{'request_key':'too-late','sessionId':'a'*32}).status_code,503)
                self.assertEqual(self.post('/api/import',{'csv':'must not import'}).status_code,503)
            finally:
                release.set();worker.join(timeout=7)
            self.thread.join(timeout=3)
            self.assertFalse(worker.is_alive());self.assertFalse(self.thread.is_alive())
            self.assertEqual(len(result),1);self.assertEqual(result[0].status_code,200)
            self.assertEqual(result[0].json(),{'customer_id':'fictional-finished-customer'})
            creating.assert_called_once()

    def test_cloud_requests_queued_before_stop_are_rejected_after_lock_is_released(self):
        class ObservedLock:
            def __init__(self):
                self.lock=threading.RLock();self.condition=threading.Condition();self.blocked=0
            def __enter__(self):
                if not self.lock.acquire(blocking=False):
                    with self.condition:
                        self.blocked+=1;self.condition.notify_all()
                    self.lock.acquire()
                return self
            def __exit__(self,*_):self.lock.release()
            def wait_for_blocked(self,count):
                with self.condition:return self.condition.wait_for(lambda:self.blocked>=count,timeout=3)

        gate=ObservedLock();results=[]

        def cloud_request(method,path):
            with requests.Session() as client:
                client.trust_env=False
                results.append(client.request(method,self.base+path,json={} if method=='POST' else None,timeout=7))

        station=self.server.station
        with patch.object(station.cloud,'lock',gate),patch.object(station.cloud,'status') as status, \
             patch.object(station.cloud,'create_customer') as creating:
            writer=threading.Thread(target=cloud_request,args=('POST','/api/cloud/customer'))
            reader=threading.Thread(target=cloud_request,args=('GET','/api/cloud/session'))
            with gate:
                writer.start();reader.start()
                self.assertTrue(gate.wait_for_blocked(2),'Both requests must reach cloud admission before stop')
                self.assertEqual(self.post('/api/shutdown',{'token':self.control.record['shutdownToken']}).status_code,202)
                self.assertTrue(self.thread.is_alive(),'Shutdown must drain the occupied cloud lock')
            writer.join(timeout=7);reader.join(timeout=7);self.thread.join(timeout=3)
            self.assertFalse(writer.is_alive());self.assertFalse(reader.is_alive());self.assertFalse(self.thread.is_alive())
            self.assertEqual(len(results),2)
            self.assertEqual(sorted(result.status_code for result in results),[503,503])
            status.assert_not_called();creating.assert_not_called()

    def test_stop_shortcut_can_be_cancelled(self):
        with patch.object(station_desktop,'message',return_value=False):
            station_desktop.stop_station(core)
        self.assertFalse(self.control.stopping.is_set())

    def test_updater_stops_only_the_identified_station_through_its_local_endpoint(self):
        import roca_update
        def close_when_stopped():
            self.thread.join()
            self.server.server_close()
        closer=threading.Thread(target=close_when_stopped,daemon=True)
        closer.start()
        roca_update.stop_existing_station(Path(core.SETTINGS_FILE).parent,self.server.server_port,lambda *_:None,timeout=4)
        closer.join(timeout=2)
        self.assertFalse(closer.is_alive())
        self.assertTrue(self.control.stopping.is_set())
        self.assertTrue(self.server.station.stop_event.is_set())

    def test_runtime_cleanup_preserves_newer_instance_record(self):
        replacement = dict(self.control.record,instance='new-instance')
        self.control.path.write_text(json.dumps(replacement),encoding='utf-8')
        self.control.close()
        self.assertEqual(read_runtime(core)['instance'],'new-instance')

    @unittest.skipUnless(os.name=='nt','Windows file sharing regression')
    def test_runtime_cleanup_retries_a_stop_poll_reader_sharing_violation(self):
        reader=self.control.path.open('rb')
        started=threading.Event();finished=threading.Event()
        def close():
            started.set();self.control.close();finished.set()
        closer=threading.Thread(target=close)
        closer.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(.1),'Cleanup must retry while a Stop reader denies deletion')
        finally:
            reader.close();closer.join(timeout=3)
        self.assertTrue(finished.is_set());self.assertFalse(self.control.path.exists())


class DesktopLifecycleTests(unittest.TestCase):
    def test_build_identity_tracks_program_inputs_and_requires_packaged_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture=SimpleNamespace(APP_DIR=str(root))
            (root/'station.js').write_text('first',encoding='utf-8')
            first=current_build(fixture)
            (root/'settings.json').write_text('merchant fixture must not affect program identity',encoding='utf-8')
            self.assertEqual(first,current_build(fixture))
            (root/'station.js').write_text('changed',encoding='utf-8')
            self.assertNotEqual(first,current_build(fixture))
            with patch.object(sys,'frozen',True,create=True):
                with self.assertRaisesRegex(RuntimeError,'build record'):current_build(fixture)
                (root/'desktop_build.json').write_text(json.dumps({'build_id':'b'*64}),encoding='utf-8')
                self.assertEqual(current_build(fixture),'package:'+'b'*64)
                (root/'desktop_build.json').write_text(json.dumps({'build_id':'invalid'}),encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError,'build record'):current_build(fixture)

    def test_launch_finally_drains_cloud_before_closing_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'station_data').mkdir()
            station=SimpleNamespace(lock=threading.RLock(),cloud=SimpleNamespace(lock=threading.RLock()),
                                    stop_event=threading.Event(),watcher=None,start_watcher=Mock(),
                                    pricing=SimpleNamespace(start=Mock(),worker=None))
            cloud_started=threading.Event();release_cloud=threading.Event();local_access=threading.Event()
            serving=threading.Event();exit_loop=threading.Event();errors=[]

            def serve_forever():
                serving.set()
                if not exit_loop.wait(4):raise AssertionError('Test did not exit fake serve loop')

            server=SimpleNamespace(server_port=23456,serve_forever=serve_forever,server_close=Mock())

            def cloud_work():
                with station.cloud.lock:
                    cloud_started.set()
                    if not station.stop_event.wait(4):raise AssertionError('Launcher cleanup did not start')
                    with station.lock:local_access.set()
                    if not release_cloud.wait(4):raise AssertionError('Test did not release cloud work')

            def launch():
                try:core.launch_desktop_station()
                except BaseException as error:errors.append(error)

            with patch.object(core,'SETTINGS_FILE',str(Path(directory)/'settings.json')), \
                 patch.object(core,'PORT',23456),patch.object(core,'HTTPServer',return_value=server), \
                 patch.object(core,'open_station_browser',return_value=True), \
                 patch('station_service.StationService',return_value=station),patch('roca_update.update_in_progress',return_value=False):
                worker=threading.Thread(target=cloud_work);launcher=threading.Thread(target=launch)
                worker.start();self.assertTrue(cloud_started.wait(2));launcher.start()
                try:
                    self.assertTrue(serving.wait(2));exit_loop.set()
                    self.assertTrue(station.stop_event.wait(2))
                    self.assertTrue(local_access.wait(2),'Launcher must not nest local/cloud drain locks')
                    self.assertTrue(launcher.is_alive(),'Launcher must wait for its cloud operation')
                    self.assertTrue(server.runtime_control.stopping.is_set())
                    server.server_close.assert_not_called()
                    self.assertIsNotNone(read_runtime(core),'Runtime identity must remain until work drains')
                finally:
                    exit_loop.set();release_cloud.set();worker.join(timeout=4);launcher.join(timeout=4)
                self.assertFalse(worker.is_alive());self.assertFalse(launcher.is_alive());self.assertEqual(errors,[])
                server.server_close.assert_called_once();self.assertIsNone(read_runtime(core))

    def test_separate_process_reopens_and_stops_without_losing_saved_records(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = str(Path(directory)/'settings.json')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0))
                port = listener.getsockname()[1]
            script = '''import sys
import faulthandler
import station_runtime
import roca_to_square as core
core.SETTINGS_FILE=sys.argv[1]
core.PORT=int(sys.argv[2])
core.open_station_browser=lambda url: True
original_stop=station_runtime.RuntimeControl.request_stop
def diagnostic_stop(self):
    faulthandler.dump_traceback_later(8)
    return original_stop(self)
station_runtime.RuntimeControl.request_stop=diagnostic_stop
try:
    core.launch_desktop_station(reuse_existing=True)
finally:
    faulthandler.cancel_dump_traceback_later()
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
                    stopped_notice=notice.call_args.args[0]
                    if 'has stopped' not in stopped_notice:
                        exit_before_cleanup=process.poll()
                        if process.poll() is None:process.kill()
                        diagnostics,_=process.communicate(timeout=3)
                        self.fail(f'{stopped_notice}\nChild exit before test cleanup: {exit_before_cleanup!r}\nChild diagnostics:\n{diagnostics}')
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
