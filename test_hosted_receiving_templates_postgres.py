"""Actual owner template authority/pagination in disposable fixture schemas."""
from copy import deepcopy
import json
import unittest
import uuid
from unittest.mock import patch
from psycopg.types.json import Jsonb

import test_hosted_receiving_postgres as receiving_fixtures
import test_hosted_settlement_postgres as postgres_fixtures
from hosted.receiving import Receiving
from hosted.receiving_plan import ReceivingError
from hosted.security import ServiceError
from test_hosted_receiving_templates import approval
from test_intake_fixtures import fresh_mfa_actor
from test_receiving_fixtures import FictionalReceivingProvider


class PostgreSQLReceivingTemplateTests(postgres_fixtures._PostgreSQLSettlementFixture):
    setUp=receiving_fixtures.PostgreSQLReceivingAcceptance.setUp
    approve=receiving_fixtures.PostgreSQLReceivingAcceptance.approve

    def template_preview(self):
        return self.service.templates.preview(self.actor,{'binding_id':self.binding['binding_id'],'variation_name_prefix':'Reviewed sticker'})

    def count(self):
        with self.database.transaction() as db:
            return db.execute('SELECT count(*) AS n FROM ss_receiving_price_templates WHERE tenant_id=%s',(self.tenant,)).fetchone()['n']

    def test_owner_fresh_approval_is_idempotent_and_preserves_disabled_write_flags_and_catalog(self):
        self.service.writes_enabled=False
        with self.database.transaction() as db:db.execute('UPDATE ss_receiving_settings SET enabled=FALSE WHERE tenant_id=%s',(self.tenant,))
        original=deepcopy(self.provider.objects);preview=self.template_preview();body=approval(preview)
        first=self.service.templates.approve(self.actor,preview['preview_id'],body)
        self.assertEqual(self.service.templates.approve(self.actor,preview['preview_id'],body),first)
        self.assertEqual(self.count(),1);self.assertFalse(first['receiving_enabled']);self.assertFalse(first['new_parent_creation'])
        self.assertEqual(self.provider.objects,original);self.assertEqual(self.provider.calls,[])
        readiness=self.service.readiness(self.actor)
        self.assertFalse(readiness['write_enabled']);self.assertFalse(readiness['new_parent_enabled'])
        row=self.service.templates.list(self.actor)['templates'][0]
        self.assertEqual(row['facts']['taxes'][0]['percentage'],'6')
        self.assertNotIn('fictional-server-token',json.dumps(row))

    def test_current_owner_role_and_enrolled_fresh_mfa_are_required_for_approval_and_revocation(self):
        preview=self.template_preview();body=approval(preview)
        for role in ('manager','buyer','cashier'):
            actor=self.actors[role]
            for action in (lambda:self.service.templates.list(actor),lambda:self.service.templates.bindings(actor),
                           lambda:self.service.templates.preview(actor,{'binding_id':self.binding['binding_id'],'variation_name_prefix':'Reviewed'}),
                           lambda:self.service.templates.approve(actor,preview['preview_id'],body)):
                with self.subTest(role=role),self.assertRaises(ServiceError) as error:action()
                self.assertEqual(error.exception.status,403)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        with self.assertRaises(ServiceError):self.service.templates.approve(self.actor|{'mfa_authenticated':True},preview['preview_id'],body)
        self.assertEqual(self.count(),0)
        with self.database.transaction() as db:fresh_mfa_actor(db,self.actor,existing_session=True)
        template=self.service.templates.approve(self.actor,preview['preview_id'],body)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        with self.assertRaises(ServiceError):self.service.templates.revoke(self.actor,template['template_id'],{'request_key':'stale-revoke','version':1,'reason':'Fictional stale proof is insufficient'})

    def test_expired_preview_changed_policy_reference_and_current_tax_settings_block_approval(self):
        preview=self.template_preview()
        with self.assertRaises(ReceivingError):self.service.templates.approve(self.actor,preview['preview_id'],approval(preview)|{'policy_reference':'different-policy'})
        self.provider.objects['fixture-tax']['tax_data']['percentage']='7'
        with self.assertRaises(ReceivingError):self.service.templates.approve(self.actor,preview['preview_id'],approval(preview))
        self.provider.objects['fixture-tax']['tax_data']['percentage']='6'
        with self.database.transaction() as db:
            db.execute("UPDATE ss_receiving_price_template_previews SET expires_at=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s",(self.tenant,preview['preview_id']))
        with self.assertRaises(ReceivingError):self.service.templates.approve(self.actor,preview['preview_id'],approval(preview))
        self.assertEqual(self.count(),0);self.assertEqual(self.provider.calls,[])

    def test_mapping_revoked_during_preflight_prevents_preview_storage(self):
        original=self.provider.catalog;revoked=[False]
        def read_then_revoke(access,ids):
            result=original(access,ids)
            if not revoked[0]:
                revoked[0]=True
                self.service.revoke_binding(self.actor,self.binding['binding_id'],{'request_key':'revoke-during-template','version':1,
                    'reason':'Fictional owner revoked during template read'})
            return result
        with patch.object(self.provider,'catalog',side_effect=read_then_revoke),self.assertRaises(ReceivingError):self.template_preview()
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_price_template_previews WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],0)
        self.assertEqual(self.provider.calls,[])

    def test_revocation_preserves_template_history_and_is_idempotent(self):
        preview=self.template_preview();template=self.service.templates.approve(self.actor,preview['preview_id'],approval(preview))
        generated=uuid.uuid4()
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_receiving_bindings(tenant_id,id,identity,identity_hash,provider,variation_id,parent_id,sku,location_id,selling_cents,identity_anchor_hash,evidence_hash,approved_by,approval_reason,approval_source,template_id)
                          SELECT tenant_id,%s,identity,identity_hash,provider,'generated-read-fixture',parent_id,'000000001250',location_id,1250,identity_anchor_hash,evidence_hash,approved_by,
                          'Fictional generated binding authority fixture','price_template',%s FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s''',
                       (generated,template['template_id'],self.tenant,self.binding['binding_id']))
        body={'request_key':'revoke-approved-template','version':1,'reason':'Fictional owner stopped new price authority'}
        first=self.service.templates.revoke(self.actor,template['template_id'],body)
        self.assertEqual(self.service.templates.revoke(self.actor,template['template_id'],body),first)
        self.assertEqual(self.service.templates.list(self.actor)['templates'][0]['state'],'revoked')
        self.assertEqual(self.count(),1);self.assertEqual(self.provider.calls,[])
        with self.database.transaction() as db:
            generated_row=db.execute('SELECT revoked_at FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s',(self.tenant,generated)).fetchone()
            source_row=db.execute('SELECT revoked_at FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s',(self.tenant,self.binding['binding_id'])).fetchone()
        self.assertIsNotNone(generated_row['revoked_at']);self.assertIsNone(source_row['revoked_at'])
        with self.assertRaises(ReceivingError):self.service.templates.revoke(self.actor,template['template_id'],body|{'request_key':'wrong-version-revoke','version':2})

    def test_paginated_bindings_use_literal_search_and_owner_tenant_scope(self):
        with self.database.transaction() as db:
            original=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s',(self.tenant,self.binding['binding_id'])).fetchone()
            # Read-pagination fixtures only; these records never authorize stock.
            for index in range(121):
                db.execute('''INSERT INTO ss_receiving_bindings(tenant_id,id,identity,identity_hash,provider,variation_id,parent_id,sku,location_id,selling_cents,identity_anchor_hash,evidence_hash,approved_by,approval_reason)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1000,%s,%s,%s,'Fictional pagination fixture only')''',
                           (self.tenant,uuid.uuid4(),Jsonb(original['identity']),f'{index+1:064x}',Jsonb(original['provider']),
                            'read-only-fixture-'+str(index),original['parent_id'],'OWN%SKU-'+str(index),original['location_id'],
                            original['identity_anchor_hash'],original['evidence_hash'],self.actor['staff_id']))
        seen=[];cursor=None
        while True:
            page=self.service.templates.bindings(self.actor,'%',limit=17,cursor=cursor)
            seen.extend(row['binding_id'] for row in page['bindings']);cursor=page['next_cursor']
            if not cursor:break
        self.assertEqual(len(seen),121);self.assertEqual(len(set(seen)),121)
        self.assertEqual(self.service.templates.bindings(self.actor,'_',limit=100)['bindings'],[])
        for value in (0,101,True):
            with self.assertRaises(ServiceError):self.service.templates.bindings(self.actor,limit=value)
        self.assertEqual(self.provider.calls,[])

    def test_template_pagination_and_foreign_ids_cannot_cross_tenants(self):
        preview=self.template_preview();own=self.service.templates.approve(self.actor,preview['preview_id'],approval(preview))
        foreign_tenant=uuid.uuid4();foreign={'tenant_id':foreign_tenant,'staff_id':uuid.uuid4(),'role':'owner'}
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional other template store')",(foreign_tenant,'other-'+foreign_tenant.hex))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'other-template@example.invalid','fictional-invalid','owner')",(foreign['staff_id'],foreign_tenant))
            foreign=fresh_mfa_actor(db,foreign)
        provider=FictionalReceivingProvider(self.database,foreign_tenant);service=Receiving(self.database,provider,self.cipher)
        identity=self.original['identity'];mapping=service.binding_preview(foreign,{'identity':identity,'variation_id':'fixture-variation','location_id':'fixture-location'})
        binding=service.approve_binding(foreign,mapping['preview_id'],{'request_key':'foreign-binding','preview_hash':mapping['preview_hash'],'identity_confirmed':True,'reason':'Fictional other owner reviewed mapping'})
        other_preview=service.templates.preview(foreign,{'binding_id':binding['binding_id'],'variation_name_prefix':'OTHER-TENANT-TEMPLATE'})
        other=service.templates.approve(foreign,other_preview['preview_id'],approval(other_preview))
        with self.database.transaction() as db:
            for index in range(112):
                db.execute('''INSERT INTO ss_receiving_price_templates(tenant_id,id,binding_id,identity_hash,parent_id,location_id,facts,approved,approved_by,approved_at,approval_reason)
                              VALUES (%s,%s,%s,%s,'fixture-parent','fixture-location',%s,TRUE,%s,clock_timestamp(),'Fictional list pagination only')''',
                           (self.tenant,uuid.uuid4(),self.binding['binding_id'],f'{index+1:064x}',Jsonb({'fixture':'own-template-'+str(index)}),self.actor['staff_id']))
        rows=[];cursor=None
        while True:
            page=self.service.templates.list(self.actor,limit=13,cursor=cursor);rows+=page['templates'];cursor=page['next_cursor']
            if not cursor:break
        self.assertEqual(len(rows),113);self.assertEqual(len({row['template_id'] for row in rows}),113)
        self.assertNotIn(other['template_id'],{row['template_id'] for row in rows})
        self.assertNotIn('OTHER-TENANT-TEMPLATE',json.dumps(rows))
        with self.assertRaises(ServiceError):self.service.templates.approve(self.actor,other_preview['preview_id'],approval(other_preview))
        with self.assertRaises(ServiceError):self.service.templates.revoke(self.actor,other['template_id'],{'request_key':'foreign-id-revoke','version':1,'reason':'A foreign ID cannot supply store authority'})
        self.assertEqual(self.provider.calls,[]);self.assertEqual(provider.calls,[])


if __name__=='__main__':unittest.main(verbosity=2)
