"""Bounded original-stock PDF/ZPL with identical verified raster human text.

Only the native exact barcode helpers are reused from hosted_labels. These
functions operate on private caller-owned stage paths, never publish files or
authorize an inventory/financial action. Hardware acceptance remains separate.
"""
import hashlib
import io
import math
import os
from pathlib import Path
import unicodedata

from hosted_labels import STOCKS, _barcode, _qr, fingerprint, geometry, zpl_code128, zpl_hex
from station_receiving_label_plan import (LabelPlanError, MAX_ARTIFACT_BYTES,
    PRINT_FIELDS, validate_render_counts)


RENDERER_VERSION = 'stock-and-sleeve:original-stock-raster:v1'
FONT_HASHES = {'regular':'c4c45690b345435b2cba52ecabe275f05e49b389b39fe68ad03afbb551288d3d',
               'bold':'cc037385e4d55bfde89b13e03091ee93bf40c0c52ddd391ff031ab276f13b8e9'}
_FONT_FILES = {'regular':'Vera.ttf','bold':'VeraBd.ttf'}


class LabelRenderError(ValueError):
    def __init__(self,message,*,code='ORIGINAL_STOCK_LABEL_RENDER_INVALID'):
        super().__init__(message);self.code=code


def _fail(message='The original stock labels could not be rendered safely.',code='ORIGINAL_STOCK_LABEL_RENDER_INVALID'):
    raise LabelRenderError(message,code=code)


def _font_bytes():
    from reportlab import __file__ as reportlab_file
    root=Path(reportlab_file).resolve().parent/'fonts';result={}
    try:
        for kind,name in _FONT_FILES.items():
            path=root/name
            if not path.is_file() or path.is_symlink() or not 1<=path.stat().st_size<=1024*1024:
                raise ValueError()
            data=path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=FONT_HASHES[kind]:raise ValueError()
            result[kind]=data
    except (OSError,ValueError):
        _fail('The pinned stock-label fonts are missing or changed. Restore the complete application font resources.',
              'ORIGINAL_STOCK_LABEL_FONT_UNAVAILABLE')
    return result


def _font_faces(data):
    from reportlab.pdfbase.ttfonts import TTFont
    try:return {kind:TTFont('SSOriginalStock-'+kind,io.BytesIO(value)).face for kind,value in data.items()}
    except (ValueError,TypeError,KeyError,OSError):
        _fail('The pinned stock-label fonts could not be opened.','ORIGINAL_STOCK_LABEL_FONT_UNAVAILABLE')


def _text_supported(text, face):
    for char in text:
        category=unicodedata.category(char)
        if (category.startswith('C') or category in ('Mn','Mc','Me')
            or unicodedata.bidirectional(char) in ('R','AL','AN')
            or (not char.isspace() and ord(char) not in face.charToGlyph)):
            _fail('The original printed label text needs a supported glyph or shaping capability. No label or SKU was changed.',
                  'ORIGINAL_STOCK_LABEL_TEXT_UNSUPPORTED')


def _pil_font(data, kind, points, dpi):
    from PIL import ImageFont
    return ImageFont.truetype(io.BytesIO(data[kind]),max(1,round(points*dpi/72)),layout_engine=ImageFont.Layout.BASIC)


def _length(font,text):return font.getlength(text)


def _prefix(font,text,width):
    low=0;high=len(text)
    while low<high:
        middle=(low+high+1)//2
        if _length(font,text[:middle])<=width:low=middle
        else:high=middle-1
    return low


def _single(font,text,width):
    if _length(font,text)<=width:return text,False
    suffix='…';length=_prefix(font,text,max(0,width-_length(font,suffix)))
    return text[:length]+suffix,True


def _name_lines(font,text,width):
    first=_prefix(font,text,width)
    if first==len(text):return [text],False
    if first==0:_fail('The original name cannot fit the frozen label text area.','ORIGINAL_STOCK_LABEL_TEXT_LAYOUT')
    # Preserve every source character in the visible prefix, including spaces.
    boundary=text.rfind(' ',0,first+1)
    if boundary>0:first=boundary+1
    second,clipped=_single(font,text[first:],width)
    return [text[:first],second],clipped


