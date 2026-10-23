import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import requests
import roca_to_square as core
from station_service import StationService
from station_store import StationError

CSV='Card Name,Card #,Condition,Printing,Set Code,Language,Price,Bin,Qty\nCard One,123,Near Mint,Normal,SET,English,10.00,2,2\nCard Two,124,NM,Surge Foil,SET,English,20.00,1,1\n'

class Response:
    status_code=200
    def __init__(self,data):self.data=data
    def json(self):return self.data
    def raise_for_status(self):pass

class StationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.settings=patch.object(core,'SETTINGS_FILE',str(self.root/'settings.json'));self.settings.start()
        self.outputs=patch.object(core,'OUTPUT_DIR',str(self.root/'outputs'));self.outputs.start()
        self.service=StationService(core)
    def tearDown(self):
        self.outputs.stop();self.settings.stop();self.temp.cleanup()
    def receipt(self,mode='labels',reviewed=True):
        r=self.service.import_csv(CSV,'cards.csv',mode)['receipt']
        if reviewed:
            for c in r['cards']:c['reviewed']=True
            r=self.service.save_receipt(r['id'],r)
        return r
    def connect(self):
        with patch.object(core,'square_locations',return_value=[{'id':'store','name':'Shop','status':'ACTIVE'}]):
            self.service.save_preferences({'squareToken':'fake-test-token','squareLocationId':'store'})
    def post(self,url,**kwargs):
        if url.endswith('/search'):return Response({})
        if url.endswith('/batch-upsert'):
            mappings=[]
            for obj in kwargs['json']['batches'][0]['objects']:
                variation=obj['item_data']['variations'][0]
                mappings.append({'client_object_id':variation['id'],'object_id':'real'+variation['id'][1:]})
            return Response({'id_mappings':mappings})
        return Response({})

    def test_import_all_bins_and_exact_file_deduplication(self):
        first=self.service.import_csv(CSV,'first.csv')['receipt']
        second=self.service.import_csv(CSV,'renamed.csv')
        self.assertEqual(len(first['cards']),2)
        self.assertTrue(second['duplicate']);self.assertEqual(second['receipt']['id'],first['id'])
        new=self.service.import_csv(CSV,'new-delivery.csv',new_delivery=True)
        self.assertNotEqual(new['receipt']['id'],first['id'])

    def test_review_required_and_labels_never_receive(self):
        r=self.receipt(reviewed=False)
        with self.assertRaisesRegex(StationError,'Review'):
            self.service.labels(r['id'])
        for c in r['cards']:c['reviewed']=True
        r=self.service.save_receipt(r['id'],r)
        with patch.object(core,'upload_to_square') as upload:
            r=self.service.labels(r['id']);upload.assert_not_called()
        self.assertEqual(r['inventory_status'],'not_received')
        self.assertTrue((Path(core.OUTPUT_DIR)/r['files']['pdf']).exists())

    def test_trade_acceptance_survives_tab_moves_and_locks_identity(self):
        r=self.receipt('trade');r['mode']='labels';r=self.service.save_receipt(r['id'],r)
        with self.assertRaisesRegex(StationError,'accepted buy'):
            self.service.labels(r['id'])
        r['mode']='trade';r=self.service.save_receipt(r['id'],r)
        r=self.service.accept_buy(r['id'],'cash')
        self.assertEqual(r['purchase']['amount'],14)
        r['cards'][0]['printing']='Etched Foil'
        with self.assertRaisesRegex(StationError,'locked'):
            self.service.save_receipt(r['id'],r)
        self.assertEqual(self.service.accept_buy(r['id'],'cash')['purchase']['amount'],14)

    def test_identity_edits_generate_new_sku_and_any_finish_is_supported(self):
        r=self.receipt();old=r['cards'][0]['sku'];r['cards'][0]['printing']='Future Custom Foil'
        r=self.service.save_receipt(r['id'],r)
        self.assertNotEqual(r['cards'][0]['sku'],old)
        self.assertEqual(r['cards'][0]['printing'],'Future Custom Foil')

    def test_split_preserves_copies_and_optimistic_revision(self):
        r=self.receipt();first=dict(r['cards'][0]);r['cards'][0]['qty']=1
        first.update(row_id='new-row',split_from=first['row_id'],qty=1,reviewed=False)
        r['cards'].append(first);old=json.loads(json.dumps(r))
        r=self.service.save_receipt(r['id'],r)
        self.assertEqual(sum(c['qty'] for c in r['cards']),3)
        with self.assertRaisesRegex(StationError,'another window'):
            self.service.save_receipt(r['id'],old)

    def test_receiving_same_receipt_twice_is_noop(self):
        self.connect();r=self.receipt()
        with patch.object(core.requests,'get',return_value=Response({'locations':[{'id':'store','status':'ACTIVE'}]})),patch.object(core.requests,'post',side_effect=self.post) as post:
            result=self.service.receive(r['id']);self.assertEqual(result['inventory_status'],'received')
            count=post.call_count
            self.service.receive(r['id']);self.assertEqual(post.call_count,count)
        self.assertEqual(result['inventory_result']['inventory_count'],3)

    def test_lost_inventory_response_reuses_identical_saved_payload(self):
        self.connect();r=self.receipt();stock=[]
        def post(url,**kwargs):
            if url.endswith('/changes/batch-create'):
                stock.append(json.loads(json.dumps(kwargs['json'])))
                if len(stock)==1:raise requests.Timeout('response lost')
                return Response({})
            return self.post(url,**kwargs)
        with patch.object(core.requests,'get',return_value=Response({'locations':[{'id':'store','status':'ACTIVE'}]})),patch.object(core.requests,'post',side_effect=post):
            failed=self.service.receive(r['id'])
            self.assertEqual(failed['inventory_status'],'needs_attention')
            # Reconstruct the station to verify the request journal survives a restart.
            service=StationService(core)
            success=service.receive(r['id'])
        self.assertEqual(success['inventory_status'],'received')
        self.assertEqual(len(stock),2);self.assertEqual(stock[0],stock[1])
        self.assertEqual(stock[0]['changes'][0]['adjustment']['from_state'],'NONE')

    def test_existing_stock_and_bulk_cannot_be_received(self):
        r=self.receipt('bulk')
        with self.assertRaisesRegex(StationError,'Bulk sift'):self.service.receive(r['id'])
        r['mode']='labels';r['inventory_intent']='existing';r=self.service.save_receipt(r['id'],r)
        with self.assertRaisesRegex(StationError,'already-owned'):self.service.receive(r['id'])

    def test_online_preparation_works_for_display_and_does_not_publish(self):
        r=self.receipt('online');r['storage_strategy']='display';r['storage_location']='Case 2 shelf B'
        r=self.service.save_receipt(r['id'],r)
        with patch.object(core,'upload_to_square') as upload:
            result=self.service.prepare_online(r['id']);upload.assert_not_called()
        self.assertTrue(result['prepared']);self.assertEqual(result['inventory_status'],'not_received')
        exported=(Path(core.OUTPUT_DIR)/result['files']['listing']).read_text(encoding='utf-8-sig')
        self.assertIn('Case 2 shelf B',exported);self.assertIn('Surge Foil',exported)

    def test_legacy_token_migration_removes_plaintext(self):
        Path(core.SETTINGS_FILE).write_text(json.dumps({'squareToken':'migration-test-token','printerDpi':300}))
        service=StationService(core)
        self.assertEqual(service.vault.read(),'migration-test-token')
        self.assertNotIn('squareToken',Path(core.SETTINGS_FILE).read_text())
        self.assertNotIn(b'migration-test-token',service.vault.path.read_bytes())

    def test_five_login_failures_are_rate_limited(self):
        self.service.auth.set_password('Long-enough-test-password')
        for _ in range(5):
            with self.assertRaisesRegex(StationError,'Incorrect'):self.service.auth.login('wrong','test')
        with self.assertRaisesRegex(StationError,'five minutes'):
            self.service.auth.login('Long-enough-test-password','test')

    def test_task_confirmation_does_not_receive_inventory(self):
        task=self.service.create_task({'order_reference':'order123','card_name':'Card','storage_location':'Case 1','qty':1})
        with patch.object(core,'upload_to_square') as upload:
            self.assertEqual(self.service.complete_task(task['id'])['status'],'pulled');upload.assert_not_called()

    def test_preflight_failure_leaves_receipt_editable(self):
        self.connect();r=self.receipt()
        with patch.object(core.requests,'get',return_value=Response({'locations':[{'id':'different','name':'Other','status':'ACTIVE'}]})):
            result=self.service.receive(r['id'])
        self.assertEqual(result['inventory_status'],'not_received')
        result['cards'][0]['final_price']=11
        self.assertEqual(self.service.save_receipt(result['id'],result)['cards'][0]['final_price'],11)

    def test_folder_pickup_is_draft_only_and_bad_file_does_not_block_good_file(self):
        import time
        folder=self.root/'exports';folder.mkdir()
        self.service.save_preferences({'watchFolder':str(folder),'watchMode':'bulk'})
        (folder/'bad.csv').write_text('Wrong,Header\nnot,a card\n')
        (folder/'good.csv').write_text(CSV)
        self.service.start_watcher()
        try:
            deadline=time.monotonic()+8
            while time.monotonic()<deadline and not self.service.store.summaries():
                time.sleep(.05)
            receipts=self.service.store.summaries()
            self.assertEqual(len(receipts),1)
            self.assertEqual(receipts[0]['inventory_status'],'not_received')
            self.assertEqual(receipts[0]['mode'],'bulk')
            self.assertEqual(receipts[0]['units'],3)
        finally:
            self.service.stop_event.set();self.service.watcher.join(timeout=3)


class AdminHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.patcher=patch.object(core,'SETTINGS_FILE',str(self.path/'settings.json'));self.patcher.start()
        self.server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler)
        self.server.station=StationService(core)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.client=requests.Session();self.client.trust_env=False
    def tearDown(self):
        self.client.close();self.server.shutdown();self.server.server_close();self.thread.join();self.patcher.stop();self.temp.cleanup()
    def post(self,path,data):return self.client.post(self.base+path,json=data)
    def test_admin_setup_and_protected_settings_without_token_echo(self):
        self.assertEqual(self.client.get(self.base+'/api/admin/settings').status_code,403)
        password='Test-password-only-246!'
        result=self.post('/api/auth/setup',{'password':password})
        self.assertEqual(result.status_code,200)
        cookie=result.headers['Set-Cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
        token='not-a-real-square-token'
        with patch.object(core,'square_locations',return_value=[{'id':'store','name':'Shop','status':'ACTIVE'}]):
            result=self.post('/api/admin/settings',{'squareToken':token,'squareLocationId':'store'})
        self.assertEqual(result.status_code,200,result.text);self.assertNotIn(token,result.text)
        self.assertTrue(result.json()['tokenSaved'])
        public=self.client.get(self.base+'/api/settings').text
        self.assertNotIn('squareToken',public);self.assertNotIn(token,public)
        self.post('/api/auth/logout',{})
        self.assertEqual(self.post('/api/admin/settings',{'squareToken':'replacement'}).status_code,403)
        self.assertEqual(self.post('/api/auth/setup',{'password':password}).status_code,400)
        self.assertEqual(self.post('/api/auth/login',{'password':password}).status_code,200)
        for file in (self.path/'station_data').iterdir():
            if file.is_file():
                data=file.read_bytes();self.assertNotIn(token.encode(),data);self.assertNotIn(password.encode(),data)

    def test_admin_location_check_requires_login_and_never_echoes_token(self):
        self.assertEqual(self.post('/api/admin/locations',{}).status_code,403)
        self.post('/api/auth/setup',{'password':'Password-test-only-246!'})
        with patch.object(core,'square_locations',return_value=[{'id':'shop','name':'Shop','status':'ACTIVE'}]) as lookup:
            response=self.post('/api/admin/locations',{'squareToken':'fake-token','squareEnvironment':'sandbox'})
            self.assertEqual(response.status_code,200);self.assertNotIn('fake-token',response.text)
            lookup.assert_called_once_with('fake-token','sandbox')

    def test_password_change_revokes_session(self):
        self.post('/api/auth/setup',{'password':'Password-test-only-246!'})
        self.assertEqual(self.post('/api/admin/password',{'password':'Replacement-test-only-246!'}).status_code,200)
        self.assertEqual(self.client.get(self.base+'/api/admin/settings').status_code,403)

    def test_foreign_origin_and_non_json_requests_are_rejected(self):
        result=self.client.post(self.base+'/api/auth/setup',json={'password':'Password-test-only-246!'},headers={'Origin':'https://unrelated.example'})
        self.assertEqual(result.status_code,403)
        self.assertEqual(self.client.post(self.base+'/api/import',data='{}').status_code,415)

if __name__=='__main__':unittest.main()
