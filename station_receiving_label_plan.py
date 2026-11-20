"""Pure bounded copy plans for verified original hosted stock.

This module validates local rendering inputs; it grants no provider, financial
or publication authority. Full card quantities and hosted marks are immutable.
"""
from decimal import Decimal, InvalidOperation
import math
import re
import uuid

from hosted_labels import STOCKS, fingerprint, profile, trusted_resolution


MAX_PART_COPIES = 1000
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
RESERVED_BYTES = 4096
ROW_COMMAND_BYTES = 4096
_HASH = re.compile(r'[a-f0-9]{64}')
_MARK_KEYS = {'version','state','receiving_id','receipt_hash','review_line_id','source_line_key',
              'identity_hash','sku','selling_cents','confirmed_quantity','fingerprint'}
PRINT_FIELDS = {'card_name':200,'card_set':200,'card_num':100,'condition':100,'printing':100}
PLAN_DOMAIN = 'stock-and-sleeve:original-stock-label-plan:v1'
PART_DOMAIN = 'stock-and-sleeve:original-stock-label-part:v1'


class LabelPlanError(ValueError):
    code = 'ORIGINAL_STOCK_LABEL_PLAN_INVALID'


def _fail(message='The original stock label range or confirmed card evidence is invalid.'):
    raise LabelPlanError(message)


def _integer(value, minimum=0, maximum=1000000):
    if type(value) is not int or not minimum <= value <= maximum:_fail()
    return value


def _uuid(value):
    if type(value) is not str:_fail()
    try:
        if str(uuid.UUID(value)) != value:_fail()
    except (ValueError,AttributeError):_fail()
    return value


def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):_fail()
    return value


def frozen_profile(chosen):
    if type(chosen) is not dict or set(chosen)!={'stock','layout','dpi'}:_fail('The original frozen label profile is required.')
    try:return profile(chosen)
    except (ValueError,TypeError,KeyError):_fail('The original frozen label profile is invalid.')


def budget_for(chosen):
    chosen=frozen_profile(chosen);width,height=STOCKS[chosen['stock']];dpi=chosen['dpi']
    # Uncompressed ^GFA uses two hex bytes per monochrome byte. Reserve enough
    # additional bytes for exact QR/Code128 transport, headers and ^PQ per row.
    row_bytes=math.ceil(width*dpi/8)*height*dpi*2+ROW_COMMAND_BYTES
    return {'maxCopiesPerPart':MAX_PART_COPIES,'maxArtifactBytes':MAX_ARTIFACT_BYTES,
            'maxZplRowBytes':row_bytes,'reservedBytes':RESERVED_BYTES}


def validate_cards(cards):
    """Return sorted compact facts, checking every original full card/mark."""
    if type(cards) is not list or not 1 <= len(cards) <= 1000:_fail()
    rows=[];seen=set();binding=None;total=0
    try:
        for card in cards:
            if type(card) is not dict:_fail()
            ident=_uuid(card.get('row_id'));quantity=_integer(card.get('qty'),1,100000)
            if ident in seen:_fail()
            seen.add(ident);total+=quantity
            if total>1000000:_fail()
            mark=card.get('hosted_sku_resolution')
            if (type(mark) is not dict or set(mark)!=_MARK_KEYS
                or type(mark.get('version')) is not int or mark['version']!=1
                or mark.get('state')!='received' or mark.get('review_line_id')!=ident
                or type(mark.get('confirmed_quantity')) is not int or mark['confirmed_quantity']!=quantity
                or type(mark.get('selling_cents')) is not int or not 0<=mark['selling_cents']<=100000000):_fail()
            _uuid(mark.get('receiving_id'));_uuid(mark.get('review_line_id'))
            for key in ('receipt_hash','source_line_key','identity_hash','fingerprint'):_hash(mark.get(key))
            if card.get('source_line_key')!=mark['source_line_key']:_fail()
            if type(card.get('sku')) is not str or type(mark.get('sku')) is not str:_fail()
            if (card.get('include') is not True or card.get('reviewed') is not True
                or card.get('price_confirmed') is not True or card.get('price_missing') is not False):_fail()
            if type(card.get('final_price')) not in (int,float) or not math.isfinite(card['final_price']):_fail()
            if Decimal(str(card['final_price']))*100!=mark['selling_cents']:_fail()
            if trusted_resolution(card) is None:_fail()
            current=(mark['receiving_id'],mark['receipt_hash'])
            if binding is not None and current!=binding:_fail('Original stock from different confirmed operations cannot share a label plan.')
            binding=current
            printed={}
            for key,limit in PRINT_FIELDS.items():
                value=card.get(key,'')
                if type(value) is not str or len(value)>limit or key=='card_name' and not value:_fail()
                printed[key]=value
            game=card.get('game')
            if type(game) is not str or not 1<=len(game)<=100:_fail()
            facts={'reviewLineId':ident,'quantity':quantity,'sourceLineKey':mark['source_line_key'],
                   'game':game,'printed':printed,'mark':dict(mark)}
            rows.append({'reviewLineId':ident,'quantity':quantity,'cardHash':fingerprint(facts)})
    except LabelPlanError:raise
    except (ValueError,TypeError,KeyError,InvalidOperation,OverflowError,UnicodeError):_fail()
    rows.sort(key=lambda row:row['reviewLineId'])
    return {'rows':rows,'cardsHash':fingerprint(rows),'totalCopies':total,
            'receivingId':binding[0],'terminalReceiptHash':binding[1]}


