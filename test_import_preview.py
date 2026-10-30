"""Import preview is read-only; mapping and stock semantics are deliberate."""
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
from tools.build_desktop import build_command


class ImportPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.patch=patch.object(core,'SETTINGS_FILE',str(self.root/'settings.json'));self.patch.start();self.addCleanup(self.patch.stop)
        self.service=StationService(core)
        self.service.store.put('preferences',self.service.preferences()|{'pricingEnabled':False})

    def test_unrecognized_headers_return_mapping_columns_and_samples_without_a_receipt(self):
        result=self.service.preview_import('Title,Copies,Sell USD,Game Kind\nFictional Card,3,1.13,Riftbound\n')
        preview=result['preview']
        self.assertFalse(preview['can_import']);self.assertEqual(preview['error_count'],1)
        self.assertEqual(preview['columns'],['Title','Copies','Sell USD','Game Kind'])
        self.assertEqual(preview['sample_rows'][0],['Fictional Card','3','1.13','Riftbound'])
        self.assertIn('card_name',{field['key'] for field in result['mapping_fields']})
        self.assertEqual(self.service.store.summaries(),[])
        with self.service.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM price_jobs').fetchone()[0],0)

    def test_mapping_selects_name_game_copies_and_exact_price_then_creates_only_a_draft(self):
        text='Title,Copies,Sell USD,Game Kind\nFictional Card,3,1.13,Riftbound\n'
        options={'adapter':'mapped','column_mapping':{'card_name':'Title','quantity':'Copies','price':'Sell USD','game':'Game Kind'},'quantity_column':'Copies','price_column':'Sell USD'}
        preview=self.service.preview_import(text,options)['preview']
        self.assertTrue(preview['can_import']);self.assertEqual(preview['row_count'],1)
        self.assertEqual((preview['rows'][0]['qty'],preview['rows'][0]['final_price'],preview['rows'][0]['game']),(3,1.13,'riftbound'))
        self.assertEqual(self.service.store.summaries(),[])
        receipt=self.service.import_csv(text,'fictional.csv','labels',intake_options=options)['receipt']
        self.assertEqual(receipt['inventory_status'],'not_received');self.assertFalse(receipt['cards'][0]['reviewed'])
        self.assertEqual(receipt['files'],{})

    def test_tcgplayer_quantity_meanings_and_snapshot_acknowledgement_are_not_interchangeable(self):
        text='Product Name,Product Line,Add to Quantity,Total Quantity,TCG Marketplace Price\nFictional,Magic,2,20,1.13\n'
        before=self.service.preview_import(text)['preview']
        self.assertFalse(before['can_import']);self.assertIn('quantity_selection',{issue['code'] for issue in before['issues']})
        added=self.service.preview_import(text,{'quantity_column':'Add to Quantity'})['preview']
        self.assertEqual(added['rows'][0]['qty'],2);self.assertFalse(added['requires_quantity_confirmation'])
        snapshot=self.service.preview_import(text,{'quantity_column':'Total Quantity'})['preview']
        self.assertEqual(snapshot['rows'][0]['qty'],20);self.assertTrue(snapshot['requires_quantity_confirmation'])
        self.assertTrue(snapshot['can_import'])
        with self.assertRaisesRegex(StationError,'snapshot'):
            self.service.import_csv(text,'snapshot.csv','labels',intake_options={'quantity_column':'Total Quantity'})
        self.assertEqual(self.service.store.summaries(),[])
        receipt=self.service.import_csv(text,'snapshot.csv','labels',intake_options={'quantity_column':'Total Quantity','confirm_snapshot':True})['receipt']
        self.assertEqual(receipt['cards'][0]['qty'],20);self.assertEqual(receipt['inventory_status'],'not_received')
        self.assertTrue(receipt['intake_summary']['quantity_snapshot_confirmed'])

    def test_preview_exposes_existing_and_deleted_batch_without_duplicate_or_restore(self):
        text='Name,Price,Quantity\nFictional,1,1\n'
        receipt=self.service.import_csv(text,'one.csv','labels')['receipt']
        existing=self.service.preview_import(text)['existing']
        self.assertEqual(existing['id'],receipt['id']);self.assertFalse(existing['deleted'])
        self.service.delete_receipt(receipt['id'],receipt['revision'])
        deleted=self.service.preview_import(text)['existing']
        self.assertTrue(deleted['deleted'])
        self.assertTrue(self.service.store.receipt(receipt['id'])['deleted_at'])
        with self.service.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM receipts').fetchone()[0],1)

    def test_errors_block_partial_commit_and_preview_payload_is_bounded(self):
        text='Name,Price,Quantity\nGood,1,1\nBad,-1,1\n'
        preview=self.service.preview_import(text)['preview']
        self.assertEqual(preview['row_count'],1);self.assertFalse(preview['can_import'])
        with self.assertRaises(StationError):self.service.import_csv(text,'bad.csv','labels')
        self.assertEqual(self.service.store.summaries(),[])
        many='Name,Price\n'+''.join(f'Fixture {index},1\n' for index in range(25))
        preview=self.service.preview_import(many)['preview']
        self.assertEqual(preview['row_count'],25);self.assertEqual(len(preview['rows']),15)
        self.assertEqual(len(preview['sample_rows']),5);self.assertEqual(len(preview['issues']),50)
        self.assertGreater(preview['issue_count'],50)

    def test_bad_mapping_types_have_actionable_errors_and_build_contains_assets(self):
        for content,options in ((None,None),('Name,Price\nA,1\n',[]),('Name,Price\nA,1\n',{'confirm_snapshot':'yes'})):
            with self.subTest(content=content,options=options),self.assertRaises(StationError):self.service.preview_import(content,options)
        command=build_command()
        self.assertTrue(any('import.js' in argument for argument in command))
        self.assertTrue(any('import.css' in argument for argument in command))


class ImportPreviewHTTPTests(unittest.TestCase):
    def test_loopback_preview_and_assets_preserve_boundary_and_do_not_create_receipts(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(core,'SETTINGS_FILE',str(Path(folder)/'settings.json')):
            server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);service=server.station=StationService(core)
            service.store.put('preferences',service.preferences()|{'pricingEnabled':False})
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            client=requests.Session();client.trust_env=False;base=f'http://127.0.0.1:{server.server_port}'
            try:
                body={'csv':'Name,Price\nFictional,1\n'}
                result=client.post(base+'/api/import/preview',json=body)
                self.assertEqual(result.status_code,200,result.text);self.assertTrue(result.json()['preview']['can_import'])
                self.assertEqual(service.store.summaries(),[])
                self.assertEqual(client.post(base+'/api/import/preview',json=body,headers={'Origin':'https://foreign.invalid'}).status_code,403)
                self.assertEqual(client.post(base+'/api/import/preview',json={'csv':None}).status_code,400)
                for path,mime in (('/import.js','javascript'),('/import.css','text/css')):
                    asset=client.get(base+path);self.assertEqual(asset.status_code,200);self.assertIn(mime,asset.headers['Content-Type'])
                self.assertIn('/import.js',client.get(base+'/').text)
            finally:
                client.close();server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':unittest.main(verbosity=2)
