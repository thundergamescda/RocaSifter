"""Real loopback disconnects at response boundaries; fictional local data only."""
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import uuid
import requests
import roca_to_square as core
from station_server import make_handler
from station_service import StationService


DISCONNECTS=(BrokenPipeError,ConnectionResetError,ConnectionAbortedError)


class FaultWriter:
    def __init__(self,writer,handler):self.writer=writer;self.handler=handler
    def __getattr__(self,name):return getattr(self.writer,name)
    def write(self,data):
        fault=self.handler.server.fail_next
        phase='headers' if data.startswith(b'HTTP/') else 'body'
        if fault and fault['phase']==phase and self.handler.path==fault['path'] and self.handler.command==fault['method']:
            self.handler.server.fail_next=None
            # Model a real closed socket, so an erroneous second response also
            # fails rather than merely hiding the first lost acknowledgement.
            self.handler.connection.shutdown(socket.SHUT_RDWR);self.handler.connection.close()
            raise fault['error']('Fictional browser disconnected during '+phase)
        return self.writer.write(data)


class TransportDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup);self.root=Path(self.temporary.name)
        for field,path in (('SETTINGS_FILE',self.root/'settings.json'),('OUTPUT_DIR',self.root/'outputs')):
            setting=patch.object(core,field,str(path));setting.start();self.addCleanup(setting.stop)
        base=make_handler(core)
        class Handler(base):
            def setup(self):super().setup();self.wfile=FaultWriter(self.wfile,self)
            def send_response(self,status,message=None):
                self.server.status_attempts.append((self.command,self.path,status));return super().send_response(status,message)
        class Server(core.HTTPServer):
            def handle_error(self,*_):self.transport_errors.append(type(sys.exc_info()[1]).__name__)
        self.server=Server(('127.0.0.1',0),Handler);self.server.fail_next=None;self.server.status_attempts=[];self.server.transport_errors=[]
        self.service=self.server.station=StationService(core);self.service.store.put('preferences',self.service.preferences()|{'pricingEnabled':False})
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.addCleanup(self.stop)
        self.client=requests.Session();self.client.trust_env=False;self.addCleanup(self.client.close);self.base=f'http://127.0.0.1:{self.server.server_port}'
        output=Path(core.OUTPUT_DIR);output.mkdir();(output/'fictional.pdf').write_bytes(b'%PDF-fictional-transport-fixture')

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3)

    def arm_disconnect(self,method,path,phase,error):
        self.server.fail_next={'method':method,'path':path,'phase':phase,'error':error};self.server.status_attempts.clear();self.server.transport_errors.clear()

    def test_get_json_static_and_download_disconnects_never_attempt_a_second_response(self):
        for path in ('/api/version','/station.css','/files/fictional.pdf'):
            for phase in ('headers','body'):
                for error in DISCONNECTS:
                    with self.subTest(path=path,phase=phase,error=error.__name__):
                        self.arm_disconnect('GET',path,phase,error)
                        with self.assertRaises(requests.RequestException):self.client.get(self.base+path,timeout=2).raise_for_status()
                        # The next request flushes any final handler cleanup.
                        healthy=self.client.get(self.base+'/api/version',timeout=2);self.assertEqual(healthy.status_code,200)
                        self.assertEqual(self.server.status_attempts,[('GET',path,200),('GET','/api/version',200)])
                        self.assertEqual(self.server.transport_errors,[])

    def test_completed_post_lost_reply_keeps_one_durable_mutation_and_same_key_retry(self):
        for phase in ('headers','body'):
            for error in DISCONNECTS:
                with self.subTest(phase=phase,error=error.__name__):
                    key=str(uuid.uuid4());body={'requestId':key,'game':'magic','mode':'labels','item':{'card_name':'Fictional transport item','qty':1,'final_price':2}}
                    before=len(self.service.store.summaries());self.arm_disconnect('POST','/api/manual-items',phase,error)
                    with self.assertRaises(requests.RequestException):self.client.post(self.base+'/api/manual-items',json=body,timeout=2).raise_for_status()
                    ident=self.service.store.find_source('manual:'+key);self.assertIsNotNone(ident)
                    saved=self.service.store.receipt(ident);revision=saved['revision'];self.assertEqual(len(self.service.store.summaries()),before+1)
                    replay=self.client.post(self.base+'/api/manual-items',json=body,timeout=2);self.assertEqual(replay.status_code,200)
                    self.assertEqual(replay.json()['receipt']['id'],ident);self.assertEqual(replay.json()['receipt']['revision'],revision)
                    self.assertEqual(len(self.service.store.summaries()),before+1)
                    with self.service.store.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM events WHERE receipt_id=? AND kind='manual_item'",(ident,)).fetchone()[0],1)
                    self.assertEqual(self.server.status_attempts,[('POST','/api/manual-items',200),('POST','/api/manual-items',200)])
                    self.assertEqual(self.server.transport_errors,[])

    def test_genuine_application_error_is_still_an_http_500_even_if_its_type_looks_like_disconnect(self):
        for failure in (RuntimeError('Fictional application failure'),ConnectionAbortedError('Application error outside response I/O')):
            with self.subTest(error=type(failure).__name__),patch.object(self.service,'public_settings',side_effect=failure):
                self.server.status_attempts.clear();result=self.client.get(self.base+'/api/settings',timeout=2)
                self.assertEqual(result.status_code,500);self.assertIn('could not read',result.json()['errors'][0])
                self.assertEqual(self.server.status_attempts,[('GET','/api/settings',500)])

    def test_serialization_errors_stay_visible_and_do_not_send_headers(self):
        handler=object.__new__(make_handler(core));handler.send_response=Mock();handler.send_header=Mock();handler.security_headers=Mock();handler.end_headers=Mock();handler.wfile=io.BytesIO()
        with self.assertRaises(TypeError):handler.send_json({'not_json_serializable':object()})
        handler.send_response.assert_not_called();self.assertEqual(handler.wfile.getvalue(),b'')

    def test_other_output_oserrors_are_not_suppressed(self):
        handler=object.__new__(make_handler(core));handler.close_connection=False;handler.send_response=Mock();handler.send_header=Mock();handler.security_headers=Mock();handler.end_headers=Mock();handler.wfile=SimpleNamespace(write=Mock(side_effect=OSError('Fictional genuine output failure')))
        with self.assertRaisesRegex(OSError,'genuine output failure'):handler.send_json({'ok':True})
        self.assertFalse(handler.close_connection)


if __name__=='__main__':unittest.main()
