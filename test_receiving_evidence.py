"""Pin actual fictional Flask/PG wire projections; no provider/session data."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
import uuid
from hosted_labels import fingerprint
from receiving_evidence import catalog_evidence,final_targets
import test_hosted_receive as desktop_fixture
import roca_to_square as core
from station_cloud import CloudClient
from unittest.mock import patch

FIXTURE=Path(__file__).parent/'testdata'/'receiving_api_contract_v1.json'


class ActualAPIReceivingEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.fixture=json.loads(FIXTURE.read_text(encoding='utf-8'));cls.cases=cls.fixture['cases']

    def test_source_attribution_and_reviewed_fictional_projection_scope(self):
        provenance=self.fixture['provenance'];self.assertEqual(provenance['source_sha256'],'773ebcc1d32125453ea59a5ec957672d71855f4009bef0062da54ffa4ca60675')
        self.assertTrue(provenance['source_stable']);self.assertEqual(len(self.cases),4)
        self.assertNotIn('access_token',FIXTURE.read_text(encoding='utf-8'));self.assertNotIn('csrf_token',FIXTURE.read_text(encoding='utf-8'))

    def test_all_four_actual_api_sequences_match_strict_catalog_and_final_target_proof(self):
        for case in self.cases:
            with self.subTest(case=case['name']):
                targets=case['ready']['targets'];self.assertFalse(catalog_evidence(case['ready'],targets)['catalog_confirmed'])
                if 'catalog_unknown' in case:self.assertFalse(catalog_evidence(case['catalog_unknown'],targets)['catalog_confirmed'])
                confirmed=catalog_evidence(case['catalog_confirmed'],targets);self.assertTrue(confirmed['catalog_confirmed'])
                self.assertEqual(confirmed,catalog_evidence(case['received'],targets))
                expected=[dict(row,confirmed_quantity=row['quantity']) for row in final_targets(case['received'],targets)]
                self.assertEqual(expected,case['received']['receipt']['confirmed_targets'])
                proof=case['received']['receipt'];self.assertEqual(proof['receipt_hash'],fingerprint({key:value for key,value in proof.items() if key!='receipt_hash'}))
                for key in ('catalog_proof_hash','catalog_resolutions_hash','catalog_resolutions'):self.assertEqual(proof[key],confirmed[key])

    def test_coalesced_rows_resolve_every_original_review_id_without_positional_assumptions(self):
        case=next(row for row in self.cases if row['name']=='inventory_coalesced');value=copy.deepcopy(case['received']);targets=case['ready']['targets']
        self.assertEqual(len(targets),2);self.assertEqual(len(value['catalog_resolutions']),1)
        aliases=value['catalog_resolutions'][0]['review_line_ids'];self.assertEqual(set(aliases),{row['review_line_id'] for row in targets})
        self.assertEqual({row['variation_id'] for row in final_targets(value,list(reversed(targets)))},{value['catalog_resolutions'][0]['variation_id']})
        value['catalog_resolutions'][0]['review_line_ids']=aliases[:1];value['catalog_resolutions_hash']=fingerprint(value['catalog_resolutions'])
        with self.assertRaises(ValueError):catalog_evidence(value,targets)

    def test_forged_resolution_cannot_change_identity_sku_price_template_alias_or_resolved_binding(self):
        original=self.cases[0]['received'];targets=self.cases[0]['ready']['targets']
        mutations={'identity_hash':'f'*64,'target_sku':'123456789012','parent_id':'other-parent','selling_cents':999,
            'template_id':str(uuid.uuid4()),'template_version':999,'variation_id':'#temporary','binding_id':'not-a-uuid',
            'binding_version':0,'binding_evidence_hash':'not-a-hash','review_line_ids':[str(uuid.uuid4())],'currency':'CAD'}
        for field,value in mutations.items():
            changed=copy.deepcopy(original);changed['catalog_resolutions'][0][field]=value;changed['catalog_resolutions_hash']=fingerprint(changed['catalog_resolutions'])
            with self.subTest(field=field),self.assertRaises(ValueError):catalog_evidence(changed,targets)


class ActualAPINativeDesktopReplayTests(unittest.TestCase):
    setUp=desktop_fixture.HostedReceivingTests.setUp
    transport_type=desktop_fixture.ReceivingFixtureTransport
    restart=desktop_fixture.HostedReceivingTests.restart

    def test_native_manual_capture_replays_actual_api_pg_catalog_and_received_proofs_without_any_write(self):
        case=json.loads((FIXTURE.parent/'receiving_desktop_contract_v1.json').read_text(encoding='utf-8'))['case']
        remote=self.remote;remote.write_enabled=False;store='fictional-store';claim=case['physical_claim'];ident=case['ready']['receiving_id']
        remote.claims[(store,claim['physical_intake_id'])]=copy.deepcopy(claim);remote.receivings[(store,ident)]=copy.deepcopy(case['ready'])
        receipt=self.service.add_manual_item({'requestId':str(uuid.uuid4()),'game':'pokemon','mode':'labels','item':{'card_name':'Fictional card','card_set':'Fictional set','card_num':'001','condition':'Near Mint','printing':'Normal','language':'English','qty':2,'final_price':12.50}})
        receipt['inventory_intent']='new';receipt['cards'][0].update(reviewed=True,operator_price=True);self.service.store.save_receipt(receipt)
        receipt=self.service.intake_claims.select_existing(receipt['id'],{'revision':receipt['revision'],'physicalIntakeId':claim['physical_intake_id'],'physicalDeliveryVerified':True},self.session['sessionId'])['receipt']
        keys=set(remote.requests);count=len([entry for entry in remote.sent if entry[0]!='GET'])
        result=self.service.hosted_receiving.recover_existing(receipt['id'],{'revision':receipt['revision'],'receivingId':ident},self.session['sessionId'])
        self.assertEqual(result['operation']['state'],'ready');self.assertEqual(result['receipt']['inventory_status'],'not_received')
        remote.receivings[(store,ident)]=copy.deepcopy(case['catalog_confirmed']);catalog=self.service.hosted_receiving.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(catalog['receipt']['inventory_status'],'not_received');self.assertNotIn('hosted_sku_resolution',catalog['receipt']['cards'][0])
        remote.receivings[(store,ident)]=copy.deepcopy(case['received']);received=self.service.hosted_receiving.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(received['receipt']['inventory_status'],'received');self.assertEqual(received['receipt']['cards'][0]['sku'],case['received']['receipt']['confirmed_targets'][0]['target_sku'])
        self.service.approved_cards(received['receipt']);self.assertEqual(set(remote.requests),keys);self.assertEqual(len([entry for entry in remote.sent if entry[0]!='GET']),count);self.assertEqual(remote.stock_calls,0)

    def test_missing_or_tampered_catalog_proof_never_authorizes_resolved_stock_targets(self):
        cases=json.loads(FIXTURE.read_text(encoding='utf-8'))['cases'];original=cases[0]['received'];targets=cases[0]['ready']['targets']
        for field,value in [('catalog_confirmed',False),('catalog_proof',None),('catalog_proof_hash','f'*64),('catalog_resolutions_hash','f'*64),('catalog_resolutions',[])]:
            changed=copy.deepcopy(original);changed[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):catalog_evidence(changed,targets)


if __name__=='__main__':unittest.main()
