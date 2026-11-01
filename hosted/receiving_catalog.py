"""Pure full-parent catalog request/proof helpers. No HTTP or database access.

Only caller-reviewed additions are appended. Lost-ack proof requires the exact
operation/review marker plus all old business facts, never a SKU alone.
"""
from copy import deepcopy
import hashlib
import json
import re

from .receiving_plan import ReceivingError, canonical, fingerprint, integer, text_hash, uuid_text
from .security import ServiceError, provider_id

MAX_BYTES=4*1024*1024
ADDITION_FIELDS=frozenset(('review_line_id','identity_hash','parent_id','source_variation_id','target_sku',
                         'selling_cents','template_id','template_version','template'))


def _unknown():
    return ReceivingError('Complete preserved catalog evidence is unavailable. Recover this same operation; do not create a replacement.',
                          code='PROVIDER_OUTCOME_UNKNOWN')


def _size(value):
    try:return len(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
    except (TypeError,ValueError):raise _unknown() from None


def price_sku(base_sku,cents):
    if not isinstance(base_sku,str) or not 1<=len(base_sku)<=100 or any(ord(c)<32 or ord(c)==127 for c in base_sku):
        raise ReceivingError('A retained base SKU is required before deriving a price variation.')
    integer(cents,'Selling price')
    # Exact legacy serialization, including its default ASCII Unicode escapes.
    raw=json.dumps(['roca-price-variation-v1',base_sku,'USD',cents],separators=(',',':'))
    return f'{int(hashlib.sha256(raw.encode()).hexdigest(),16)%10**12:012d}'


def _children(parent):
    if (not isinstance(parent,dict) or parent.get('type')!='ITEM' or parent.get('is_deleted') or
            type(parent.get('version')) is not int or parent['version']<1 or
            (parent.get('item_data') or {}).get('is_archived') is True):raise _unknown()
    ident=provider_id(parent.get('id'),'Parent ID')
    values=(parent.get('item_data') or {}).get('variations')
    if not isinstance(values,list) or not 1<=len(values)<=250:raise _unknown()
    seen=set()
    for child in values:
        if (not isinstance(child,dict) or child.get('type')!='ITEM_VARIATION' or child.get('is_deleted') or
                not isinstance(child.get('id'),str) or not child['id'] or child['id'] in seen or
                (child.get('item_variation_data') or {}).get('item_id')!=ident):raise _unknown()
        if not child['id'].startswith('#') and (type(child.get('version')) is not int or child['version']<1):raise _unknown()
        seen.add(child['id'])
    return values


def _addition(value):
    if not isinstance(value,dict) or set(value)!=ADDITION_FIELDS:raise _unknown()
    result=deepcopy(value)
    for key in ('review_line_id','template_id'):result[key]=uuid_text(value[key],key)
    result['identity_hash']=text_hash(value['identity_hash'],'Identity hash')
    result['parent_id']=provider_id(value['parent_id'],'Parent ID')
    result['source_variation_id']=provider_id(value['source_variation_id'],'Source variation ID')
    result['selling_cents']=integer(value['selling_cents'],'Selling price')
    result['template_version']=integer(value['template_version'],'Template version',1,2**63-1)
    template=value['template']
    required={'approved','new_price_enabled','location_ids','name_prefix'}
    if (not isinstance(template,dict) or not required.issubset(template) or set(template)-required-{'name_format_version'} or
            template['approved'] is not True or template['new_price_enabled'] is not True):raise _unknown()
    name_format=template.get('name_format_version',1)
    if type(name_format) is not int or name_format not in (1,2):raise _unknown()
    locations=template['location_ids'];prefix=template['name_prefix']
    if (not isinstance(locations,list) or not 1<=len(locations)<=1000 or
            not isinstance(prefix,str) or not 1<=len(prefix)<=100 or any(ord(c)<32 or ord(c)==127 for c in prefix)):
        raise _unknown()
    locations=[provider_id(location,'Template location') for location in locations]
    if len(set(locations))!=len(locations):raise _unknown()
    if not isinstance(value['target_sku'],str) or not re.fullmatch('[0-9]{12}',value['target_sku']):raise _unknown()
    result['template']=dict(template,location_ids=locations)
    return result


def _temp(row):return '#'+row['review_line_id'].replace('-','')


def _marker(operation,row):
    value={'ss_price':2,'identity':row['identity_hash'],'operation':operation,'review':row['review_line_id']}
    if row['template'].get('name_format_version',1)==2:value['name_format']=2
    result=canonical(value).decode()
    if len(result)>255:raise _unknown()
    return result


def _name(row):
    prefix=row['template']['name_prefix'];cents=row['selling_cents']
    if row['template'].get('name_format_version',1)==2:return prefix+f' · ${cents//100}.{cents%100:02d}'
    return prefix+' '+str(cents)


def build_catalog_request(operation_id,parents,additions,idempotency_key):
    operation=uuid_text(operation_id,'Receiving operation');key=uuid_text(idempotency_key,'Catalog idempotency key')
    if not isinstance(parents,dict) or not parents or not isinstance(additions,list) or not 1<=len(additions)<=100:
        raise _unknown()
    rows=[_addition(row) for row in additions];rows.sort(key=lambda row:row['review_line_id'])
    if (len({row['review_line_id'] for row in rows})!=len(rows) or
            len({row['target_sku'] for row in rows})!=len(rows) or set(parents)!={row['parent_id'] for row in rows}):raise _unknown()
    output=deepcopy(parents);all_ids=set();count=0
    for ident,parent in output.items():
        if parent.get('id')!=ident or ident.startswith('#') or ident in all_ids:raise _unknown()
        children=_children(parent);all_ids.add(ident);count+=1+len(children)
        for child in children:
            if child['id'].startswith('#') or child['id'] in all_ids:raise _unknown()
            all_ids.add(child['id'])
    for row in rows:
        parent=output[row['parent_id']];children=_children(parent)
        source=next((child for child in children if child['id']==row['source_variation_id']),None)
        if not source:raise _unknown()
        source_sku=(source.get('item_variation_data') or {}).get('sku')
        if row['target_sku']!=price_sku(source_sku,row['selling_cents']):raise _unknown()
        if any((child.get('item_variation_data') or {}).get('sku','').casefold()==row['target_sku'].casefold() for child in children):
            raise ReceivingError('The derived price SKU already belongs to a sibling. Review it before creating another.',code='SKU_COLLISION_REVIEW_REQUIRED')
        if len(children)>=250 or _temp(row) in all_ids:raise _unknown()
        child={'type':'ITEM_VARIATION','id':_temp(row),'present_at_all_locations':False,
               'present_at_location_ids':list(row['template']['location_ids']),
               'item_variation_data':{'item_id':row['parent_id'],'name':_name(row),
                   'sku':row['target_sku'],'pricing_type':'FIXED_PRICING','price_money':{'amount':row['selling_cents'],'currency':'USD'},
                   'track_inventory':True,'stockable':True,'sellable':True,'user_data':_marker(operation,row)}}
        children.append(child);all_ids.add(child['id']);count+=1
    if count>1000:raise _unknown()
    body={'idempotency_key':key,'batches':[{'objects':[output[ident] for ident in sorted(output)]}]}
    if _size(body)>MAX_BYTES:raise _unknown()
    return body


def _expected(body,additions):
    """Reconstruct the exact saved request before trusting any proof input."""
    try:
        if not isinstance(body,dict) or set(body)!={'idempotency_key','batches'} or len(body['batches'])!=1:raise _unknown()
        batch=body['batches'][0]
        if not isinstance(batch,dict) or set(batch)!={'objects'} or not isinstance(batch['objects'],list):raise _unknown()
        rows=[_addition(row) for row in additions];temporaries={_temp(row) for row in rows}
        parents={};operations=set();found=set()
        for parent in batch['objects']:
            children=_children(parent);old=deepcopy(parent);old['item_data']['variations']=[]
            if parent['id'] in parents:raise _unknown()
            for child in children:
                if child['id'] in temporaries:
                    found.add(child['id']);marker=json.loads(child['item_variation_data']['user_data'])
                    operations.add(uuid_text(marker['operation'],'Marker operation'))
                else:old['item_data']['variations'].append(deepcopy(child))
            parents[parent['id']]=old
        if found!=temporaries or len(operations)!=1:raise _unknown()
        rebuilt=build_catalog_request(operations.pop(),parents,rows,body['idempotency_key'])
        if canonical(rebuilt)!=canonical(body):raise _unknown()
        return rows
    except (ValueError,TypeError,KeyError,AttributeError,ServiceError):raise _unknown() from None


def _business(value):
    result=deepcopy(value)
    for key in ('version','created_at','updated_at'):result.pop(key,None)
    if result.get('type')=='ITEM_VARIATION':(result.get('item_variation_data') or {}).pop('ordinal',None)
    return result


def _index(values):
    if not isinstance(values,list) or len(values)>1000:raise _unknown()
    result={}
    for obj in values:
        if not isinstance(obj,dict):raise _unknown()
        ident=provider_id(obj.get('id'),'Catalog object ID')
        if ident.startswith('#') or obj.get('is_deleted'):raise _unknown()
        if ident in result and result[ident]!=obj:raise _unknown()
        result[ident]=obj
        if obj.get('type')=='ITEM':
            for child in _children(obj):
                child_id=provider_id(child.get('id'),'Variation ID')
                if child_id.startswith('#') or (child_id in result and result[child_id]!=child):raise _unknown()
                result[child_id]=child
        if len(result)>1000:raise _unknown()
    return result


def _prove(body,objects,rows,mappings=None):
    resolutions=[];used=set();evidence=[]
    for expected_parent in body['batches'][0]['objects']:
        ident=expected_parent['id'];actual=objects.get(ident)
        if not actual:raise _unknown()
        expected_children=_children(expected_parent);children=_children(actual)
        if actual['version']<expected_parent['version']:raise _unknown()
        expected_header=_business(expected_parent);actual_header=_business(actual)
        expected_header['item_data'].pop('variations');actual_header['item_data'].pop('variations')
        if expected_header!=actual_header:raise _unknown()
        by_id={child['id']:child for child in children};positions={child['id']:index for index,child in enumerate(children)}
        sequence=[]
        for expected in expected_children:
            temporary=expected['id'].startswith('#')
            if temporary:
                candidates=[child for child in children if (child.get('item_variation_data') or {}).get('user_data')==expected['item_variation_data']['user_data']]
                if len(candidates)!=1:raise _unknown()
                child=candidates[0]
                if mappings is not None and mappings.get(expected['id'])!=child['id']:raise _unknown()
                comparison=deepcopy(expected);comparison['id']=child['id']
            else:
                child=by_id.get(expected['id']);comparison=expected
                if not child:raise _unknown()
                if child['version']<expected['version']:raise _unknown()
            if _business(comparison)!=_business(child):raise _unknown()
            sequence.append(positions[child['id']])
            if temporary:
                if child['id'] in used:raise _unknown()
                used.add(child['id'])
                addition=next(row for row in rows if _temp(row)==expected['id'])
                resolutions.append({key:addition[key] for key in ('review_line_id','identity_hash','target_sku','parent_id','selling_cents','template_id','template_version')}|
                                   {'variation_id':child['id'],'currency':'USD'})
        if sequence!=sorted(sequence):raise _unknown()
        evidence.append(actual)
    if len(resolutions)!=len(rows):raise _unknown()
    return sorted(resolutions,key=lambda row:row['review_line_id']),evidence


def catalog_ack(body,result,additions):
    try:
        rows=_expected(body,additions)
        if not isinstance(result,dict) or result.get('errors') or _size(result)>MAX_BYTES:raise _unknown()
        mappings=result.get('id_mappings')
        if not isinstance(mappings,list) or len(mappings)!=len(rows):raise _unknown()
        mapped={};ids=set()
        for row in mappings:
            if not isinstance(row,dict) or set(row)!={'client_object_id','object_id'}:raise _unknown()
            temporary=row['client_object_id'];ident=provider_id(row['object_id'],'Resolved variation ID')
            if temporary in mapped or ident in ids or ident.startswith('#'):raise _unknown()
            mapped[temporary]=ident;ids.add(ident)
        if set(mapped)!={_temp(row) for row in rows}:raise _unknown()
        objects=_index(result.get('objects'));resolutions,_=_prove(body,objects,rows,mapped)
        return {'proof':{'kind':'catalog_batch_ack','request_hash':fingerprint(body),'response_hash':fingerprint(result)},'resolutions':resolutions}
    except (ValueError,TypeError,KeyError,AttributeError,ServiceError):raise _unknown() from None


def catalog_state(body,objects,additions):
    try:
        rows=_expected(body,additions)
        if not isinstance(objects,dict) or _size(objects)>MAX_BYTES or any(key!=value.get('id') for key,value in objects.items()):raise _unknown()
        indexed=_index(list(objects.values()));resolutions,evidence=_prove(body,indexed,rows)
        return {'proof':{'kind':'catalog_state_match','request_hash':fingerprint(body),'evidence_hash':fingerprint(evidence)},'resolutions':resolutions}
    except (ValueError,TypeError,KeyError,AttributeError,ServiceError):raise _unknown() from None