def build_plan(cards, chosen, copy_offset=0, copy_count=None):
    facts=validate_cards(cards);chosen=frozen_profile(chosen)
    offset=_integer(copy_offset,0,facts['totalCopies']-1)
    count=facts['totalCopies']-offset if copy_count is None else _integer(copy_count,1,facts['totalCopies']-offset)
    budget=budget_for(chosen)
    size=min(MAX_PART_COPIES,(MAX_ARTIFACT_BYTES-RESERVED_BYTES)//budget['maxZplRowBytes'])
    if size<1:_fail('The frozen profile cannot fit a bounded stock label part.')
    result={'version':1,'profile':chosen,**facts,'copyOffset':offset,'copyCount':count,
            'partSize':size,'partCount':math.ceil(count/size),'budget':budget}
    result['renderPlanHash']=fingerprint({'domain':PLAN_DOMAIN,'plan':result})
    return result


def _validated_plan(value):
    keys={'version','profile','rows','cardsHash','totalCopies','receivingId','terminalReceiptHash',
          'copyOffset','copyCount','partSize','partCount','budget','renderPlanHash'}
    if type(value) is not dict or set(value)!=keys or type(value['version']) is not int or value['version']!=1:_fail()
    chosen=frozen_profile(value['profile']);_uuid(value['receivingId']);_hash(value['terminalReceiptHash'])
    _hash(value['cardsHash']);_hash(value['renderPlanHash'])
    rows=value['rows']
    if type(rows) is not list or not 1<=len(rows)<=1000:_fail()
    previous=None;total=0
    for row in rows:
        if type(row) is not dict or set(row)!={'reviewLineId','quantity','cardHash'}:_fail()
        ident=_uuid(row['reviewLineId']);_hash(row['cardHash']);total+=_integer(row['quantity'],1,100000)
        if previous is not None and ident<=previous:_fail()
        previous=ident
    if total>1000000 or type(value['totalCopies']) is not int or value['totalCopies']!=total:_fail()
    if fingerprint(rows)!=value['cardsHash']:_fail()
    offset=_integer(value['copyOffset'],0,total-1);count=_integer(value['copyCount'],1,total-offset)
    budget=budget_for(chosen)
    if (type(value['budget']) is not dict or fingerprint(value['budget'])!=fingerprint(budget)):_fail()
    size=min(MAX_PART_COPIES,(MAX_ARTIFACT_BYTES-RESERVED_BYTES)//budget['maxZplRowBytes'])
    if type(value['partSize']) is not int or value['partSize']!=size:_fail()
    if type(value['partCount']) is not int or value['partCount']!=math.ceil(count/size):_fail()
    body={key:item for key,item in value.items() if key!='renderPlanHash'}
    if value['renderPlanHash']!=fingerprint({'domain':PLAN_DOMAIN,'plan':body}):_fail()
    return value


def part(plan, index):
    """Intersect one bounded part with sorted full quantities, without expansion."""
    value=_validated_plan(plan);index=_integer(index,0,value['partCount']-1)
    start=value['copyOffset']+index*value['partSize'];count=min(value['partSize'],value['copyCount']-index*value['partSize'])
    end=start+count;cursor=0;counts={}
    for row in value['rows']:
        upper=cursor+row['quantity'];selected=max(0,min(upper,end)-max(cursor,start))
        if selected:counts[row['reviewLineId']]=selected
        cursor=upper
    if sum(counts.values())!=count:_fail()
    result={'version':1,'renderPlanHash':value['renderPlanHash'],'partIndex':index,
            'copyOffset':start,'copyCount':count,'renderCounts':counts}
    result['partHash']=fingerprint({'domain':PART_DOMAIN,'part':result})
    return result


def validate_render_counts(cards, chosen, render_counts):
    facts=validate_cards(cards);chosen=frozen_profile(chosen)
    if type(render_counts) is not dict or not render_counts:_fail()
    quantities={row['reviewLineId']:row['quantity'] for row in facts['rows']};counts={};total=0
    for ident,count in render_counts.items():
        _uuid(ident)
        if ident not in quantities:_fail()
        counts[ident]=_integer(count,1,quantities[ident]);total+=count
    budget=budget_for(chosen)
    if total>MAX_PART_COPIES:_fail('Choose a bounded label part of at most 1,000 copies.')
    if len(counts)*budget['maxZplRowBytes']+RESERVED_BYTES>MAX_ARTIFACT_BYTES:
        _fail('This stock label part exceeds its bounded raster output budget. Choose a smaller explicit copy range.')
    return facts,chosen,dict(sorted(counts.items()))
