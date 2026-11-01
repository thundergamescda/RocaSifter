"""Catalog-then-inventory extension using immutable price-family templates."""
from copy import deepcopy
from datetime import datetime, timezone
import uuid
from psycopg.types.json import Jsonb
from intake_manifest import source_line_id
from .database import audit
from .intake import current_authority
from .receiving import Receiving, principal
from .receiving_catalog import price_sku, build_catalog_request, catalog_ack, catalog_state
from .receiving_plan import (API_VERSION, ReceivingError, fingerprint, canonical, bind_targets,
                             effective_variation, inventory_body, history_proof, uuid_text)
from .receiving_templates import parent_policy_hash,authority_lane
from .security import ServiceError,provider_id


class CatalogReceiving(Receiving):
    def readiness(self,actor):
        value=super().readiness(actor);value['new_price_supported']=True
        with self.database.transaction() as db:
            current_authority(db,actor)
            row=db.execute('SELECT new_price_enabled FROM ss_receiving_settings WHERE tenant_id=%s',(actor['tenant_id'],)).fetchone()
        value['new_price_write_enabled']=value['write_enabled'] and bool(row and row['new_price_enabled'])
        return value

    def _prepare(self,actor,plan):
        try:return super()._prepare(actor,plan)
        except ReceivingError as error:
            if error.code!='CATALOG_MAPPING_REVIEW_REQUIRED':raise
        tenant=actor['tenant_id']
        with self.database.transaction() as db:
            current_authority(db,actor);_,budget,accepted=self._source(db,tenant,plan)
            lookup={str(row['id']):row for row in accepted};needed=set()
            for row in plan['lines']:
                identity=lookup.get(row.get('accepted_line_id'),{}).get('identity') if plan['workflow_kind']=='accepted_trade' else row['identity']
                if not identity:raise ReceivingError('The immutable accepted line is unavailable.')
                needed.add((source_line_id(identity),row['selling_cents']))
            if len(needed)>100:raise ReceivingError('At most 100 identity/price targets are supported.')
            bindings=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND location_id=%s AND identity_hash=ANY(%s::text[]) AND revoked_at IS NULL LIMIT 201',
                (tenant,plan['location_id'],list({row[0] for row in needed}))).fetchall()
            templates=db.execute('SELECT * FROM ss_receiving_price_templates WHERE tenant_id=%s AND location_id=%s AND identity_hash=ANY(%s::text[]) AND approved=TRUE AND revoked_at IS NULL LIMIT 201',
                (tenant,plan['location_id'],list({row[0] for row in needed}))).fetchall()
        if len(bindings)>200 or len(templates)>100:raise ReceivingError('Catalog family selection is ambiguous or exceeds the bounded query.')
        access=self.provider.access(tenant);self.provider.location(access,plan['location_id']);provider=principal(access,plan['location_id'])
        bindings=[row for row in bindings if row['provider']==provider]
        by_id={str(row['id']):row for row in bindings};virtual=[];selected_templates={}
        for identity,cents in sorted(needed):
            exact=[row for row in bindings if row['identity_hash']==identity and row['selling_cents']==cents]
            if len(exact)==1:virtual.append(exact[0]);continue
            if exact:raise ReceivingError('Multiple approved exact-price mappings require review.')
            choices=[row for row in templates if row['identity_hash']==identity and str(row['binding_id']) in by_id]
            if len(choices)!=1:raise ReceivingError('The owner must approve one existing-parent price template for this identity.',code='CATALOG_MAPPING_REVIEW_REQUIRED')
            template=choices[0];source=by_id[str(template['binding_id'])]
            if template['facts']['provider']!={key:provider[key] for key in ('merchant_id','environment','application_id')}:raise ReceivingError('The price template belongs to another provider principal.')
            virtual.append(dict(source,action='create_price',selling_cents=cents,source_selling_cents=source['selling_cents'],
                new_target_sku=price_sku(source['sku'],cents),template_id=template['id'],template_version=template['version'],
                template_evidence_hash=fingerprint({'id':str(template['id']),'version':template['version'],'facts':template['facts']})))
            selected_templates[str(template['id'])]=template
        ids={value for row in virtual for value in (row['variation_id'],row['parent_id'])}
        objects=self.provider.catalog(access,list(ids))
        tax_ids={value for row in virtual for value in (objects[row['parent_id']].get('item_data') or {}).get('tax_ids',[])}
        tax_ids.update(value.get('id') for row in virtual for value in (objects[row['parent_id']].get('item_data') or {}).get('categories',[]) if isinstance(value,dict))
        tax_ids.update((objects[row['parent_id']].get('item_data') or {}).get('category_id') for row in virtual if (objects[row['parent_id']].get('item_data') or {}).get('category_id'))
        missing=tax_ids-set(objects)
        if missing:objects.update(self.provider.catalog(access,list(missing)))
        for row in virtual:
            if row.get('action')!='create_price':continue
            template=selected_templates[str(row['template_id'])]
            if parent_policy_hash(objects[row['parent_id']],objects)!=template['facts']['parent_policy_hash']:
                raise ReceivingError('The reviewed parent tax/catalog policy changed. Review a new owner template.',code='CATALOG_VERSION_CHANGED')
        targets,selected,excluded=bind_targets(plan,budget,accepted,virtual,objects)
        self.provider.assert_skus(access,targets)
        additions=[];aliases={};groups={}
        for target in targets:
            if target['action']!='create_price':continue
            group=(target['template_id'],target['parent_id'],target['target_sku'],target['selling_cents'])
            groups.setdefault(group,[]).append(target)
        for _,rows in sorted(groups.items()):
            rows.sort(key=lambda row:row['review_line_id']);target=rows[0];template=selected_templates[target['template_id']]
            source=by_id[str(template['binding_id'])]
            additions.append({'review_line_id':target['review_line_id'],'identity_hash':target['identity_hash'],
                'parent_id':target['parent_id'],'source_variation_id':source['variation_id'],'target_sku':target['target_sku'],
                'selling_cents':target['selling_cents'],'template_id':target['template_id'],'template_version':target['template_version'],
                'template':{'approved':True,'new_price_enabled':True,'location_ids':[plan['location_id']],
                            'name_prefix':template['facts']['variation_name_prefix'],'name_format_version':2}})
            for row in rows:aliases[row['review_line_id']]=target['review_line_id']
        parents={row['parent_id']:objects[row['parent_id']] for row in additions}
        # Prove complete-parent size/capacity before persisting a ready plan.
        build_catalog_request(str(uuid.uuid4()),parents,additions,str(uuid.uuid4()))
        prepared=dict(plan,provider=provider,targets=targets,selected_quantities=selected,excluded_quantities=excluded,
            catalog_required=True,catalog_additions=additions,catalog_aliases=aliases,
            parent_snapshot_hashes={key:fingerprint(value) for key,value in parents.items()})
        return prepared,access

    def _templates_current(self,db,tenant,plan,objects):
        ids=list({row['template_id'] for row in plan['catalog_additions']})
        rows=db.execute('SELECT * FROM ss_receiving_price_templates WHERE tenant_id=%s AND id=ANY(%s::uuid[]) AND approved=TRUE AND revoked_at IS NULL FOR SHARE',(tenant,ids)).fetchall()
        by_id={str(row['id']):row for row in rows}
        if len(by_id)!=len(ids):raise ReceivingError('A price template was revoked before the next provider step.')
        source_ids=list({row['binding_id'] for row in rows})
        bindings=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=ANY(%s::uuid[]) AND revoked_at IS NULL FOR SHARE',(tenant,source_ids)).fetchall()
        sources={str(row['id']):row for row in bindings}
        if len(sources)!=len(source_ids):raise ReceivingError('A price-family source mapping was revoked.')
        for addition in plan['catalog_additions']:
            template=by_id[addition['template_id']];source=sources[str(template['binding_id'])]
            if (template['version']!=addition['template_version'] or source['variation_id']!=addition['source_variation_id'] or
                    parent_policy_hash(objects[addition['parent_id']],objects)!=template['facts']['parent_policy_hash']):
                raise ReceivingError('The frozen price-family policy changed.',code='CATALOG_VERSION_CHANGED')
            effective=effective_variation(objects[source['variation_id']],objects[source['parent_id']],plan['location_id'])
            if any(effective[key]!=source[key] for key in ('variation_id','parent_id','sku','selling_cents','identity_anchor_hash')):
                raise ReceivingError('The approved source variation changed.',code='CATALOG_VERSION_CHANGED')
        return by_id,sources

    def _catalog_objects(self,access,plan,*,resolutions=None):
        ids={value for row in plan['catalog_additions'] for value in (row['source_variation_id'],row['parent_id'])}
        ids.update(row['variation_id'] for row in (resolutions or []) if row.get('variation_id'))
        objects=self.provider.catalog(access,list(ids))
        taxes={ident for row in plan['catalog_additions'] for ident in (objects[row['parent_id']].get('item_data') or {}).get('tax_ids',[])}
        taxes.update(value.get('id') for row in plan['catalog_additions'] for value in (objects[row['parent_id']].get('item_data') or {}).get('categories',[]) if isinstance(value,dict))
        taxes.update((objects[row['parent_id']].get('item_data') or {}).get('category_id') for row in plan['catalog_additions'] if (objects[row['parent_id']].get('item_data') or {}).get('category_id'))
        if taxes-set(objects):objects.update(self.provider.catalog(access,list(taxes-set(objects))))
        return objects

    def commit(self,actor,ident,data):
        with self.database.transaction() as db:
            current_authority(db,actor);row=self._row(db,actor['tenant_id'],ident)
        if not row['plan'].get('catalog_required'):return super().commit(actor,ident,data)
        return self._commit_catalog(actor,ident,data)

    def _commit_catalog(self,actor,ident,data):
        key,revision,hashed_plan=self._terms(data,('confirmed','excluded_not_received_ack'))
        if data.get('confirmed') is not True or data.get('excluded_not_received_ack') is not True:raise ServiceError('Confirm the exact receiving plan and excluded quantities.')
        ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan,'confirmed':True,'excluded_not_received_ack':True}
        with self.database.transaction() as db:
            current_authority(db,actor);_,old=self._old(db,tenant,key,'commit',intent)
            if old:return old
            row=self._row(db,tenant,ident);self._match(row,revision,hashed_plan)
            if row['first_admitted_at'] is not None:
                hashed,_=self._old(db,tenant,key,'commit',intent)
                return self._finish(db,tenant,key,'commit',hashed,self._view(row),ident)
            self._projection_budget(row['plan'],ident,row['revision'])
            current_authority(db,actor,fresh_mfa=True)
            settings=db.execute('SELECT * FROM ss_receiving_settings WHERE tenant_id=%s',(tenant,)).fetchone()
            if not self.writes_enabled or not settings or not settings['enabled'] or not settings['new_price_enabled']:
                raise ReceivingError('New-price catalog receiving is disabled for this server or store.',403,code='RECEIVING_DISABLED')
        plan=row['plan'];access=self.provider.access(tenant,write=True,catalog=True)
        if principal(access,plan['location_id'])!=plan['provider']:raise ReceivingError('Square principal changed.')
        self.provider.location(access,plan['location_id']);objects=self._catalog_objects(access,plan)
        self.provider.assert_skus(access,plan['targets'])
        if any(fingerprint(objects[parent])!=hashed for parent,hashed in plan['parent_snapshot_hashes'].items()):
            raise ReceivingError('The full parent changed before catalog admission. Explicitly revise this unadmitted plan.',code='CATALOG_VERSION_CHANGED')
        with self.database.transaction() as db:
            current_authority(db,actor,fresh_mfa=True);self.provider.assert_current(db,tenant,access)
            hashed,old=self._old(db,tenant,key,'commit',intent)
            if old:return old
            self._source(db,tenant,plan,lock=True);row=self._row(db,tenant,ident,lock=True);self._match(row,revision,hashed_plan)
            if row['first_admitted_at'] is not None:return self._finish(db,tenant,key,'commit',hashed,self._view(row),ident)
            settings=db.execute('SELECT * FROM ss_receiving_settings WHERE tenant_id=%s FOR SHARE',(tenant,)).fetchone()
            if not self.writes_enabled or not settings or not settings['enabled'] or not settings['new_price_enabled'] or row['state']!='ready':raise ReceivingError('Catalog admission is disabled.',403,code='RECEIVING_DISABLED')
            self._templates_current(db,tenant,plan,objects)
            provider=plan['provider']
            db.execute('INSERT INTO ss_receiving_seller_lanes(environment,merchant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',(provider['environment'],provider['merchant_id']))
            lane=db.execute('SELECT * FROM ss_receiving_seller_lanes WHERE environment=%s AND merchant_id=%s FOR UPDATE',(provider['environment'],provider['merchant_id'])).fetchone()
            if lane['operation_id'] is not None and (lane['tenant_id']!=tenant or str(lane['operation_id'])!=ident):raise ReceivingError('Recover the occupied seller operation first.',code='SELLER_OPERATION_PENDING')
            at=db.execute('SELECT clock_timestamp() AS at').fetchone()['at'];provider_key=uuid.uuid4()
            parents={parent:objects[parent] for parent in plan['parent_snapshot_hashes']}
            body=build_catalog_request(ident,parents,plan['catalog_additions'],str(provider_key))
            db.execute('''INSERT INTO ss_receiving_steps(tenant_id,operation_id,ordinal,endpoint,api_version,body_hash,encrypted_body,provider_key,occurred_at,authorized_by,session_hash,authority_hash)
                          VALUES (%s,%s,0,'catalog_batch',%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (tenant,ident,API_VERSION,fingerprint(body),self._seal(tenant,ident,body,ordinal=0,endpoint='catalog_batch'),provider_key,at,actor['staff_id'],actor['session_hash'],fingerprint(access.proof)))
            db.execute("UPDATE ss_receiving_operations SET state='catalog_pending',first_admitted_at=%s,lease_generation=lease_generation+1,lease_until=clock_timestamp()+interval '90 seconds',updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(at,actor['staff_id'],tenant,ident))
            db.execute("UPDATE ss_receiving_seller_lanes SET state='occupied',tenant_id=%s,operation_id=%s WHERE environment=%s AND merchant_id=%s",(tenant,ident,provider['environment'],provider['merchant_id']))
            row=self._row(db,tenant,ident)
            audit(db,tenant,actor['staff_id'],'receiving-catalog-admitted',ident,{'plan_hash':hashed_plan,'body_hash':fingerprint(body)})
            self._finish(db,tenant,key,'commit',hashed,self._view(row),ident)
        try:result=self.provider.dispatch_catalog(access,canonical(body),plan['catalog_additions'])
        except Exception:result=None
        return self._save_catalog(actor,ident,row['lease_generation'],result,key)

    def _save_catalog(self,actor,ident,generation,result,request_key):
        tenant=actor['tenant_id']
        with self.database.transaction() as db:
            row=self._row(db,tenant,ident,lock=True)
            if row['catalog_proof'] or row['state']=='received' or row['lease_generation']!=generation:
                view=self._view(row)
                db.execute('INSERT INTO ss_receiving_request_outcomes(tenant_id,request_key,result) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(tenant,request_key,Jsonb(view)))
                return view
            step=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=0',(tenant,ident)).fetchone()
            body=self._open(tenant,ident,step);plan=row['plan'];proof=None;resolutions=[]
            try:
                proof=result['proof'];values=result['resolutions']
                if proof['request_hash']!=step['body_hash'] or proof['kind'] not in ('catalog_batch_ack','catalog_state_match'):raise ValueError()
                evidence_key='response_hash' if proof['kind']=='catalog_batch_ack' else 'evidence_hash'
                if set(proof)!={'kind','request_hash',evidence_key}:raise ValueError()
                from .receiving_plan import text_hash
                text_hash(proof[evidence_key])
                by_review={value['review_line_id']:value for value in values}
                if len(by_review)!=len(plan['catalog_additions']):raise ValueError()
                for addition in plan['catalog_additions']:
                    value=by_review[addition['review_line_id']]
                    if set(value)!={'review_line_id','identity_hash','parent_id','target_sku','selling_cents','template_id','template_version','variation_id','currency'}:raise ValueError()
                    if any(value[key]!=addition[key] for key in ('review_line_id','identity_hash','parent_id','target_sku','selling_cents','template_id','template_version')) or provider_id(value.get('variation_id')).startswith('#') or value.get('currency')!='USD':raise ValueError()
                if len({value['variation_id'] for value in values})!=len(values):raise ValueError()
            except (TypeError,ValueError,KeyError,ServiceError):proof=None
            provider=plan['provider']
            if proof:
                authority_lane(db,tenant)
                for addition in plan['catalog_additions']:
                    value=by_review[addition['review_line_id']]
                    template=db.execute('SELECT * FROM ss_receiving_price_templates WHERE tenant_id=%s AND id=%s FOR SHARE',(tenant,addition['template_id'])).fetchone()
                    if not template or not template['approved'] or template['version']!=addition['template_version']:raise ReceivingError('Admitted template evidence is unavailable.')
                    expected_parent=next(parent for parent in body['batches'][0]['objects'] if parent['id']==addition['parent_id'])
                    temporary='#'+addition['review_line_id'].replace('-','')
                    expected=deepcopy(next(child for child in expected_parent['item_data']['variations'] if child['id']==temporary));expected['id']=value['variation_id']
                    effective=effective_variation(expected,expected_parent,plan['location_id'])
                    identity=next(target['identity'] for target in plan['targets'] if target['review_line_id']==addition['review_line_id'])
                    evidence=fingerprint({'catalog_proof':proof,'resolution':value,'template_id':addition['template_id']})
                    existing=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND identity_hash=%s AND location_id=%s AND selling_cents=%s AND revoked_at IS NULL FOR SHARE',
                        (tenant,addition['identity_hash'],plan['location_id'],addition['selling_cents'])).fetchone()
                    if existing:
                        if any(existing[key]!=effective[key] for key in ('variation_id','parent_id','sku','selling_cents','identity_anchor_hash')):raise ReceivingError('An approved target mapping conflicts with the catalog outcome.')
                        binding=existing['id'];evidence=existing['evidence_hash']
                    else:
                        binding=uuid.uuid4()
                        db.execute('''INSERT INTO ss_receiving_bindings(tenant_id,id,identity,identity_hash,provider,variation_id,parent_id,sku,location_id,selling_cents,identity_anchor_hash,evidence_hash,approved_by,approval_reason,approval_source,template_id,revoked_at,revoked_by,revoke_reason)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'price_template',%s,%s,%s,%s)''',
                          (tenant,binding,Jsonb(identity),addition['identity_hash'],Jsonb(provider),value['variation_id'],value['parent_id'],value['target_sku'],plan['location_id'],value['selling_cents'],effective['identity_anchor_hash'],evidence,template['approved_by'],
                           'Created under owner-reviewed price template '+addition['template_id'],template['id'],template['revoked_at'],template['revoked_by'],template['revoke_reason']))
                    aliases=sorted(review for review,representative in plan['catalog_aliases'].items() if representative==addition['review_line_id'])
                    resolutions.append(dict(value,review_line_ids=aliases,binding_id=str(binding),binding_version=1,binding_evidence_hash=evidence))
                db.execute("UPDATE ss_receiving_operations SET state='catalog_confirmed',catalog_proof=%s,catalog_resolutions=%s,lease_until=NULL,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(Jsonb(proof),Jsonb(resolutions),tenant,ident))
                db.execute("UPDATE ss_receiving_seller_lanes SET state='occupied' WHERE environment=%s AND merchant_id=%s AND tenant_id=%s AND operation_id=%s",(provider['environment'],provider['merchant_id'],tenant,ident))
                audit(db,tenant,actor['staff_id'],'receiving-catalog-confirmed',ident,{'proof_hash':fingerprint(proof)})
            else:
                db.execute("UPDATE ss_receiving_operations SET state='outcome_unknown',lease_until=NULL,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(tenant,ident))
                db.execute("UPDATE ss_receiving_seller_lanes SET state='unknown' WHERE environment=%s AND merchant_id=%s AND tenant_id=%s AND operation_id=%s",(provider['environment'],provider['merchant_id'],tenant,ident))
                db.execute("INSERT INTO ss_receiving_exceptions(tenant_id,operation_id,code) VALUES (%s,%s,'CATALOG_OUTCOME_UNKNOWN') ON CONFLICT(tenant_id,operation_id,code) DO UPDATE SET occurrences=ss_receiving_exceptions.occurrences+1,last_seen_at=clock_timestamp()",(tenant,ident))
            db.execute('INSERT INTO ss_receiving_attempts(id,tenant_id,operation_id,generation,action,evidence) VALUES (%s,%s,%s,%s,%s,%s)',(uuid.uuid4(),tenant,ident,generation,'catalog-outcome',Jsonb({'proof':proof})))
            view=self._view(self._row(db,tenant,ident))
            db.execute('INSERT INTO ss_receiving_request_outcomes(tenant_id,request_key,result) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(tenant,request_key,Jsonb(view)))
            return view

    def _resolved_targets(self,row):
        targets=self._view(row)['targets'];by_review={value['review_line_id']:value for value in row['catalog_resolutions'] or []}
        result=[]
        for target in targets:
            if target['action']=='create_price':
                representative=row['plan']['catalog_aliases'][target['review_line_id']];resolution=by_review.get(representative)
                if not resolution:raise ReceivingError('The authoritative catalog target is not resolved.')
                target=dict(target,source_binding_id=target['binding_id'],source_binding_version=target['binding_version'],source_binding_evidence_hash=target['binding_evidence_hash'],
                    binding_id=resolution['binding_id'],binding_version=resolution['binding_version'],binding_evidence_hash=resolution['binding_evidence_hash'],
                    variation_id=resolution['variation_id'],binding_role='resolved_target')
            result.append(target)
        return result

    def _final_targets(self,row):
        return self._resolved_targets(row) if row['plan'].get('catalog_required') else super()._final_targets(row)

    def _verify_catalog(self,actor,plan,access):
        if not plan.get('catalog_required'):return super()._verify_catalog(actor,plan,access)
        tenant=actor['tenant_id']
        with self.database.transaction() as db:
            current_authority(db,actor)
            row=db.execute('SELECT * FROM ss_receiving_operations WHERE tenant_id=%s AND claim_id=%s',(tenant,plan['physical_intake_id'])).fetchone()
            if not row or not row['catalog_proof']:raise ReceivingError('Catalog outcome is not confirmed.')
            step=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=0',(tenant,row['id'])).fetchone()
            body=self._open(tenant,row['id'],step);resolved=self._resolved_targets(row)
        objects=self._catalog_objects(access,plan,resolutions=resolved);self.provider.location(access,plan['location_id']);self.provider.assert_skus(access,resolved)
        catalog_state(body,objects,plan['catalog_additions'])
        with self.database.transaction() as db:
            current_authority(db,actor);self.provider.assert_current(db,tenant,access);self._source(db,tenant,plan,lock=True)
            self._templates_current(db,tenant,plan,objects)
            ids=list({target['binding_id'] for target in resolved})
            bindings=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=ANY(%s::uuid[]) AND revoked_at IS NULL FOR SHARE',(tenant,ids)).fetchall()
            by_id={str(binding['id']):binding for binding in bindings}
            if len(by_id)!=len(ids):raise ReceivingError('An inventory target mapping was revoked.')
            for target in resolved:
                binding=by_id[target['binding_id']]
                effective=effective_variation(objects[target['variation_id']],objects[target['parent_id']],plan['location_id'])
                if any(effective[key]!=binding[key] for key in ('variation_id','parent_id','sku','selling_cents','identity_anchor_hash')) or effective['sku']!=target['target_sku'] or effective['selling_cents']!=target['selling_cents']:
                    raise ReceivingError('Resolved target price, identity anchor or SKU changed.',code='CATALOG_VERSION_CHANGED')

    def authorize(self,actor,ident,data):
        with self.database.transaction() as db:
            current_authority(db,actor);row=self._row(db,actor['tenant_id'],ident)
            has_inventory=db.execute('SELECT 1 FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1',(actor['tenant_id'],row['id'])).fetchone()
        if not row['plan'].get('catalog_required') or not row['catalog_proof'] or has_inventory or row['state']=='received':return super().authorize(actor,ident,data)
        key,revision,hashed_plan=self._terms(data,('resume_same_operation',))
        if data.get('resume_same_operation') is not True:raise ServiceError('Explicitly authorize the same receiving operation.')
        ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan,'resume_same_operation':True}
        with self.database.transaction() as db:
            current_authority(db,actor);_,old=self._old(db,tenant,key,'authorize',intent)
            if old:return old
            current_authority(db,actor,fresh_mfa=True);self._match(row,revision,hashed_plan)
            self._projection_budget(row['plan'],ident,row['revision'])
            settings=db.execute('SELECT * FROM ss_receiving_settings WHERE tenant_id=%s',(tenant,)).fetchone()
            if not self.writes_enabled or not settings or not settings['enabled'] or not settings['new_price_enabled']:raise ReceivingError('Receiving admission is disabled.',403,code='RECEIVING_DISABLED')
        plan=row['plan'];access=self.provider.access(tenant,write=True)
        if principal(access,plan['location_id'])!=plan['provider']:raise ReceivingError('Provider identity changed.')
        self._verify_catalog(actor,plan,access);objects=self._catalog_objects(access,plan,resolutions=self._resolved_targets(row))
        with self.database.transaction() as db:
            current_authority(db,actor,fresh_mfa=True);self.provider.assert_current(db,tenant,access)
            hashed,old=self._old(db,tenant,key,'authorize',intent)
            if old:return old
            self._source(db,tenant,plan,lock=True);row=self._row(db,tenant,ident,lock=True);self._match(row,revision,hashed_plan)
            if row['state']=='received' or db.execute('SELECT 1 FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1',(tenant,ident)).fetchone():return self._finish(db,tenant,key,'authorize',hashed,self._view(row),ident)
            settings=db.execute('SELECT * FROM ss_receiving_settings WHERE tenant_id=%s FOR SHARE',(tenant,)).fetchone()
            if not self.writes_enabled or not settings or not settings['enabled'] or not settings['new_price_enabled'] or not row['catalog_proof']:raise ReceivingError('Receiving admission is disabled or catalog proof is missing.',403,code='RECEIVING_DISABLED')
            self._templates_current(db,tenant,plan,objects);targets=self._resolved_targets(row)
            binding_ids=list({target['binding_id'] for target in targets})
            current=db.execute('SELECT id FROM ss_receiving_bindings WHERE tenant_id=%s AND id=ANY(%s::uuid[]) AND revoked_at IS NULL FOR SHARE',(tenant,binding_ids)).fetchall()
            if len(current)!=len(binding_ids):raise ReceivingError('A resolved inventory target was revoked.')
            provider=plan['provider'];lane=db.execute('SELECT * FROM ss_receiving_seller_lanes WHERE environment=%s AND merchant_id=%s FOR UPDATE',(provider['environment'],provider['merchant_id'])).fetchone()
            if not lane or lane['tenant_id']!=tenant or str(lane['operation_id'])!=ident:raise ReceivingError('The durable seller lane is inconsistent.')
            at=db.execute('SELECT clock_timestamp() AS at').fetchone()['at'];provider_key=uuid.uuid4()
            body=inventory_body(ident,targets,plan['location_id'],at.isoformat(),provider_key)
            db.execute('''INSERT INTO ss_receiving_steps(tenant_id,operation_id,ordinal,endpoint,api_version,body_hash,encrypted_body,provider_key,occurred_at,authorized_by,session_hash,authority_hash)
                          VALUES (%s,%s,1,'inventory_batch',%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (tenant,ident,API_VERSION,fingerprint(body),self._seal(tenant,ident,body),provider_key,at,actor['staff_id'],actor['session_hash'],fingerprint(access.proof)))
            db.execute("UPDATE ss_receiving_operations SET state='inventory_pending',lease_generation=lease_generation+1,lease_until=clock_timestamp()+interval '90 seconds',updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(actor['staff_id'],tenant,ident))
            db.execute("UPDATE ss_receiving_seller_lanes SET state='occupied' WHERE environment=%s AND merchant_id=%s",(provider['environment'],provider['merchant_id']))
            row=self._row(db,tenant,ident)
            audit(db,tenant,actor['staff_id'],'receiving-inventory-admitted',ident,{'plan_hash':hashed_plan,'body_hash':fingerprint(body)})
            self._finish(db,tenant,key,'authorize',hashed,self._view(row),ident)
        try:proof=self.provider.dispatch_inventory(access,canonical(body))
        except Exception:return self._save_outcome(actor,ident,row['lease_generation'],None,'PROVIDER_OUTCOME_UNKNOWN',key)
        error=None
        try:self._verify_catalog(actor,plan,access)
        except Exception:error='CATALOG_VERSION_CHANGED'
        return self._save_outcome(actor,ident,row['lease_generation'],proof,error,key)

    def recover(self,actor,ident,data):
        with self.database.transaction() as db:
            current_authority(db,actor);initial=self._row(db,actor['tenant_id'],ident)
            has_inventory=db.execute('SELECT 1 FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1',(actor['tenant_id'],initial['id'])).fetchone()
        if not initial['plan'].get('catalog_required') or has_inventory:return super().recover(actor,ident,data)
        key,revision,hashed_plan=self._terms(data);ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan}
        with self.database.transaction() as db:
            current_authority(db,actor);hashed,old=self._old(db,tenant,key,'recover',intent)
            if old:return old
            self._source(db,tenant,initial['plan'],lock=True);row=self._row(db,tenant,ident,lock=True);self._match(row,revision,hashed_plan)
            if row['catalog_proof'] or row['first_admitted_at'] is None or (row['lease_until'] and row['lease_until']>db.execute('SELECT clock_timestamp() AS at').fetchone()['at']):return self._finish(db,tenant,key,'recover',hashed,self._view(row),ident)
            step=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=0',(tenant,ident)).fetchone()
            body=self._open(tenant,ident,step)
            db.execute("UPDATE ss_receiving_operations SET lease_generation=lease_generation+1,lease_until=clock_timestamp()+interval '90 seconds' WHERE tenant_id=%s AND id=%s",(tenant,ident))
            row=self._row(db,tenant,ident);self._finish(db,tenant,key,'recover',hashed,self._view(row),ident)
        result=None
        try:
            access=self.provider.access(tenant)
            if principal(access,row['plan']['location_id'])!=row['plan']['provider']:raise ReceivingError('Provider principal changed.')
            objects=self._catalog_objects(access,row['plan']);result=catalog_state(body,objects,row['plan']['catalog_additions'])
            self.provider.assert_skus(access,result['resolutions'])
        except Exception:result=None
        return self._save_catalog(actor,ident,row['lease_generation'],result,key)
