import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import roca_to_square as app

CARD = dict(card_name='Black Lotus', card_num='233', condition='Near Mint',
            printing='Normal', final_price=125.50, sku='BL-233-NM-N-001', qty=3)

class Response:
    status_code = 200
    text = ''
    def __init__(self, data): self.data = data
    def json(self): return self.data
    def raise_for_status(self): pass

class Tests(unittest.TestCase):
    def test_inventory_uses_real_variation_location_and_quantity(self):
        calls = []
        def post(url, **kwargs):
            calls.append((url, kwargs['json']))
            if url.endswith('/search'): return Response({})
            if url.endswith('/batch-upsert'):
                obj = kwargs['json']['batches'][0]['objects'][0]
                variation = obj['item_data']['variations'][0]
                self.assertEqual(variation['item_variation_data']['sku'], app.barcode_value(CARD))
                self.assertTrue(variation['item_variation_data']['track_inventory'])
                self.assertNotIn('upc', variation['item_variation_data'])
                self.assertNotIn('gtin', variation['item_variation_data'])
                return Response({'id_mappings': [{'client_object_id': variation['id'], 'object_id':'real-variation'}]})
            return Response({})
        with patch.object(app.requests, 'get', return_value=Response({'locations':[{'id':'store', 'status':'ACTIVE'}]})), patch.object(app.requests, 'post', side_effect=post):
            result = app.upload_to_square([CARD], 'fake', environment='sandbox')
        self.assertEqual(result, {'uploaded_count':1, 'inventory_count':3, 'errors':[]})
        count = calls[-1][1]['changes'][0]['adjustment']
        self.assertEqual((count['catalog_object_id'], count['location_id'], count['quantity'], count['to_state']), ('real-variation','store','3','IN_STOCK'))
        self.assertEqual(count['from_state'], 'NONE')

    def test_multiple_stores_require_selection(self):
        with patch.object(app.requests, 'get', return_value=Response({'locations':[{'id':'a','status':'ACTIVE'},{'id':'b','status':'ACTIVE'}]})), patch.object(app.requests, 'post') as post:
            result = app.upload_to_square([CARD], 'fake')
        self.assertTrue(result['errors'])
        post.assert_not_called()

    def test_existing_sku_adds_stock_without_catalog_creation_or_reset(self):
        calls = []
        def post(url, **kwargs):
            calls.append((url,kwargs['json']))
            if url.endswith('/search'):
                return Response({'objects':[{'id':'existing','type':'ITEM_VARIATION',
                    'item_variation_data':{'sku':app.barcode_value(CARD),'track_inventory':True}}]})
            self.assertNotIn('batch-upsert', url)
            return Response({})
        with patch.object(app.requests, 'get', return_value=Response({'locations':[{'id':'a','status':'ACTIVE'}]})), patch.object(app.requests, 'post', side_effect=post):
            result = app.upload_to_square([CARD], 'fake')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['inventory_count'],3)
        change = calls[-1][1]['changes'][0]
        self.assertEqual(change['type'],'ADJUSTMENT')
        self.assertEqual(change['adjustment']['catalog_object_id'],'existing')
        self.assertEqual(change['adjustment']['quantity'],'3')
        # Add three incoming copies, not an absolute count of three (which would erase sales).
        self.assertEqual(change['adjustment']['from_state'],'NONE')
        self.assertEqual(change['adjustment']['to_state'],'IN_STOCK')

    def test_stock_failure_is_reported_separately_from_catalog_success(self):
        def post(url, **kwargs):
            if url.endswith('/search'): return Response({})
            if url.endswith('/batch-upsert'):
                v = kwargs['json']['batches'][0]['objects'][0]['item_data']['variations'][0]
                return Response({'id_mappings':[{'client_object_id':v['id'],'object_id':'v'}]})
            return Response({'errors':[{'code':'FORBIDDEN'}]})
        with patch.object(app.requests, 'get', return_value=Response({'locations':[{'id':'a','status':'ACTIVE'}]})), patch.object(app.requests, 'post', side_effect=post):
            result = app.upload_to_square([CARD], 'fake', log_fn=lambda _: None)
        self.assertEqual(result['uploaded_count'],1)
        self.assertEqual(result['inventory_count'],0)
        self.assertTrue(result['errors'])

    def test_pdf_barcodes_decode_at_printer_resolution(self):
        import fitz
        import zxingcpp
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            for layout in ('standard','fold','side'):
                for dpi in (203,300):
                    pdf = str(Path(d)/f'{layout}-{dpi}.pdf')
                    app.generate_zebra_pdf_labels([CARD],pdf,layout=layout,dpi=dpi)
                    with fitz.open(pdf) as doc:
                        self.assertEqual(len(doc),3)
                        self.assertEqual(tuple(doc[0].rect)[2:], (144,72))
                        for page in doc:
                            pix = page.get_pixmap(dpi=dpi)
                            img = Image.frombytes('RGB', [pix.width,pix.height], pix.samples)
                            decoded = zxingcpp.read_barcodes(img)
                            self.assertEqual([b.text for b in decoded], [app.barcode_value(CARD)])
                    zpl = str(Path(d)/f'{layout}-{dpi}.zpl')
                    app.generate_zebra_zpl([CARD],zpl,dpi=dpi,layout=layout)
                    text = Path(zpl).read_text()
                    self.assertIn('^PQ3',text)
                    self.assertIn(('^FDMA,' if layout == 'side' else '^FD>;')+app.barcode_value(CARD), text)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings_patch = patch.object(app, 'SETTINGS_FILE', str(Path(self.directory.name)/'settings.json'))
        self.settings_patch.start()

    def tearDown(self):
        self.settings_patch.stop()
        self.directory.cleanup()

    def test_busy_port_does_not_open_browser(self):
        with patch.object(app, 'HTTPServer', side_effect=OSError('in use')), patch.object(app.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'older Roca'):
                app.launch_desktop_station()
            launch.assert_not_called()

    def test_chrome_absent_does_not_launch_edge(self):
        from unittest.mock import MagicMock
        server = MagicMock()
        server.server_port = 8765
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(app, 'HTTPServer', return_value=server), patch.object(app.platform, 'system', return_value='Linux'), patch.object(app.shutil, 'which', return_value=None), patch.object(app.subprocess, 'Popen') as launch:
            app.launch_desktop_station()
            launch.assert_not_called()
            server.server_close.assert_called_once()

    def test_windows_registry_chrome_launch(self):
        import sys
        from unittest.mock import MagicMock
        registry = MagicMock()
        registry.QueryValueEx.return_value = ('C:/Custom/Chrome/chrome.exe', 1)
        server = MagicMock()
        server.server_port = 8765
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.dict(sys.modules, {'winreg': registry}), patch.object(app, 'HTTPServer', return_value=server), patch.object(app.platform, 'system', return_value='Windows'), patch.object(app.shutil, 'which', return_value=None), patch.object(app.os.path, 'isfile', side_effect=lambda p: p == 'C:/Custom/Chrome/chrome.exe'), patch.object(app.subprocess, 'Popen') as launch:
            app.launch_desktop_station()
            self.assertEqual(launch.call_args.args[0][0], 'C:/Custom/Chrome/chrome.exe')

    def test_full_browser_api_csv_to_labels_without_square_write(self):
        import threading
        import requests
        from pypdf import PdfReader
        import io
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'OUTPUT_DIR', directory), patch.object(app, 'SETTINGS_FILE', str(Path(directory)/'settings.json')):
            server = app.HTTPServer(('127.0.0.1', 0), app.RocaStationHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            client=requests.Session();client.trust_env=False
            try:
                page=client.get(base+'/')
                self.assertEqual(page.status_code,200)
                self.assertIn('Roca Station',page.text)
                self.assertEqual(client.get(base+'/api/version').json()['version'],'2.2.0')
                receipt=client.post(base+'/api/import',json={'csv':'Card Name,Card #,Price,Bin,Qty\nBlack Lotus,233,125.50,1,2\n','fileName':'test.csv','mode':'labels'}).json()['receipt']
                receipt['cards'][0]['reviewed']=True
                receipt=client.post(base+'/api/receipts/'+receipt['id'],json=receipt).json()['receipt']
                with patch.object(app,'upload_to_square') as upload:
                    result=client.post(base+'/api/receipts/'+receipt['id']+'/labels',json={})
                    self.assertEqual(result.status_code,200,result.text)
                    upload.assert_not_called()
                pdf=result.json()['receipt']['files']['pdf']
                self.assertEqual(len(PdfReader(io.BytesIO(client.get(base+'/files/'+pdf).content)).pages),2)
                self.assertNotIn('squareToken',client.get(base+'/api/settings').text)
                self.assertEqual(client.get(base+'/api/settings',headers={'Origin':'https://example.com'}).status_code,403)
            finally:
                client.close();server.shutdown();server.server_close();thread.join()

class LocationTests(unittest.TestCase):
    def test_wrong_location_reports_actual_options_and_prevents_writes(self):
        with patch.object(app.requests, 'get', return_value=Response({'locations':[
            {'id':'correct-id','name':'Card Store','status':'ACTIVE'}]})), patch.object(app.requests, 'post') as post:
            result = app.upload_to_square([CARD], 'fake', location_id='old-id')
        self.assertIn('correct-id', result['errors'][0])
        self.assertIn('Card Store', result['errors'][0])
        self.assertIn('Production', result['errors'][0])
        self.assertEqual(result['uploaded_count'],0)
        post.assert_not_called()

    def test_whitespace_in_location_is_ignored(self):
        self.assertEqual(app.resolve_square_location([{'id':'store','status':'ACTIVE'}], ' store ', 'production'), 'store')

    def test_no_active_locations_has_specific_diagnosis(self):
        with self.assertRaisesRegex(ValueError,'no active locations'):
            app.resolve_square_location([{'id':'closed','status':'INACTIVE'}], '', 'production')

    def test_auth_and_permission_failures_are_distinct(self):
        for code, expected in [(401,'selected environment'),(403,'MERCHANT_PROFILE_READ')]:
            response = Response({})
            response.status_code = code
            with patch.object(app.requests,'get',return_value=response):
                with self.assertRaisesRegex(ValueError,expected):
                    app.square_locations('fake','production')

    def test_sandbox_request_uses_sandbox_host(self):
        with patch.object(app.requests,'get',return_value=Response({'locations':[]})) as get:
            app.square_locations('fake','sandbox')
        self.assertEqual(get.call_args.args[0],'https://connect.squareupsandbox.com/v2/locations')

    def test_old_unguarded_location_api_is_retired(self):
        import threading,requests
        with tempfile.TemporaryDirectory() as d,patch.object(app,'SETTINGS_FILE',str(Path(d)/'settings.json')):
            server=app.HTTPServer(('127.0.0.1',0),app.RocaStationHandler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            client=requests.Session();client.trust_env=False
            try:
                result=client.post(f'http://127.0.0.1:{server.server_port}/api/square/locations',json={})
                self.assertEqual(result.status_code,410)
            finally:
                client.close();server.shutdown();server.server_close();thread.join()

class DuplicateTests(unittest.TestCase):
    def test_csv_sku_is_stable_and_duplicates_keep_label_quantities(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'cards.csv'
            header='Card Name,Card #,Condition,Printing,Set,Language,Price,Bin,Qty\n'
            p.write_text(header+'Black Lotus,233,NM,Normal,LEA,English,12,1,2\nOther,1,NM,Normal,LEA,English,1,1,1\nBlack Lotus,233,Near Mint,Normal,LEA,English,12,1,1\n')
            cards=app.parse_roca_csv(str(p))
            self.assertEqual(cards[0]['sku'],cards[2]['sku'])
            self.assertEqual([cards[0]['qty'],cards[2]['qty']],[2,1])
            p.write_text(header+'Other,1,NM,Normal,LEA,English,1,1,1\nBlack Lotus,233,NM,Normal,LEA,English,12,1,1\n')
            self.assertEqual(app.parse_roca_csv(str(p))[1]['sku'],cards[0]['sku'])

    def test_condition_finish_set_and_language_are_separate(self):
        base=['Card','123','NM','Normal',1,'English','LEA']
        original=app.generate_sku(*base)
        for index,value in [(2,'LP'),(3,'Foil'),(5,'Japanese'),(6,'LEB')]:
            variant=list(base);variant[index]=value
            self.assertNotEqual(app.generate_sku(*variant),original)
        self.assertEqual(app.generate_sku('Card','123','NM','Non-Foil',999,'English','LEA'),original)

    def test_duplicate_rows_are_aggregated_into_one_stock_adjustment(self):
        calls=[]
        def post(url,**kwargs):
            calls.append((url,kwargs['json']))
            if url.endswith('/search'):
                return Response({'objects':[{'id':'existing','item_variation_data':{'track_inventory':True}}]})
            return Response({})
        with patch.object(app.requests,'get',return_value=Response({'locations':[{'id':'a','status':'ACTIVE'}]})),patch.object(app.requests,'post',side_effect=post):
            result=app.upload_to_square([CARD,dict(CARD,qty=2)],'fake')
        self.assertEqual(result['inventory_count'],5)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[-1][1]['changes'][0]['adjustment']['quantity'],'5')
        self.assertEqual(CARD['qty'],3)

    def test_duplicate_square_skus_are_ambiguous(self):
        with patch.object(app.requests,'get',return_value=Response({'locations':[{'id':'a','status':'ACTIVE'}]})),patch.object(app.requests,'post',return_value=Response({'objects':[{'id':'a'},{'id':'b'}]})) as post:
            result=app.upload_to_square([CARD],'fake')
        self.assertIn('Multiple Square variations',result['errors'][0])
        self.assertEqual(post.call_count,1)

    def test_existing_untracked_variation_is_enabled_without_changing_price(self):
        calls=[]
        def post(url,**kwargs):
            calls.append((url,kwargs['json']))
            if url.endswith('/search'):
                return Response({'objects':[{'id':'v','version':42,'type':'ITEM_VARIATION',
                  'item_variation_data':{'track_inventory':False,'sku':app.barcode_value(CARD),
                  'price_money':{'amount':12550,'currency':'USD'}}}]})
            return Response({})
        with patch.object(app.requests,'get',return_value=Response({'locations':[{'id':'a','status':'ACTIVE'}]})),patch.object(app.requests,'post',side_effect=post):
            result=app.upload_to_square([CARD],'fake')
        self.assertEqual(result['errors'],[])
        obj=calls[1][1]['batches'][0]['objects'][0]
        self.assertEqual(obj['version'],42)
        self.assertTrue(obj['item_variation_data']['track_inventory'])
        self.assertEqual(obj['item_variation_data']['price_money']['amount'],12550)
        self.assertEqual(calls[-1][1]['changes'][0]['adjustment']['catalog_object_id'],'v')

    def test_existing_price_mismatch_prevents_stock_and_wrong_price_labels(self):
        existing={'id':'v','item_variation_data':{'track_inventory':True,'price_money':{'amount':1,'currency':'USD'}}}
        with patch.object(app.requests,'get',return_value=Response({'locations':[{'id':'a','status':'ACTIVE'}]})),patch.object(app.requests,'post',return_value=Response({'objects':[existing]})) as post:
            result=app.upload_to_square([CARD],'fake')
        self.assertIn('different price',result['errors'][0])
        self.assertEqual(post.call_count,1)

    def test_explicit_square_sku_supports_previously_imported_cards(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'old.csv'
            p.write_text('Card Name,Card #,Price,Bin,Qty,Square SKU\nBlack Lotus,233,125.50,1,2,123456789012\n')
            cards=app.parse_roca_csv(str(p))
            self.assertEqual(app.barcode_value(cards[0]),'123456789012')

class LayoutDefaultTests(unittest.TestCase):
    def test_existing_linear_settings_migrate_to_side_once(self):
        import json
        with tempfile.TemporaryDirectory() as d, patch.object(app,'SETTINGS_FILE',str(Path(d)/'settings.json')):
            Path(app.SETTINGS_FILE).write_text(json.dumps({'labelLayout':'standard','printerDpi':300}))
            cfg=app.load_settings()
            self.assertEqual(cfg['labelLayout'],'side')
            self.assertEqual(cfg['printerDpi'],300)
            self.assertTrue(cfg['sideFoldDefaultApplied'])
            cfg['labelLayout']='fold'
            app.save_settings(cfg)
            self.assertEqual(app.load_settings()['labelLayout'],'fold')

    def test_default_outputs_are_right_hand_qr(self):
        import fitz
        import zxingcpp
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            pdf=str(Path(d)/'labels.pdf');zpl=str(Path(d)/'labels.zpl')
            app.generate_zebra_pdf_labels([dict(CARD,qty=1)],pdf)
            app.generate_zebra_zpl([dict(CARD,qty=1)],zpl)
            self.assertIn('^BQN',Path(zpl).read_text())
            with fitz.open(pdf) as doc:
                pix=doc[0].get_pixmap(dpi=203)
                img=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
                result=zxingcpp.read_barcodes(img)
                self.assertEqual(result[0].text,app.barcode_value(CARD))
                self.assertGreater(result[0].position.top_left.x,img.width/2)

if __name__ == '__main__': unittest.main()