def _raster(card, spec, data, faces):
    from PIL import Image,ImageDraw
    dpi=spec['dpi'];w=spec['width_dots'];h=spec['height_dots'];dot=lambda points:round(points*dpi/72)
    image=Image.new('L',(w,h),255);fold=spec['layout']=='fold';side=spec['layout']=='side'
    layer=Image.new('L',(w,h//2),255) if fold else image
    draw=ImageDraw.Draw(layer);left=w//2-dot(10) if side else w-dot(12)
    name_font=_pil_font(data,'bold',7 if fold else 8,dpi)
    name=card['card_name'];_text_supported(name,faces['bold']);_text_supported('…',faces['bold'])
    lines,clipped=_name_lines(name_font,name,left)
    overflows=[]
    for index,line in enumerate(lines):draw.text((dot(5),dot(4+index*(8 if fold else 9))),line,font=name_font,fill=0,anchor='lt')
    if clipped:overflows.append({'field':'card_name','shownText':'\n'.join(lines),'originalLength':len(name)})
    if side:
        details=[('set_number',(card.get('card_set','')+' #'+card.get('card_num','')) if card.get('card_set') or card.get('card_num') else ''),
                 ('condition',card.get('condition','')),('printing',card.get('printing',''))]
        regular=_pil_font(data,'regular',5.5,dpi);_text_supported('…',faces['regular'])
        for index,(field,text) in enumerate(details):
            _text_supported(text,faces['regular']);shown,clipped=_single(regular,text,left)
            draw.text((dot(5),dot(24+index*7)),shown,font=regular,fill=0,anchor='lt')
            if clipped:overflows.append({'field':field,'shownText':shown,'originalLength':len(text)})
    price='$'+format(card['final_price'],'.2f');price_points=12 if side else 10
    price_font=_pil_font(data,'bold',price_points,dpi);_text_supported(price,faces['bold'])
    if _length(price_font,price)>left:_fail('The original price cannot fit the frozen label text area.','ORIGINAL_STOCK_LABEL_TEXT_LAYOUT')
    if side:
        height_pts=h*72/dpi
        price_y=h-dot(max(spec['human_height_dots']*72/dpi+2,height_pts-62)+price_points)
    else:price_y=dot(24)
    if price_y<0 or price_y+dot(price_points)>layer.height:
        _fail('The original price cannot fit the frozen label text area.','ORIGINAL_STOCK_LABEL_TEXT_LAYOUT')
    draw.text((dot(5),price_y),price,font=price_font,fill=0,anchor='lt')
    if fold:image.paste(layer.transpose(Image.Transpose.ROTATE_180),(0,0))
    draw=ImageDraw.Draw(image);sku_font=_pil_font(data,'regular',5,dpi)
    for index,line in enumerate(spec['human_lines']):
        _text_supported(line,faces['regular']);length=_length(sku_font,line)
        if length>w-dot(8):_fail('The complete exact SKU text cannot fit the frozen label. It will not be shortened.',
                                  'ORIGINAL_STOCK_LABEL_TEXT_LAYOUT')
        y=h-dot(4+(len(spec['human_lines'])-index)*6)
        draw.text((round((w-length)/2),y),line,font=sku_font,fill=0,anchor='lt')
    return image.point(lambda pixel:255 if pixel>=128 else 0,mode='1'),overflows


def _black_bytes(image):
    from PIL import ImageOps,Image
    return ImageOps.invert(image.convert('L')).convert('1',dither=Image.Dither.NONE).tobytes()


def _zpl(card,spec,image,count):
    width=spec['width_dots'];height=spec['height_dots'];raw=_black_bytes(image);stride=math.ceil(width/8)
    fields=f'^XA\n^PW{width}^LL{height}^LH0,0^LS0\n^FO0,0^GFA,{len(raw)},{len(raw)},{stride},{raw.hex().upper()}^FS\n'
    if spec['symbology']=='QR':
        quiet=4*spec['module_dots'];x=spec['x_dots']+quiet;y=height-spec['y_dots']-spec['total_width_dots']+quiet
        fields+=f'^FO{x},{y}^BQN,2,{spec["module_dots"]}^FH^FDMM,B{len(spec["value"].encode("ascii")):04d}{zpl_hex(spec["value"])}^FS\n'
    else:
        x=spec['x_dots']+10*spec['module_dots'];y=height-spec['y_dots']-spec['bar_height_dots']
        fields+=f'^BY{spec["module_dots"]},2^FO{x},{y}^BCN,{spec["bar_height_dots"]},N,N,N,N^FH^FD{zpl_code128(spec["value"])}^FS\n'
    return (fields+f'^PQ{count}^XZ\n').encode('ascii')


def _prepare(cards,chosen,render_counts):
    try:facts,chosen,counts=validate_render_counts(cards,chosen,render_counts)
    except LabelPlanError as error:_fail(str(error),error.code)
    data=_font_bytes();faces=_font_faces(data);by_id={card['row_id']:card for card in cards};rows=[];overflows=[];estimated=0
    try:
        for ident,count in counts.items():
            card=by_id[ident];spec=geometry(card['sku'],chosen);image,clipped=_raster(card,spec,data,faces)
            commands=_zpl(card,spec,image,count);estimated+=len(commands)
            if estimated>MAX_ARTIFACT_BYTES:_fail('The bounded ZPL artifact exceeds its byte limit.','ORIGINAL_STOCK_LABEL_BYTES_EXCEEDED')
            rows.append({'id':ident,'card':card,'spec':spec,'image':image,'count':count,'zpl':commands})
            overflows.extend({'reviewLineId':ident,**value} for value in clipped)
    except LabelRenderError:raise
    except (ValueError,TypeError,KeyError,UnicodeError,OverflowError,OSError):_fail()
    bundle=fingerprint(FONT_HASHES)
    basis={'rendererVersion':RENDERER_VERSION,'profile':chosen,'cardsHash':facts['cardsHash'],
        'renderCounts':counts,'fontBundleHash':bundle,
        'rasters':[{'reviewLineId':row['id'],'sha256':hashlib.sha256(row['image'].tobytes()).hexdigest(),
            'width':row['image'].width,'height':row['image'].height} for row in rows],
        'overflows':overflows}
    copies=sum(counts.values())
    metadata={'version':1,'rendererVersion':RENDERER_VERSION,'profile':chosen,'cardsHash':facts['cardsHash'],
        'receivingId':facts['receivingId'],'terminalReceiptHash':facts['terminalReceiptHash'],
        'copyCount':copies,'rowCount':len(rows),'renderCountsHash':fingerprint(counts),'fonts':dict(FONT_HASHES),
        'fontBundleHash':bundle,'renderBasisHash':fingerprint(basis),'expectedPdfPages':copies,
        'expectedZplCopies':copies,'estimatedZplBytes':estimated,'overflows':overflows,'hardwareAcceptanceComplete':False}
    return metadata,rows


def preflight(cards,chosen,render_counts):
    """Validate and measure actual selected raster content before any file write."""
    return _prepare(cards,chosen,render_counts)[0]


class _LimitedWriter:
    def __init__(self,stream):self.stream=stream;self.count=0;self.name=stream.name
    def write(self,value):
        if self.count+len(value)>MAX_ARTIFACT_BYTES:
            _fail('The bounded label artifact exceeds its byte limit.','ORIGINAL_STOCK_LABEL_BYTES_EXCEEDED')
        written=self.stream.write(value);self.count+=written;return written
    def flush(self):return self.stream.flush()


def _pdf(rows,stream,chosen):
    from reportlab.pdfgen import canvas
    from reportlab.graphics import renderPDF
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.utils import ImageReader
    width,height=STOCKS[chosen['stock']];unit=72/chosen['dpi']
    output=canvas.Canvas(stream,pagesize=(width*72,height*72),pageCompression=1,invariant=1)
    output.setTitle('Stock & Sleeve original stock labels');output.setAuthor('Stock & Sleeve')
    for row in rows:
        spec=row['spec'];image=ImageReader(row['image'].convert('L'))
        form='stock-'+row['id'];output.beginForm(form,0,0,width*72,height*72)
        output.drawImage(image,0,0,width*72,height*72)
        if spec['symbology']=='QR':
            widget=_qr(spec['value']);size=spec['total_width_dots']*unit;x0,y0,x1,y1=widget.getBounds()
            drawing=Drawing(size,size,transform=[size/(x1-x0),0,0,size/(y1-y0),0,0]);drawing.add(widget)
            renderPDF.draw(drawing,output,spec['x_dots']*unit,spec['y_dots']*unit)
        else:
            symbol=_barcode(spec['value'],spec['module_dots']*unit,spec['bar_height_dots']*unit)
            symbol.drawOn(output,spec['x_dots']*unit,spec['y_dots']*unit)
        output.endForm()
        for _ in range(row['count']):
            output.doForm(form);output.showPage()
    output.save()


def render_pair(cards,pdf_path,zpl_path,chosen,render_counts):
    """Write a complete private pair exclusively; never overwrite older output.

    A publication coordinator owns path containment and authorization. This
    helper removes only paths it successfully created if either stage fails.
    """
    metadata,rows=_prepare(cards,chosen,render_counts);pdf=Path(pdf_path);zpl=Path(zpl_path)
    if pdf.resolve()==zpl.resolve() or pdf.exists() or zpl.exists():
        _fail('Choose fresh private paths for both original stock label files.')
    created=[]
    try:
        with pdf.open('xb') as raw:
            created.append(pdf);_pdf(rows,_LimitedWriter(raw),metadata['profile']);raw.flush();os.fsync(raw.fileno())
        with zpl.open('xb') as raw:
            created.append(zpl);writer=_LimitedWriter(raw)
            for row in rows:writer.write(row['zpl'])
            raw.flush();os.fsync(raw.fileno())
        outputs={}
        for kind,path in (('pdf',pdf),('zpl',zpl)):
            length=path.stat().st_size
            if not 1<=length<=MAX_ARTIFACT_BYTES:_fail('The label artifact has an invalid byte size.','ORIGINAL_STOCK_LABEL_BYTES_EXCEEDED')
            with path.open('rb') as stream:
                hashed=hashlib.file_digest(stream,'sha256').hexdigest()
            outputs[kind]={'sha256':hashed,'bytes':length}
        outputs['pdf']['pages']=metadata['expectedPdfPages'];outputs['zpl']['expectedCopies']=metadata['expectedZplCopies']
        return {**metadata,**outputs}
    except BaseException as error:
        for path in created:
            try:path.unlink(missing_ok=True)
            except OSError:pass
        if isinstance(error,(OSError,ValueError,TypeError,OverflowError)) and not isinstance(error,LabelRenderError):
            _fail('The private label pair could not be written completely. No older output was replaced.',
                  'ORIGINAL_STOCK_LABEL_WRITE_FAILED')
        raise
