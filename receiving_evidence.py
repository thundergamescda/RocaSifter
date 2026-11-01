"""Pure desktop verification of typed catalog projections, never provider HTTP."""
import copy
import re
import unicodedata
import uuid
from hosted_labels import fingerprint

HASH=re.compile('[a-f0-9]{64}')
CATALOG_FIELDS=('catalog_confirmed','catalog_proof','catalog_proof_hash','catalog_resolutions','catalog_resolutions_hash')


def provider_id(value):
    if (not isinstance(value,str) or not 1<=len(value)<=255 or value!=value.strip() or value in ('.','..')
        or any(unicodedata.category(char) in ('Cc','Cs') for char in value)):raise ValueError('The exact provider ID is invalid.')
    return value


def hashed(value):
    if not isinstance(value,str) or not HASH.fullmatch(value):raise ValueError('Invalid evidence hash.')
    return value


def uid(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise ValueError('Noncanonical evidence UUID.')
    return value


def catalog_evidence(value,targets):
    creating={row['review_line_id']:row for row in targets if row['action']=='create_price'}
    result={key:copy.deepcopy(value.get(key)) for key in CATALOG_FIELDS}
    if type(result['catalog_confirmed']) is not bool:raise ValueError('Catalog proof status is missing.')
    if not result['catalog_confirmed']:
        if (result['catalog_proof'] is not None or result['catalog_proof_hash'] is not None
            or result['catalog_resolutions']!=[] or result['catalog_resolutions_hash'] is not None
            or value.get('state') in ('catalog_confirmed',) or not creating and value.get('state')=='catalog_pending' or creating and (value.get('state') in ('inventory_pending','received') or value.get('inventory_confirmed') is True)):
            raise ValueError('Unconfirmed catalog cannot imply stock targets.')
        return result
    proof=result['catalog_proof'];resolutions=result['catalog_resolutions']
    if not creating or not isinstance(proof,dict):raise ValueError('Unexpected catalog write proof.')
    kind=proof.get('kind');last='response_hash' if kind=='catalog_batch_ack' else 'evidence_hash' if kind=='catalog_state_match' else None
    if last is None or set(proof)!={'kind','request_hash',last}:raise ValueError('Unknown catalog proof schema.')
    hashed(proof['request_hash']);hashed(proof[last])
    if hashed(result['catalog_proof_hash'])!=fingerprint(proof):raise ValueError('Catalog proof hash changed.')
    if not isinstance(resolutions,list) or not 1<=len(resolutions)<=100 or hashed(result['catalog_resolutions_hash'])!=fingerprint(resolutions):raise ValueError('Catalog resolution hash changed.')
    fields={'review_line_id','review_line_ids','identity_hash','target_sku','parent_id','selling_cents','template_id','template_version','variation_id','currency','binding_id','binding_version','binding_evidence_hash'}
    seen=set();variations={target['variation_id'] for target in targets if target['action']=='reuse_existing'};bindings=set()
    for row in resolutions:
        if not isinstance(row,dict) or set(row)!=fields:raise ValueError('Unknown catalog resolution fields.')
        representative=uid(row['review_line_id']);aliases=row['review_line_ids']
        if not isinstance(aliases,list) or not aliases or len(aliases)>1000 or representative not in aliases or len(set(aliases))!=len(aliases):raise ValueError('Invalid catalog review aliases.')
        if type(row['selling_cents']) is not int or type(row['template_version']) is not int or row['template_version']<0 or row['currency']!='USD':raise ValueError('Catalog price/template changed.')
        variation=provider_id(row['variation_id']);provider_id(row['parent_id']);uid(row['template_id']);uid(row['binding_id']);hashed(row['binding_evidence_hash'])
        if variation.startswith('#') or variation in variations or row['binding_id'] in bindings or type(row['binding_version']) is not int or row['binding_version']!=1:raise ValueError('Catalog target was not resolved uniquely.')
        variations.add(variation);bindings.add(row['binding_id'])
        family=None
        for alias in aliases:
            uid(alias);target=creating.get(alias)
            if alias in seen or target is None or any(row[key]!=target[key] for key in ('identity_hash','target_sku','parent_id','selling_cents','template_id','template_version')):raise ValueError('Catalog alias differs from frozen source family.')
            current_family=tuple(target[key] for key in ('binding_id','binding_version','binding_evidence_hash','source_sku','template_evidence_hash'))
            if family is not None and family!=current_family:raise ValueError('Coalesced rows do not share the same immutable source family.')
            family=current_family
            seen.add(alias)
    if seen!=set(creating):raise ValueError('Catalog does not resolve every selected new-price line.')
    return result


def final_targets(value,targets):
    evidence=catalog_evidence(value,targets);by_review={alias:row for row in evidence['catalog_resolutions'] for alias in row['review_line_ids']};result=[]
    for frozen in targets:
        target=copy.deepcopy(frozen)
        if target['action']=='create_price':
            row=by_review[target['review_line_id']]
            target.update(source_binding_id=frozen['binding_id'],source_binding_version=frozen['binding_version'],source_binding_evidence_hash=frozen['binding_evidence_hash'],
                binding_id=row['binding_id'],binding_version=row['binding_version'],binding_evidence_hash=row['binding_evidence_hash'],binding_role='resolved_target',variation_id=row['variation_id'])
        result.append(target)
    return result
