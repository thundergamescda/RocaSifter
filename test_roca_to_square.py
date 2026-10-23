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
        count = calls[-1][1]['changes'][0]['physical_count']
        self.assertEqual((count['catalog_object_id'], count['location_id'], count['quantity'], count['state']), ('real-variation','store','3','IN_STOCK'))

    def test_multiple_stores_require_selection(self):
        with patch.object(app.requests, 'get', return_value=Response({'locations':[{'id':'a','status':'ACTIVE'},{'id':'b','status':'ACTIVE'}]})), patch.object(app.requests, 'post') as post:
            result = app.upload_to_square([CARD], 'fake')
        self.assertTrue(result['errors'])
        post.assert_not_called()

    def test_existing_sku_prevents_duplicate_upload(self):
        with patch.object(app.requests, 'get', return_value=Response({'locations':[{'id':'a','status':'ACTIVE'}]})), patch.object(app.requests, 'post', return_value=Response({'objects':[{'id':'existing'}]})) as post:
            result = app.upload_to_square([CARD], 'fake')
        self.assertIn('already exists', result['errors'][0])
        self.assertEqual(post.call_count, 1)

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
            for layout in ('standard','fold'):
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
                    self.assertIn('^FD>;'+app.barcode_value(CARD), text)


class DesktopTests(unittest.TestCase):
    def test_busy_port_does_not_open_browser(self):
        with patch.object(app, 'HTTPServer', side_effect=OSError('in use')), patch.object(app.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'older Roca'):
                app.launch_desktop_station()
            launch.assert_not_called()

    def test_chrome_absent_does_not_launch_edge(self):
        from unittest.mock import MagicMock
        server = MagicMock()
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
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.dict(sys.modules, {'winreg': registry}), patch.object(app, 'HTTPServer', return_value=server), patch.object(app.platform, 'system', return_value='Windows'), patch.object(app.shutil, 'which', return_value=None), patch.object(app.os.path, 'isfile', side_effect=lambda p: p == 'C:/Custom/Chrome/chrome.exe'), patch.object(app.subprocess, 'Popen') as launch:
            app.launch_desktop_station()
            self.assertEqual(launch.call_args.args[0][0], 'C:/Custom/Chrome/chrome.exe')

    def test_full_browser_api_csv_to_decodable_labels_without_square_write(self):
        import threading
        import requests
        from pypdf import PdfReader
        import io
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'OUTPUT_DIR', directory), patch.object(app, 'SETTINGS_FILE', str(Path(directory)/'settings.json')):
            server = app.HTTPServer(('127.0.0.1', 0), app.RocaStationHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            client = requests.Session()
            client.trust_env = False
            try:
                page = client.get(base+'/')
                self.assertEqual(page.status_code, 200)
                self.assertIn('Create labels only', page.text)
                self.assertEqual(client.get(base+'/api/version').json()['version'], '1.1.0')
                config = {'labelLayout':'fold', 'printerDpi':203}
                parsed = client.post(base+'/api/import', json={'settings':config,'csv':'Card Name,Card #,Price,Bin,Qty\nBlack Lotus,233,125.50,1,2\n'}).json()
                self.assertEqual(len(parsed['barcodes'][0]), 12)
                with patch.object(app, 'upload_to_square') as upload:
                    result = client.post(base+'/api/process',json={'cards':parsed['cards'],'settings':config,'uploadToSquare':False})
                    self.assertEqual(result.status_code, 200, result.text)
                    upload.assert_not_called()
                data = result.json()
                self.assertEqual(data['version'], '1.1.0')
                self.assertEqual(len(PdfReader(io.BytesIO(client.get(base+data['pdfUrl']).content)).pages),2)
                zpl = client.get(base+data['zplUrl']).text
                self.assertIn('^FD>;'+parsed['barcodes'][0], zpl)
                rejected = client.post(base+'/api/process',json={'cards':parsed['cards'],'settings':config,'uploadToSquare':True})
                self.assertEqual(rejected.status_code, 400)
                self.assertIn('token', rejected.json()['errors'][0])
                cross_origin = client.get(base+'/api/settings', headers={'Origin':'https://example.com'})
                self.assertEqual(cross_origin.status_code,403)
            finally:
                client.close()
                server.shutdown()
                server.server_close()
                thread.join()

if __name__ == '__main__': unittest.main()
