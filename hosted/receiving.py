"""Durable hosted receiving. Global and per-tenant writes default OFF.

First vertical slice reuses approved, same-price tracked variations. It never
reprices/deletes catalog objects or calls the desktop Square vault. Unknown
provider outcomes retain the exact encrypted step and seller occupancy.
"""
from datetime import datetime, timezone
import hashlib
import re
import uuid
from psycopg.types.json import Jsonb
from intake_manifest import source_lines, source_line_id, normalize_identity, ManifestError
from .database import audit
from .intake import current_authority
from .receiving_plan import (API_VERSION, READ_SCOPES, WRITE_SCOPES, ReceivingError, normalize_plan,
    canonical, fingerprint, uuid_text, text_hash, integer, keys, bind_targets,
    effective_variation, inventory_body, history_proof, public_response_bytes)
from .security import ServiceError, provider_id
from .settlement import evidence_hash


def _key(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9_-]{1,150}',value):raise ServiceError('Use a stable receiving request key.')
    return value


def _reason(value):
    if not isinstance(value,str) or not 10<=len(value.strip())<=200 or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ServiceError('A 10 to 200 character review reason is required.')
    return value.strip()


def principal(access, location):
    return {key:access.proof[key] for key in ('merchant_id','environment','application_id')}|{'location_id':location}


