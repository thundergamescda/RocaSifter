"""Actual bounded PDF/ZPL, exact software decode and shared-raster evidence."""
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

import pypdfium2 as pdfium
import zxingcpp

import station_receiving_label_plan as plan
import station_receiving_label_render as render
import test_receiving_label_plan as fixture


def owned_pil(bitmap):
    """Copy the real decoded bitmap before its native buffer can be closed."""
    view=bitmap.to_pil()
    try:return view.copy()
    finally:view.close()


class ReceivingLabelRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def pair(self,cards,chosen=None,counts=None):
        chosen=chosen or fixture.PROFILE
        counts=counts or {value['row_id']:value['qty'] for value in cards}
        pdf=self.root/'label.pdf';zpl=self.root/'label.zpl'
        result=render.render_pair(cards,pdf,zpl,chosen,counts)
        return result,pdf,zpl

    def test_actual_pdf_and_zpl_exact_sku_across_layouts_and_dpis(self):
        for layout in ('standard','fold','side'):
            for dpi in (203,300):
                for sku in ('000123456789','A>5B^XZ~JA',' lead','trail '):
                    with self.subTest(layout=layout,dpi=dpi,sku=sku):
                        cards=[fixture.card(sku=sku,quantity=1)];chosen={'stock':'2x1','layout':layout,'dpi':dpi}
                        result,pdf,zpl=self.pair(cards,chosen)
                        with closing(pdfium.PdfDocument(pdf)) as document:
                            self.assertEqual(len(document),1)
                            with closing(document[0]) as page:
                                with closing(page.render(scale=dpi/72)) as bitmap:
                                    with closing(owned_pil(bitmap)) as image:
                                        self.assertIn(sku,[value.text for value in zxingcpp.read_barcodes(image)])
                        wire=zpl.read_text('ascii');self.assertEqual(wire.count('^XA'),1);self.assertEqual(wire.count('^XZ'),1)
                        payload=render.zpl_hex(sku) if layout=='side' else render.zpl_code128(sku)
                        self.assertIn(payload,wire);self.assertFalse(result['hardwareAcceptanceComplete'])
                        pdf.unlink();zpl.unlink()

    def test_pdf_human_image_and_zpl_gfa_are_exact_same_raster(self):
        result,pdf,zpl=self.pair([fixture.card(quantity=1,name='Pokémon café fixture')])
        wire=zpl.read_text('ascii');match=re.search(r'\^GFA,(\d+),(\d+),(\d+),([0-9A-F]+)\^FS',wire)
        black=bytes.fromhex(match[4]);self.assertEqual(len(black),int(match[1]));self.assertEqual(match[1],match[2])
        with closing(pdfium.PdfDocument(pdf)) as document:
            with closing(document[0]) as page:
                # Enumerate actual embedded objects, including the renderer's
                # Form XObject; the page owns these attached image objects.
                images=list(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE],max_depth=15))
                self.assertEqual(len(images),1)
                with closing(images[0].get_bitmap(render=False)) as bitmap:
                    with closing(owned_pil(bitmap)) as image:
                        with closing(image.convert('1')) as embedded:
                            self.assertEqual(render._black_bytes(embedded),black)
        self.assertEqual(result['pdf']['sha256'],hashlib.sha256(pdf.read_bytes()).hexdigest())
        self.assertEqual(result['zpl']['sha256'],hashlib.sha256(zpl.read_bytes()).hexdigest())

    def test_partial_copy_counts_preserve_original_full_cards_and_marks(self):
        cards=[fixture.card(2,quantity=4),fixture.card(1,quantity=3)];before=copy.deepcopy(cards)
        print_plan=plan.build_plan(cards,fixture.PROFILE,2,3);piece=plan.part(print_plan,0)
        result,pdf,zpl=self.pair(cards,counts=piece['renderCounts'])
        with closing(pdfium.PdfDocument(pdf)) as document:self.assertEqual(len(document),3)
        self.assertEqual(sorted(map(int,re.findall(r'\^PQ(\d+)\^XZ',zpl.read_text()))),[1,2])
        self.assertEqual(cards,before);self.assertEqual(result['cardsHash'],print_plan['cardsHash'])
        self.assertEqual(result['expectedPdfPages'],3);self.assertEqual(result['expectedZplCopies'],3)

    def test_unprinted_metadata_and_worker_projection_do_not_change_basis(self):
        cards=[fixture.card(quantity=1)];cards[0].update(notes='日本語',customer_reference='א',receipt_title='Unprinted')
        counts={cards[0]['row_id']:1};first=render.preflight(cards,fixture.PROFILE,counts)
        fields={'row_id','source_line_key','qty','sku','include','reviewed','price_confirmed','price_missing',
                'final_price','card_name','game','card_set','card_num','condition','printing','hosted_sku_resolution'}
        projected=[{key:value for key,value in cards[0].items() if key in fields}]
        self.assertEqual(render.preflight(projected,fixture.PROFILE,counts),first)

    def test_unsupported_printed_glyph_bidi_shaping_and_controls_write_nothing(self):
        for name in ('日本語','שלום','e\u0301','Line\nBreak'):
            with self.subTest(name=name):
                cards=[fixture.card(name=name)];before=copy.deepcopy(cards)
                with self.assertRaises(render.LabelRenderError) as caught:self.pair(cards)
                self.assertEqual(caught.exception.code,'ORIGINAL_STOCK_LABEL_TEXT_UNSUPPORTED')
                self.assertEqual(list(self.root.iterdir()),[]);self.assertEqual(cards,before)

    def test_only_layouts_that_print_details_require_their_glyphs(self):
        cards=[fixture.card(quantity=1)];cards[0]['card_set']='日本語';cards[0]['condition']='שלום'
        result,pdf,zpl=self.pair(cards,{'stock':'2x1','layout':'standard','dpi':203})
        pdf.unlink();zpl.unlink();self.assertEqual(result['copyCount'],1)
        with self.assertRaises(render.LabelRenderError):self.pair(cards)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_visible_clipping_reports_ellipsis_without_changing_sku_or_identity(self):
        cards=[fixture.card(quantity=1,name='W'*200)];cards[0].update(card_set='W'*200,condition='W'*100,printing='W'*100)
        before=copy.deepcopy(cards);result,pdf,zpl=self.pair(cards)
        self.assertEqual(len(result['overflows']),4)
        self.assertTrue(all('…' in value['shownText'] for value in result['overflows']))
        self.assertEqual(cards,before);self.assertIn(render.zpl_hex(cards[0]['sku']),zpl.read_text())

    def test_pinned_font_mismatch_and_frozen_barcode_geometry_fail_before_writes(self):
        with patch.dict(render.FONT_HASHES,{'regular':'0'*64}):
            with self.assertRaises(render.LabelRenderError) as caught:self.pair([fixture.card()])
        self.assertEqual(caught.exception.code,'ORIGINAL_STOCK_LABEL_FONT_UNAVAILABLE')
        with self.assertRaises(render.LabelRenderError):self.pair([fixture.card(sku='Provider-SKU-'+('W'*80))])
        self.assertEqual(list(self.root.iterdir()),[])

    def test_existing_destinations_and_second_file_failure_preserve_older_files(self):
        old=self.root/'old.pdf';old.write_bytes(b'older valid output')
        with self.assertRaises(render.LabelRenderError):
            render.render_pair([fixture.card()],old,self.root/'new.zpl',fixture.PROFILE,{fixture.card()['row_id']:1})
        self.assertEqual(old.read_bytes(),b'older valid output');self.assertFalse((self.root/'new.zpl').exists())
        pdf=self.root/'new.pdf';zpl=self.root/'new.zpl';original=Path.open
        def fail_second(path,*args,**kwargs):
            if path==zpl and args and args[0]=='xb':raise OSError('Fictional second file failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',fail_second),self.assertRaises(render.LabelRenderError):
            render.render_pair([fixture.card()],pdf,zpl,fixture.PROFILE,{fixture.card()['row_id']:1})
        self.assertFalse(pdf.exists());self.assertFalse(zpl.exists());self.assertEqual(old.read_bytes(),b'older valid output')

    def test_partial_zpl_write_and_pdf_byte_overflow_remove_only_own_pair(self):
        cards=[fixture.card(quantity=1)];counts={cards[0]['row_id']:1}
        pdf=self.root/'new.pdf';zpl=self.root/'new.zpl';old=self.root/'older.pdf';old.write_bytes(b'older')
        original=render._LimitedWriter.write
        def fail_after_partial(writer,value):
            if str(writer.name).endswith('.zpl'):
                writer.stream.write(b'partial owned output');raise OSError('Fictional partial ZPL failure')
            return original(writer,value)
        with patch.object(render._LimitedWriter,'write',fail_after_partial),self.assertRaises(render.LabelRenderError):
            render.render_pair(cards,pdf,zpl,fixture.PROFILE,counts)
        self.assertFalse(pdf.exists());self.assertFalse(zpl.exists());self.assertEqual(old.read_bytes(),b'older')
        # Inject only the byte-bound fault after real preflight has completed.
        prepared=render._prepare(cards,fixture.PROFILE,counts)
        with patch.object(render,'_prepare',return_value=prepared),patch.object(render,'MAX_ARTIFACT_BYTES',128):
            with self.assertRaises(render.LabelRenderError) as caught:render.render_pair(cards,pdf,zpl,fixture.PROFILE,counts)
        self.assertEqual(caught.exception.code,'ORIGINAL_STOCK_LABEL_BYTES_EXCEEDED')
        self.assertFalse(pdf.exists());self.assertFalse(zpl.exists());self.assertEqual(old.read_bytes(),b'older')

    def test_thousand_copy_part_is_actual_bounded_pdf_with_one_zpl_row(self):
        cards=[fixture.card(quantity=100000)];counts={cards[0]['row_id']:1000};started=time.monotonic()
        result,pdf,zpl=self.pair(cards,counts=counts)
        with closing(pdfium.PdfDocument(pdf)) as document:self.assertEqual(len(document),1000)
        self.assertEqual(zpl.read_text().count('^XA'),1);self.assertIn('^PQ1000^XZ',zpl.read_text())
        self.assertLessEqual(result['pdf']['bytes'],plan.MAX_ARTIFACT_BYTES)
        self.assertLessEqual(result['zpl']['bytes'],plan.MAX_ARTIFACT_BYTES)
        print(json.dumps({'bounded1000CopiesElapsed':time.monotonic()-started,'pdfBytes':result['pdf']['bytes'],'zplBytes':result['zpl']['bytes']}))

    def test_maximum_distinct_row_part_measures_metadata_and_adaptive_zpl_bound(self):
        cards=[fixture.card(index,quantity=1,name='W'*200) for index in range(1,1001)]
        for value in cards:value.update(card_set='W'*200,condition='W'*100,printing='W'*100)
        print_plan=plan.build_plan(cards,fixture.PROFILE);piece=plan.part(print_plan,0);started=time.monotonic()
        result=render.preflight(cards,fixture.PROFILE,piece['renderCounts'])
        length=len(json.dumps(result,ensure_ascii=False).encode('utf-8'))
        self.assertEqual(result['rowCount'],1000);self.assertLess(length,2*1024*1024)
        self.assertLessEqual(result['estimatedZplBytes'],plan.MAX_ARTIFACT_BYTES)
        wide=plan.build_plan(cards,{'stock':'4x2','layout':'side','dpi':300})
        self.assertLess(wide['partSize'],1000)
        print(json.dumps({'thousandRowsPreflightElapsed':time.monotonic()-started,'metadataBytes':length,
            'estimatedZplBytes':result['estimatedZplBytes'],'widePartSize':wide['partSize']}))

    def test_actual_widest_profile_adaptive_part_is_bounded_without_dropping_rows(self):
        cards=[fixture.card(index,quantity=1,name='Wide stock '+str(index)) for index in range(1,1001)]
        chosen={'stock':'4x2','layout':'side','dpi':300};job=plan.build_plan(cards,chosen);piece=plan.part(job,0)
        started=time.monotonic();result,pdf,zpl=self.pair(cards,chosen,piece['renderCounts'])
        self.assertEqual(piece['copyCount'],182);self.assertEqual(job['partCount'],6)
        self.assertEqual(plan.part(job,5)['copyCount'],90)
        with closing(pdfium.PdfDocument(pdf)) as document:self.assertEqual(len(document),182)
        self.assertEqual(zpl.read_text().count('^XA'),182)
        self.assertEqual(result['zpl']['bytes'],result['estimatedZplBytes'])
        self.assertLessEqual(result['zpl']['bytes'],plan.MAX_ARTIFACT_BYTES)
        print(json.dumps({'wide182RowPairElapsed':time.monotonic()-started,
            'pdfBytes':result['pdf']['bytes'],'zplBytes':result['zpl']['bytes'],'remainingParts':job['partCount']-1}))


if __name__=='__main__':unittest.main(verbosity=2)
