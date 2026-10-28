"""Real local HTTP boundary rejects stale/null staff-session bindings before cloud calls."""
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
import requests
import roca_to_square as core
from station_service import StationService
from station_cloud import CloudClient
from test_station_cloud import SharedFixtureTransport


class CloudHTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        with patch.object(core,'SETTINGS_FILE',str(root/'settings.json')):self.service=StationService(core)
        self.remote=SharedFixtureTransport();self.service.cloud=CloudClient(self.service.store.root,transport=self.remote)
        self.server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);self.server.station=self.service
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.session=requests.Session();self.session.trust_env=False
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.close)
    def close(self):
        self.session.close();self.service.stop_event.set();self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
    def post(self,path,body):return self.session.post(self.base+'/api/cloud/'+path,json=body,timeout=5)
    def test_missing_null_malformed_and_old_nonce_cannot_create_under_current_staff(self):
        credentials={'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'}
        old=self.post('login',credentials).json()['sessionId']
        self.post('logout',{'sessionId':old})
        fresh=self.post('login',credentials|{'storeSlug':'another-store'}).json()['sessionId']
        body={'request_key':'stale-window','display_name':'Fictional customer','permission_confirmed':True}
        calls=len(self.remote.calls)
        for marker in ('missing',None,False,1,[],{},old,'\u2603'*32):
            with self.subTest(nonce=marker):
                payload=body if marker=='missing' else body|{'sessionId':marker}
                result=self.post('customer',payload)
                self.assertEqual(result.status_code,409,result.text);self.assertTrue(result.json()['sessionChanged'])
        self.assertEqual(len(self.remote.calls),calls);self.assertEqual(self.remote.customers,{})
        good=self.post('customer',body|{'sessionId':fresh});self.assertEqual(good.status_code,200,good.text)
        self.assertEqual(len(self.remote.customers),1)
    def test_cross_origin_cannot_sign_in_or_read_shared_session(self):
        result=self.session.get(self.base+'/api/cloud/session',headers={'Origin':'https://attacker.invalid'},timeout=5)
        self.assertEqual(result.status_code,403)
        result=self.session.post(self.base+'/api/cloud/login',json={},headers={'Origin':'https://attacker.invalid'},timeout=5)
        self.assertEqual(result.status_code,403);self.assertEqual(self.remote.calls,[])


if __name__=='__main__':unittest.main(verbosity=2)
