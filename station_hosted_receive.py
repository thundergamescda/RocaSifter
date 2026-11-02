"""Typed desktop hosted receiving and a separate encrypted recovery journal.

No Square tokens, provider endpoints or raw provider payloads belong here.
"""
import copy
import json
import re
import uuid
import unicodedata
from urllib.parse import quote

from station_cloud import CloudError,ORIGIN
from station_store import StationError,Vault,now
from hosted_labels import fingerprint,profile as label_profile,readiness as label_readiness
from intake_manifest import source_line_id,source_lines,manifest_digest
from station_intake_claim import required_manifest,source_key_for,original_identity
from square_variations import price_cents,variant_sku
from receiving_evidence import provider_id,catalog_evidence,final_targets,CATALOG_FIELDS

KEY=re.compile(r'[A-Za-z0-9_-]{1,150}')
RECEIPT=re.compile(r'R-[A-Za-z0-9-]{1,100}')
HASH=re.compile('[a-f0-9]{64}')
STATES={'ready','catalog_pending','catalog_confirmed','inventory_pending','outcome_unknown','needs_review','received','cancelled_before_write'}


def uuid_value(value,label='Receiving ID'):
    try:
        if not isinstance(value,str):raise ValueError()
        return str(uuid.UUID(value))
    except (ValueError,TypeError,AttributeError):raise CloudError(label+' is invalid.') from None


