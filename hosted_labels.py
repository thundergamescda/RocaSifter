"""Exact trusted hosted SKU labels. Legacy/manual SKU behavior is separate.

Widths are integer printer dots. Engineering minima do not certify a scanner,
printer, Square device or GS1 identifier. No payload is trimmed or shortened.
"""
import hashlib
import json
import math
import re
import textwrap
import uuid
from decimal import Decimal,InvalidOperation

STOCKS={'2x1':(2,1),'3x1':(3,1),'4x2':(4,2)}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def exact_sku(value):
    if (not isinstance(value,str) or not 1<=len(value)<=100 or not value.strip()
        or any(not 32<=ord(char)<=126 for char in value)):
        raise ValueError('This exact Square SKU needs supported printable ASCII label data. Unicode, control characters and blank SKUs require explicit label support; the SKU will not be changed.')
    return value


def profile(value=None):
    value=value or {'stock':'2x1','layout':'side','dpi':203}
    if (not isinstance(value,dict) or set(value)!={'stock','layout','dpi'} or value['stock'] not in STOCKS
        or value['layout'] not in ('standard','fold','side') or type(value['dpi']) is not int or value['dpi'] not in (203,300)):
        raise ValueError('Choose 2×1, 3×1 or 4×2 stock, a supported layout and 203/300 DPI.')
    return dict(value)


def trusted_resolution(item):
    value=item.get('hosted_sku_resolution')
    if value is None:return None
    try:
        if not isinstance(value,dict) or value.get('version')!=1 or value.get('state')!='received':raise ValueError()
        body={key:item for key,item in value.items() if key!='fingerprint'}
        if value.get('fingerprint')!=fingerprint(body):raise ValueError()
        for key in ('receiving_id','review_line_id'):uuid.UUID(value[key])
        for key in ('receipt_hash','source_line_key','identity_hash'):
            if not isinstance(value.get(key),str) or not re.fullmatch('[a-f0-9]{64}',value[key]):raise ValueError()
        if exact_sku(value['sku'])!=item.get('sku'):raise ValueError()
        if type(value.get('selling_cents')) is not int or not 0<=value['selling_cents']<=100_000_000:raise ValueError()
        if type(value.get('confirmed_quantity')) is not int or value['confirmed_quantity']!=item.get('qty'):raise ValueError()
        if Decimal(str(item.get('final_price')))*100!=value['selling_cents']:raise ValueError()
        return value
    except (ValueError,TypeError,KeyError,AttributeError,InvalidOperation):
        raise ValueError('The saved hosted SKU resolution needs owner recovery. Its exact SKU cannot be replaced or regenerated.') from None


def mark(item,*,receiving_id,receipt_hash,review_line_id,source_line_key,identity_hash,sku,selling_cents,confirmed_quantity):
    result={'version':1,'state':'received','receiving_id':str(uuid.UUID(receiving_id)),
            'receipt_hash':receipt_hash,'review_line_id':str(uuid.UUID(review_line_id)),
            'source_line_key':source_line_key,'identity_hash':identity_hash,'sku':exact_sku(sku),
            'selling_cents':selling_cents,'confirmed_quantity':confirmed_quantity}
    result['fingerprint']=fingerprint(result);return result


def _barcode(value,module,height):
    from reportlab.graphics.barcode import code128
    class ExactB(code128.Code128):
        def encode(self):
            encoded=[104]+[ord(char)-32 for char in self.validated]
            self.encoded=encoded+[(encoded[0]+sum(index*number for index,number in enumerate(encoded[1:],1)))%103,106]
            return self.encoded
    cls=code128.Code128 if re.fullmatch('[0-9]{12}',value) else ExactB
    result=cls(value,barWidth=module,barHeight=height,quiet=True,lquiet=10*module,rquiet=10*module,humanReadable=False)
    result.validate();result.encode();result.decompose();return result


def _qr(value):
    from reportlab.graphics.barcode import qr,qrencoder
    # Fixed byte mode matches ZPL's explicit MM,Bdddd switches, independent of
    # either encoder's automatic numeric/alphanumeric segmentation decisions.
    result=qr.QrCodeWidget([qrencoder.QR8bitByte(value.encode('ascii'))],barLevel='M',barBorder=4)
    result.qr.make();return result


