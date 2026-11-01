"""Owner-reviewed existing-parent price templates, never tax-law approval."""
import re
import unicodedata
import uuid
from decimal import Decimal
from psycopg.types.json import Jsonb
from .database import audit
from .intake import current_authority
from .receiving_plan import ReceivingError, effective_variation, fingerprint, keys, uuid_text, text_hash, integer
from .security import ServiceError


def parent_policy_hash(parent, objects=None):
    item=parent.get('item_data') or {}
    return fingerprint({'item':{key:item.get(key) for key in ('product_type','is_taxable','tax_ids','categories','category_id','item_options','modifier_list_info')},
                        'locations':{key:parent.get(key) for key in ('present_at_all_locations','present_at_location_ids','absent_at_location_ids')},
                        'referenced_taxes':{ident:{key:((objects or {}).get(ident) or {}).get(key) for key in ('type','is_deleted','tax_data')} for ident in item.get('tax_ids',[])},
                        'referenced_categories':{ident:{key:((objects or {}).get(ident) or {}).get(key) for key in ('type','is_deleted','category_data')}
                          for ident in list(dict.fromkeys([row.get('id') for row in item.get('categories',[]) if isinstance(row,dict)]+([item['category_id']] if item.get('category_id') else [])))}})


def authority_lane(db,tenant):
    db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(tenant)+':receiving-catalog-authority',))


def revoke_delegated_authority(db,tenant,staff,reason,*,binding_id=None,template_id=None):
    """Revoke the entire delegated graph under its allocation/revocation lane."""
    authority_lane(db,tenant)
    seeds=[binding_id] if binding_id else [row['id'] for row in db.execute('SELECT id FROM ss_receiving_bindings WHERE tenant_id=%s AND template_id=%s',(tenant,template_id)).fetchall()]
    bindings=[]
    if seeds:
        bindings=[row['id'] for row in db.execute('''WITH RECURSIVE family(id) AS (
                    SELECT unnest(%s::uuid[]) UNION
                    SELECT child.id FROM family f JOIN ss_receiving_price_templates t ON t.tenant_id=%s AND t.binding_id=f.id
                    JOIN ss_receiving_bindings child ON child.tenant_id=t.tenant_id AND child.template_id=t.id)
                    SELECT id FROM family''',(seeds,tenant)).fetchall()]
    templates=[row['id'] for row in db.execute('SELECT id FROM ss_receiving_price_templates WHERE tenant_id=%s AND binding_id=ANY(%s::uuid[])',(tenant,bindings)).fetchall()] if bindings else []
    if template_id and template_id not in templates:templates.append(template_id)
    explanation='Source catalog authority revoked: '+reason[:150]
    if templates:db.execute('UPDATE ss_receiving_price_templates SET revoked_at=clock_timestamp(),revoked_by=%s,revoke_reason=%s WHERE tenant_id=%s AND id=ANY(%s::uuid[]) AND revoked_at IS NULL',(staff,explanation,tenant,templates))
    if bindings:db.execute('UPDATE ss_receiving_bindings SET revoked_at=clock_timestamp(),revoked_by=%s,revoke_reason=%s WHERE tenant_id=%s AND id=ANY(%s::uuid[]) AND revoked_at IS NULL',(staff,explanation,tenant,bindings))


def _reason(value):
    if not isinstance(value,str) or not 10<=len(value.strip())<=200 or any(unicodedata.category(c) in ('Cc','Cf','Cs') for c in value):
        raise ServiceError('Use a 10 to 200 character internal review reason.')
    return value.strip()


def _key(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9_-]{1,150}',value):raise ServiceError('A stable template request key is required.')
    return value


