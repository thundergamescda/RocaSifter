"""Render fictional exact-SKU label matrix and independently decode every PDF.

ZPL validation checks geometry/transport/data fields, not printer firmware.
Native printer/scanner/Square acceptance remains an explicit separate gate.
"""
import argparse
import ast
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import traceback
import uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PIL import Image,ImageDraw
import pymupdf
import zxingcpp
import hosted_labels as labels
import roca_to_square as core
from test_hosted_labels import card,decode_fh


def run():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--legacy-source',type=Path);args=parser.parse_args()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');folder=ROOT/'Testing'/'receiving-saga'/('labels-'+stamp);folder.mkdir(parents=True)
    global FAILURE_ROOT
    FAILURE_ROOT=folder
    sources={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('hosted_labels.py','roca_to_square.py','tools/verify_hosted_labels.py')}
    rows=[];tiles=[];payload='A>5B^XZ~JA'
    for stock,(width,height) in labels.STOCKS.items():
        for layout in ('standard','fold','side'):
            for dpi in (203,300):
                chosen={'stock':stock,'layout':layout,'dpi':dpi};name=f'{stock}-{layout}-{dpi}';item=card(payload)
                item['card_name']='Fictional long card name for wrapping and collision review';item['card_set']='Fictional Set';pdf=folder/(name+'.pdf');zpl=folder/(name+'.zpl')
                core.generate_zebra_pdf_labels([item],pdf,width_in=width,height_in=height,layout=layout,dpi=dpi)
                core.generate_zebra_zpl([item],zpl,width_in=width,height_in=height,layout=layout,dpi=dpi)
                with pymupdf.open(pdf) as document:
                    pix=document[0].get_pixmap(dpi=dpi,alpha=False);image=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
                    decoded=[value.text for value in zxingcpp.read_barcodes(image)];assert payload in decoded,(name,decoded)
                # Poppler gives an independent renderer for the visual matrix.
                subprocess.run(['pdftoppm','-png','-singlefile','-r','203',str(pdf),str(folder/name)],check=True,capture_output=True)
                image=Image.open(folder/(name+'.png')).convert('RGB');image.thumbnail((550,280))
                tile=Image.new('RGB',(570,320),'#eef2f0');draw=ImageDraw.Draw(tile);draw.text((10,8),name+' | exact software decode PASS',fill='#18382b');tile.paste(image,(10,30));tiles.append(tile)
                text=zpl.read_text(encoding='ascii');assert text.count('^XA')==text.count('^XZ')==1
                rows.append({'profile':chosen,'payload':payload,'decoded':decoded,'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),
                    'zpl_sha256':hashlib.sha256(zpl.read_bytes()).hexdigest(),'geometry':labels.geometry(payload,chosen)})
    sheet=Image.new('RGB',(570*3,320*6),'white')
    for index,tile in enumerate(tiles):sheet.paste(tile,((index%3)*570,(index//3)*320))
    sheet.save(folder/'label-matrix.png')
    legacy=[]
    if args.legacy_source:
        raw=args.legacy_source.read_text(encoding='utf-8');tree=ast.parse(raw)
        node=next(value for value in tree.body if isinstance(value,ast.FunctionDef) and value.name=='generate_zebra_zpl')
        namespace=dict(vars(core));exec(compile(ast.Module(body=[node],type_ignores=[]),str(args.legacy_source),'exec'),namespace)
        item={'card_name':'Legacy Fixture','card_set':'Fictional','card_num':'001','condition':'Near Mint','printing':'Normal','sku':'000123456789','qty':2,'final_price':3.13}
        for layout in ('standard','fold','side'):
            for dpi in (203,300):
                old=folder/f'legacy-before-{layout}-{dpi}.zpl';new=folder/f'legacy-after-{layout}-{dpi}.zpl'
                namespace['generate_zebra_zpl']([item],old,layout=layout,dpi=dpi);core.generate_zebra_zpl([item],new,layout=layout,dpi=dpi)
                assert old.read_bytes()==new.read_bytes(),(layout,dpi)
                legacy.append({'layout':layout,'dpi':dpi,'byte_identical':True,'sha256':hashlib.sha256(new.read_bytes()).hexdigest()})
    after={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
    report={'scope':'Fictional PDF/Poppler render/software barcode decode and ZPL field/geometry evidence only. No physical printer/scanner/Square-device certification.',
        'sources_before':sources,'sources_after':after,'source_stable':sources==after,'profiles':rows,'legacy_zpl':legacy,
        'legacy_source_sha256':hashlib.sha256(args.legacy_source.read_bytes()).hexdigest() if args.legacy_source else None,
        'hardware_acceptance_complete':False}
    (folder/'label-verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8');assert report['source_stable'];print('PASS 18 exact-decode/render profiles;',len(legacy),'legacy byte comparisons. Evidence:',folder)


if __name__=='__main__':
    try:run()
    except Exception:
        if 'FAILURE_ROOT' in globals():(FAILURE_ROOT/'failure.log').write_text(traceback.format_exc(),encoding='utf-8')
        raise
