import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
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
        r['inventory_intent']='new'
        r=self.service.save_receipt(r['id'],r)
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

    def test_new_station_starts_with_processing_but_preserves_saved_workflow(self):
        self.assertEqual(self.service.preferences()['watchMode'],'labels')
        self.service.save_preferences({'watchMode':'bulk'})
        restarted=StationService(core)
        self.assertEqual(restarted.preferences()['watchMode'],'bulk')

    def manual_data(self, receipt=None, **item):
        return {'requestId':str(uuid.uuid4()), 'mode':'labels',
                'receiptId':receipt['id'] if receipt else None,
                'revision':receipt['revision'] if receipt else None,
                'item':dict(card_name='Charizard',condition='PSA 10',final_price='85.13',qty='2',**item)}

    def test_manual_item_needs_no_csv_keeps_exact_price_and_distinguishes_grade(self):
        with patch.object(core,'upload_to_square') as upload:
            r=self.service.add_manual_item(self.manual_data())
            self.assertEqual(len(r['cards']),1)
            card=r['cards'][0]
            self.assertEqual(card['final_price'],85.13)
            self.assertEqual(card['qty'],2)
            self.assertEqual(card['condition'],'PSA 10')
            self.assertFalse(card['reviewed']);self.assertIsNone(r['inventory_intent'])
            self.assertEqual(r['files'],{});self.assertEqual(r['inventory_status'],'not_received')
            second=self.manual_data();second['item']['condition']='PSA 9'
            other=self.service.add_manual_item(second)
            self.assertNotEqual(card['sku'],other['cards'][0]['sku'])
            third=self.service.add_manual_item(self.manual_data())
            self.assertEqual(card['sku'],third['cards'][0]['sku'])
            upload.assert_not_called()

    def test_manual_addition_keeps_scanned_rows_and_invalidates_stale_documents(self):
        r=self.receipt();r=self.service.labels(r['id'])
        old_cards=r['cards'];old_hash=r['source_hash']
        result=self.service.add_manual_item(self.manual_data(r))
        self.assertEqual(result['cards'][:-1],old_cards)
        self.assertEqual(result['source_hash'],old_hash)
        self.assertEqual(result['files'],{})
        self.assertFalse(result['prepared'])
        self.assertFalse(result['cards'][-1]['reviewed'])
        self.assertEqual(self.service.import_csv(CSV,'same-export.csv')['receipt']['id'],r['id'])
        with self.assertRaisesRegex(StationError,'Review'):
            self.service.labels(r['id'])

    def test_manual_item_retries_do_not_duplicate_new_or_existing_batches(self):
        data=self.manual_data();first=self.service.add_manual_item(data)
        restarted=StationService(core)
        self.assertEqual(restarted.add_manual_item(data)['revision'],first['revision'])
        self.assertEqual(len(restarted.store.summaries()),1)
        more=self.manual_data(first)
        second=restarted.add_manual_item(more)
        again=restarted.add_manual_item(more)
        self.assertEqual(again['revision'],second['revision'])
        self.assertEqual(len(again['cards']),2)
        more['item']['final_price']='90.00'
        with self.assertRaisesRegex(StationError,'already saved'):
            restarted.add_manual_item(more)

    def test_manual_items_reject_stale_deleted_and_committed_batches(self):
        r=self.receipt();data=self.manual_data(r);data['revision']-=1
        with self.assertRaisesRegex(StationError,'another window'):
            self.service.add_manual_item(data)
        for status in ('received','needs_attention','committing'):
            r['inventory_status']=status;self.service.store.save_receipt(r)
            with self.assertRaisesRegex(StationError,'locked'):
                self.service.add_manual_item(self.manual_data(r))
        r['inventory_status']='not_received';self.service.store.save_receipt(r)
        self.service.delete_receipt(r['id'],r['revision'])
        with self.assertRaisesRegex(StationError,'deleted'):
            self.service.add_manual_item(self.manual_data(r))
        trade=self.service.import_csv(CSV,'new-trade.csv','trade',new_delivery=True)['receipt']
        for card in trade['cards']:card['reviewed']=True
        trade=self.service.save_receipt(trade['id'],trade)
        trade=self.service.accept_buy(trade['id'],'cash')
        with self.assertRaisesRegex(StationError,'locked'):
            self.service.add_manual_item(self.manual_data(trade))

    def test_manual_item_validation_cannot_bypass_required_fields_or_review(self):
        for key,value in [('card_name',''),('condition',''),('qty','0'),('qty','1.5'),
                          ('final_price','NaN'),('final_price','-1'),('qty','100001')]:
            data=self.manual_data();data['item'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(StationError):
                self.service.add_manual_item(data)
        self.assertEqual(self.service.store.summaries(),[])
        data=self.manual_data();data['item'].update(reviewed=True,include=False,entry_source='scanner')
        r=self.service.add_manual_item(data)
        self.assertFalse(r['cards'][0]['reviewed']);self.assertTrue(r['cards'][0]['include'])
        self.assertEqual(r['cards'][0]['entry_source'],'manual')

    def test_manual_trade_uses_unchanged_buying_percentages(self):
        data=self.manual_data();data['mode']='trade'
        r=self.service.add_manual_item(data)
        with self.assertRaisesRegex(StationError,'Review'):
            self.service.accept_buy(r['id'],'cash')
        r['cards'][0]['reviewed']=True;r=self.service.save_receipt(r['id'],r)
        self.assertEqual(self.service.accept_buy(r['id'],'cash')['purchase']['amount'],59.59)

    def test_manual_item_creates_existing_labels_and_square_inventory_with_safe_reprints(self):
        self.connect();r=self.service.add_manual_item(self.manual_data())
        r['inventory_intent']='new';r['cards'][0]['reviewed']=True
        r=self.service.save_receipt(r['id'],r)
        with patch.object(core.requests,'get',return_value=Response({'locations':[{'id':'store','status':'ACTIVE'}]})),patch.object(core.requests,'post',side_effect=self.post) as posted:
            r=self.service.receive(r['id'])
        self.assertEqual(r['inventory_status'],'received')
        catalog=next(c.kwargs['json'] for c in posted.call_args_list if c.args[0].endswith('/batch-upsert'))
        variation=catalog['batches'][0]['objects'][0]['item_data']['variations'][0]['item_variation_data']
        self.assertEqual(variation['price_money']['amount'],8513)
        stock=next(c.kwargs['json'] for c in posted.call_args_list if '/inventory/' in c.args[0])
        self.assertEqual(stock['changes'][0]['adjustment']['quantity'],'2')
        with patch.object(core,'upload_to_square') as upload:
            self.service.receive(r['id']);r=self.service.labels(r['id']);upload.assert_not_called()
        import fitz
        with fitz.open(Path(core.OUTPUT_DIR)/r['files']['pdf']) as pdf:
            self.assertEqual(len(pdf),2)
            text=' '.join(page.get_text() for page in pdf)
        self.assertIn('PSA 10',text);self.assertIn('$85.13',text)

    def test_import_all_bins_and_exact_file_deduplication(self):
        first=self.service.import_csv(CSV,'first.csv')['receipt']
        second=self.service.import_csv(CSV,'renamed.csv')
        self.assertEqual(len(first['cards']),2)
        self.assertTrue(second['duplicate']);self.assertEqual(second['receipt']['id'],first['id'])
        new=self.service.import_csv(CSV,'new-delivery.csv',new_delivery=True)
        self.assertNotEqual(new['receipt']['id'],first['id'])

    def test_inventory_choice_is_explicit_and_separate_for_each_batch(self):
        first=self.service.import_csv(CSV,'first.csv','labels')['receipt']
        self.assertIsNone(first['inventory_intent'])
        with patch.object(core,'upload_to_square') as upload:
            with self.assertRaisesRegex(StationError,'Choose whether'):
                self.service.receive(first['id'])
            upload.assert_not_called()
        first['inventory_intent']='existing'
        self.service.save_receipt(first['id'],first)
        second=self.service.import_csv(CSV,'next-delivery.csv','labels',new_delivery=True)['receipt']
        self.assertIsNone(second['inventory_intent'])
        self.assertEqual(self.service.store.receipt(first['id'])['inventory_intent'],'existing')

    def test_delete_and_restore_draft_preserves_source_and_blocks_hidden_writes(self):
        receipt=self.receipt()
        with patch.object(core,'upload_to_square') as upload:
            deleted=self.service.delete_receipt(receipt['id'],receipt['revision'])
            self.assertTrue(deleted['deleted_at'])
            self.assertEqual(self.service.store.summaries(),[])
            for action in (lambda:self.service.save_receipt(receipt['id'],receipt),
                           lambda:self.service.labels(receipt['id']),
                           lambda:self.service.receive(receipt['id']),
                           lambda:self.service.prepare_online(receipt['id']),
                           lambda:self.service.accept_buy(receipt['id'],'cash')):
                with self.assertRaisesRegex(StationError,'deleted'):
                    action()
            duplicate=self.service.import_csv(CSV,'renamed.csv')
            self.assertTrue(duplicate['duplicate'])
            self.assertEqual(duplicate['receipt']['id'],receipt['id'])
            self.assertTrue(duplicate['receipt']['deleted_at'])
            restored=self.service.restore_receipt(receipt['id'])
            self.assertFalse(restored.get('deleted_at'))
            self.assertEqual(restored['cards'],receipt['cards'])
            self.assertEqual(len(self.service.store.summaries()),1)
            upload.assert_not_called()

    def test_deleting_received_stock_keeps_journal_and_cannot_receive_it_again(self):
        self.connect();receipt=self.receipt()
        with patch.object(core.requests,'get',return_value=Response({'locations':[{'id':'store','status':'ACTIVE'}]})),patch.object(core.requests,'post',side_effect=self.post):
            received=self.service.receive(receipt['id'])
        self.assertEqual(received['inventory_status'],'received')
        with self.service.store.db() as db:
            original=[tuple(row) for row in db.execute('SELECT * FROM operations WHERE receipt_id=?',(receipt['id'],))]
        self.assertTrue(original)
        with patch.object(core,'upload_to_square') as upload:
            self.service.delete_receipt(receipt['id'],received['revision'])
            restarted=StationService(core)
            self.assertTrue(restarted.import_csv(CSV,'same.csv')['duplicate'])
            restored=restarted.restore_receipt(receipt['id'])
            self.assertEqual(restored['inventory_status'],'received')
            self.assertEqual(restarted.receive(receipt['id'])['inventory_status'],'received')
            with restarted.store.db() as db:
                self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM operations WHERE receipt_id=?',(receipt['id'],))],original)
            upload.assert_not_called()

    def test_delete_rejects_stale_revision_and_unresolved_receiving(self):
        receipt=self.receipt()
        with self.assertRaisesRegex(StationError,'another window'):
            self.service.delete_receipt(receipt['id'],receipt['revision']-1)
        for status in ('committing','needs_attention'):
            receipt['inventory_status']=status
            self.service.store.save_receipt(receipt)
            with self.assertRaisesRegex(StationError,'Resolve'):
                self.service.delete_receipt(receipt['id'],receipt['revision'])
        self.assertEqual(len(self.service.store.summaries()),1)

    def test_delete_and_restore_accepted_buy_keeps_purchase_and_output(self):
        receipt=self.service.accept_buy(self.receipt('trade')['id'],'cash')
        self.service.delete_receipt(receipt['id'],receipt['revision'])
        restored=self.service.restore_receipt(receipt['id'])
        self.assertEqual(restored['purchase'],receipt['purchase'])
        self.assertEqual(restored['files'],receipt['files'])
        self.assertTrue(restored['requires_purchase'])

    def test_old_database_receipts_survive_deleted_column_migration(self):
        import sqlite3
        from station_store import Store
        root=self.root/'legacy';root.mkdir()
        with sqlite3.connect(root/'station.sqlite3') as db:
            db.execute('CREATE TABLE receipts (id TEXT PRIMARY KEY, source_hash TEXT, updated TEXT NOT NULL, body TEXT NOT NULL)')
            db.execute('INSERT INTO receipts VALUES (?,?,?,?)',('legacy','source','2026-01-01',json.dumps({'id':'legacy','cards':[]})))
        store=Store(root)
        self.assertEqual(store.summaries()[0]['id'],'legacy')
        self.assertEqual(store.find_source('source'),'legacy')
        self.assertEqual(store.pickup_queue(),[])
        store.save_receipt(store.receipt('legacy'))
        self.assertEqual(len(Store(root).summaries()),1)

    def test_downloads_shortcut_respects_windows_folder_redirection(self):
        import ctypes
        import station_service
        from types import SimpleNamespace
        from unittest.mock import Mock
        folder=self.root/'Redirected Downloads';folder.mkdir()
        buffer=ctypes.create_unicode_buffer(str(folder))
        def known_folder(folder_id,flags,token,result):
            ctypes.cast(result,ctypes.POINTER(ctypes.c_wchar_p))[0]=ctypes.cast(buffer,ctypes.c_wchar_p)
            return 0
        lookup=Mock(side_effect=known_folder);free=Mock()
        libraries=SimpleNamespace(shell32=SimpleNamespace(SHGetKnownFolderPath=lookup),
                                  ole32=SimpleNamespace(CoTaskMemFree=free))
        with patch.object(station_service,'os',SimpleNamespace(name='nt')),patch.object(ctypes,'windll',libraries,create=True):
            self.assertEqual(self.service.downloads_folder(),str(folder))
        free.assert_called_once()

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

    def pickup_folder(self):
        folder=self.root/'exports';folder.mkdir()
        self.service.save_preferences({'watchFolder':str(folder),'watchMode':'labels'})
        return folder

    def test_pickup_skips_old_files_and_waits_for_finished_download(self):
        folder=self.root/'exports';folder.mkdir()
        (folder/'old.csv').write_text(CSV)
        self.service.save_preferences({'watchFolder':str(folder),'watchMode':'labels'})
        path=folder/'new.CSV.crdownload';path.write_text(CSV.replace('Card One','New card'))
        self.service.poll_exports();self.service.poll_exports()
        self.assertEqual(self.service.store.summaries(),[])
        path=path.rename(folder/'new.CSV')
        self.service.poll_exports()
        self.assertEqual(self.service.store.summaries(),[])
        path.write_text(CSV.replace('Card One','A changed card'))
        self.service.poll_exports()
        self.assertEqual(self.service.store.summaries(),[])
        self.service.poll_exports()
        queue=self.service.store.pickup_queue()
        self.assertEqual(len(queue),1)
        self.assertEqual(queue[0]['source_name'],'new.CSV')
        r=self.service.store.receipt(queue[0]['id'])
        self.assertEqual(r['cards'][0]['card_name'],'A changed card')
        self.assertFalse(any(c['reviewed'] for c in r['cards']))
        self.assertIsNone(r['inventory_intent'])
        self.assertEqual(r['files'],{})
        self.assertEqual(r['inventory_status'],'not_received')

    def test_pickup_restarts_with_pending_queue_and_finds_exports_saved_while_closed(self):
        folder=self.pickup_folder()
        (folder/'first.csv').write_text(CSV)
        self.service.poll_exports();self.service.poll_exports()
        first=self.service.store.pickup_queue()[0]['id']
        (folder/'second.csv').write_text(CSV.replace('Card One','Second card'))
        restarted=StationService(core)
        with patch.object(core,'upload_to_square') as upload,patch.object(restarted,'labels') as labels:
            restarted.poll_exports();restarted.poll_exports()
            self.assertEqual(len(restarted.store.pickup_queue()),2)
            self.assertEqual(restarted.store.pickup_queue()[0]['id'],first)
            opened=restarted.acknowledge_pickup(first)
            self.assertFalse(opened['pickup_pending'])
            self.assertEqual(restarted.acknowledge_pickup(first)['revision'],opened['revision'])
            self.assertEqual(len(restarted.store.pickup_queue()),1)
            self.assertEqual(len(restarted.store.summaries()),2)
            upload.assert_not_called();labels.assert_not_called()

    def test_pickup_duplicate_reexports_do_not_queue_completed_or_deleted_batches(self):
        folder=self.pickup_folder()
        (folder/'first.csv').write_text(CSV)
        self.service.poll_exports();self.service.poll_exports()
        r=self.service.acknowledge_pickup(self.service.store.pickup_queue()[0]['id'])
        for name in ['renamed.csv','browser download (1).csv']:
            (folder/name).write_text(CSV)
        self.service.poll_exports();self.service.poll_exports()
        self.assertEqual(self.service.store.pickup_queue(),[])
        self.assertEqual(len(self.service.store.summaries()),1)
        self.service.delete_receipt(r['id'],r['revision'])
        (folder/'another.csv').write_text(CSV)
        self.service.poll_exports();self.service.poll_exports()
        self.assertEqual(self.service.store.pickup_queue(),[])
        self.assertEqual(self.service.store.summaries(),[])

    def test_pickup_bad_file_isolated_corrected_and_retried(self):
        folder=self.pickup_folder()
        (folder/'bad.csv').write_text('Wrong,Header\nnot,a card\n')
        (folder/'good.csv').write_text(CSV)
        self.service.poll_exports();self.service.poll_exports()
        self.assertEqual(len(self.service.store.pickup_queue()),1)
        self.assertEqual(self.service.watch_status['state'],'error')
        (folder/'bad.csv').write_text(CSV.replace('Card One','Corrected card'))
        self.service.poll_exports();self.service.poll_exports()
        self.assertEqual(len(self.service.store.pickup_queue()),2)
        self.assertEqual(self.service.watch_status['state'],'watching')

    def test_pickup_retries_locked_file_without_blocking_other_exports(self):
        folder=self.pickup_folder()
        locked=folder/'locked.csv';locked.write_text(CSV)
        (folder/'good.csv').write_text(CSV.replace('Card One','Readable card'))
        self.service.poll_exports()
        read=Path.read_text
        def sometimes_locked(path,*args,**kwargs):
            if path==locked:raise PermissionError('locked by downloader')
            return read(path,*args,**kwargs)
        with patch.object(Path,'read_text',sometimes_locked):self.service.poll_exports()
        self.assertEqual(len(self.service.store.pickup_queue()),1)
        self.assertEqual(self.service.watch_status['state'],'error')
        self.service.poll_exports()
        self.assertEqual(len(self.service.store.pickup_queue()),2)
        self.assertEqual(self.service.watch_status['state'],'watching')

    def test_switching_pickup_folder_baselines_old_files_and_retains_waiting_batches(self):
        folder=self.pickup_folder();(folder/'first.csv').write_text(CSV)
        self.service.poll_exports();self.service.poll_exports()
        other=self.root/'other';other.mkdir()
        (other/'old.csv').write_text(CSV.replace('Card One','Historical card'))
        self.service.save_preferences({'watchFolder':str(other)})
        self.service.poll_exports();self.service.poll_exports()
        self.assertEqual(len(self.service.store.pickup_queue()),1)
        self.service.save_preferences({'watchFolder':''})
        (other/'new.csv').write_text(CSV.replace('Card One','Disabled card'))
        self.service.poll_exports();self.service.poll_exports()
        self.assertEqual(len(self.service.store.pickup_queue()),1)
        self.assertEqual(self.service.watch_status['state'],'off')

    def test_application_import_does_not_require_cryptography(self):
        import subprocess,sys
        script = """import sys,importlib.abc
class NoCrypto(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'cryptography' or fullname.startswith('cryptography.'):
            raise ImportError('cryptography intentionally unavailable')
sys.meta_path.insert(0,NoCrypto())
import roca_to_square
assert 'cryptography' not in sys.modules
print('Application imports without cryptography')
"""
        result=subprocess.run([sys.executable,'-c',script],cwd=Path(core.__file__).parent,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_windows_vault_uses_dpapi_without_loading_fernet(self):
        import station_store
        actual_os=station_store.os
        class WindowsOS:
            name='nt'
            def __getattr__(self,key):return getattr(actual_os,key)
        vault=self.service.vault
        with patch.object(station_store,'os',WindowsOS()),patch.object(vault,'dpapi',side_effect=lambda data,encrypt:data[::-1]) as dpapi,patch.object(vault,'fernet',side_effect=AssertionError('Fernet must not be used on Windows')):
            vault.set('test-only-token')
            self.assertEqual(vault.read(),'test-only-token')
            self.assertTrue(vault.path.read_bytes().startswith(b'DP1'))
            self.assertGreaterEqual(dpapi.call_count,3)


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

    def test_downloads_shortcut_is_admin_only_and_saves_no_settings_until_approved(self):
        folder=self.path/'Downloads';folder.mkdir()
        self.assertEqual(self.post('/api/admin/downloads-folder',{}).status_code,403)
        self.post('/api/auth/setup',{'password':'Only-test-password-246!'})
        with patch.object(self.server.station,'downloads_folder',return_value=str(folder)):
            result=self.post('/api/admin/downloads-folder',{})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['folder'],str(folder))
        self.assertEqual(self.server.station.preferences()['watchFolder'],'')

    def test_pickup_queue_is_visible_and_acknowledged_without_stock_changes(self):
        r=self.server.station.import_csv(CSV,'export.csv','labels',intake_source='folder')['receipt']
        self.assertEqual(self.client.get(self.base+'/api/receipts').json()['pickupQueue'][0]['id'],r['id'])
        with patch.object(core,'upload_to_square') as upload:
            result=self.post('/api/receipts/'+r['id']+'/pickup-opened',{})
            self.assertEqual(result.status_code,200)
            self.assertFalse(result.json()['receipt']['pickup_pending'])
            self.assertEqual(self.client.get(self.base+'/api/receipts').json()['pickupQueue'],[])
            upload.assert_not_called()

    def test_manual_item_endpoint_creates_and_appends_without_stock_changes(self):
        payload={'requestId':str(uuid.uuid4()),'mode':'trade',
                 'item':{'card_name':'Graded card','condition':'PSA 10','final_price':'100.13','qty':'1'}}
        with patch.object(core,'upload_to_square') as upload:
            result=self.post('/api/manual-items',payload)
            self.assertEqual(result.status_code,200,result.text)
            r=result.json()['receipt']
            self.assertEqual(r['mode'],'trade');self.assertTrue(r['requires_purchase'])
            self.assertFalse(r['cards'][0]['reviewed'])
            payload.update(requestId=str(uuid.uuid4()),receiptId=r['id'],revision=r['revision'])
            result=self.post('/api/manual-items',payload)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(len(result.json()['receipt']['cards']),2)
            upload.assert_not_called()

    def test_direct_price_links_do_not_fetch_prices_or_change_receipts(self):
        before=self.server.station.store.summaries()
        result=self.post('/api/pricing/links',{'game':'pokemon','card_name':'Charizard ex','card_set':'151',
                                             'card_num':'006/165','condition':'PSA 10','printing':'Holofoil','language':'English'})
        self.assertEqual(result.status_code,200,result.text)
        data=result.json();self.assertIs(data['livePricesFetched'],False)
        self.assertEqual(data['links']['tcgplayer_role'],'raw_card_reference')
        self.assertIn('LH_Sold=1',data['links']['ebay_sold'])
        self.assertNotIn('LH_Sold',data['links']['ebay_listings'])
        self.assertIn('www.pricecharting.com',data['links']['pricecharting'])
        self.assertEqual(self.server.station.store.summaries(),before)
        result=self.post('/api/pricing/links',{'game':'pokemon','card_name':'CGC card','condition':'CGC 10'})
        self.assertTrue(result.json()['gradeNeedsReview'])
        self.assertEqual(self.post('/api/pricing/links',{'game':'pokemon','card_name':''}).status_code,400)

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

    def test_delete_and_restore_receipt_endpoints(self):
        receipt=self.post('/api/import',{'csv':CSV,'fileName':'sample.csv','mode':'labels'}).json()['receipt']
        path='/api/receipts/'+receipt['id']
        self.assertEqual(self.post(path+'/delete',{}).status_code,400)
        foreign=self.client.post(self.base+path+'/delete',json={'revision':receipt['revision']},headers={'Origin':'https://unrelated.example'})
        self.assertEqual(foreign.status_code,403)
        self.assertEqual(self.post(path+'/delete',{'revision':receipt['revision']}).status_code,200)
        self.assertEqual(self.client.get(self.base+'/api/receipts').json()['receipts'],[])
        self.assertEqual(self.post(path+'/receive',{}).status_code,400)
        restored=self.post(path+'/restore',{}).json()['receipt']
        self.assertEqual(restored['id'],receipt['id'])
        self.assertIsNone(restored['inventory_intent'])
        self.assertEqual(len(self.client.get(self.base+'/api/receipts').json()['receipts']),1)

if __name__=='__main__':unittest.main()