class ReceivingJournal:
    def __init__(self,cloud,root,vault=None):
        self.cloud=cloud;self.vault=vault or Vault(root)
        if vault is None:self.vault.path=self.vault.root/'cloud.receiving'

    def read(self):
        try:raw=self.vault.read()
        except (StationError,UnicodeError,OSError):
            raise CloudError('The saved hosted-receiving journal could not be opened. Keep the original action for owner recovery on this Windows account; do not replace it or send the batch through a local Square connection.',status=409,pending=True) from None
        if not raw:return []
        try:
            value=json.loads(raw)
            if (not isinstance(value,dict) or value.get('version')!=1 or value.get('origin')!=ORIGIN
                or not isinstance(value.get('operations'),list) or len(value['operations'])>50):raise ValueError()
            seen=set();key_kinds={}
            for row in value['operations']:
                if (not isinstance(row,dict) or row.get('kind') not in ('plan','commit','recover','authorize','cancel','revise','retire')
                    or not isinstance(row.get('store'),str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',row['store'])
                    or not isinstance(row.get('receipt_id'),str) or not RECEIPT.fullmatch(row['receipt_id'])
                    or not isinstance(row.get('request_key'),str) or not KEY.fullmatch(row['request_key'])
                    or not isinstance(row.get('body'),dict)):raise ValueError()
                original=row['body'].get('original_plan') if row['kind']=='retire' else row['body']
                if not isinstance(original,dict) or original.get('request_key')!=row['request_key']:raise ValueError()
                if not isinstance(row.get('label_profile'),dict):raise ValueError()
                chosen=label_profile(row['label_profile'])
                if row.get('fingerprint')!=fingerprint({'body':row['body'],'label_profile':chosen}):raise ValueError()
                if row['kind'] in ('plan','retire'):
                    if row.get('receiving_id') is not None:raise ValueError()
                else:uuid_value(row.get('receiving_id'))
                identity=(row['store'],row['request_key'],row['kind'])
                if identity in seen:raise ValueError()
                seen.add(identity)
                kinds=key_kinds.setdefault(identity[:2],set());kinds.add(row['kind'])
                if len(kinds)>1 and kinds!={'plan','retire'}:raise ValueError()
                if 'result' in row and (not isinstance(row['result'],dict) or row.get('result_fingerprint')!=fingerprint(row['result'])):raise ValueError()
            return value['operations']
        except (ValueError,TypeError,AttributeError,CloudError):
            raise CloudError('The saved hosted-receiving journal needs owner recovery. Keep the original operation and source; do not create another plan or reroute to a local token.',status=409,pending=True) from None

    def save(self,rows):
        if rows:self.vault.set(json.dumps({'version':1,'origin':ORIGIN,'operations':rows},ensure_ascii=False,separators=(',',':')))
        elif self.vault.path.exists():self.vault.path.unlink()

    def session(self,expected):
        self.cloud._cancel_guard();saved=self.cloud._saved();self.cloud._expected(saved,expected)
        if not saved or saved['expires_at']<=self.cloud.clock():raise CloudError('Sign in to the store that owns this hosted receiving operation.',status=401)
        if saved['role'] not in ('owner','manager'):raise CloudError('An owner or manager must review hosted receiving.',status=403)
        return saved

    def row(self,key,expected,kind=None):
        with self.cloud.lock:
            saved=self.session(expected)
            return next((copy.deepcopy(row) for row in self.read() if (row['store'],row['request_key'])==(saved['store'],key) and (kind is None or row['kind']==kind)),None)

    def remember(self,key,result,expected,kind=None):
        with self.cloud.lock:
            saved=self.session(expected);rows=self.read();row=next((row for row in rows if (row['store'],row['request_key'])==(saved['store'],key) and (kind is None or row['kind']==kind)),None)
            if row is None or not isinstance(result,dict):raise CloudError('The original hosted-receiving journal is missing. Keep this batch for owner recovery.',status=409,pending=True)
            try:hashed=fingerprint(result)
            except (ValueError,TypeError):raise CloudError('The hosted-receiving confirmation was malformed. Keep the original request and recover the same operation.',status=502,pending=True) from None
            row['result']=copy.deepcopy(result);row['result_fingerprint']=hashed;self.save(rows)

    def finish(self,receipt_id,expected):
        with self.cloud.lock:
            saved=self.session(expected)
            self.save([row for row in self.read() if (row['store'],row['receipt_id'])!=(saved['store'],receipt_id)])

    def finish_key(self,key,expected):
        with self.cloud.lock:
            saved=self.session(expected)
            self.save([row for row in self.read() if (row['store'],row['request_key'])!=(saved['store'],key)])

    def mutate(self,kind,body,receipt_id,chosen,expected,receiving_id=None):
        with self.cloud.lock:
            saved=self.session(expected);chosen=label_profile(chosen)
            if kind not in ('plan','commit','recover','authorize','cancel','revise','retire') or not isinstance(body,dict):raise CloudError('Use a typed hosted-receiving action.')
            key=(body.get('original_plan') or {}).get('request_key') if kind=='retire' else body.get('request_key')
            if not isinstance(key,str) or not KEY.fullmatch(key) or not isinstance(receipt_id,str) or not RECEIPT.fullmatch(receipt_id):raise CloudError('Use the saved hosted-receiving batch and request key.')
            if kind in ('plan','retire'):
                if receiving_id is not None:raise CloudError('A new plan cannot target another saved operation.')
                path='/api/receiving/plans' if kind=='plan' else '/api/receiving/requests/'+quote(key,safe='')+'/retire';method='POST'
            else:
                receiving_id=uuid_value(receiving_id);path='/api/receiving/'+receiving_id+('/plan' if kind=='revise' else '/'+kind);method='PUT' if kind=='revise' else 'POST'
            rows=self.read();existing=next((row for row in rows if (row['store'],row['request_key'],row['kind'])==(saved['store'],key,kind)),None)
            other_kinds={row['kind'] for row in rows if (row['store'],row['request_key'])==(saved['store'],key)}
            if other_kinds and other_kinds|{kind} not in ({kind},{'plan','retire'}):raise CloudError('The original request key belongs to a different action. Recover it without changing its type.',status=409,pending=True)
            hashed=fingerprint({'body':body,'label_profile':chosen})
            if existing:
                if (existing['kind']!=kind or existing['receipt_id']!=receipt_id or existing['receiving_id']!=receiving_id or existing['fingerprint']!=hashed):
                    raise CloudError('The saved hosted-receiving request has different details. Recover its original operation instead of changing the request or label profile.',status=409,pending=True)
                if 'result' in existing:return copy.deepcopy(existing['result'])
            else:
                if len(rows)>=50:raise CloudError('Recover the saved receiving actions on this computer before starting another.',status=409,pending=True)
                same=[row for row in rows if row['store']==saved['store'] and row['receipt_id']==receipt_id]
                if kind=='plan' and same:raise CloudError('This batch already has a saved receiving request. Recover it with its original key.',status=409,pending=True)
                rows.append({'kind':kind,'store':saved['store'],'receipt_id':receipt_id,'receiving_id':receiving_id,
                    'request_key':key,'body':copy.deepcopy(body),'label_profile':chosen,'fingerprint':hashed,'created_at':self.cloud.clock()})
                self.save(rows)
            try:result=self.cloud._authenticated(method,path,data=body,expected_session_id=expected)
            except CloudError as error:error.pending=True;raise
            self.remember(key,result,expected,kind);return result


def locked(receipt):
    intent=receipt.get('hosted_receive')
    return bool(intent and intent.get('state') not in ('confirmed','cancelled_before_write','retired_before_creation'))


def require_editable(receipt):
    if locked(receipt):raise StationError('This batch has a saved hosted-receiving operation. Open its original plan and recover it before changing the batch or label profile.')


def reviewed_rows(service,receipt):
    required_manifest(receipt);result=[];budget={line['source_line_key']:line['quantity'] for line in source_lines(receipt['source_manifest'])};used={}
    for original in receipt['cards']:
        if not original.get('include',True):continue
        card=service.validate_card(original)
        if not card.get('reviewed') or card.get('price_missing'):raise StationError('Review every included receiving identity, quantity and selling price first.')
        key=source_key_for(receipt,card);used[key]=used.get(key,0)+card['qty']
        if used[key]>budget[key]:raise StationError('Selected quantities exceed the original physical intake; review the original cards.')
        identity=original_identity(card);identity_hash=source_line_id(identity);cents=price_cents(card)
        if cents==0 and card.get('price_confirmed') is not True:raise StationError('Explicitly confirm each zero selling price before planning hosted stock.')
        result.append({'local_row_id':uuid_value(card['row_id'],'Local row ID'),'source_line_key':key,
            'identity':identity,'identity_hash':identity_hash,'quantity':card['qty'],'selling_cents':cents,
            'zero_price_confirmed':bool(cents==0 and card.get('price_confirmed') is True),'reviewed':True})
    if not 1<=len(result)<=1000:raise StationError('Select 1 to 1,000 reviewed receiving rows.')
    return sorted(result,key=lambda row:row['local_row_id'])


def local_snapshot(service,receipt,chosen,kind,location):
    return {'version':1,'source_manifest_hash':manifest_digest(required_manifest(receipt)),
        'claim_id':receipt['intake_claim']['physical_intake_id'],'label_profile':label_profile(chosen),
        'workflow_kind':kind,'location_id':location,'rows':reviewed_rows(service,receipt)}


class HostedReceive:
    def __init__(self,service):self.service=service

    @property
    def cloud(self):return self.service.cloud

    def _session(self,expected,store=None):
        saved=self.cloud.receiving.session(expected)
        if store and saved['store']!=store:raise CloudError('Sign in to the store that owns this receiving operation. It cannot be transferred to another store.',status=409)
        return saved

    def _receipt(self,ident):
        with self.service.lock:return self.service.active_receipt(ident)

    def _eligible(self,receipt):
        self.service.intake_claims._legacy_guard(receipt);required_manifest(receipt)
        if receipt.get('mode')=='bulk':raise StationError('Move this intake to Price & label or Online before receiving stock.')
        if receipt.get('inventory_intent')!='new':raise StationError('Explicitly choose Add new copies for this physical intake before hosted receiving.')
        if receipt.get('shared_trade') and receipt['shared_trade'].get('state') not in ('confirmed','cancelled'):
            raise StationError('Recover or cancel the saved shared trade before receiving its physical cards.')
        intent=receipt.get('intake_claim')
        if not intent or intent.get('state')!='confirmed':raise StationError('Create or recover the central physical intake reference before hosted receiving.')
        if receipt.get('inventory_status')=='received' and not receipt.get('hosted_receive'):raise StationError('Already received stock cannot be converted to a new hosted operation.')

    def _claim(self,receipt,expected):
        self._session(expected,receipt['intake_claim']['store'])
        claim=self.cloud.intake_get(receipt['intake_claim']['physical_intake_id'],expected_session_id=expected)
        self.service.intake_claims._verify(receipt,claim);return claim

    def _readiness(self,expected):
        value=self.cloud.receiving_readiness(expected_session_id=expected)
        if (value.get('protocol_version')!=1 or value.get('same_price_existing_supported') is not True
            or value.get('fresh_mfa_required') is not True or value.get('location_selection_required') is not True
            or value.get('max_targets')!=100 or any(type(value.get(key)) is not bool for key in ('write_enabled','server_write_enabled','tenant_write_enabled'))
            or value['write_enabled']!=(value['server_write_enabled'] and value['tenant_write_enabled'])):
            raise CloudError('The hosted receiving protocol/readiness could not be verified. No stock was submitted.',status=502)
        if type(value.get('new_price_supported')) is not bool or ('new_price_write_enabled' in value and (type(value['new_price_write_enabled']) is not bool or value['new_price_write_enabled'] and (not value['write_enabled'] or not value['new_price_supported']))):raise CloudError('New-price readiness is invalid. No provider step was submitted.',status=502)
        return value

    def context(self,ident,expected):
        with self.cloud.lock:
            self._session(expected);receipt=self._receipt(ident);required_manifest(receipt)
            readiness=self._readiness(expected);locations=self.cloud.receiving_locations(expected_session_id=expected).get('locations')
            if not isinstance(locations,list) or len(locations)>1000:raise CloudError('Hosted Square locations could not be verified.',status=502)
            seen=set()
            for row in locations:
                if (not isinstance(row,dict) or provider_id(row.get('id'))!=row['id']
                    or row['id'] in seen or row.get('status')!='ACTIVE' or row.get('currency')!='USD' or not isinstance(row.get('name'),str)):
                    raise CloudError('Select only a verified active USD Square location.',status=502)
                seen.add(row['id'])
            claim=self._claim(receipt,expected) if receipt.get('intake_claim',{}).get('state')=='confirmed' else None
            trade=None
            if claim and claim.get('bound_trade_id'):
                trade=self.cloud.trade_get(claim['bound_trade_id'],expected_session_id=expected)
            operation=None
            if receipt.get('hosted_receive',{}).get('state')=='cancelled_before_write':
                intent=receipt['hosted_receive'];operation=self.cloud.receiving_get(intent['receiving_id'],expected_session_id=expected)
                if operation.get('state')!='cancelled_before_write' or operation.get('plan_hash')!=intent['plan_hash'] or operation.get('revision')!=intent['server_revision']:
                    raise CloudError('The original cancellation is no longer current. Recover the original operation before editing.',status=409,pending=True)
            return {'receipt':receipt,'readiness':readiness,'locations':locations,'claim':claim,'acceptedTrade':trade,'operation':operation,
                'needsPhysicalIntake':claim is None,'hardwareAcceptanceComplete':False}

    def _plan_body(self,receipt,data,claim,trade):
        if not isinstance(data.get('labelProfile'),dict):raise StationError('Explicitly select the label stock, layout and printer DPI before planning.')
        kind=data.get('workflowKind');location=data.get('locationId');chosen=label_profile(data['labelProfile'])
        if kind not in ('accepted_trade','inventory_only') or provider_id(location)!=location:
            raise StationError('Select the receiving workflow and exact verified Square location.')
        if data.get('newInventoryConfirmed') is not True or data.get('excludedNotReceivedAck') is not True:
            raise StationError('Confirm these are new copies and that excluded quantities will not be received.')
        snapshot=local_snapshot(self.service,receipt,chosen,kind,location);lines=[]
        body={'physical_intake_id':claim['physical_intake_id'],'physical_manifest_hash':claim['manifest_hash'],
            'source_snapshot_hash':fingerprint(snapshot),'workflow_kind':kind,'location_id':location,
            'client_label_profile':chosen,'new_inventory_confirmed':True,'excluded_not_received_ack':True,'lines':lines}
        if kind=='accepted_trade':
            if not trade or not str(trade.get('state','')).startswith('accepted_') or claim.get('bound_trade_id')!=trade.get('id'):
                raise StationError('Use the immutable accepted hosted trade linked to this exact physical intake.')
            evidence=trade.get('accepted_receipt');mapping=data.get('acceptedLineMap') or {};used={}
            from station_shared_trade import fingerprint as trade_fingerprint
            if (not isinstance(evidence,dict) or trade_fingerprint(evidence)!=trade.get('receipt_hash')
                or evidence.get('physical_intake_id')!=claim['physical_intake_id'] or evidence.get('physical_manifest_hash')!=claim['manifest_hash']):
                raise StationError('The accepted financial receipt and physical intake evidence do not match.')
            body.update(accepted_trade_id=trade['id'],accepted_receipt_hash=trade['receipt_hash'])
            for row in snapshot['rows']:
                candidates=[line for line in evidence['lines'] if line.get('included') is True and line.get('source_line_key')==row['source_line_key'] and source_line_id(line['identity'])==row['identity_hash']]
                chosen_id=mapping.get(row['local_row_id'])
                selected=next((line for line in candidates if line['id']==chosen_id),None) if chosen_id else candidates[0] if len(candidates)==1 else None
                if not selected:raise StationError('Choose the exact accepted receipt line for each receiving row; identity, source key and accepted quantity must agree.')
                used[selected['id']]=used.get(selected['id'],0)+row['quantity']
                if used[selected['id']]>selected['quantity']:raise StationError('Selected price splits exceed the quantity accepted on the immutable trade receipt.')
                lines.append({key:row[key] for key in ('source_line_key','quantity','selling_cents','zero_price_confirmed','reviewed')}|{'review_line_id':row['local_row_id'],'accepted_line_id':selected['id']})
        else:
            if claim.get('state')!='open' or claim.get('bound_trade_id') or receipt.get('purchase',{} ) and receipt['purchase'].get('scope')=='hosted':
                raise StationError('An accepted hosted trade must be received against its original trade; it cannot become inventory-only.')
            if data.get('externalOwnershipConfirmed') is not True:raise StationError('Confirm the store owns these copies outside shared trade credit before inventory-only receiving.')
            body['external_ownership_confirmed']=True
            for row in snapshot['rows']:lines.append({key:row[key] for key in ('source_line_key','identity','quantity','selling_cents','zero_price_confirmed','reviewed')}|{'review_line_id':row['local_row_id']})
        return body,snapshot

    def _assert_source(self,receipt):
        intent=receipt['hosted_receive'];current=local_snapshot(self.service,receipt,intent['label_profile'],intent['workflow_kind'],intent['location_id'])
        if fingerprint(current)!=intent['local_snapshot_hash']:
            raise CloudError('The local reviewed source differs from the saved receiving plan. Keep the original operation for owner recovery; no receipt or SKU will be guessed.',status=409,pending=True)
        return current

    def _verify(self,receipt,value,*,initial=False):
        intent=receipt['hosted_receive'];snapshot=self._assert_source(receipt);body=intent['plan_body']
        try:
            if not isinstance(value,dict):raise ValueError()
            operation=uuid_value(value.get('receiving_id'))
            if intent.get('receiving_id') and operation!=intent['receiving_id']:raise ValueError()
            if value.get('state') not in STATES or type(value.get('revision')) is not int or value['revision']<0:raise ValueError()
            if not isinstance(value.get('plan_hash'),str) or not HASH.fullmatch(value['plan_hash']):raise ValueError()
            for key in ('physical_intake_id','physical_manifest_hash','source_snapshot_hash','workflow_kind'):
                if value.get(key)!=body[key]:raise ValueError()
            for key in ('accepted_trade_id','accepted_receipt_hash'):
                if value.get(key)!=body.get(key):raise ValueError()
            if value.get('client_label_profile')!=intent['label_profile']:raise ValueError()
            provider=value.get('provider')
            if (not isinstance(provider,dict) or set(provider)!={'merchant_id','environment','application_id','location_id'}
                or provider['location_id']!=intent['location_id'] or provider['environment'] not in ('production','sandbox')
                or any(provider_id(provider[key])!=provider[key] for key in ('merchant_id','application_id','location_id'))):raise ValueError()
            rows={row['review_line_id']:row for row in body['lines']};mapping=intent.get('row_map',{})
            identities={mapping.get(row['local_row_id'],row['local_row_id']):row['identity_hash'] for row in snapshot['rows']}
            targets=value.get('targets')
            if not isinstance(targets,list) or len(targets)!=len(rows) or len({row.get('review_line_id') for row in targets})!=len(rows):raise ValueError()
            used={};allowed={'review_line_id','source_line_key','accepted_line_id','identity_hash','binding_id','binding_version','binding_evidence_hash','source_sku','target_sku','variation_id','parent_id','selling_cents','currency','quantity','action','binding_role','template_id','template_version','template_evidence_hash'}
            for target in targets:
                if not isinstance(target,dict) or set(target)-allowed:raise ValueError()
                row=rows.get(target.get('review_line_id'))
                if not row or any(target.get(key)!=row[key] for key in ('source_line_key','quantity','selling_cents')):raise ValueError()
                if (type(target.get('quantity')) is not int or not 1<=target['quantity']<=100000
                    or type(target.get('selling_cents')) is not int or not 0<=target['selling_cents']<=100_000_000
                    or not isinstance(target.get('target_sku'),str) or not 1<=len(target['target_sku'])<=100
                    or any(ord(char)<32 or ord(char)==127 for char in target['target_sku'])):raise ValueError()
                if (target.get('identity_hash')!=identities[target['review_line_id']] or target.get('accepted_line_id')!=row.get('accepted_line_id') or target.get('currency')!='USD'):raise ValueError()
                if not isinstance(target.get('source_sku'),str) or not 1<=len(target['source_sku'])<=100 or any(unicodedata.category(char) in ('Cc','Cs') for char in target['source_sku']):raise ValueError()
                provider_id(target.get('parent_id'))
                if target.get('action')=='reuse_existing':
                    provider_id(target.get('variation_id'))
                    if target['variation_id'].startswith('#') or target['source_sku']!=target['target_sku'] or any(key in target for key in ('binding_role','template_id','template_version','template_evidence_hash')):raise ValueError()
                elif target.get('action')=='create_price':
                    if target.get('variation_id') is not None or target.get('binding_role')!='source_family' or target['target_sku']!=variant_sku(target['source_sku'],target['selling_cents']):raise ValueError()
                    uuid_value(target.get('template_id'),'Price template ID')
                    if type(target.get('template_version')) is not int or target['template_version']<0 or not isinstance(target.get('template_evidence_hash'),str) or not HASH.fullmatch(target['template_evidence_hash']):raise ValueError()
                else:raise ValueError()
                uuid_value(target.get('binding_id'),'Catalog binding ID')
                if type(target.get('binding_version')) is not int or target['binding_version']<0 or not isinstance(target.get('binding_evidence_hash'),str) or not HASH.fullmatch(target['binding_evidence_hash']):raise ValueError()
                used[row['source_line_key']]=used.get(row['source_line_key'],0)+row['quantity']
            budget={row['source_line_key']:row['quantity'] for row in source_lines(receipt['source_manifest'])}
            for key in ('selected_quantities','excluded_quantities'):
                if not isinstance(value.get(key),dict) or any(not isinstance(group,str) or not HASH.fullmatch(group) or type(qty) is not int or qty<0 for group,qty in value[key].items()):raise ValueError()
            if value.get('selected_quantities')!=used or value.get('excluded_quantities')!={key:qty-used.get(key,0) for key,qty in budget.items()}:raise ValueError()
            if len({target['variation_id'] or (target['parent_id'],target['target_sku']) for target in targets})>100:raise ValueError()
            catalog=catalog_evidence(value,targets)
            if intent.get('catalog_evidence',{}).get('catalog_confirmed') and catalog!=intent['catalog_evidence']:raise ValueError()
            if type(value.get('inventory_confirmed')) is not bool or value['state']!='received' and value.get('receipt') is not None:raise ValueError()
            if not initial and (value['plan_hash']!=intent['plan_hash'] or value['revision']!=intent['server_revision'] or provider!=intent['provider'] or targets!=intent['targets']):raise ValueError()
            return True
        except (ValueError,TypeError,KeyError,AttributeError,CloudError):
            raise CloudError('The hosted receiving plan does not match the locked source, exact SKU, label profile or provider binding. Keep this original operation for owner recovery.',status=502,pending=True) from None

    def _perform(self,receipt,kind,expected):
        intent=receipt['hosted_receive'];action=intent['actions'][kind];body=action['body'];key=body['original_plan']['request_key'] if kind=='retire' else body['request_key']
        row=self.cloud.receiving.row(key,expected,kind)
        if row:
            if row['body']!=body or row['label_profile']!=intent['label_profile']:raise CloudError('The encrypted receiving request differs from its saved local intent. Keep the original operation for owner recovery.',status=409,pending=True)
            if 'result' in row:return row['result']
            status=self.cloud.receiving_request(key,expected_session_id=expected)
            if status.get('found') is True and isinstance(status.get('result'),dict):
                if kind=='retire' and status['result'].get('retired') is not True:
                    raise CloudError('The original claim already has a receiving operation. Explicitly read and recover that original record; this request cannot be retired.',status=409,pending=True,receiving_id=status['result'].get('receiving_id'))
                self.cloud.receiving.remember(key,status['result'],expected,kind);return status['result']
            if status.get('found') is not False:raise CloudError('The original receiving request is uncertain. Recover it without creating a replacement key.',status=502,pending=True)
        return self.cloud.receiving_mutate(kind,body,receipt['id'],intent['label_profile'],expected_session_id=expected,
            receiving_id=None if kind in ('plan','retire') else intent['receiving_id'])

    def _view(self,receipt,value,readiness=None):
        try:labels=label_readiness(value['targets'],receipt['hosted_receive']['label_profile']);error=None
        except ValueError as exc:labels=None;error=str(exc)
        return {'receipt':receipt,'operation':value,'readiness':readiness,'labelReadiness':labels,'labelError':error,
            'readOnly':False,'hardwareAcceptanceComplete':False}

    @staticmethod
    def _local_target(intent,targets,card):
        local=uuid_value(card['row_id'],'Local row ID')
        return targets[intent.get('row_map',{}).get(local,local)]

    def recover_existing(self,ident,data,expected):
        """Bind independently imported rows without creating a key or admission."""
        with self.cloud.lock:
            saved=self._session(expected);receipt=self._receipt(ident)
            previous=receipt.get('hosted_receive')
            if previous and previous.get('receiving_id'):return self.status(ident,expected)
            if previous:
                self._session(expected,previous['store'])
                original=self.cloud.receiving_request(previous['actions']['plan']['body']['request_key'],expected_session_id=expected)
                if original.get('found') is True and original.get('result',{}).get('state')!='retired_before_creation' and original.get('result',{}).get('receiving_id')!=data.get('receivingId'):
                    raise CloudError('The original request has its own authoritative result. Recover that exact record before selecting another operation.',status=409,pending=True)
                if original.get('found') not in (True,False):raise CloudError('The original planning request could not be checked. Keep its exact key.',status=502,pending=True)
            self._eligible(receipt);claim=self._claim(receipt,expected)
            value=self.cloud.receiving_get(uuid_value(data.get('receivingId')),expected_session_id=expected)
            if not isinstance(value,dict):raise CloudError('The original hosted record could not be read.',status=502)
            chosen=label_profile(value.get('client_label_profile'));kind=value.get('workflow_kind');location=(value.get('provider') or {}).get('location_id')
            if (value.get('physical_intake_id')!=claim['physical_intake_id'] or value.get('physical_manifest_hash')!=claim['manifest_hash']
                or kind not in ('accepted_trade','inventory_only') or not isinstance(location,str)):
                raise CloudError('This operation belongs to another physical intake. No local receipt was changed.',status=409)
            snapshot=local_snapshot(self.service,receipt,chosen,kind,location);targets=value.get('targets')
            if not isinstance(targets,list):raise CloudError('The original hosted targets are unavailable.',status=502)
            def signature(row):return (row.get('source_line_key'),row.get('identity_hash'),row.get('quantity'),row.get('selling_cents'))
            local_groups={};server_groups={}
            for row in snapshot['rows']:local_groups.setdefault(signature(row),[]).append(row)
            for row in targets:server_groups.setdefault(signature(row),[]).append(row)
            matches=set(local_groups)==set(server_groups) and all(len(rows)==1 and len(server_groups[key])==1 for key,rows in local_groups.items())
            if kind=='accepted_trade':
                trade=self.cloud.trade_get(value.get('accepted_trade_id'),expected_session_id=expected)
                from station_shared_trade import fingerprint as trade_fingerprint
                evidence=trade.get('accepted_receipt')
                matches=matches and claim.get('bound_trade_id')==value.get('accepted_trade_id') and isinstance(evidence,dict) and trade_fingerprint(evidence)==value.get('accepted_receipt_hash')==trade.get('receipt_hash')
                if matches:
                    accepted={line['id']:line for line in evidence['lines'] if line.get('included',True)}
                    matches=all(target.get('accepted_line_id') in accepted and signature(target)[:2]==(accepted[target['accepted_line_id']].get('source_line_key'),source_line_id(accepted[target['accepted_line_id']]['identity'])) and target['quantity']<=accepted[target['accepted_line_id']]['quantity'] for target in targets)
            elif claim.get('bound_trade_id'):matches=False
            if not matches:
                return {'receipt':receipt,'operation':value,'readOnly':True,'mappingMismatch':True,
                    'message':'Original server record only. This import has different selected identities, quantities, prices or ambiguous splits. Fresh CSV prices cannot replace the frozen plan. No local stock, SKU, financial receipt or provider request was changed.',
                    'labelReadiness':None,'hardwareAcceptanceComplete':False}
            mapping={rows[0]['local_row_id']:server_groups[key][0]['review_line_id'] for key,rows in local_groups.items()};lines=[]
            for row in snapshot['rows']:
                target=server_groups[signature(row)][0]
                line={key:row[key] for key in ('source_line_key','quantity','selling_cents','zero_price_confirmed','reviewed')};line['review_line_id']=target['review_line_id']
                if kind=='accepted_trade':line['accepted_line_id']=target['accepted_line_id']
                else:line['identity']=copy.deepcopy(row['identity'])
                lines.append(line)
            body={key:value[key] for key in ('physical_intake_id','physical_manifest_hash','source_snapshot_hash','workflow_kind')}
            body.update(location_id=location,client_label_profile=chosen,lines=lines)
            if kind=='accepted_trade':body.update(accepted_trade_id=value['accepted_trade_id'],accepted_receipt_hash=value['accepted_receipt_hash'])
            candidate=copy.deepcopy(receipt)
            candidate['hosted_receive']={'version':1,'state':'recovering','store':saved['store'],'created_at':now(),
                'label_profile':chosen,'workflow_kind':kind,'location_id':location,'local_snapshot_hash':fingerprint(snapshot),
                'plan_body':body,'actions':{},'row_map':mapping,'recovered_reference':True}
            self._verify(candidate,value,initial=True)
            with self.service.lock:
                current=self._receipt(ident)
                if current['revision']!=data.get('revision') or current.get('hosted_receive')!=previous:raise StationError('The local batch changed. Reopen the original receiving record.')
                if previous:current.setdefault('hosted_receiving_history',[]).append(copy.deepcopy(previous))
                current['hosted_receive']=candidate['hosted_receive'];self.service.store.save_receipt(current,('hosted_receiving_recovery_binding','Exact canonical source/identity/quantity/price mapping bound to the original server operation; no new key or stock call.'))
            current=self._save_plan(current,value,expected)
            return self._adopt(current,value,expected,self._readiness(expected))

    def _save_plan(self,receipt,value,expected):
        self._verify(receipt,value,initial=True)
        with self.service.lock:
            receipt=self._receipt(receipt['id']);self._assert_source(receipt)
            receipt['hosted_receive'].update(receiving_id=value['receiving_id'],server_revision=value['revision'],plan_hash=value['plan_hash'],
                provider=copy.deepcopy(value['provider']),targets=copy.deepcopy(value['targets']),state='planned',server_state=value['state'],
                catalog_evidence=catalog_evidence(value,value['targets']),
                message='The original hosted plan is saved. Review exact targets, label stock and location before authorizing any new stock.')
            self.service.store.save_receipt(receipt,('hosted_receiving_plan','Typed hosted receiving plan bound durably; no provider write authorized by this desktop action.'))
        return receipt

    def prepare(self,ident,data,expected):
        with self.cloud.lock:
            saved=self._session(expected);receipt=self._receipt(ident)
            previous=receipt.get('hosted_receive')
            if previous and previous.get('state')!='retired_before_creation':return self.status(ident,expected)
            self._eligible(receipt);claim=self._claim(receipt,expected);ready=self._readiness(expected)
            locations=self.cloud.receiving_locations(expected_session_id=expected).get('locations',[])
            if not any(row.get('id')==data.get('locationId') and row.get('status')=='ACTIVE' and row.get('currency')=='USD' for row in locations):raise StationError('Select an independently verified active USD location; it will not be selected automatically.')
            trade=self.cloud.trade_get(claim['bound_trade_id'],expected_session_id=expected) if claim.get('bound_trade_id') else None
            body,snapshot=self._plan_body(receipt,data,claim,trade);body['request_key']=uuid.uuid4().hex
            with self.service.lock:
                receipt=self._receipt(ident)
                if receipt['revision']!=data.get('revision') or receipt.get('hosted_receive')!=previous:raise StationError('The reviewed batch changed. Reopen it before creating a receiving plan.')
                if previous:receipt.setdefault('hosted_receiving_history',[]).append(copy.deepcopy(previous))
                receipt['hosted_receive']={'version':1,'state':'planning','store':saved['store'],'created_at':now(),
                    'label_profile':body['client_label_profile'],'workflow_kind':body['workflow_kind'],'location_id':body['location_id'],
                    'local_snapshot_hash':fingerprint(snapshot),'plan_body':copy.deepcopy(body),
                    'actions':{'plan':{'body':copy.deepcopy(body)}},'message':'Original receiving intent saved before its encrypted journal and cloud I/O. Recover this same plan.'}
                self.service.store.save_receipt(receipt,('hosted_receiving_intent','Original reviewed source, location, profile and request key saved before hosted I/O.'))
            receipt=self._save_plan(receipt,self._perform(receipt,'plan',expected),expected)
            current=self.cloud.receiving_get(receipt['hosted_receive']['receiving_id'],expected_session_id=expected)
            return self._adopt(receipt,current,expected,ready)

    def _receipt_proof(self,receipt,value):
        self._verify(receipt,value);proof=value.get('receipt');intent=receipt['hosted_receive']
        try:
            if value['state']!='received' or value.get('inventory_confirmed') is not True or not isinstance(proof,dict):raise ValueError()
            hashed=proof.get('receipt_hash')
            if not isinstance(hashed,str) or not HASH.fullmatch(hashed) or fingerprint({key:item for key,item in proof.items() if key!='receipt_hash'})!=hashed:raise ValueError()
            created=any(row['action']=='create_price' for row in value['targets']);catalog=catalog_evidence(value,value['targets'])
            if proof.get('receipt_schema_version')!=1 or proof.get('inventory_confirmed') is not True or proof.get('catalog_write_performed') is not created:raise ValueError()
            for key in ('receiving_id','revision','plan_hash','physical_intake_id','physical_manifest_hash','source_snapshot_hash','workflow_kind','accepted_trade_id','accepted_receipt_hash','provider','selected_quantities','excluded_quantities','client_label_profile'):
                if proof.get(key)!=value.get(key):raise ValueError()
            if created and any(proof.get(key)!=catalog[key] for key in ('catalog_proof_hash','catalog_resolutions_hash','catalog_resolutions')):raise ValueError()
            expected_targets=[dict(target,confirmed_quantity=target['quantity']) for target in final_targets(value,value['targets'])]
            if proof.get('confirmed_targets')!=expected_targets or not isinstance(proof.get('provider_proof_hash'),str) or not HASH.fullmatch(proof['provider_proof_hash']):raise ValueError()
            from datetime import datetime
            if datetime.fromisoformat(proof['received_at'].replace('Z','+00:00')).tzinfo is None:raise ValueError()
            label_readiness(value['targets'],intent['label_profile'])
            return proof
        except (ValueError,TypeError,KeyError,AttributeError):raise CloudError('The hosted receipt does not prove every selected quantity, exact SKU and original provider binding. Keep this same operation for owner recovery; stock will not be guessed.',status=502,pending=True) from None

    def _adopt(self,receipt,value,expected,readiness=None):
        self._session(expected,receipt['hosted_receive']['store']);self._verify(receipt,value)
        if value['state']=='received':
            proof=self._receipt_proof(receipt,value)
            if receipt['hosted_receive'].get('state')=='confirmed':
                if receipt['hosted_receive'].get('receipt_hash')!=proof['receipt_hash']:raise CloudError('The original received receipt hash changed. Owner recovery is required.',status=409,pending=True)
                self.cloud.receiving.finish(receipt['id'],expected);return self._view(receipt,value,readiness)
            from hosted_labels import mark
            with self.service.lock:
                receipt=self._receipt(receipt['id']);self._assert_source(receipt);by_id={target['review_line_id']:target for target in proof['confirmed_targets']}
                for card in receipt['cards']:
                    if not card.get('include',True):continue
                    target=self._local_target(receipt['hosted_receive'],by_id,card);ident=target['review_line_id']
                    card.pop('square_sku_resolution',None);card['sku']=target['target_sku'];card['operator_price']=True
                    card['hosted_sku_resolution']=mark(card,receiving_id=proof['receiving_id'],receipt_hash=proof['receipt_hash'],
                        review_line_id=ident,source_line_key=target['source_line_key'],identity_hash=target['identity_hash'],sku=target['target_sku'],
                        selling_cents=target['selling_cents'],confirmed_quantity=target['confirmed_quantity'])
                for key in ('pdf','zpl','listing','ticket'):receipt['files'].pop(key,None)
                receipt['prepared']=False;receipt['inventory_status']='received';receipt['error']=''
                receipt['hosted_label_profile']=copy.deepcopy(receipt['hosted_receive']['label_profile'])
                receipt['hosted_receive'].update(state='confirmed',server_state='received',receipt_hash=proof['receipt_hash'],
                    catalog_evidence=catalog_evidence(value,value['targets']),
                    server_receipt=copy.deepcopy(proof),message='Selected quantities and exact SKUs are confirmed in the original hosted receiving receipt. Excluded copies remain unreceived.')
                self.service.store.save_receipt(receipt,('hosted_received','Confirmed hosted inventory receipt mirrored durably; obsolete labels/files invalidated. No local token or wallet was used.'))
            self.cloud.receiving.finish(receipt['id'],expected)
        else:
            with self.service.lock:
                receipt=self._receipt(receipt['id']);self._assert_source(receipt)
                receipt['hosted_receive'].update(server_state=value['state'],state='cancelled_before_write' if value['state']=='cancelled_before_write' else 'planned' if value['state']=='ready' else 'needs_attention',
                    catalog_evidence=catalog_evidence(value,value['targets']),
                    message='Hosted plan cancelled before any provider admission.' if value['state']=='cancelled_before_write' else 'The new price variation is confirmed. Stock has not been added; explicitly authorize the selected stock for this same plan with fresh MFA.' if value['state']=='catalog_confirmed' else 'Original hosted operation saved. Check its evidence; do not create a replacement plan or use a local Square token.')
                self.service.store.save_receipt(receipt,('hosted_receiving_status','Original hosted receiving state checked; no local inventory result inferred.'))
            if value['state']=='cancelled_before_write':self.cloud.receiving.finish(receipt['id'],expected)
        return self._view(receipt,value,readiness)

    def confirmed_cards(self,receipt,cards=None):
        """Reapply only the immutable hosted receipt, never an incoming marker."""
        intent=receipt.get('hosted_receive') or {}
        if intent.get('state')!='confirmed':raise StationError('Hosted label SKUs require a confirmed receiving receipt.')
        proof=intent['server_receipt'];value={key:proof.get(key) for key in ('receiving_id','revision','plan_hash','physical_intake_id','physical_manifest_hash','source_snapshot_hash','workflow_kind','accepted_trade_id','accepted_receipt_hash','provider','selected_quantities','excluded_quantities','client_label_profile')}
        value.update(state='received',inventory_confirmed=True,receipt=proof,targets=intent['targets'],**intent['catalog_evidence']);self._receipt_proof(receipt,value)
        from hosted_labels import mark
        by_id={row['review_line_id']:row for row in proof['confirmed_targets']};result=[]
        for original in cards if cards is not None else receipt['cards']:
            card=copy.deepcopy(original)
            if card.get('include',True):
                target=self._local_target(intent,by_id,card);ident=target['review_line_id'];card['sku']=target['target_sku']
                card['hosted_sku_resolution']=mark(card,receiving_id=proof['receiving_id'],receipt_hash=proof['receipt_hash'],review_line_id=ident,
                    source_line_key=target['source_line_key'],identity_hash=target['identity_hash'],sku=target['target_sku'],selling_cents=target['selling_cents'],confirmed_quantity=target['confirmed_quantity'])
            result.append(card)
        return result

    def status(self,ident,expected):
        with self.cloud.lock:
            receipt=self._receipt(ident);intent=receipt.get('hosted_receive')
            if not intent:raise StationError('This batch has no saved hosted receiving operation.')
            self._session(expected,intent['store'])
            if intent.get('state')=='retiring':return self.retire(ident,{},expected)
            if intent.get('state')=='retired_before_creation':return {'receipt':receipt,'operation':None,'message':intent['message'],'readOnly':False}
            self._assert_source(receipt)
            if intent.get('state')=='revising':receipt=self._save_plan(receipt,self._perform(receipt,'revise',expected),expected)
            elif not intent.get('receiving_id'):receipt=self._save_plan(receipt,self._perform(receipt,'plan',expected),expected)
            value=self.cloud.receiving_get(receipt['hosted_receive']['receiving_id'],expected_session_id=expected)
            # GET is authoritative current state; status never dispatches stock.
            return self._adopt(receipt,value,expected,self._readiness(expected))

    def retire(self,ident,data,expected):
        with self.cloud.lock:
            receipt=self._receipt(ident);intent=receipt.get('hosted_receive')
            if not intent:raise StationError('There is no original planning request to retire.')
            self._session(expected,intent['store'])
            if intent.get('receiving_id'):
                raise CloudError('This intake already has a receiving operation. Recover that same operation; a request retirement cannot cancel provider work.',status=409,pending=True,receiving_id=intent['receiving_id'])
            if intent.get('state')=='retired_before_creation':return {'receipt':receipt,'operation':None,'message':intent['message'],'readOnly':False}
            if not intent.get('actions',{}).get('retire'):
                if data.get('retireUncreatedRequest') is not True:raise StationError('Explicitly ask the server to retire only this original uncreated plan request.')
                original=intent.get('actions',{}).get('plan',{}).get('body')
                if not original:raise StationError('Keep this original planning intent for owner recovery.')
                with self.service.lock:
                    receipt=self._receipt(ident);receipt['hosted_receive']['actions']['retire']={'body':{'original_plan':copy.deepcopy(original),'retire_uncreated_request':True}}
                    receipt['hosted_receive']['state']='retiring';self.service.store.save_receipt(receipt,('hosted_receiving_retire_intent','Explicit request-key retirement saved before encrypted journal; only server tombstone proof can unlock the uncreated plan.'))
            proof=self._perform(receipt,'retire',expected);original=receipt['hosted_receive']['actions']['plan']['body']
            expected_proof={'request_key':original['request_key'],'retired':True,'state':'retired_before_creation','receiving_id':None,'original_plan_sha256':fingerprint(original)}
            if (not isinstance(proof,dict) or set(proof)!=set(expected_proof)|{'retirement_basis'} or any(proof.get(key)!=value for key,value in expected_proof.items())
                or proof.get('retirement_basis') not in ('registered_original_wire','unregistered_key_no_operation')):raise CloudError('The server did not prove retirement of this exact uncreated request. Keep its original key and journal; no local lock was cleared.',status=502,pending=True)
            with self.service.lock:
                receipt=self._receipt(ident);receipt['hosted_receive'].update(state='retired_before_creation',server_retirement=copy.deepcopy(proof),
                    message='The server retired the original request before any receiving operation was created. Its key/body/proof are retained. Correct the batch, then explicitly review a new plan for the same physical intake.')
                self.service.store.save_receipt(receipt,('hosted_receiving_retired','Matching server request-key tombstone saved durably; no provider operation existed and old in-flight creation is permanently fenced.'))
            self.cloud.receiving.finish(ident,expected)
            return {'receipt':receipt,'operation':None,'readOnly':False,'message':receipt['hosted_receive']['message']}

    def revise(self,ident,data,expected):
        with self.cloud.lock:
            receipt=self._receipt(ident);old=receipt.get('hosted_receive') or {};self._session(expected,old.get('store'))
            if old.get('state')=='revising':return self.status(ident,expected)
            if old.get('recovered_reference') or old.get('state')!='cancelled_before_write':
                raise StationError('Only an explicitly cancelled, unadmitted original plan can be corrected on its originating computer. Uncertain provider actions cannot be replanned.')
            current=self.cloud.receiving_get(old['receiving_id'],expected_session_id=expected)
            if (current.get('state')!='cancelled_before_write' or current.get('receiving_id')!=old['receiving_id']
                or current.get('plan_hash')!=old['plan_hash'] or current.get('revision')!=old['server_revision']
                or current.get('targets')!=old['targets'] or current.get('provider')!=old['provider']):
                raise CloudError('The original cancellation is no longer current. Recover that same operation; no replacement plan was created.',status=409,pending=True)
            self._eligible(receipt);claim=self._claim(receipt,expected)
            locations=self.cloud.receiving_locations(expected_session_id=expected).get('locations',[])
            if not any(row.get('id')==data.get('locationId') and row.get('status')=='ACTIVE' and row.get('currency')=='USD' for row in locations):raise StationError('Select a verified active USD location.')
            trade=self.cloud.trade_get(claim['bound_trade_id'],expected_session_id=expected) if claim.get('bound_trade_id') else None
            body,snapshot=self._plan_body(receipt,data,claim,trade);body.update(request_key=uuid.uuid4().hex,revision=current['revision'])
            with self.service.lock:
                receipt=self._receipt(ident)
                if receipt['revision']!=data.get('revision') or receipt['hosted_receive']!=old:raise StationError('The cancelled batch changed. Reopen it before correcting the original plan.')
                receipt.setdefault('hosted_receiving_history',[]).append(copy.deepcopy(old))
                receipt['hosted_receive']={'version':1,'state':'revising','store':old['store'],'created_at':now(),'receiving_id':old['receiving_id'],
                    'label_profile':copy.deepcopy(body['client_label_profile']),'workflow_kind':body['workflow_kind'],'location_id':body['location_id'],
                    'local_snapshot_hash':fingerprint(snapshot),'plan_body':copy.deepcopy(body),'actions':{'revise':{'body':copy.deepcopy(body)}}}
                self.service.store.save_receipt(receipt,('hosted_receiving_revision_intent','Explicit correction of the same cancelled pre-admission operation saved before encrypted journal and I/O.'))
            receipt=self._save_plan(receipt,self._perform(receipt,'revise',expected),expected)
            return self._adopt(receipt,self.cloud.receiving_get(old['receiving_id'],expected_session_id=expected),expected,self._readiness(expected))

    def act(self,ident,data,expected,kind):
        if kind not in ('commit','recover','authorize','cancel'):raise StationError('Use a supported saved receiving action.')
        with self.cloud.lock:
            receipt=self._receipt(ident);intent=receipt.get('hosted_receive')
            if not intent or not intent.get('receiving_id'):raise StationError('Recover and review the saved server plan first.')
            self._session(expected,intent['store']);self._assert_source(receipt)
            current=self.cloud.receiving_get(intent['receiving_id'],expected_session_id=expected);self._verify(receipt,current)
            if current['state']=='received':return self._adopt(receipt,current,expected)
            if intent.get('recovered_reference') and kind!='recover' and not (kind=='authorize' and current.get('catalog_confirmed') is True):
                raise CloudError('This computer can recover the original operation. A confirmed catalog may proceed only through explicit same-plan stock authorization with fresh MFA; no replacement plan or blind stock resend is allowed.',status=409,pending=True)
            ready=self._readiness(expected)
            creating=any(target['action']=='create_price' for target in current['targets'])
            if kind=='commit':
                if data.get('confirmed') is not True or data.get('excludedNotReceivedAck') is not True or data.get('planHash')!=intent['plan_hash']:
                    raise StationError('Review this exact server plan, its location and selected/excluded quantities before confirming stock.')
                if not ready['write_enabled']:raise CloudError('Hosted receiving writes are disabled by the server or store. The saved plan is read-only; no local-token fallback is available.',status=409,code='RECEIVING_DISABLED')
                if creating and (not ready['new_price_supported'] or ready.get('new_price_write_enabled') is not True):raise CloudError('New-price receiving is disabled for this store. Keep the same reviewed plan; no catalog or stock step was admitted.',status=409,code='RECEIVING_DISABLED')
                label_readiness(current['targets'],intent['label_profile'])
                if current['state']!='ready':raise CloudError('This operation has already been admitted or needs review. Use read-only evidence recovery; do not send another stock call.',status=409,pending=True)
            if kind=='authorize' and creating and current.get('catalog_confirmed') is True and current['inventory_confirmed'] is False:
                if data.get('stockContinuationConfirmed') is not True:raise StationError('Review the confirmed catalog variation and explicitly authorize stock for this same original plan.')
                if not ready['write_enabled'] or ready.get('new_price_write_enabled') is not True:raise CloudError('The store has disabled new-price stock admission. The confirmed catalog evidence remains saved; no stock was submitted.',status=409,code='RECEIVING_DISABLED')
                label_readiness(current['targets'],intent['label_profile'])
            if kind=='cancel' and current['state']!='ready':raise CloudError('An admitted or uncertain operation cannot be cancelled locally. Recover its original provider evidence.',status=409,pending=True)
            with self.service.lock:
                receipt=self._receipt(ident);self._assert_source(receipt);intent=receipt['hosted_receive']
                action=intent['actions'].get(kind)
                # An explicitly repeated read-only query may use a new key only
                # after the prior response was durably acknowledged locally.
                if action and action.get('completed') and kind in ('recover','authorize'):
                    intent.setdefault('action_history',[]).append(copy.deepcopy(action));action=None
                if not action:
                    body={'request_key':uuid.uuid4().hex,'revision':intent['server_revision'],'plan_hash':intent['plan_hash']}
                    if kind=='commit':body.update(confirmed=True,excluded_not_received_ack=True)
                    elif kind=='authorize':body['resume_same_operation']=True
                    elif kind=='cancel':
                        reason=data.get('reason','')
                        if not isinstance(reason,str) or not 10<=len(reason.strip())<=200:raise StationError('Give a clear 10–200 character reason for cancelling before any write.')
                        body['reason']=reason.strip()
                    action={'body':body};intent['actions'][kind]=action
                intent['state']='committing' if kind=='commit' else 'checking';self.service.store.save_receipt(receipt,('hosted_receiving_'+kind+'_intent','Exact '+kind+' intent saved before encrypted journal and hosted I/O.'))
            result=self._perform(receipt,kind,expected)
            current=self.cloud.receiving_get(intent['receiving_id'],expected_session_id=expected)
            adopted=self._adopt(self._receipt(ident),current,expected,ready)
            if kind in ('recover','authorize') and adopted['receipt']['hosted_receive']['state'] not in ('confirmed','cancelled_before_write'):
                with self.service.lock:
                    receipt=self._receipt(ident);receipt['hosted_receive']['actions'][kind]['completed']=True;self.service.store.save_receipt(receipt)
                adopted['receipt']=receipt
                self.cloud.receiving.finish_key(action['body']['request_key'],expected)
            return adopted