class PriceTemplates:
    def __init__(self,database,provider,journal):self.database,self.provider,self.journal=database,provider,journal

    def bindings(self,actor,query='',*,limit=50,cursor=None):
        if not isinstance(query,str) or len(query)>200:raise ServiceError('Catalog binding search is too long.')
        integer(limit,'Page size',1,100);after=uuid_text(cursor,'Cursor') if cursor else None
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',))
            rows=db.execute('''SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND revoked_at IS NULL
                             AND strpos(lower(COALESCE(identity->>'name','')||' '||COALESCE(identity->>'game','')||' '||sku),lower(%s))>0
                             AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s''',(actor['tenant_id'],query,after,after,limit+1)).fetchall()
        access=self.provider.access(actor['tenant_id']);locations={row['id']:row for row in self.provider.locations(access)}
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',));self.provider.assert_current(db,actor['tenant_id'],access)
        more=len(rows)>limit;rows=rows[:limit]
        result=[{'binding_id':str(row['id']),'version':row['version'],'identity':row['identity'],'identity_hash':row['identity_hash'],
                 'source_sku':row['sku'],'selling_cents':row['selling_cents'],'currency':'USD',
                 'location':{'id':row['location_id'],'name':locations.get(row['location_id'],{}).get('name','Unavailable location')},
                 'provider':{key:row['provider'][key] for key in ('merchant_id','environment','application_id')}} for row in rows]
        return {'bindings':result,'next_cursor':str(rows[-1]['id']) if more and rows else None}

    def list(self,actor,*,limit=50,cursor=None):
        integer(limit,'Page size',1,100);after=uuid_text(cursor,'Cursor') if cursor else None
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',))
            rows=db.execute('SELECT * FROM ss_receiving_price_templates WHERE tenant_id=%s AND approved=TRUE AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s',
                            (actor['tenant_id'],after,after,limit+1)).fetchall()
        more=len(rows)>limit;rows=rows[:limit]
        result=[{'template_id':str(row['id']),'version':row['version'],'state':'revoked' if row['revoked_at'] else 'approved',
                 'facts':row['facts'],'approved_at':row['approved_at'].isoformat(),'revoked_at':row['revoked_at'].isoformat() if row['revoked_at'] else None,
                 'approval_reason':row['approval_reason']} for row in rows]
        return {'templates':result,'next_cursor':str(rows[-1]['id']) if more and rows else None}

    def _facts(self,actor,binding,prefix):
        access=self.provider.access(actor['tenant_id']);location=self.provider.location(access,binding['location_id'])
        expected={key:access.proof[key] for key in ('merchant_id','environment','application_id')}|{'location_id':binding['location_id']}
        if binding['provider']!=expected:raise ReceivingError('The mapping belongs to a different Square principal.')
        objects=self.provider.catalog(access,[binding['variation_id'],binding['parent_id']])
        variation,parent=objects[binding['variation_id']],objects[binding['parent_id']]
        effective=effective_variation(variation,parent,binding['location_id']);self.provider.assert_skus(access,[effective])
        if any(effective[key]!=binding[key] for key in ('variation_id','parent_id','sku','selling_cents','identity_anchor_hash')):
            raise ReceivingError('Review a new current source mapping before approving a price family.')
        item=parent['item_data'];children=item.get('variations')
        if not isinstance(children,list) or not children or len(children)>=250 or not any(row.get('id')==binding['variation_id'] for row in children):
            raise ReceivingError('The complete current parent/sibling list or capacity is unavailable.')
        if type(parent.get('version')) is not int:raise ReceivingError('The parent version is required.')
        taxes=[];categories=[]
        tax_ids=item.get('tax_ids') or []
        if not isinstance(tax_ids,list) or len(tax_ids)>100 or any(not isinstance(value,str) or not value for value in tax_ids) or len(set(tax_ids))!=len(tax_ids):
            raise ReceivingError('The existing tax references are invalid or ambiguous.')
        if not isinstance(item.get('categories',[]),list) or len(item.get('categories',[]))>100:raise ReceivingError('The existing category references are invalid.')
        category_ids=[row.get('id') for row in item.get('categories',[]) if isinstance(row,dict)]
        if len(category_ids)!=len(item.get('categories',[])) or any(not isinstance(value,str) or not value for value in category_ids):raise ReceivingError('The existing category references are incomplete.')
        if item.get('category_id') and item['category_id'] not in category_ids:category_ids.append(item['category_id'])
        missing=[value for value in tax_ids+category_ids if value not in objects]
        if missing:objects.update(self.provider.catalog(access,missing))
        for ident in tax_ids:
            obj=objects.get(ident);data=(obj or {}).get('tax_data') or {}
            if not obj or obj.get('type')!='TAX' or obj.get('is_deleted') or not isinstance(data.get('name'),str):raise ReceivingError('A referenced tax setting cannot be read. Resolve it before template approval.')
            percentage=data.get('percentage')
            if (not isinstance(percentage,str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,6})?',percentage) or
                    not 0<=Decimal(percentage)<=100 or data.get('inclusion_type') not in ('ADDITIVE','INCLUSIVE') or
                    type(data.get('enabled')) is not bool or data.get('calculation_phase') not in ('TAX_SUBTOTAL_PHASE','TAX_TOTAL_PHASE')):
                raise ReceivingError('The referenced tax rate, inclusion, calculation phase or enablement is unresolved. Review Square settings before template approval.')
            taxes.append({'id':ident,'name':data['name'],'percentage':percentage,'inclusion_type':data['inclusion_type'],
                          'enabled':data['enabled'],'calculation_phase':data['calculation_phase']})
        for ident in category_ids:
            obj=objects.get(ident);data=(obj or {}).get('category_data') or {}
            if not obj or obj.get('type')!='CATEGORY' or obj.get('is_deleted') or not isinstance(data.get('name'),str):raise ReceivingError('A referenced category cannot be read. Resolve it before template approval.')
            categories.append({'id':ident,'name':data['name']})
        facts={'binding_id':str(binding['id']),'binding_version':binding['version'],'identity':binding['identity'],'identity_hash':binding['identity_hash'],
               'base_sku':binding['sku'],'parent':{'id':parent['id'],'name':item.get('name','')},
               'location':{'id':binding['location_id'],'name':location.get('name',binding['location_id'])},
               'provider':{key:access.proof[key] for key in ('merchant_id','environment','application_id')},
               'variation_name_prefix':prefix,'taxes':taxes,'categories':categories,'taxability':item.get('is_taxable'),
               'policy_reference':'existing-parent-settings:'+parent['id']+':'+str(parent['version']),
               'parent_policy_hash':parent_policy_hash(parent,objects),'catalog_snapshot_hash':fingerprint({'parent':parent,'variation':variation}),
               'new_parent_creation':False,'receiving_enabled':False}
        return facts,access

    def preview(self,actor,data):
        keys(data,('binding_id','variation_name_prefix'));ident=uuid_text(data.get('binding_id'),'Binding ID');prefix=data.get('variation_name_prefix')
        if not isinstance(prefix,str) or not 1<=len(prefix.strip())<=80 or any(unicodedata.category(c) in ('Cc','Cf','Cs') for c in prefix):raise ServiceError('Use a readable variation name prefix of 1 to 80 characters.')
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',))
            binding=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s AND revoked_at IS NULL',(actor['tenant_id'],ident)).fetchone()
            if not binding:raise ServiceError('Approved catalog binding not found.',404)
        facts,access=self._facts(actor,binding,prefix.strip());preview=uuid.uuid4();hashed=fingerprint(facts)
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',));self.provider.assert_current(db,actor['tenant_id'],access)
            if not db.execute('SELECT 1 FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s AND revoked_at IS NULL FOR SHARE',(actor['tenant_id'],ident)).fetchone():raise ReceivingError('The source mapping was revoked.')
            db.execute("INSERT INTO ss_receiving_price_template_previews(tenant_id,id,binding_id,facts,preview_hash,created_by,expires_at) VALUES (%s,%s,%s,%s,%s,%s,clock_timestamp()+interval '15 minutes')",
                       (actor['tenant_id'],preview,ident,Jsonb(facts),hashed,actor['staff_id']))
        return {'preview_id':str(preview),'preview_hash':hashed,'expires_in':900,'facts':facts,'approval_required':True}

    def approve(self,actor,preview_id,data):
        allowed=('request_key','preview_hash','preserve_existing_tax_configuration_confirmed','preserve_existing_siblings_confirmed','source_identity_confirmed','approval_reason','policy_reference')
        keys(data,allowed)
        if any(data.get(key) is not True for key in allowed[2:5]):raise ServiceError('Confirm the existing tax settings, sibling preservation and exact source identity.')
        key=_key(data.get('request_key'));ident=uuid_text(preview_id,'Preview ID');reason=_reason(data.get('approval_reason'))
        intent=dict(data,preview_id=ident);intent.pop('request_key',None);text_hash(data.get('preview_hash'))
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',));_,old=self.journal._old(db,actor['tenant_id'],key,'template_approve',intent)
            if old:return old
            row=db.execute('SELECT * FROM ss_receiving_price_template_previews WHERE tenant_id=%s AND id=%s AND expires_at>clock_timestamp()',(actor['tenant_id'],ident)).fetchone()
            if not row or row['preview_hash']!=data['preview_hash'] or row['facts']['policy_reference']!=data.get('policy_reference'):raise ReceivingError('The template preview or policy reference changed/expired.')
            binding=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s AND revoked_at IS NULL',(actor['tenant_id'],row['binding_id'])).fetchone()
            if not binding:raise ReceivingError('The source mapping was revoked.')
        facts,access=self._facts(actor,binding,row['facts']['variation_name_prefix'])
        if fingerprint(facts)!=row['preview_hash']:raise ReceivingError('The current catalog/settings changed. Review a new template preview.')
        template=uuid.uuid4()
        with self.database.transaction() as db:
            current_authority(db,actor,fresh_mfa=True,allowed_roles=('owner',));self.provider.assert_current(db,actor['tenant_id'],access)
            hashed,old=self.journal._old(db,actor['tenant_id'],key,'template_approve',intent)
            if old:return old
            authority_lane(db,actor['tenant_id'])
            if not db.execute('SELECT 1 FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s AND revoked_at IS NULL FOR SHARE',(actor['tenant_id'],binding['id'])).fetchone():raise ReceivingError('The source mapping was revoked.')
            if not db.execute('SELECT 1 FROM ss_receiving_price_template_previews WHERE tenant_id=%s AND id=%s AND expires_at>clock_timestamp() FOR SHARE',(actor['tenant_id'],ident)).fetchone():raise ReceivingError('The template preview expired.')
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(actor['tenant_id'])+':price-template:'+facts['identity_hash']+':'+binding['location_id'],))
            existing=db.execute('SELECT * FROM ss_receiving_price_templates WHERE tenant_id=%s AND identity_hash=%s AND parent_id=%s AND location_id=%s AND approved=TRUE AND revoked_at IS NULL',(actor['tenant_id'],facts['identity_hash'],binding['parent_id'],binding['location_id'])).fetchone()
            if existing:
                if existing['facts']!=facts:raise ReceivingError('A different approved family template exists. Revoke/review it explicitly.')
                template=existing['id']
            else:
                db.execute('''INSERT INTO ss_receiving_price_templates(tenant_id,id,binding_id,identity_hash,parent_id,location_id,facts,approved,approved_by,approved_at,approval_reason)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s,clock_timestamp(),%s)''',
                           (actor['tenant_id'],template,binding['id'],facts['identity_hash'],binding['parent_id'],binding['location_id'],Jsonb(facts),actor['staff_id'],reason))
                audit(db,actor['tenant_id'],actor['staff_id'],'receiving-price-template-approved',template,{'policy_reference':facts['policy_reference']})
            return self.journal._finish(db,actor['tenant_id'],key,'template_approve',hashed,{'template_id':str(template),'version':1,'state':'approved','receiving_enabled':False,'new_parent_creation':False})

    def revoke(self,actor,template_id,data):
        keys(data,('request_key','version','reason'));key=_key(data.get('request_key'));version=integer(data.get('version'),'Template version',1)
        ident=uuid_text(template_id,'Template ID');reason=_reason(data.get('reason'));intent={'template_id':ident,'version':version,'reason':reason}
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',));hashed,old=self.journal._old(db,actor['tenant_id'],key,'template_revoke',intent)
            if old:return old
            current_authority(db,actor,fresh_mfa=True,allowed_roles=('owner',))
            authority_lane(db,actor['tenant_id'])
            row=db.execute('SELECT * FROM ss_receiving_price_templates WHERE tenant_id=%s AND id=%s FOR UPDATE',(actor['tenant_id'],ident)).fetchone()
            if not row:raise ServiceError('Price template not found.',404)
            if row['version']!=version:raise ReceivingError('The template version changed.')
            if row['revoked_at'] is None:
                revoke_delegated_authority(db,actor['tenant_id'],actor['staff_id'],reason,template_id=row['id'])
                audit(db,actor['tenant_id'],actor['staff_id'],'receiving-price-template-revoked',ident)
            return self.journal._finish(db,actor['tenant_id'],key,'template_revoke',hashed,{'template_id':ident,'version':version,'state':'revoked'})
