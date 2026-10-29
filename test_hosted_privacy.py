"""Owner/tenant/session/stream privacy boundaries with fictional data only."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib
import json
import unittest
import uuid
from unittest.mock import Mock
from cryptography.fernet import Fernet
from hosted.app import create_app
from hosted.privacy import Privacy, Limits, DATASETS, DATASET_NAMES, attestation
from hosted.privacy_test_fixtures import Database, request
from hosted.security import ServiceError


def decode(lines):
    chain=bytes(32);counts={name:0 for name in DATASET_NAMES};records={name:[] for name in DATASET_NAMES};manifest=footer=None
    for line in lines:
        value=json.loads(line)
        if value['kind']=='complete':
            footer=value
            assert footer['content_chain_sha256']==chain.hex()
        elif value['kind']=='error':return manifest,records,value
        else:
            chain=hashlib.sha256(chain+line).digest()
            if value['kind']=='manifest':manifest=value
            if value['kind']=='page':
                assert len(value['records'])==value['count']
                counts[value['dataset']]+=value['count'];records[value['dataset']]+=value['records']
    if footer:
        assert counts==footer['record_counts']==manifest['record_counts']
    return manifest,records,footer


class PrivacyExportTests(unittest.TestCase):
    def setUp(self):
        self.db=Database();self.actor=self.db.actor
        self.service=Privacy(self.db,authorize_session=self.db.resolve,proof_key='fictional-proof-key')
        self.data=request(self.actor,self.db.customer)

    def begin(self,**kwargs):return self.service.begin(kwargs.get('actor',self.actor),kwargs.get('customer',self.db.customer),kwargs.get('data',self.data),'fictional-token')

    def test_identity_purpose_case_and_representative_attestations_are_enforced(self):
        variants=[self.data|{key:False} for key in ('identity_verified','authority_confirmed','scope_reviewed','copy_handling_confirmed')]
        variants += [self.data|{'purpose':'marketing'},self.data|{'verification_method':'not-verified'},
                     self.data|{'confirmed_customer_id':str(uuid.uuid4())},self.data|{'expected_store':'other-store'},
                     self.data|{'case_reference':'email@example.invalid'},self.data|{'verification_method':'authorized_representative'},
                     self.data|{'extra_payload':'not permitted'}]
        for data in variants:
            with self.subTest(data=data),self.assertRaises(ServiceError):self.begin(data=data)
        valid=self.data|{'verification_method':'authorized_representative','agent_authority_verified':True}
        self.assertTrue(attestation(valid,self.actor,self.db.customer)['agent_authority_verified'])
        self.assertFalse(self.db.audits);self.assertEqual(self.db.active_transactions,0)

    def test_only_current_owner_session_and_current_enrolled_mfa_can_export(self):
        for role in ('cashier','buyer','manager'):
            with self.subTest(role=role),self.assertRaises(ServiceError) as error:self.begin(actor=self.actor|{'role':role})
            self.assertEqual(error.exception.status,403)
        self.db.current_role='manager'
        with self.assertRaises(ServiceError):self.begin()
        self.db.current_role='owner';self.db.actor['mfa_enabled']=True
        with self.assertRaises(ServiceError):self.begin()
        self.db.actor['mfa_authenticated']=True
        output=self.begin();output.close()
        self.assertEqual(self.db.active_transactions,0)

    def test_recent_signin_uses_mfa_proof_or_session_creation_and_rechecks_age(self):
        instant=datetime(2026,10,28,20,0,tzinfo=timezone.utc)
        self.service.wall_clock=lambda:instant
        for enrolled in (False,True):
            self.db.actor.update(mfa_enabled=enrolled,mfa_authenticated=enrolled)
            field='mfa_authenticated_at' if enrolled else 'session_created_at'
            for proof,allowed in ((instant-timedelta(seconds=899),True),(instant-timedelta(seconds=901),False),
                                   (instant+timedelta(seconds=6),False),(None,False)):
                self.db.actor[field]=proof
                with self.subTest(enrolled=enrolled,proof=proof):
                    if allowed:
                        output=self.begin();output.close()
                    else:
                        with self.assertRaises(ServiceError) as error:self.begin()
                        self.assertEqual(error.exception.status,401)
        self.db.actor.update(mfa_enabled=False,mfa_authenticated=False,session_created_at=instant-timedelta(seconds=899))
        output=self.begin();self.service.wall_clock=lambda:instant+timedelta(seconds=2)
        lines=list(output.iter_bytes())
        self.assertEqual(len(lines),1);self.assertEqual(json.loads(lines[0])['code'],'authorization_changed')

    def test_full_more_than_100_history_count_hash_and_scoped_projection(self):
        self.db.rows['idempotency_records']=[{'key':'fixture-key','result':{'customer_id':str(self.db.customer),
            'access_token':'DO-NOT-EXPORT-TOKEN','password_hash':'DO-NOT-EXPORT-PASSWORD','nested':{'refresh_token':'DO-NOT-EXPORT-REFRESH'}}}]
        self.db.rows['audit']=[{'id':1,'action':'credit-adjusted','details':{'amount_cents':1,'session_binding':'DO-NOT-EXPORT-SESSION','unrelated_customer_contact':'DO-NOT-EXPORT-OTHER'}}]
        output=self.begin();lines=list(output.iter_bytes());manifest,records,footer=decode(lines)
        self.assertEqual(len(records['credit_entries']),275)
        self.assertEqual(footer['record_counts']['credit_entries'],275)
        self.assertGreater(footer['redacted_fields'],0)
        body=b''.join(lines)
        for sentinel in (b'DO-NOT-EXPORT-TOKEN',b'DO-NOT-EXPORT-PASSWORD',b'DO-NOT-EXPORT-REFRESH',b'DO-NOT-EXPORT-SESSION',b'DO-NOT-EXPORT-OTHER'):
            self.assertNotIn(sentinel,body)
        self.assertTrue(all(size==1 for size in self.db.fetch_sizes))
        self.assertEqual((self.db.open_cursors,self.db.active_transactions),(0,0))
        self.assertLessEqual(self.db.peak_transactions,2)
        details=json.dumps([row['details'] for row in self.db.audits])
        self.assertNotIn('fictional-rights@example.invalid',details)
        self.assertNotIn('FICTIONAL-CASE-001',details)
        self.assertFalse(any(r['action']=='customer-export-client-verified' for r in self.db.audits))
        confirmation={key:footer[key] for key in ('start_audit_id','content_chain_sha256','record_counts')}
        result=self.service.confirm(self.actor,output.id,confirmation,'fictional-token')
        self.assertTrue(result['verified']);self.assertFalse(result['delivery_confirmed'])
        self.assertTrue(self.service.confirm(self.actor,output.id,confirmation,'fictional-token')['already_verified'])
        self.assertEqual(sum(r['action']=='customer-export-client-verified' for r in self.db.audits),1)

    def test_consistent_snapshot_excludes_later_writes_without_mutating_source(self):
        original=deepcopy(self.db.rows);output=self.begin()
        self.db.rows['credit_entries'].append({'id':uuid.uuid4(),'customer_id':self.db.customer,'reference':'LATER-WRITE'})
        _,records,footer=decode(list(output.iter_bytes()))
        self.assertEqual(len(records['credit_entries']),275)
        self.assertEqual(footer['record_counts']['credit_entries'],275)
        self.assertEqual(self.db.rows['customer'],original['customer'])

    def test_multi_large_records_split_by_page_bytes_without_loss_or_truncation(self):
        self.db.rows['credit_entries']=self.db.rows['credit_entries'][:7]
        for row in self.db.rows['credit_entries']:row['reference']='X'*(700*1024)
        output=self.begin();lines=list(output.iter_bytes());_,records,footer=decode(lines)
        pages=[(line,json.loads(line)) for line in lines if json.loads(line)['kind']=='page']
        entry_pages=[value for _,value in pages if value['dataset']=='credit_entries']
        self.assertEqual([page['count'] for page in entry_pages],[2,2,2,1])
        self.assertEqual([row['id'] for row in records['credit_entries']], [str(row['id']) for row in self.db.rows['credit_entries']])
        self.assertEqual(footer['record_counts']['credit_entries'],7)
        self.assertTrue(all(len(line)<=self.service.limits.max_page_bytes for line,_ in pages))
        self.assertTrue(all(size==1 for size in self.db.fetch_sizes))

    def test_interruption_closes_snapshot_and_cursor_and_never_verifies(self):
        output=self.begin();iterator=output.iter_bytes();next(iterator);next(iterator);iterator.close();output.close()
        self.assertEqual((self.db.open_cursors,self.db.active_transactions),(0,0))
        self.assertEqual(self.db.audits[-1]['details']['outcome'],'interrupted')
        forged={'start_audit_id':output.start_audit_id,'content_chain_sha256':output.chain.hex(),'record_counts':output.counts}
        with self.assertRaises(ServiceError):self.service.confirm(self.actor,output.id,forged,'fictional-token')
        self.assertFalse(any(r['action']=='customer-export-client-verified' for r in self.db.audits))
        other=self.begin();other.close()

    def test_revocation_during_fetch_prevents_late_customer_payload(self):
        output=self.begin()
        self.db.on_fetch=lambda *_:setattr(self.db,'session_active',False)
        lines=list(output.iter_bytes());body=b''.join(lines)
        self.assertNotIn(b'fictional-rights@example.invalid',body)
        self.assertEqual(json.loads(lines[-1])['kind'],'error')
        self.assertEqual(json.loads(lines[-1])['code'],'authorization_changed')
        self.assertEqual(self.db.audits[-1]['details']['outcome'],'interrupted')
        self.assertEqual((self.db.open_cursors,self.db.active_transactions),(0,0))

    def test_new_session_cannot_confirm_old_export_and_wrong_hash_never_verifies(self):
        output=self.begin();_,_,footer=decode(list(output.iter_bytes()))
        confirmation={key:footer[key] for key in ('start_audit_id','content_chain_sha256','record_counts')}
        with self.assertRaises(ServiceError):self.service.confirm(self.actor,output.id,confirmation|{'content_chain_sha256':'0'*64},'fictional-token')
        self.db.actor=self.actor|{'session_hash':'different-session'}
        with self.assertRaises(ServiceError):self.service.confirm(self.db.actor,output.id,confirmation,'new-token')
        self.assertFalse(any(r['action']=='customer-export-client-verified' for r in self.db.audits))

    def test_snapshot_count_mismatch_or_foreign_customer_json_has_no_complete_footer(self):
        self.db.drop_last=True
        output=self.begin();_,_,last=decode(list(output.iter_bytes()))
        self.assertEqual(last['kind'],'error')
        self.db.drop_last=False
        self.db.rows['trade_receipts']=[{'customer_id':self.db.customer,'accepted_receipt':{'customer':{'id':str(uuid.uuid4()),'display_name':'OTHER-CUSTOMER-SENTINEL'}}}]
        output=self.begin();lines=list(output.iter_bytes())
        self.assertEqual(json.loads(lines[-1])['kind'],'error')
        self.assertNotIn(b'OTHER-CUSTOMER-SENTINEL',b''.join(lines))

    def test_pool_and_concurrency_limits_release_without_deadlock(self):
        self.db._max_size=2
        with self.assertRaises(ServiceError) as error:self.begin()
        self.assertEqual(error.exception.status,503)
        self.db._max_size=5;output=self.begin()
        with self.assertRaises(ServiceError) as error:self.begin()
        self.assertEqual(error.exception.status,429)
        output.close();other=self.begin();other.close()
        self.assertEqual(self.db.active_transactions,0)

    def test_integrity_oversize_and_timer_fail_explicitly_without_complete_export(self):
        self.db.integrity_failure=True
        with self.assertRaises(ServiceError):self.begin()
        self.assertEqual(self.db.active_transactions,0)
        self.db.integrity_failure=False
        self.service.limits=Limits(max_record_bytes=1024)
        self.db.rows['credit_entries'][0]['reference']='X'*2048
        output=self.begin();lines=list(output.iter_bytes())
        self.assertEqual(json.loads(lines[-1])['code'],'engineering_limit')
        self.assertNotIn(b'X'*2048,b''.join(lines))
        self.service.clock=Mock(return_value=0);output=self.begin();self.service.clock.return_value=121
        lines=list(output.iter_bytes());self.assertEqual(len(lines),1);self.assertEqual(json.loads(lines[0])['kind'],'error')

    def test_stream_end_audit_failure_cannot_be_confirmed(self):
        output=self.begin();self.db.fail_audit='customer-export-stream-ended'
        with self.assertRaises(ServiceError):list(output.iter_bytes())
        self.assertEqual(self.db.active_transactions,0)
        self.assertFalse(any(r['action']=='customer-export-client-verified' for r in self.db.audits))

    def test_catalog_has_explicit_fields_and_no_authentication_tables(self):
        for item in DATASETS:
            with self.subTest(dataset=item.name):
                self.assertNotIn('SELECT *',item.query)
                self.assertIn('%(tenant)s',item.query)
                for table in ('ss_staff','ss_sessions','ss_staff_mfa','ss_square_connections','ss_invites','ss_oauth_states'):
                    self.assertNotIn(table,item.query)


class PrivacyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.db=Database();self.actor=self.db.actor
        self.app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://privacy.example.test',
                             'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=self.db)
        self.auth=self.app.extensions['ss_auth'];self.auth.resolve=Mock(side_effect=self.db.resolve)
        self.client=self.app.test_client();self.path='/api/privacy/customers/'+str(self.db.customer)+'/export'
        self.headers={'Authorization':'Bearer fictional-token'}

    def post(self,path=None,**kwargs):return self.client.post(path or self.path,base_url='https://privacy.example.test',**kwargs)

    def test_role_tenant_csrf_and_origin_boundaries_have_no_export_payload(self):
        for role in ('manager','buyer','cashier'):
            self.db.current_role=role
            result=self.post(json=request(self.actor,self.db.customer),headers=self.headers)
            self.assertEqual(result.status_code,403);self.assertNotIn('fictional-rights@example.invalid',result.text)
        self.db.current_role='owner'
        self.assertEqual(self.post(json=request(self.actor,self.db.customer)).status_code,403)
        self.assertEqual(self.post(json=request(self.actor,self.db.customer),headers=self.headers|{'Origin':'https://other.test'}).status_code,403)
        self.assertEqual(self.post('/api/privacy/customers/'+str(uuid.uuid4())+'/export',json=request(self.actor,self.db.customer)|{'confirmed_customer_id':str(uuid.uuid4())},headers=self.headers).status_code,409)
        stranger=uuid.uuid4()
        self.assertEqual(self.post('/api/privacy/customers/'+str(stranger)+'/export',json=request(self.actor,stranger),headers=self.headers).status_code,404)

    def test_stream_headers_full_records_and_confirmation(self):
        response=self.post(json=request(self.actor,self.db.customer),headers=self.headers,buffered=True)
        self.assertEqual(response.status_code,200)
        self.assertIn('application/x-ndjson',response.content_type)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertIn('attachment;',response.headers['Content-Disposition'])
        self.assertNotIn('Fictional rights',response.headers['Content-Disposition'])
        manifest,records,footer=decode(response.data.splitlines(keepends=True))
        self.assertEqual(len(records['credit_entries']),275)
        confirmed=self.post('/api/privacy/exports/'+manifest['export_id']+'/confirm',json={key:footer[key] for key in ('start_audit_id','content_chain_sha256','record_counts')},headers=self.headers)
        self.assertEqual(confirmed.status_code,200);self.assertTrue(confirmed.json['verified'])
        response.close();self.assertEqual(self.db.active_transactions,0)

    def test_unconsumed_response_close_and_late_lookup_revocation(self):
        response=self.post(json=request(self.actor,self.db.customer),headers=self.headers,buffered=False)
        response.close();self.assertEqual(self.db.active_transactions,0)
        def late_lookup(*_):
            self.db.session_active=False
            return [{'id':self.db.customer,'display_name':'NO-LATE-CUSTOMER-NAME'}]
        self.app.extensions['ss_ledger'].customers=Mock(side_effect=late_lookup)
        result=self.client.get('/api/privacy/customers?q=fixture',base_url='https://privacy.example.test',headers=self.headers)
        self.assertEqual(result.status_code,401);self.assertNotIn('NO-LATE-CUSTOMER-NAME',result.text)


if __name__=='__main__':unittest.main()
