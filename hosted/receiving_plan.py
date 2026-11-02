"""Typed receiving plans and provider evidence validation; no network or SQL."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid
from intake_manifest import normalize_identity, source_line_id, ManifestError
from .security import ServiceError, provider_id

API_VERSION = '2026-09-16'
READ_SCOPES = ('MERCHANT_PROFILE_READ', 'ITEMS_READ', 'INVENTORY_READ')
WRITE_SCOPES = READ_SCOPES + ('INVENTORY_WRITE',)


class ReceivingError(ServiceError):
    def __init__(self, message, status=409, *, code='RECEIVING_REVIEW_REQUIRED', receiving_id=None):
        super().__init__(message, status)
        self.code, self.receiving_id = code, str(receiving_id) if receiving_id else None


def canonical(value):return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


# Typed receiving operation responses alone use this transport budget. Inputs
# remain 512 KiB and unrelated APIs retain their independent response limits.
PUBLIC_RESPONSE_BYTES=8*1024*1024


def public_response_bytes(value):
    result=canonical(value)
    if len(result)>PUBLIC_RESPONSE_BYTES:
        raise ReceivingError('The complete receiving result exceeds the supported response budget. Keep the original operation and journal for review before any new provider action.',
                             code='RECEIVING_PROJECTION_LIMIT')
    return result


def fingerprint(value):return hashlib.sha256(canonical(value)).hexdigest()


def uuid_text(value, label='ID'):
    try:
        if not isinstance(value, (str, uuid.UUID)):raise ValueError()
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):raise ServiceError(label+' is invalid.') from None


def text_hash(value, label='Hash'):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}',value):raise ServiceError(label+' is invalid.')
    return value


def integer(value, label, low=0, high=100_000_000):
    if type(value) is not int or not low<=value<=high:raise ServiceError(label+' must be a whole number in the supported range.')
    return value


def keys(value, allowed):
    if not isinstance(value,dict) or set(value)-set(allowed):raise ServiceError('Unsupported receiving fields.')


def normalize_plan(data):
    keys(data, ('request_key','physical_intake_id','physical_manifest_hash','source_snapshot_hash','workflow_kind',
                'accepted_trade_id','accepted_receipt_hash','location_id','new_inventory_confirmed',
                'excluded_not_received_ack','external_ownership_confirmed','client_label_profile','lines'))
    if data.get('new_inventory_confirmed') is not True or data.get('excluded_not_received_ack') is not True:
        raise ServiceError('Explicitly confirm new inventory and that excluded quantities will not be received.')
    mode=data.get('workflow_kind')
    if mode not in ('accepted_trade','inventory_only'):raise ServiceError('Select a supported receiving workflow.')
    result={key:uuid_text(data.get(key),key) for key in ('physical_intake_id',)}
    result.update({key:text_hash(data.get(key),key) for key in ('physical_manifest_hash','source_snapshot_hash')})
    result.update(workflow_kind=mode, location_id=provider_id(data.get('location_id'),'Square location ID'),
                  new_inventory_confirmed=True, excluded_not_received_ack=True)
    profile=data.get('client_label_profile')
    keys(profile,('stock','layout','dpi'))
    if (set(profile)!={'stock','layout','dpi'} or profile['stock'] not in ('2x1','3x1','4x2') or
            profile['layout'] not in ('standard','fold','side') or type(profile['dpi']) is not int or profile['dpi'] not in (203,300)):
        raise ServiceError('Choose an explicit supported label stock, layout and DPI before planning.')
    result['client_label_profile']=dict(profile)
    if mode=='accepted_trade':
        result.update(accepted_trade_id=uuid_text(data.get('accepted_trade_id'),'Accepted trade ID'),
                      accepted_receipt_hash=text_hash(data.get('accepted_receipt_hash'),'Accepted receipt hash'))
        if 'external_ownership_confirmed' in data:raise ServiceError('Do not mix accepted-trade and external ownership facts.')
    else:
        if data.get('external_ownership_confirmed') is not True or 'accepted_trade_id' in data or 'accepted_receipt_hash' in data:
            raise ServiceError('Inventory-only receiving needs explicit external ownership confirmation, without a trade.')
        result['external_ownership_confirmed']=True
    lines=data.get('lines')
    if not isinstance(lines,list) or not 1<=len(lines)<=1000:raise ServiceError('Select 1 to 1,000 reviewed source rows.')
    result['lines']=[];seen=set()
    for row in lines:
        keys(row, ('review_line_id','source_line_key','accepted_line_id','identity','quantity','selling_cents','zero_price_confirmed','reviewed'))
        ident=uuid_text(row.get('review_line_id'),'Reviewed line ID')
        if ident in seen:raise ServiceError('A reviewed line ID cannot appear twice.')
        seen.add(ident)
        value={'review_line_id':ident,'source_line_key':text_hash(row.get('source_line_key'),'Original source line'),
               'quantity':integer(row.get('quantity'),'Quantity',1,100000),
               'selling_cents':integer(row.get('selling_cents'),'Selling price'), 'reviewed':True}
        if row.get('reviewed') is not True:raise ServiceError('Review every selected receiving line.')
        if type(row.get('zero_price_confirmed',False)) is not bool:raise ServiceError('Zero-price confirmation must be boolean.')
        if value['selling_cents']==0 and row.get('zero_price_confirmed') is not True:raise ServiceError('Explicitly confirm a zero selling price.')
        value['zero_price_confirmed']=row.get('zero_price_confirmed',False)
        if mode=='accepted_trade':
            if 'identity' in row:raise ServiceError('Accepted identity comes from the server receipt.')
            value['accepted_line_id']=uuid_text(row.get('accepted_line_id'),'Accepted line ID')
        else:
            if 'accepted_line_id' in row:raise ServiceError('Inventory-only lines cannot assert accepted-trade line IDs.')
            try:value['identity']=normalize_identity(row.get('identity'))
            except ManifestError:raise ServiceError('A complete reviewed card identity is required.') from None
        result['lines'].append(value)
    result['lines'].sort(key=lambda row:row['review_line_id'])
    return result


def effective_variation(variation, parent, location):
    """Only an explicitly stockable, tracked, fixed-price whole-unit variation."""
    if (not isinstance(variation,dict) or variation.get('type')!='ITEM_VARIATION' or variation.get('is_deleted') or
            not isinstance(parent,dict) or parent.get('type')!='ITEM' or parent.get('is_deleted')):
        raise ReceivingError('The current catalog item or variation is unavailable.',code='CATALOG_MAPPING_REVIEW_REQUIRED')
    data=variation.get('item_variation_data') or {};item=parent.get('item_data') or {}
    if item.get('is_archived') is True:
        raise ReceivingError('This Square item is archived. Review its sale availability before receiving.',code='CATALOG_MAPPING_REVIEW_REQUIRED')
    if data.get('item_id')!=parent.get('id') or item.get('product_type') not in (None,'REGULAR'):
        raise ReceivingError('This parent/variation needs catalog review.',code='CATALOG_MAPPING_REVIEW_REQUIRED')
    for obj in (variation,parent):
        if obj.get('present_at_all_locations') is not True and location not in (obj.get('present_at_location_ids') or []):
            raise ReceivingError('This catalog target is not present at the selected location.',code='CATALOG_MAPPING_REVIEW_REQUIRED')
        if location in (obj.get('absent_at_location_ids') or []):raise ReceivingError('This catalog target excludes the selected location.')
    if data.get('measurement_unit_id') or data.get('stockable_conversion') or data.get('item_option_values'):
        raise ReceivingError('Unit conversions and item-option targets require explicit support before receiving.')
    if data.get('stockable') is not True or data.get('sellable') is not True:
        raise ReceivingError('This variation must explicitly support whole-unit stock and sales.')
    price=data.get('price_money');pricing=data.get('pricing_type');tracking=data.get('track_inventory',False)
    overrides=data.get('location_overrides') or []
    if not isinstance(overrides,list):raise ReceivingError('Location overrides need catalog review.')
    matching=[row for row in overrides if isinstance(row,dict) and row.get('location_id')==location]
    if len(matching)>1:raise ReceivingError('Duplicate location overrides require review.')
    if matching:
        override=matching[0]
        if 'price_money' in override:price=override['price_money']
        if 'pricing_type' in override:pricing=override['pricing_type']
        if 'track_inventory' in override:tracking=override['track_inventory']
    if (tracking is not True or pricing!='FIXED_PRICING' or not isinstance(price,dict) or
            price.get('currency')!='USD' or type(price.get('amount')) is not int or not 0<=price['amount']<=100_000_000):
        raise ReceivingError('The selected variation needs tracked, fixed USD pricing.',code='CATALOG_MAPPING_REVIEW_REQUIRED')
    sku=data.get('sku')
    if not isinstance(sku,str) or not 1<=len(sku)<=100 or any(ord(c)<32 or ord(c)==127 for c in sku):
        raise ReceivingError('The selected target needs a usable retained SKU.')
    return {'variation_id':provider_id(variation.get('id'),'Variation ID'),'parent_id':provider_id(parent.get('id'),'Parent ID'),
            'sku':sku,'selling_cents':price['amount'],'currency':'USD','track_inventory':True,
            'identity_anchor_hash':fingerprint({'sku':sku,'item_id':parent['id'],'user_data':data.get('user_data'),
                                               'measurement_unit_id':data.get('measurement_unit_id')})}


def bind_targets(plan, budget, accepted_lines, bindings, objects):
    """Derive immutable identity/cost from accepted lines; cap all price splits."""
    used={};accepted_used={};targets=[]
    by_id={str(row['id']):row for row in accepted_lines}
    for row in plan['lines']:
        key=row['source_line_key'];quantity=row['quantity']
        if key not in budget:raise ReceivingError('Original source-line binding is not in this intake.')
        used[key]=used.get(key,0)+quantity
        if used[key]>budget[key]:raise ReceivingError('Selected quantities exceed the immutable physical budget.')
        if plan['workflow_kind']=='accepted_trade':
            accepted=by_id.get(row['accepted_line_id'])
            if not accepted or accepted.get('included') is not True or accepted.get('source_line_key')!=key:raise ReceivingError('The accepted line binding does not match this trade.')
            accepted_used[row['accepted_line_id']]=accepted_used.get(row['accepted_line_id'],0)+quantity
            if accepted_used[row['accepted_line_id']]>accepted['quantity']:raise ReceivingError('Price splits exceed the accepted line quantity.')
            identity=accepted['identity']
        else:identity=row['identity']
        identity_hash=source_line_id(identity)
        candidates=[binding for binding in bindings if binding['identity_hash']==identity_hash and
                    binding['location_id']==plan['location_id'] and binding['selling_cents']==row['selling_cents']]
        if len(candidates)!=1:raise ReceivingError('Approve one exact identity/price/location catalog binding first.',code='CATALOG_MAPPING_REVIEW_REQUIRED')
        binding=candidates[0];variation=objects.get(binding['variation_id']);parent=objects.get(binding['parent_id'])
        effective=effective_variation(variation,parent,plan['location_id'])
        creating=binding.get('action')=='create_price'
        if (any(effective[key]!=binding[key] for key in ('variation_id','parent_id','sku','identity_anchor_hash')) or
                effective['selling_cents']!=(binding['source_selling_cents'] if creating else binding['selling_cents'])):
            raise ReceivingError('The current catalog binding changed. Review it before receiving.',code='CATALOG_VERSION_CHANGED')
        targets.append(dict(row)|{'identity':identity,'identity_hash':identity_hash,'binding_id':str(binding['id']),
                        'binding_version':binding['version'],'binding_evidence_hash':binding['evidence_hash'],
                        'source_sku':binding['sku'],'target_sku':binding['new_target_sku'] if creating else effective['sku'],
                        'variation_id':None if creating else effective['variation_id'],
                        'parent_id':effective['parent_id'],'currency':'USD','action':'create_price' if creating else 'reuse_existing'})
        if creating:
            targets[-1].update(binding_role='source_family',template_id=str(binding['template_id']),template_version=binding['template_version'],
                               template_evidence_hash=binding['template_evidence_hash'])
    if len({row['variation_id'] or (row['parent_id'],row['target_sku']) for row in targets})>100:raise ReceivingError('This version supports at most 100 inventory targets.')
    return targets,used,{key:amount-used.get(key,0) for key,amount in budget.items()}


def inventory_body(operation_id, targets, location_id, occurred_at, idempotency_key):
    quantities={}
    for target in targets:quantities[target['variation_id']]=quantities.get(target['variation_id'],0)+target['quantity']
    if not 1<=len(quantities)<=100:raise ServiceError('An inventory request needs 1 to 100 targets.')
    changes=[]
    for variation,quantity in sorted(quantities.items()):
        reference='ss-receiving:'+str(operation_id)+':'+hashlib.sha256(variation.encode()).hexdigest()[:24]
        changes.append({'type':'ADJUSTMENT','adjustment':{'reference_id':reference,'catalog_object_id':variation,
            'catalog_object_type':'ITEM_VARIATION','from_state':'NONE','to_state':'IN_STOCK',
            'from_location_id':location_id,'to_location_id':location_id,'quantity':str(quantity),'occurred_at':occurred_at}})
    return {'idempotency_key':uuid_text(idempotency_key,'Provider idempotency key'),'changes':changes}


def validate_inventory_ack(body, result):
    if not isinstance(result,dict) or result.get('errors'):raise ReceivingError('Square did not confirm this inventory request.',code='PROVIDER_OUTCOME_UNKNOWN')
    expected={(row['adjustment']['catalog_object_id'],row['adjustment']['to_location_id']) for row in body['changes']}
    counts=result.get('counts')
    if not isinstance(counts,list):raise ReceivingError('Inventory acknowledgement is incomplete.',code='PROVIDER_OUTCOME_UNKNOWN')
    if len(counts)>1000:raise ReceivingError('Inventory acknowledgement exceeds the supported bound.',code='PROVIDER_OUTCOME_UNKNOWN')
    for row in counts:
        try:
            if not isinstance(row,dict) or not isinstance(row.get('quantity'),str) or not Decimal(row['quantity']).is_finite():raise ValueError()
        except (ValueError,InvalidOperation):raise ReceivingError('Inventory count evidence is malformed.',code='PROVIDER_OUTCOME_UNKNOWN') from None
    found={(row.get('catalog_object_id'),row.get('location_id')) for row in counts if isinstance(row,dict) and
           row.get('catalog_object_type')=='ITEM_VARIATION' and row.get('state')=='IN_STOCK'}
    if not expected.issubset(found):raise ReceivingError('Inventory acknowledgement omitted a target.',code='PROVIDER_OUTCOME_UNKNOWN')
    return {'kind':'batch_create_ack','request_hash':fingerprint(body),'response_hash':fingerprint(result)}


def history_proof(body, changes, application_id):
    matches={row['adjustment']['reference_id']:[] for row in body['changes']};seen={}
    for change in changes:
        if not isinstance(change,dict) or change.get('type')!='ADJUSTMENT':continue
        value=change.get('adjustment') or {};reference=value.get('reference_id')
        if reference not in matches:continue
        ident=value.get('id')
        if not isinstance(ident,str) or not ident:raise ReceivingError('History lacks an adjustment ID.',code='PROVIDER_OUTCOME_UNKNOWN')
        if ident in seen:
            if seen[ident]!=value:raise ReceivingError('History adjustment evidence conflicts.',code='PROVIDER_OUTCOME_UNKNOWN')
            continue
        seen[ident]=value;matches[reference].append(value)
    proven=[]
    for change in body['changes']:
        expected=change['adjustment'];found=matches[expected['reference_id']]
        if len(found)!=1:raise ReceivingError('Complete unique adjustment evidence is unavailable.',code='PROVIDER_OUTCOME_UNKNOWN')
        actual=found[0]
        if any(actual.get(key)!=expected[key] for key in ('reference_id','catalog_object_id','catalog_object_type','from_state','to_state','from_location_id','to_location_id')):
            raise ReceivingError('Inventory history target or location conflicts.',code='PROVIDER_OUTCOME_UNKNOWN')
        try:
            amount=Decimal(actual['quantity'])
            if not amount.is_finite() or amount!=Decimal(expected['quantity']):raise ValueError()
            at=datetime.fromisoformat(actual['occurred_at'].replace('Z','+00:00'))
            target=datetime.fromisoformat(expected['occurred_at'].replace('Z','+00:00'))
            if at.tzinfo is None or target.tzinfo is None or at!=target:raise ValueError()
        except (InvalidOperation,ValueError,TypeError,KeyError,AttributeError):
            raise ReceivingError('Inventory history amount/time conflicts.',code='PROVIDER_OUTCOME_UNKNOWN') from None
        if (actual.get('source') or {}).get('application_id')!=application_id:
            raise ReceivingError('Inventory history belongs to another application.',code='PROVIDER_OUTCOME_UNKNOWN')
        proven.append(actual['id'])
    return {'kind':'history_adjustments','request_hash':fingerprint(body),'adjustment_ids':sorted(proven),
            'evidence_hash':fingerprint([seen[key] for key in sorted(proven)])}


def preserved_price_sibling(parent, variation, *, new_sku, cents, temporary_id, template):
    """Pure next-path planner: complete parent, unchanged siblings, one append.

    Dispatch remains separately gated. No caller raw template is an API input.
    """
    if not template.get('approved') or template.get('new_price_enabled') is not True:
        raise ReceivingError('An approved new-price variation template is required.')
    children=(parent.get('item_data') or {}).get('variations')
    if not isinstance(children,list) or not children or not any(row.get('id')==variation.get('id') for row in children):
        raise ReceivingError('Retrieve the complete current parent and all siblings first.')
    if type(parent.get('version')) is not int or not temporary_id.startswith('#') or len(children)>=250:
        raise ReceivingError('The parent version or sibling capacity needs review.')
    if any((row.get('item_variation_data') or {}).get('sku')==new_sku for row in children):
        raise ReceivingError('A candidate SKU already exists; resolve it before creating a sibling.',code='SKU_COLLISION_REVIEW_REQUIRED')
    integer(cents,'Selling price');result=deepcopy(parent)
    new={'type':'ITEM_VARIATION','id':temporary_id,'present_at_all_locations':False,
         'present_at_location_ids':list(template['location_ids']),
         'item_variation_data':{'item_id':parent['id'],'name':template['name_prefix']+' '+str(cents),
             'sku':new_sku,'pricing_type':'FIXED_PRICING','price_money':{'amount':cents,'currency':'USD'},
             'track_inventory':True,'sellable':True,'stockable':True}}
    result['item_data']['variations'].append(new)
    if len(canonical(result))>4*1024*1024:raise ReceivingError('The preserved parent exceeds the supported catalog payload bound.')
    return result
