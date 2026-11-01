"""Actual PDF barcode decoding and exact trusted-SKU label boundaries."""
import copy
import re
from pathlib import Path
import tempfile
import unittest
import uuid
import pymupdf as fitz
from PIL import Image
import zxingcpp
import roca_to_square as core
import hosted_labels as labels


def card(sku):
    item={'card_name':'Fictional exact SKU','card_set':'Fixture','card_num':'001','condition':'Near Mint',
          'printing':'Normal','language':'English','sku':sku,'qty':1,'final_price':3.13}
    item['hosted_sku_resolution']=labels.mark(item,receiving_id=str(uuid.uuid4()),receipt_hash='a'*64,
        review_line_id=str(uuid.uuid4()),source_line_key='b'*64,identity_hash='c'*64,sku=sku,selling_cents=313,confirmed_quantity=1)
    return item


def decode_pdf(path,dpi):
    with fitz.open(path) as document:
        pixmap=document[0].get_pixmap(dpi=dpi,alpha=False)
        image=Image.frombytes('RGB',(pixmap.width,pixmap.height),pixmap.samples)
        return [result.text for result in zxingcpp.read_barcodes(image)]


def decode_fh(value):return re.sub(r'_([0-9A-F]{2})',lambda match:chr(int(match.group(1),16)),value)


class HostedLabelTests(unittest.TestCase):
    def test_exact_payload_is_never_trimmed_hashed_or_adopted_by_manual_policy(self):
        for value in ('000123456789','12345','ABC123','>0','>5','>:','>=','A>5B^XZ~JA',' lead','trail ','two spaces  inside'):
            with self.subTest(value=value):self.assertEqual(core.barcode_value(card(value)),value)
        self.assertRegex(core.barcode_value({'sku':'ABC123'}),r'^\d{12}$')
        broken=card('ABC123');broken['sku']='OTHER'
        with self.assertRaises(ValueError):core.barcode_value(broken)
        broken=card('ABC123');broken['final_price']=9.99
        with self.assertRaises(ValueError):core.barcode_value(broken)

    def test_real_pdf_code128_and_qr_decode_exact_ascii_and_special_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            for value in ('000123456789','12345','ABC123','>0','>5','>:','>=','A>5B^XZ~JA',' lead','trail '):
                for layout in ('standard','fold','side'):
                    for dpi in (203,300):
                        with self.subTest(value=value,layout=layout,dpi=dpi):
                            path=Path(folder)/'label.pdf';core.generate_zebra_pdf_labels([card(value)],path,layout=layout,dpi=dpi)
                            self.assertIn(value,decode_pdf(path,dpi))

    def test_fixed_geometry_blocks_small_stock_and_accepts_explicit_larger_qr(self):
        value='Exact-Provider-SKU-'+('Z'*80)
        for layout in ('standard','fold','side'):
            with self.subTest(layout=layout),self.assertRaisesRegex(ValueError,'does not fit'):labels.geometry(value,{'stock':'2x1','layout':layout,'dpi':203})
        spec=labels.geometry(value,{'stock':'4x2','layout':'side','dpi':203})
        self.assertGreater(spec['symbol_modules'],21);self.assertEqual(spec['quiet_modules'],4);self.assertEqual(spec['module_dots'],4)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'wide.pdf';core.generate_zebra_pdf_labels([card(value)],path,width_in=4,height_in=2,layout='side')
            self.assertIn(value,decode_pdf(path,203))

    def test_zpl_transport_and_code128_invocations_are_separate_and_exact(self):
        value='A>5B^XZ~JA';wire=labels.zpl_code128(value);decoded=decode_fh(wire)
        self.assertTrue(decoded.startswith('>:'));body=decoded[2:];result='';index=0
        invocations={'>0':'>','><':'^','>=':'~'}
        while index<len(body):
            if body[index]=='>':self.assertIn(body[index:index+2],invocations);result+=invocations[body[index:index+2]];index+=2
            else:result+=body[index];index+=1
        self.assertEqual(result,value)
        self.assertNotIn('^XZ',wire);self.assertNotIn('~JA',wire)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'label.zpl';core.generate_zebra_zpl([card(value)],path,layout='standard')
            text=path.read_text();self.assertIn('^FH^FD>:',text);self.assertNotIn('^FD>;',text)
            self.assertEqual(text.count('^XA'),1);self.assertEqual(text.count('^XZ'),1);self.assertIn(wire,text)
            self.assertIn(labels.zpl_hex(value),text)  # exact human-readable text

    def test_dynamic_qr_geometry_and_zpl_payload_match_exact_pdf_value(self):
        value='Provider-SKU-'+('a'*40);spec=labels.geometry(value,{'stock':'4x2','layout':'side','dpi':300})
        self.assertGreater(spec['symbol_modules'],21)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'label.zpl';core.generate_zebra_zpl([card(value)],path,layout='side',dpi=300,width_in=4,height_in=2)
            text=path.read_text();match=re.search(r'\^FDMM,B([0-9]{4})([^\^]+)\^FS',text);field=match.group(2)
            self.assertEqual(int(match.group(1)),len(value.encode('ascii')))
            self.assertEqual(decode_fh(field),value)
            self.assertGreaterEqual(spec['x_dots'],spec['width_dots']//2)
            self.assertLessEqual(spec['x_dots']+spec['total_width_dots'],spec['width_dots'])

    def test_unicode_controls_nonterminal_and_tampered_resolution_fail_closed(self):
        for value in ('Pokémon','A\nB','A\tB','A\x7fB','   '):
            with self.subTest(value=value),self.assertRaises(ValueError):labels.exact_sku(value)
        value=card('ABC');value['hosted_sku_resolution']['state']='planned'
        with self.assertRaises(ValueError):labels.trusted_resolution(value)


if __name__=='__main__':unittest.main(verbosity=2)