def geometry(value,chosen=None):
    value=exact_sku(value);chosen=profile(chosen);width,height=STOCKS[chosen['stock']];dpi=chosen['dpi']
    pw,ph=width*dpi,height*dpi;margin=math.ceil(4*dpi/72)
    chars=max(1,math.floor((width*72-8)/3));human_lines=[value[index:index+chars] for index in range(0,len(value),chars)]
    human_height=math.ceil((len(human_lines)*6+4)*dpi/72)
    result=dict(chosen,width_dots=pw,height_dots=ph,quiet_modules=4 if chosen['layout']=='side' else 10,
                human_lines=human_lines,human_height_dots=human_height,value=value)
    if chosen['layout']=='side':
        widget=_qr(value);modules=widget.qr.getModuleCount();module=4 if dpi==203 else 6;total=(modules+8)*module
        if total>pw//2-2*margin or total>ph-human_height-2*margin:
            raise ValueError('The exact SKU QR does not fit this selected stock at the fixed readable module size. Choose larger stock; compatible 2D scanner and Square-device acceptance are still required.')
        result.update(symbology='QR',symbol_modules=modules,module_dots=module,total_width_dots=total,
                      x_dots=pw//2+(pw//2-total)//2,y_dots=human_height+(ph-human_height-total)//2)
    else:
        module=2 if dpi==203 else 3;barcode=_barcode(value,module,1)
        modules=sum(ord(character.lower())-96 for character in barcode.decomposed);total=(modules+20)*module
        top=math.ceil((height*36 if chosen['layout']=='fold' else 36)*dpi/72);bar_height=ph-top-human_height-margin
        if total>pw-2*margin or bar_height<math.ceil(18*dpi/72):
            raise ValueError('The exact SKU Code 128 does not fit this stock/layout at the fixed readable bar size and quiet zones. Choose larger stock or side QR with a compatible 2D scanner; the SKU will not be shortened.')
        result.update(symbology='Code128',symbol_modules=modules,module_dots=module,total_width_dots=total,
                      x_dots=(pw-total)//2,y_dots=human_height,bar_height_dots=bar_height)
    return result


def from_dimensions(width,height,layout,dpi):
    stock=next((name for name,size in STOCKS.items() if size==(width,height)),None)
    return profile({'stock':stock,'layout':layout,'dpi':dpi})


def readiness(targets,chosen):
    chosen=profile(chosen);rows=[];by_sku={}
    for row in targets:
        sku=row['target_sku']
        if sku not in by_sku:by_sku[sku]=geometry(sku,chosen)
        spec=by_sku[sku]
        rows.append({'review_line_id':row['review_line_id'],'sku':spec['value'],'symbology':spec['symbology'],
                     'module_dots':spec['module_dots'],'symbol_modules':spec['symbol_modules'],'quiet_modules':spec['quiet_modules']})
    return {'profile':chosen,'targets':rows,'compatible_2d_scanner_required':chosen['layout']=='side',
            'hardware_acceptance_complete':False,'payloads_unchanged':True}


def zpl_hex(value):return ''.join('_'+format(byte,'02X') for byte in value.encode('ascii'))


def zpl_code128(value):
    """Code128 invocations first, then independent ^FH transport escaping."""
    exact_sku(value)
    if re.fullmatch('[0-9]{12}',value):return '>;'+value
    invocation=''.join({'>':'>0','~':'>=','^':'><'}.get(char,char) for char in value)
    return '>:'+zpl_hex(invocation)


def render_pdf(cards,path,chosen):
    from reportlab.pdfgen import canvas
    from reportlab.graphics import renderPDF
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.utils import simpleSplit
    chosen=profile(chosen);width,height=STOCKS[chosen['stock']];specs=[geometry(exact_sku(card['sku']),chosen) for card in cards]
    c=canvas.Canvas(str(path),pagesize=(width*72,height*72));unit=72/chosen['dpi']
    for card,spec in zip(cards,specs):
        if not trusted_resolution(card):raise ValueError('Only a confirmed hosted resolution can use exact hosted labels.')
        for _ in range(card['qty']):
            w,h=width*72,height*72;left=w/2-10 if chosen['layout']=='side' else w-12
            fold=chosen['layout']=='fold';c.saveState()
            if fold:c.translate(w,h*1.5);c.rotate(180)
            name_size=7 if fold else 8;c.setFont('Helvetica-Bold',name_size)
            for index,line in enumerate(simpleSplit(str(card['card_name']),'Helvetica-Bold',name_size,left)[:2]):c.drawString(5,h-(7+index*7 if fold else 11+index*9),line)
            if chosen['layout']=='side':
                details=[f"{card.get('card_set','')} #{card.get('card_num','')}",str(card.get('condition','')),str(card.get('printing',''))]
                c.setFont('Helvetica',5.5)
                for index,line in enumerate(details):
                    while c.stringWidth(line,'Helvetica',5.5)>left:line=line[:-1]
                    c.drawString(5,h-29-index*7,line)
                price_y=max(spec['human_height_dots']*unit+2,h-62)
            else:price_y=h-(26 if fold else 34)
            c.setFont('Helvetica-Bold',12 if chosen['layout']=='side' else 10);c.drawString(5,price_y,'$'+format(card['final_price'],'.2f'));c.restoreState()
            if spec['symbology']=='QR':
                widget=_qr(spec['value']);size=spec['total_width_dots']*unit;x0,y0,x1,y1=widget.getBounds()
                drawing=Drawing(size,size,transform=[size/(x1-x0),0,0,size/(y1-y0),0,0]);drawing.add(widget)
                renderPDF.draw(drawing,c,spec['x_dots']*unit,spec['y_dots']*unit)
            else:
                barcode=_barcode(spec['value'],spec['module_dots']*unit,spec['bar_height_dots']*unit)
                barcode.drawOn(c,spec['x_dots']*unit,spec['y_dots']*unit)
            c.setFont('Courier',5)
            for index,line in enumerate(spec['human_lines']):c.drawCentredString(w/2,4+(len(spec['human_lines'])-index-1)*6,line)
            c.showPage()
    c.save()


def render_zpl(cards,path,chosen):
    chosen=profile(chosen);specs=[geometry(exact_sku(card['sku']),chosen) for card in cards];commands=[]
    for card,spec in zip(cards,specs):
        if not trusted_resolution(card):raise ValueError('Only a confirmed hosted resolution can use exact hosted labels.')
        dpi=chosen['dpi'];dot=lambda value:round(value*dpi/72);w,h=spec['width_dots'],spec['height_dots']
        text=lambda value:''.join('_'+format(byte,'02X') for byte in str(value).encode('utf-8'))
        left=w//2-dot(10) if chosen['layout']=='side' else w-dot(12)
        fold=chosen['layout']=='fold';orientation='I' if fold else 'N'
        name_x=w-dot(5) if fold else dot(5);name_y=dot(h*36/dpi-7) if fold else dot(5)
        fields=f'^XA\n^CI28^PW{w}^LL{h}^LH0,0^LS0\n^FO{name_x},{name_y}^A0{orientation},{dot(7 if fold else 8)},{dot(3.5 if fold else 4)}^FB{left},2,0,L^FH^FD{text(card["card_name"])}^FS\n'
        price_y=dot(24)
        if chosen['layout']=='side':
            details=[f"{card.get('card_set','')} #{card.get('card_num','')}",str(card.get('condition','')),str(card.get('printing',''))]
            for index,line in enumerate(details):fields+=f'^FO{dot(5)},{dot(24+index*7)}^A0N,{dot(5.5)},{dot(3)}^FB{left},1,0,L^FH^FD{text(line)}^FS\n'
            price_y=dot(h*72/dpi-max(spec['human_height_dots']*72/dpi+2,h*72/dpi-62)-12)
        if fold:price_y=dot(h*36/dpi-26)
        fields+=f'^FO{name_x},{price_y}^A0{orientation},{dot(12 if chosen["layout"]=="side" else 10)},{dot(7 if chosen["layout"]=="side" else 6)}^FH^FD{text("$"+format(card["final_price"],".2f"))}^FS\n'
        if spec['symbology']=='QR':
            # ^BQ origin addresses the matrix; explicitly reserve its quiet zone.
            quiet=4*spec['module_dots'];x=spec['x_dots']+quiet;y=h-spec['y_dots']-spec['total_width_dots']+quiet
            fields+=f'^FO{x},{y}^BQN,2,{spec["module_dots"]}^FH^FDMM,B{len(spec["value"].encode("ascii")):04d}{zpl_hex(spec["value"])}^FS\n'
        else:
            x=spec['x_dots']+10*spec['module_dots'];y=h-spec['y_dots']-spec['bar_height_dots']
            fields+=f'^BY{spec["module_dots"]},2^FO{x},{y}^BCN,{spec["bar_height_dots"]},N,N,N,N^FH^FD{zpl_code128(spec["value"])}^FS\n'
        for index,line in enumerate(spec['human_lines']):
            fields+=f'^FO{dot(4)},{h-dot(4+(len(spec["human_lines"])-index)*6)}^AAN,{dot(5)},{dot(3)}^FB{w-dot(8)},1,0,C^FH^FD{zpl_hex(line)}^FS\n'
        commands.append(fields+f'^PQ{card["qty"]}^XZ\n')
    with open(path,'w',encoding='ascii',newline='\n') as stream:stream.writelines(commands)
