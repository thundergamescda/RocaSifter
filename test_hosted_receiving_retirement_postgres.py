"""Atomic original-request retirement; never unlock from generic HTTP failure."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import unittest
import uuid
from unittest.mock import patch
from hosted.receiving_plan import ReceivingError, fingerprint
from hosted.security import ServiceError
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_receiving import plan_data
import test_hosted_receiving_postgres as fixture


class PostgreSQLReceivingRetirementAcceptance(_PostgreSQLSettlementFixture):
    approve=fixture.PostgreSQLReceivingAcceptance.approve
    terms=fixture.PostgreSQLReceivingAcceptance.terms
    commit=fixture.PostgreSQLReceivingAcceptance.commit
    def setUp(self):fixture.PostgreSQLReceivingAcceptance.setUp(self)

    def retire(self,body,actor=None):
        return self.service.retire_plan_request(actor or self.actor,body['request_key'],{'original_plan':deepcopy(body),'retire_uncreated_request':True})

    def count_operations(self):
        with self.database.transaction() as db:return db.execute('SELECT count(*) AS n FROM ss_receiving_operations WHERE tenant_id=%s',(self.tenant,)).fetchone()['n']

    def test_missing_mapping_rejection_needs_matching_atomic_tombstone_then_new_correction_key(self):
        body=plan_data(self.claim);body['lines'][0]['selling_cents']=1250
        with self.assertRaises(ReceivingError):self.service.create_plan(self.actor,body)
        self.assertFalse(self.service.request_status(self.actor,body['request_key'])['found'])
        with self.database.transaction() as db:
            pending=db.execute('SELECT * FROM ss_receiving_plan_intents WHERE tenant_id=%s AND request_key=%s',(self.tenant,body['request_key'])).fetchone()
        self.assertEqual((pending['wire_hash'],pending['workflow_kind']),(fingerprint(body),'inventory_only'))
        self.assertIsNone(pending['accepted_trade_id']);self.assertIsNone(pending['accepted_receipt_hash'])
        proof=self.retire(body)
        self.assertEqual((proof['retired'],proof['retirement_basis'],proof['original_plan_sha256']),(True,'registered_original_wire',fingerprint(body)))
        self.assertEqual(self.service.request_status(self.actor,body['request_key']),{'found':True,'result':proof})
        self.assertEqual(self.retire(body),proof)
        with self.assertRaises(ReceivingError) as caught:self.service.create_plan(self.actor,body)
        self.assertEqual(caught.exception.code,'PLAN_REQUEST_RETIRED')
        fixed=deepcopy(body);fixed['request_key']='explicit-corrected-new-key';fixed['lines'][0]['selling_cents']=1000
        self.assertEqual(self.service.create_plan(self.actor,fixed)['state'],'ready');self.assertEqual(self.provider.calls,[])

    def test_retirement_during_inflight_preflight_prevents_late_creation(self):
        body=plan_data(self.claim);entered=threading.Event();release=threading.Event();original=self.service._prepare
        def held(*args):
            entered.set()
            if not release.wait(5):raise RuntimeError('Fictional preflight barrier expired.')
            return original(*args)
        with ThreadPoolExecutor(max_workers=1) as pool,patch.object(self.service,'_prepare',side_effect=held):
            future=pool.submit(self.service.create_plan,self.actor,body)
            try:
                self.assertTrue(entered.wait(5));proof=self.retire(body)
                self.assertEqual(proof['state'],'retired_before_creation');self.assertEqual(self.count_operations(),0)
            finally:release.set()
            with self.assertRaises(ReceivingError) as caught:future.result(5)
            self.assertEqual(caught.exception.code,'PLAN_REQUEST_RETIRED')
        self.assertEqual((self.count_operations(),self.provider.calls),(0,[]))

    def test_changed_original_wire_cannot_retire_registered_or_inflight_intent(self):
        body=plan_data(self.claim);body['lines'][0]['selling_cents']=1250
        with self.assertRaises(ReceivingError):self.service.create_plan(self.actor,body)
        changed=deepcopy(body);changed['client_label_profile']['stock']='4x2'
        with self.assertRaises(ReceivingError) as caught:self.retire(changed)
        self.assertEqual(caught.exception.code,'PLAN_CHANGED')
        self.assertFalse(self.service.request_status(self.actor,body['request_key'])['found'])
        self.assertTrue(self.retire(body)['retired']);self.assertEqual(self.count_operations(),0)

    def test_other_pc_operation_wins_claim_lock_and_returns_recoverable_id(self):
        body=plan_data(self.claim);entered=threading.Event();release=threading.Event();original=self.service._prepare
        def held(actor,plan):
            if plan['source_snapshot_hash']=='b'*64:
                entered.set()
                if not release.wait(5):raise RuntimeError('Fictional barrier expired.')
            return original(actor,plan)
        copied=deepcopy(body);copied['request_key']='other-pc-wins';copied['source_snapshot_hash']='c'*64;copied['lines'][0]['review_line_id']=str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=1) as pool,patch.object(self.service,'_prepare',side_effect=held):
            future=pool.submit(self.service.create_plan,self.actor,body)
            try:
                self.assertTrue(entered.wait(5));operation=self.service.create_plan(self.actor,copied)
                with self.assertRaises(ReceivingError) as caught:self.retire(body)
                self.assertEqual((caught.exception.code,caught.exception.receiving_id),('CLAIM_WORKFLOW_CONFLICT',operation['receiving_id']))
            finally:release.set()
            with self.assertRaises(ReceivingError) as late:future.result(5)
            self.assertEqual(late.exception.receiving_id,operation['receiving_id'])
        self.assertFalse(self.service.request_status(self.actor,body['request_key'])['found']);self.assertEqual(self.count_operations(),1)

    def test_created_or_uncertain_admitted_operation_cannot_be_retired(self):
        body=plan_data(self.claim);plan=self.service.create_plan(self.actor,body)
        self.provider.behavior='lost_response';self.assertEqual(self.commit(plan)['state'],'outcome_unknown')
        with self.assertRaises(ReceivingError) as caught:self.retire(body)
        self.assertEqual((caught.exception.code,caught.exception.receiving_id),('CLAIM_WORKFLOW_CONFLICT',plan['receiving_id']))
        self.assertEqual(self.service.get(self.actor,plan['receiving_id'])['state'],'outcome_unknown')
        self.assertEqual(len(self.provider.calls),1)
        self.assertFalse(self.service.request_status(self.actor,body['request_key'])['result'].get('retired',False))

    def test_unregistered_invalid_body_retirement_is_explicit_and_permanent_for_key(self):
        body=plan_data(self.claim);body['lines'][0]['quantity']=True
        with self.assertRaises(ServiceError):self.service.create_plan(self.actor,body)
        proof=self.retire(body)
        self.assertEqual((proof['retirement_basis'],proof['original_plan_sha256']),('unregistered_key_no_operation',fingerprint(body)))
        corrected=deepcopy(body);corrected['lines'][0]['quantity']=2
        for payload in (body,corrected):
            with self.assertRaises(ReceivingError) as caught:self.service.create_plan(self.actor,payload)
            self.assertEqual(caught.exception.code,'PLAN_REQUEST_RETIRED')
        self.assertEqual(self.count_operations(),0);self.assertEqual(self.provider.calls,[])

    def test_retirement_requires_current_role_tenant_and_exact_path_key_without_fresh_proof(self):
        body=plan_data(self.claim);body['lines'][0]['selling_cents']=1250
        with self.assertRaises(ReceivingError):self.service.create_plan(self.actor,body)
        for role in ('buyer','cashier'):
            with self.subTest(role=role),self.assertRaises(ServiceError):self.retire(body,self.actors[role])
        with self.assertRaises(ServiceError):self.retire(body,self.actor|{'tenant_id':uuid.uuid4()})
        with self.assertRaises(ServiceError):self.service.retire_plan_request(self.actor,'wrong-path-key',{'original_plan':body,'retire_uncreated_request':True})
        with self.database.transaction() as db:db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '20 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        self.assertTrue(self.retire(body)['retired']);self.assertEqual(self.provider.calls,[])