class Receiving:
    def __init__(self, database, provider, cipher, *, writes_enabled=False):
        self.database,self.provider,self.cipher=database,provider,cipher
        self.writes_enabled=writes_enabled is True
        from .receiving_templates import PriceTemplates
        self.templates=PriceTemplates(database,provider,self)

    def _old(self, db, tenant, key, action, intent):
        key=_key(key);hashed=fingerprint({'action':action,'intent':intent})
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(tenant)+':receiving-request:'+key,))
        row=db.execute('''SELECT q.*,COALESCE(o.result,q.result) AS result FROM ss_receiving_requests q LEFT JOIN ss_receiving_request_outcomes o
                          ON o.tenant_id=q.tenant_id AND o.request_key=q.request_key WHERE q.tenant_id=%s AND q.request_key=%s''',(tenant,key)).fetchone()
        if row and action=='plan' and row['action']=='plan' and row['result'].get('retired') is True:
            raise ReceivingError('This original plan request was explicitly retired before creation. Preserve its retirement proof and start a reviewed correction with a new key.',code='PLAN_REQUEST_RETIRED')
        if row and (row['intent_hash']!=hashed or row['action']!=action):raise ReceivingError('This receiving request key has different immutable terms.',code='PLAN_CHANGED')
        return hashed,row['result'] if row else None

    @staticmethod
    def _finish(db, tenant, key, action, hashed, result, operation=None):
        db.execute('INSERT INTO ss_receiving_requests(tenant_id,request_key,action,intent_hash,operation_id,result) VALUES (%s,%s,%s,%s,%s,%s)',
                   (tenant,key,action,hashed,operation,Jsonb(result)))
        return result

    @staticmethod
    def _row(db, tenant, ident, *, lock=False):
        row=db.execute('SELECT * FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s'+(' FOR UPDATE' if lock else ''),(tenant,uuid_text(ident,'Receiving ID'))).fetchone()
        if not row:raise ServiceError('Receiving operation not found.',404)
        if fingerprint(row['plan'])!=row['plan_hash']:raise ReceivingError('Stored receiving plan evidence is inconsistent.')
        return row

    @staticmethod
    def _view(row):
        plan=row['plan'];result={key:plan.get(key) for key in ('physical_intake_id','physical_manifest_hash','source_snapshot_hash','client_label_profile','workflow_kind',
            'accepted_trade_id','accepted_receipt_hash','provider','selected_quantities','excluded_quantities')}
        fields=('review_line_id','source_line_key','accepted_line_id','identity_hash','binding_id','binding_version','binding_evidence_hash',
                'source_sku','target_sku','variation_id','parent_id','selling_cents','currency','quantity','action','binding_role',
                'template_id','template_version','template_evidence_hash')
        result.update(receiving_id=str(row['id']),revision=row['revision'],state=row['state'],plan_hash=row['plan_hash'],
            targets=[{key:target[key] for key in fields if key in target} for target in plan['targets']],
            inventory_confirmed=bool(row['inventory_proof']),receipt=row['receipt'],blocked_reasons=[],requires_fresh_mfa=True)
        result['catalog_confirmed']=bool(row.get('catalog_proof'))
        result['catalog_resolutions']=row.get('catalog_resolutions') or []
        result['catalog_proof']=row.get('catalog_proof')
        result['catalog_proof_hash']=fingerprint(row['catalog_proof']) if row.get('catalog_proof') else None
        result['catalog_resolutions_hash']=fingerprint(result['catalog_resolutions']) if row.get('catalog_proof') else None
        if row['state'] in ('outcome_unknown','needs_review'):result['blocked_reasons']=['PROVIDER_OUTCOME_UNKNOWN' if row['state']=='outcome_unknown' else 'RECEIVING_REVIEW_REQUIRED']
        return result

    @staticmethod
    def _receipt(row,targets,proof,*,received_at=None):
        """One public receipt builder, shared by admission budgeting and outcome."""
        receipt={key:row['plan'].get(key) for key in ('physical_intake_id','physical_manifest_hash','source_snapshot_hash','client_label_profile','workflow_kind','accepted_trade_id','accepted_receipt_hash','provider','selected_quantities','excluded_quantities')}
        receipt.update(receipt_schema_version=1,receiving_id=str(row['id']),revision=row['revision'],plan_hash=row['plan_hash'],
            confirmed_targets=[dict(target,confirmed_quantity=target['quantity']) for target in targets],inventory_confirmed=True,
            catalog_write_performed=bool(row.get('catalog_proof')),received_at=received_at or datetime.now(timezone.utc).isoformat(),provider_proof_hash=fingerprint(proof))
        if row.get('catalog_proof'):
            receipt.update(catalog_proof_hash=fingerprint(row['catalog_proof']),catalog_resolutions_hash=fingerprint(row['catalog_resolutions']),catalog_resolutions=row['catalog_resolutions'])
        receipt['receipt_hash']=fingerprint(receipt)
        return receipt

    def _projection_budget(self,plan,ident,revision=0):
        """Bound the largest terminal DTO before persisting/admitting a plan.

        A catalog result may introduce an opaque provider ID up to 255 Unicode
        characters. Reserve its largest UTF-8 representation, new binding facts,
        coalesced review aliases, and both repeated receipt/resolution copies.
        This contains no provider body or customer identity/free text.
        """
        proof={'kind':'batch_create_ack','request_hash':'f'*64,'response_hash':'f'*64}
        row={'id':str(ident),'revision':max(revision,2**63-1),'state':'received','plan':plan,'plan_hash':fingerprint(plan),
             'inventory_proof':proof,'catalog_proof':None,'catalog_resolutions':[],'receipt':None}
        if plan.get('catalog_required'):
            row['catalog_proof']={'kind':'catalog_state_match','request_hash':'f'*64,'evidence_hash':'f'*64}
            for addition in plan['catalog_additions']:
                value={key:addition[key] for key in ('review_line_id','identity_hash','parent_id','target_sku','selling_cents','template_id','template_version')}
                value.update(variation_id='\U0010ffff'*255,currency='USD',binding_id='f'*36,binding_version=1,binding_evidence_hash='f'*64,
                             review_line_ids=sorted(review for review,representative in plan['catalog_aliases'].items() if representative==addition['review_line_id']))
                row['catalog_resolutions'].append(value)
        row['receipt']=self._receipt(row,self._final_targets(row),proof,received_at='9999-12-31T23:59:59.999999+00:00')
        # Request-key recovery wraps this same full DTO, adding a small envelope.
        wire=public_response_bytes({'found':True,'result':self._view(row)})
        return len(wire)

    def get(self, actor, ident):
        with self.database.transaction() as db:
            current_authority(db,actor)
            return self._view(self._row(db,actor['tenant_id'],ident))

    def request_status(self, actor, key):
        key=_key(key)
        with self.database.transaction() as db:
            current_authority(db,actor)
            row=db.execute('''SELECT COALESCE(o.result,q.result) AS result FROM ss_receiving_requests q LEFT JOIN ss_receiving_request_outcomes o
                              ON o.tenant_id=q.tenant_id AND o.request_key=q.request_key WHERE q.tenant_id=%s AND q.request_key=%s''',(actor['tenant_id'],key)).fetchone()
            return {'found':bool(row),'result':row['result'] if row else None}

    def readiness(self, actor):
        with self.database.transaction() as db:
            current_authority(db,actor)
            row=db.execute('SELECT enabled FROM ss_receiving_settings WHERE tenant_id=%s',(actor['tenant_id'],)).fetchone()
        tenant=bool(row and row['enabled'])
        return {'protocol_version':1,'write_enabled':self.writes_enabled and tenant,'server_write_enabled':self.writes_enabled,
                'tenant_write_enabled':tenant,'same_price_existing_supported':True,'new_price_supported':False,'new_parent_enabled':False,
                'fresh_mfa_required':True,'max_targets':100,'location_selection_required':True,'grants_verified':False,
                'required_scopes':list(WRITE_SCOPES)}

    def locations(self, actor):
        with self.database.transaction() as db:current_authority(db,actor)
        access=self.provider.access(actor['tenant_id'])
        result=self.provider.locations(access)
        with self.database.transaction() as db:
            current_authority(db,actor);self.provider.assert_current(db,actor['tenant_id'],access)
        return {'locations':result}

    def binding_preview(self, actor, data):
        keys(data,('variation_id','location_id','identity'))
        with self.database.transaction() as db:current_authority(db,actor,allowed_roles=('owner',))
        try:identity=normalize_identity(data.get('identity'))
        except ManifestError:raise ServiceError('A complete reviewed identity is required.') from None
        variation_id=provider_id(data.get('variation_id'),'Variation ID');location=provider_id(data.get('location_id'),'Location ID')
        access=self.provider.access(actor['tenant_id']);self.provider.location(access,location)
        objects=self.provider.catalog(access,[variation_id]);variation=objects[variation_id]
        parent_id=(variation.get('item_variation_data') or {}).get('item_id')
        if parent_id not in objects:objects.update(self.provider.catalog(access,[provider_id(parent_id,'Parent ID')]))
        parent=objects[parent_id];effective=effective_variation(variation,parent,location)
        self.provider.assert_skus(access,[effective])
        facts={'identity':identity,'identity_hash':source_line_id(identity),'provider':principal(access,location),
               'snapshot_hash':fingerprint({'variation':variation,'parent':parent}),'location_id':location}|effective
        ident=uuid.uuid4();hashed=fingerprint(facts)
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',));self.provider.assert_current(db,actor['tenant_id'],access)
            db.execute("INSERT INTO ss_receiving_binding_previews(tenant_id,id,facts,preview_hash,created_by,expires_at) VALUES (%s,%s,%s,%s,%s,clock_timestamp()+interval '15 minutes')",
                       (actor['tenant_id'],ident,Jsonb(facts),hashed,actor['staff_id']))
        return {'preview_id':str(ident),'preview_hash':hashed,'expires_in':900,'facts':facts,'approval_required':True}

    def approve_binding(self, actor, preview_id, data):
        keys(data,('request_key','preview_hash','identity_confirmed','reason'))
        if data.get('identity_confirmed') is not True:raise ServiceError('Explicitly confirm this physical identity maps to the selected Square variation.')
        key=_key(data.get('request_key'));reason=_reason(data.get('reason'));ident=uuid_text(preview_id,'Preview ID')
        intent={'preview_id':ident,'preview_hash':text_hash(data.get('preview_hash')),'identity_confirmed':True,'reason':reason}
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',))
            _,old=self._old(db,actor['tenant_id'],key,'binding_approve',intent)
            if old:return old
            preview=db.execute('SELECT * FROM ss_receiving_binding_previews WHERE tenant_id=%s AND id=%s AND expires_at>clock_timestamp()', (actor['tenant_id'],ident)).fetchone()
            if not preview or preview['preview_hash']!=intent['preview_hash']:raise ReceivingError('The mapping preview changed or expired. Review it again.')
        facts=preview['facts'];access=self.provider.access(actor['tenant_id'])
        objects=self.provider.catalog(access,[facts['variation_id'],facts['parent_id']]);self.provider.location(access,facts['location_id'])
        if principal(access,facts['location_id'])!=facts['provider'] or fingerprint({'variation':objects[facts['variation_id']],'parent':objects[facts['parent_id']]})!=facts['snapshot_hash']:
            raise ReceivingError('The provider catalog changed. Review a fresh mapping preview.',code='CATALOG_VERSION_CHANGED')
        self.provider.assert_skus(access,[facts])
        binding=uuid.uuid4()
        with self.database.transaction() as db:
            current_authority(db,actor,fresh_mfa=True,allowed_roles=('owner',));self.provider.assert_current(db,actor['tenant_id'],access)
            hashed,old=self._old(db,actor['tenant_id'],key,'binding_approve',intent)
            if old:return old
            if not db.execute('SELECT 1 FROM ss_receiving_binding_previews WHERE tenant_id=%s AND id=%s AND expires_at>clock_timestamp() FOR SHARE',(actor['tenant_id'],ident)).fetchone():
                raise ReceivingError('The mapping preview expired.')
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(actor['tenant_id'])+':receiving-binding:'+facts['identity_hash']+':'+facts['location_id']+':'+str(facts['selling_cents']),))
            existing=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND identity_hash=%s AND location_id=%s AND selling_cents=%s AND revoked_at IS NULL',
                                (actor['tenant_id'],facts['identity_hash'],facts['location_id'],facts['selling_cents'])).fetchone()
            if existing:
                if any(existing[field]!=facts[field] for field in ('variation_id','parent_id','sku','provider','identity_anchor_hash')):
                    raise ReceivingError('Another approved mapping conflicts with this identity/price. Revoke/review explicitly.')
                return self._finish(db,actor['tenant_id'],key,'binding_approve',hashed,{'binding_id':str(existing['id']),'version':existing['version'],'approved':True,'provider_write_performed':False})
            db.execute('''INSERT INTO ss_receiving_bindings(tenant_id,id,identity,identity_hash,provider,variation_id,parent_id,sku,location_id,selling_cents,identity_anchor_hash,evidence_hash,approved_by,approval_reason)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (actor['tenant_id'],binding,Jsonb(facts['identity']),facts['identity_hash'],Jsonb(facts['provider']),facts['variation_id'],facts['parent_id'],facts['sku'],facts['location_id'],facts['selling_cents'],facts['identity_anchor_hash'],preview['preview_hash'],actor['staff_id'],reason))
            audit(db,actor['tenant_id'],actor['staff_id'],'receiving-binding-approved',binding,{'identity_hash':facts['identity_hash']})
            return self._finish(db,actor['tenant_id'],key,'binding_approve',hashed,{'binding_id':str(binding),'version':1,'approved':True,'provider_write_performed':False})

    def revoke_binding(self, actor, binding_id, data):
        keys(data,('request_key','version','reason'));key=_key(data.get('request_key'));version=integer(data.get('version'),'Binding version',1)
        ident=uuid_text(binding_id,'Binding ID');reason=_reason(data.get('reason'))
        intent={'binding_id':ident,'version':version,'reason':reason}
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',));hashed,old=self._old(db,actor['tenant_id'],key,'binding_revoke',intent)
            if old:return old
            current_authority(db,actor,fresh_mfa=True,allowed_roles=('owner',))
            from .receiving_templates import authority_lane,revoke_delegated_authority
            authority_lane(db,actor['tenant_id'])
            row=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s FOR UPDATE',(actor['tenant_id'],ident)).fetchone()
            if not row:raise ServiceError('Catalog binding not found.',404)
            if row['version']!=version:raise ReceivingError('The catalog binding changed.')
            if row['revoked_at'] is None:
                revoke_delegated_authority(db,actor['tenant_id'],actor['staff_id'],reason,binding_id=row['id'])
                audit(db,actor['tenant_id'],actor['staff_id'],'receiving-binding-revoked',ident)
            return self._finish(db,actor['tenant_id'],key,'binding_revoke',hashed,{'binding_id':ident,'version':version,'revoked':True})

    def _source(self, db, tenant, plan, *, lock=False):
        claim=db.execute('SELECT * FROM ss_intake_claims WHERE tenant_id=%s AND id=%s'+(' FOR UPDATE' if lock else ' FOR SHARE'),(tenant,plan['physical_intake_id'])).fetchone()
        if not claim or claim['manifest_hash']!=plan['physical_manifest_hash']:raise ReceivingError('The central physical claim binding is unavailable or changed.')
        if plan['workflow_kind']=='accepted_trade':
            if claim['state']!='consumed' or str(claim['bound_trade_id'])!=plan['accepted_trade_id']:raise ReceivingError('Receiving must use this claim\'s exact accepted trade.')
            trade=db.execute('SELECT * FROM ss_trade_receipts WHERE tenant_id=%s AND id=%s',(tenant,plan['accepted_trade_id'])).fetchone()
            if (not trade or not trade['state'].startswith('accepted_') or trade['receipt_hash']!=plan['accepted_receipt_hash'] or
                    evidence_hash(trade['accepted_receipt'])!=trade['receipt_hash']):raise ReceivingError('The immutable accepted receipt evidence changed.')
            lines=trade['accepted_receipt']['lines'];budget={}
            for line in lines:
                key=line.get('source_line_key')
                if not key:raise ReceivingError('A legacy trade without original source bindings needs migration review.',code='UPGRADE_REIMPORT_REQUIRED')
                budget[key]=budget.get(key,0)+line['quantity']
        else:
            if claim['state']!='open':raise ReceivingError('An accepted trade claim cannot be received as inventory-only.')
            lines=[];budget={row['source_line_key']:row['quantity'] for row in source_lines(claim['source_manifest'])}
        return claim,budget,lines

    def _prepare(self, actor, plan):
        tenant=actor['tenant_id']
        with self.database.transaction() as db:
            current_authority(db,actor);_,budget,lines=self._source(db,tenant,plan)
            accepted={str(row['id']):row for row in lines};needed=set()
            for row in plan['lines']:
                if plan['workflow_kind']=='accepted_trade':
                    original=accepted.get(row['accepted_line_id'])
                    if not original:raise ReceivingError('The accepted line is not in the immutable receipt.')
                    identity=original['identity']
                else:identity=row['identity']
                needed.add((source_line_id(identity),row['selling_cents']))
            if len(needed)>100:raise ReceivingError('This version supports at most 100 identity/price targets.')
            bindings=db.execute('''SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND location_id=%s AND revoked_at IS NULL
                                   AND identity_hash=ANY(%s::text[]) AND selling_cents=ANY(%s::bigint[]) LIMIT 201''',
                                (tenant,plan['location_id'],list({value[0] for value in needed}),list({value[1] for value in needed}))).fetchall()
            bindings=[row for row in bindings if (row['identity_hash'],row['selling_cents']) in needed]
        if not bindings:raise ReceivingError('The owner must approve exact catalog identity/price mappings before receiving.',code='CATALOG_MAPPING_REVIEW_REQUIRED')
        if len(bindings)>100:raise ReceivingError('Use a bounded mapping query before receiving.')
        access=self.provider.access(tenant);self.provider.location(access,plan['location_id'])
        provider=principal(access,plan['location_id'])
        bindings=[row for row in bindings if row['provider']==provider]
        if not bindings:raise ReceivingError('Catalog mappings belong to a different Square connection.')
        objects=self.provider.catalog(access,[value for row in bindings for value in (row['variation_id'],row['parent_id'])])
        targets,selected,excluded=bind_targets(plan,budget,lines,bindings,objects)
        self.provider.assert_skus(access,targets)
        return dict(plan,provider=provider,targets=targets,selected_quantities=selected,excluded_quantities=excluded),access

    def create_plan(self, actor, data):
        key=_key(data.get('request_key'));tenant=actor['tenant_id']
        try:plan=normalize_plan(data)
        except ServiceError:
            with self.database.transaction() as db:
                current_authority(db,actor)
                db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(tenant)+':receiving-request:'+key,))
                retained=db.execute("SELECT result FROM ss_receiving_requests WHERE tenant_id=%s AND request_key=%s AND action='plan'",(tenant,key)).fetchone()
                if retained and retained['result'].get('retired') is True:raise ReceivingError('This original plan request was retired before creation.',code='PLAN_REQUEST_RETIRED') from None
            raise
        with self.database.transaction() as db:
            current_authority(db,actor);_,old=self._old(db,tenant,key,'plan',plan)
            if old:return old
            pending=db.execute('SELECT * FROM ss_receiving_plan_intents WHERE tenant_id=%s AND request_key=%s',(tenant,key)).fetchone()
            if pending:
                if pending['wire_hash']!=fingerprint(data) or pending['intent_hash']!=fingerprint(plan):raise ReceivingError('The original plan request wire body changed.',code='PLAN_CHANGED')
            else:
                db.execute('''INSERT INTO ss_receiving_plan_intents(tenant_id,request_key,intent_hash,wire_hash,claim_id,created_by,workflow_kind,accepted_trade_id,accepted_receipt_hash)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                           (tenant,key,fingerprint(plan),fingerprint(data),plan['physical_intake_id'],actor['staff_id'],plan['workflow_kind'],plan.get('accepted_trade_id'),plan.get('accepted_receipt_hash')))
        prepared,access=self._prepare(actor,plan);ident=uuid.uuid4();hashed_plan=fingerprint(prepared)
        self._projection_budget(prepared,ident)
        with self.database.transaction() as db:
            current_authority(db,actor);self.provider.assert_current(db,tenant,access)
            hashed,old=self._old(db,tenant,key,'plan',plan)
            if old:return old
            self._source(db,tenant,plan,lock=True)
            existing=db.execute('SELECT id FROM ss_receiving_operations WHERE tenant_id=%s AND claim_id=%s',(tenant,plan['physical_intake_id'])).fetchone()
            if existing:raise ReceivingError('This physical claim already has a receiving operation. Recover it instead.',code='CLAIM_WORKFLOW_CONFLICT',receiving_id=existing['id'])
            db.execute('''INSERT INTO ss_receiving_operations(tenant_id,id,claim_id,workflow_kind,plan_hash,plan,created_by,updated_by)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',(tenant,ident,plan['physical_intake_id'],plan['workflow_kind'],hashed_plan,Jsonb(prepared),actor['staff_id'],actor['staff_id']))
            db.execute('INSERT INTO ss_receiving_plan_revisions(tenant_id,operation_id,revision,plan_hash,plan,created_by) VALUES (%s,%s,0,%s,%s,%s)',(tenant,ident,hashed_plan,Jsonb(prepared),actor['staff_id']))
            row=self._row(db,tenant,ident)
            audit(db,tenant,actor['staff_id'],'receiving-plan-created',ident,{'plan_hash':hashed_plan})
            return self._finish(db,tenant,key,'plan',hashed,self._view(row),ident)

    def retire_plan_request(self,actor,request_key,data):
        keys(data,('original_plan','retire_uncreated_request'))
        key=_key(request_key);original=data.get('original_plan')
        if data.get('retire_uncreated_request') is not True or not isinstance(original,dict) or original.get('request_key')!=key:
            raise ServiceError('Confirm retirement of the exact original uncreated plan request.')
        wire=fingerprint(original);tenant=actor['tenant_id']
        try:normalized=normalize_plan(original)
        except ServiceError:normalized={'invalid_original_plan_wire_sha256':wire}
        try:claim_id=uuid_text(original.get('physical_intake_id'),'Physical intake ID')
        except ServiceError:claim_id=None
        with self.database.transaction() as db:
            current_authority(db,actor)
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(tenant)+':receiving-request:'+key,))
            pending=db.execute('SELECT * FROM ss_receiving_plan_intents WHERE tenant_id=%s AND request_key=%s',(tenant,key)).fetchone()
            if pending and pending['wire_hash']!=wire:raise ReceivingError('Retirement must match the registered original wire body.',code='PLAN_CHANGED')
            existing=db.execute('SELECT * FROM ss_receiving_requests WHERE tenant_id=%s AND request_key=%s',(tenant,key)).fetchone()
            if existing:
                if existing['action']!='plan':raise ReceivingError('This key belongs to another receiving action.',code='PLAN_CHANGED')
                if existing['result'].get('retired') is True:
                    if existing['result'].get('original_plan_sha256')!=wire:raise ReceivingError('The original retirement request body changed.',code='PLAN_CHANGED')
                    return existing['result']
                if existing['operation_id']:raise ReceivingError('This original request already created an operation. Recover it.',code='CLAIM_WORKFLOW_CONFLICT',receiving_id=existing['operation_id'])
                raise ReceivingError('This request has conflicting retained evidence.',code='PLAN_CHANGED')
            if pending:claim_id=str(pending['claim_id']) if pending['claim_id'] else None
            if claim_id:
                db.execute('SELECT id FROM ss_intake_claims WHERE tenant_id=%s AND id=%s FOR UPDATE',(tenant,claim_id)).fetchone()
                operation=db.execute('SELECT id FROM ss_receiving_operations WHERE tenant_id=%s AND claim_id=%s',(tenant,claim_id)).fetchone()
                if operation:raise ReceivingError('This physical claim already has a receiving operation. Recover it rather than retiring the local intent.',code='CLAIM_WORKFLOW_CONFLICT',receiving_id=operation['id'])
            result={'request_key':key,'retired':True,'state':'retired_before_creation','receiving_id':None,'original_plan_sha256':wire,
                    'retirement_basis':'registered_original_wire' if pending else 'unregistered_key_no_operation'}
            hashed=fingerprint({'action':'plan','intent':normalized})
            self._finish(db,tenant,key,'plan',hashed,result)
            audit(db,tenant,actor['staff_id'],'receiving-plan-request-retired',key,{'original_plan_sha256':wire})
            return result

    @staticmethod
    def _terms(data, allowed=()):
        keys(data,('request_key','revision','plan_hash')+tuple(allowed))
        return _key(data.get('request_key')),integer(data.get('revision'),'Plan revision',0,2**63-1),text_hash(data.get('plan_hash'),'Plan hash')

    @staticmethod
    def _match(row, revision, hashed):
        if row['revision']!=revision or row['plan_hash']!=hashed:raise ReceivingError('The receiving plan changed. Refresh it before continuing.',code='PLAN_CHANGED',receiving_id=row['id'])

    def _seal(self, tenant, operation, body, *, ordinal=1, endpoint='inventory_batch'):
        if self.cipher is None:raise ServiceError('Server encryption is required for receiving.',503)
        if (ordinal,endpoint) not in ((0,'catalog_batch'),(1,'inventory_batch')):raise ServiceError('Unsupported receiving step.')
        context={'domain':'stock-sleeve-receiving-step-v1','tenant':str(tenant),'operation':str(operation),'ordinal':ordinal,'endpoint':endpoint,'api_version':API_VERSION}
        return self.cipher.encrypt(canonical({'context':context,'body':body})).decode()

    def _open(self, tenant, operation, step):
        try:
            value=__import__('json').loads(self.cipher.decrypt(step['encrypted_body'].encode()))
            ordinal=step.get('ordinal',1);endpoint=step['endpoint']
            if (ordinal,endpoint) not in ((0,'catalog_batch'),(1,'inventory_batch')):raise ValueError()
            expected={'domain':'stock-sleeve-receiving-step-v1','tenant':str(tenant),'operation':str(operation),'ordinal':ordinal,'endpoint':endpoint,'api_version':API_VERSION}
            if value['context']!=expected or step['api_version']!=API_VERSION or fingerprint(value['body'])!=step['body_hash'] or value['body']['idempotency_key']!=str(step['provider_key']):raise ValueError()
            return value['body']
        except Exception:raise ReceivingError('The encrypted receiving journal is unavailable or inconsistent. Do not create a replacement operation.') from None

    def _current_targets(self, db, tenant, plan, objects):
        claim,budget,lines=self._source(db,tenant,plan,lock=True)
        rows=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=ANY(%s::uuid[]) AND revoked_at IS NULL FOR SHARE',
                        (tenant,list({row['binding_id'] for row in plan['targets']}))).fetchall()
        targets,selected,excluded=bind_targets(plan,budget,lines,rows,objects)
        if targets!=plan['targets'] or selected!=plan['selected_quantities'] or excluded!=plan['excluded_quantities']:
            raise ReceivingError('The exact receiving targets changed. Review before any new admission.',code='CATALOG_VERSION_CHANGED')
        if any(row['provider']!=plan['provider'] for row in rows):raise ReceivingError('The catalog provider binding changed.')
        return claim

    def commit(self, actor, ident, data):
        key,revision,hashed_plan=self._terms(data,('confirmed','excluded_not_received_ack'))
        if data.get('confirmed') is not True or data.get('excluded_not_received_ack') is not True:raise ServiceError('Explicitly confirm the exact receiving plan and excluded quantities.')
        ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan,'confirmed':True,'excluded_not_received_ack':True}
        with self.database.transaction() as db:
            current_authority(db,actor);_,old=self._old(db,tenant,key,'commit',intent)
            if old:return old
            row=self._row(db,tenant,ident);self._match(row,revision,hashed_plan)
            if row['first_admitted_at'] is not None:
                hashed,_=self._old(db,tenant,key,'commit',intent)
                return self._finish(db,tenant,key,'commit',hashed,self._view(row),ident)
            settings=db.execute('SELECT enabled FROM ss_receiving_settings WHERE tenant_id=%s',(tenant,)).fetchone()
            if not self.writes_enabled or not settings or not settings['enabled']:
                raise ReceivingError('Hosted receiving writes are disabled for this server or store.',403,code='RECEIVING_DISABLED')
            if row['state']!='ready':raise ReceivingError('Only a ready reviewed plan can be committed.')
            self._projection_budget(row['plan'],ident,row['revision'])
            current_authority(db,actor,fresh_mfa=True)
        plan=row['plan'];access=self.provider.access(tenant,write=True)
        if principal(access,plan['location_id'])!=plan['provider']:raise ReceivingError('This operation belongs to a different Square principal.')
        self.provider.location(access,plan['location_id'])
        objects=self.provider.catalog(access,[value for target in plan['targets'] for value in (target['variation_id'],target['parent_id'])])
        self.provider.assert_skus(access,plan['targets'])
        with self.database.transaction() as db:
            current_authority(db,actor,fresh_mfa=True);self.provider.assert_current(db,tenant,access)
            hashed,old=self._old(db,tenant,key,'commit',intent)
            if old:return old
            # Global lock order: staff/security/session -> claim -> operation -> seller.
            self._source(db,tenant,plan,lock=True)
            row=self._row(db,tenant,ident,lock=True);self._match(row,revision,hashed_plan)
            if row['first_admitted_at'] is not None:return self._finish(db,tenant,key,'commit',hashed,self._view(row),ident)
            settings=db.execute('SELECT enabled FROM ss_receiving_settings WHERE tenant_id=%s FOR SHARE',(tenant,)).fetchone()
            if not self.writes_enabled or not settings or not settings['enabled'] or row['state']!='ready':raise ReceivingError('Receiving admission is disabled or this plan is no longer ready.',403,code='RECEIVING_DISABLED')
            self._current_targets(db,tenant,plan,objects)
            provider=plan['provider']
            db.execute('INSERT INTO ss_receiving_seller_lanes(environment,merchant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',(provider['environment'],provider['merchant_id']))
            lane=db.execute('SELECT * FROM ss_receiving_seller_lanes WHERE environment=%s AND merchant_id=%s FOR UPDATE',(provider['environment'],provider['merchant_id'])).fetchone()
            if lane['operation_id'] is not None and (lane['tenant_id']!=tenant or str(lane['operation_id'])!=ident):
                raise ReceivingError('Another operation has an occupied or uncertain seller lane. Recover it before more stock.',code='SELLER_OPERATION_PENDING')
            at=db.execute('SELECT clock_timestamp() AS at').fetchone()['at'];provider_key=uuid.uuid4()
            body=inventory_body(ident,plan['targets'],plan['location_id'],at.isoformat(),provider_key)
            sealed=self._seal(tenant,ident,body)
            db.execute('''INSERT INTO ss_receiving_steps(tenant_id,operation_id,ordinal,endpoint,api_version,body_hash,encrypted_body,provider_key,occurred_at,authorized_by,session_hash,authority_hash)
                          VALUES (%s,%s,1,'inventory_batch',%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (tenant,ident,API_VERSION,fingerprint(body),sealed,provider_key,at,actor['staff_id'],actor['session_hash'],fingerprint(access.proof)))
            db.execute("UPDATE ss_receiving_operations SET state='inventory_pending',first_admitted_at=%s,lease_generation=lease_generation+1,lease_until=clock_timestamp()+interval '90 seconds',updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(at,actor['staff_id'],tenant,ident))
            db.execute("UPDATE ss_receiving_seller_lanes SET state='occupied',tenant_id=%s,operation_id=%s WHERE environment=%s AND merchant_id=%s",(tenant,ident,provider['environment'],provider['merchant_id']))
            row=self._row(db,tenant,ident)
            audit(db,tenant,actor['staff_id'],'receiving-inventory-admitted',ident,{'plan_hash':hashed_plan,'body_hash':fingerprint(body)})
            self._finish(db,tenant,key,'commit',hashed,self._view(row),ident)
        # Crash here leaves the durable body/key/occupancy before any HTTP.
        try:
            proof=self.provider.dispatch_inventory(access,canonical(body))
        except Exception:
            return self._save_outcome(actor,ident,row['lease_generation'],None,'PROVIDER_OUTCOME_UNKNOWN',key)
        error=None
        try:self._verify_catalog(actor,plan,access)
        except Exception:error='CATALOG_VERSION_CHANGED'
        return self._save_outcome(actor,ident,row['lease_generation'],proof,error,key)

    def _verify_catalog(self, actor, plan, access):
        with self.database.transaction() as db:current_authority(db,actor)
        objects=self.provider.catalog(access,[value for target in plan['targets'] for value in (target['variation_id'],target['parent_id'])])
        self.provider.assert_skus(access,plan['targets'])
        self.provider.location(access,plan['location_id'])
        with self.database.transaction() as db:
            current_authority(db,actor);self.provider.assert_current(db,actor['tenant_id'],access)
            self._current_targets(db,actor['tenant_id'],plan,objects)

    def _save_outcome(self, actor, ident, generation, proof, error_code, request_key):
        tenant=actor['tenant_id']
        with self.database.transaction() as db:
            # Outcome recording is not another mutation admission. A logout or
            # staff disable after dispatch must not discard provider evidence.
            row=self._row(db,tenant,ident,lock=True)
            if row['state']=='received':return self._view(row)
            if proof:
                step=db.execute('SELECT body_hash FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1',(tenant,ident)).fetchone()
                try:
                    if (not isinstance(proof,dict) or set(proof)-{'kind','request_hash','response_hash','adjustment_ids','evidence_hash'} or
                            not step or proof.get('request_hash')!=step['body_hash'] or proof.get('kind') not in ('batch_create_ack','history_adjustments')):raise ValueError()
                    if proof['kind']=='batch_create_ack':text_hash(proof.get('response_hash'))
                    else:
                        text_hash(proof.get('evidence_hash'))
                        if not isinstance(proof.get('adjustment_ids'),list) or not 1<=len(proof['adjustment_ids'])<=100:raise ValueError()
                        for value in proof['adjustment_ids']:provider_id(value,'Adjustment ID')
                except (ValueError,ServiceError):proof=None;error_code='PROVIDER_OUTCOME_UNKNOWN'
            if row['lease_generation']!=generation:
                db.execute('INSERT INTO ss_receiving_attempts(id,tenant_id,operation_id,generation,action,evidence) VALUES (%s,%s,%s,%s,%s,%s)',
                           (uuid.uuid4(),tenant,ident,generation,'stale-outcome',Jsonb({'proof':proof,'code':error_code})))
                return self._view(row)
            provider=row['plan']['provider']
            if proof and not error_code:
                receipt=self._receipt(row,self._final_targets(row),proof)
                db.execute("UPDATE ss_receiving_operations SET state='received',inventory_proof=%s,receipt=%s,lease_until=NULL,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(Jsonb(proof),Jsonb(receipt),tenant,ident))
                db.execute("UPDATE ss_receiving_seller_lanes SET state='idle',tenant_id=NULL,operation_id=NULL WHERE environment=%s AND merchant_id=%s AND tenant_id=%s AND operation_id=%s",(provider['environment'],provider['merchant_id'],tenant,ident))
                audit(db,tenant,actor['staff_id'],'receiving-completed',ident,{'receipt_hash':receipt['receipt_hash']})
            else:
                code=error_code if error_code in ('PROVIDER_OUTCOME_UNKNOWN','CATALOG_VERSION_CHANGED','RECEIVING_REVIEW_REQUIRED') else 'PROVIDER_OUTCOME_UNKNOWN'
                db.execute('''UPDATE ss_receiving_operations SET state=%s,inventory_proof=COALESCE(%s,inventory_proof),lease_until=NULL,updated_at=clock_timestamp()
                              WHERE tenant_id=%s AND id=%s''',('needs_review' if proof else 'outcome_unknown',Jsonb(proof) if proof else None,tenant,ident))
                db.execute("UPDATE ss_receiving_seller_lanes SET state='unknown' WHERE environment=%s AND merchant_id=%s AND tenant_id=%s AND operation_id=%s",(provider['environment'],provider['merchant_id'],tenant,ident))
                db.execute('''INSERT INTO ss_receiving_exceptions(tenant_id,operation_id,code) VALUES (%s,%s,%s) ON CONFLICT(tenant_id,operation_id,code)
                              DO UPDATE SET occurrences=ss_receiving_exceptions.occurrences+1,last_seen_at=clock_timestamp()''',(tenant,ident,code))
            db.execute('INSERT INTO ss_receiving_attempts(id,tenant_id,operation_id,generation,action,evidence) VALUES (%s,%s,%s,%s,%s,%s)',
                       (uuid.uuid4(),tenant,ident,generation,'outcome',Jsonb({'proof':proof,'code':error_code})))
            result=self._view(self._row(db,tenant,ident))
            db.execute('INSERT INTO ss_receiving_request_outcomes(tenant_id,request_key,result) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(tenant,request_key,Jsonb(result)))
            return result

    def _final_targets(self, row):return self._view(row)['targets']

    def recover(self, actor, ident, data):
        key,revision,hashed_plan=self._terms(data);ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan}
        with self.database.transaction() as db:
            current_authority(db,actor);hashed,old=self._old(db,tenant,key,'recover',intent)
            if old:return old
            initial=self._row(db,tenant,ident);self._match(initial,revision,hashed_plan)
            self._source(db,tenant,initial['plan'],lock=True)
            row=self._row(db,tenant,ident,lock=True)
            if row['state']=='received' or row['first_admitted_at'] is None:
                return self._finish(db,tenant,key,'recover',hashed,self._view(row),ident)
            if row['lease_until'] and row['lease_until']>db.execute('SELECT clock_timestamp() AS at').fetchone()['at']:
                return self._finish(db,tenant,key,'recover',hashed,self._view(row),ident)
            step=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1',(tenant,ident)).fetchone()
            if not step:raise ReceivingError('An admitted receiving operation is missing its durable step journal.')
            body=self._open(tenant,ident,step)
            db.execute("UPDATE ss_receiving_operations SET lease_generation=lease_generation+1,lease_until=clock_timestamp()+interval '90 seconds' WHERE tenant_id=%s AND id=%s",(tenant,ident))
            row=self._row(db,tenant,ident)
            self._finish(db,tenant,key,'recover',hashed,self._view(row),ident)
        proof=row['inventory_proof'];error=None
        try:
            access=self.provider.access(tenant)
            if principal(access,row['plan']['location_id'])!=row['plan']['provider']:raise ReceivingError('Provider identity changed.')
            if not proof:
                history=self.provider.history(access,body,row['first_admitted_at'])
                proof=history_proof(body,history,row['plan']['provider']['application_id'])
            self._verify_catalog(actor,row['plan'],access)
        except Exception:error='CATALOG_VERSION_CHANGED' if proof else 'PROVIDER_OUTCOME_UNKNOWN'
        return self._save_outcome(actor,ident,row['lease_generation'],proof,error,key)

    def authorize(self, actor, ident, data):
        key,revision,hashed_plan=self._terms(data,('resume_same_operation',))
        if data.get('resume_same_operation') is not True:raise ServiceError('Explicitly authorize the same immutable operation.')
        ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan,'resume_same_operation':True}
        with self.database.transaction() as db:
            current_authority(db,actor);hashed,old=self._old(db,tenant,key,'authorize',intent)
            if old:return old
            row=self._row(db,tenant,ident);self._match(row,revision,hashed_plan)
            if row['state']!='received':current_authority(db,actor,fresh_mfa=True)
            # This first slice never redispatches an uncertain inventory call.
            audit(db,tenant,actor['staff_id'],'receiving-same-operation-authorized',ident,{'plan_hash':hashed_plan})
            return self._finish(db,tenant,key,'authorize',hashed,self._view(row),ident)

    def cancel(self, actor, ident, data):
        key,revision,hashed_plan=self._terms(data,('reason',));reason=_reason(data.get('reason'))
        ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan,'reason':reason}
        with self.database.transaction() as db:
            current_authority(db,actor);hashed,old=self._old(db,tenant,key,'cancel',intent)
            if old:return old
            initial=self._row(db,tenant,ident);self._source(db,tenant,initial['plan'],lock=True)
            row=self._row(db,tenant,ident,lock=True);self._match(row,revision,hashed_plan)
            if row['first_admitted_at'] is not None:raise ReceivingError('An admitted or uncertain inventory request cannot be cancelled or replaced. Recover it.',code='PROVIDER_OUTCOME_UNKNOWN',receiving_id=ident)
            db.execute("UPDATE ss_receiving_operations SET state='cancelled_before_write',updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(actor['staff_id'],tenant,ident))
            audit(db,tenant,actor['staff_id'],'receiving-cancelled-before-write',ident,{'plan_hash':hashed_plan})
            return self._finish(db,tenant,key,'cancel',hashed,self._view(self._row(db,tenant,ident)),ident)

    def revise(self, actor, ident, data):
        values=dict(data);revision=integer(values.pop('revision',None),'Plan revision',0,2**63-1)
        ident=uuid_text(ident,'Receiving ID');key=_key(values.get('request_key'));plan=normalize_plan(values);tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan':plan}
        with self.database.transaction() as db:
            current_authority(db,actor);_,old=self._old(db,tenant,key,'revise',intent)
            if old:return old
            row=self._row(db,tenant,ident)
            if row['revision']!=revision or row['first_admitted_at'] is not None or str(row['claim_id'])!=plan['physical_intake_id']:
                raise ReceivingError('Only the current unadmitted operation can be revised on its same physical claim.',code='PLAN_CHANGED')
        prepared,access=self._prepare(actor,plan);new_hash=fingerprint(prepared)
        self._projection_budget(prepared,ident,revision+1)
        with self.database.transaction() as db:
            current_authority(db,actor);self.provider.assert_current(db,tenant,access)
            hashed,old=self._old(db,tenant,key,'revise',intent)
            if old:return old
            self._source(db,tenant,plan,lock=True);row=self._row(db,tenant,ident,lock=True)
            if row['revision']!=revision or row['first_admitted_at'] is not None:raise ReceivingError('The plan changed or was admitted.',code='PLAN_CHANGED')
            db.execute("UPDATE ss_receiving_operations SET revision=revision+1,workflow_kind=%s,plan_hash=%s,plan=%s,state='ready',updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(plan['workflow_kind'],new_hash,Jsonb(prepared),actor['staff_id'],tenant,ident))
            db.execute('INSERT INTO ss_receiving_plan_revisions(tenant_id,operation_id,revision,plan_hash,plan,created_by) VALUES (%s,%s,%s,%s,%s,%s)',(tenant,ident,revision+1,new_hash,Jsonb(prepared),actor['staff_id']))
            return self._finish(db,tenant,key,'revise',hashed,self._view(self._row(db,tenant,ident)),ident)
