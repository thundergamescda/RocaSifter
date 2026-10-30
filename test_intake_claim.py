"""Fictional original-source/central-claim desktop tests; never contact Square."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import roca_to_square as core
from intake_manifest import semantic_fingerprint,source_lines
from station_intake_claim import required_manifest,source_key_for
from station_service import StationService
from station_store import StationError
from station_cloud import CloudClient,CloudError
from test_shared_trade import TradeFixtureTransport


class OriginalIntakeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for target,value in [('SETTINGS_FILE',str(self.root/'settings.json')),('OUTPUT_DIR',str(self.root/'outputs'))]:
            item=patch.object(core,target,value);item.start();self.addCleanup(item.stop)
        item=patch('requests.sessions.Session.request',side_effect=AssertionError('Live provider calls forbidden'));item.start();self.addCleanup(item.stop)
        self.service=StationService(core);self.service.store.put('preferences',self.service.preferences()|{'pricingEnabled':False})

    def csv(self,text=None):
        return self.service.import_csv(text or 'Name,Game,Price,Quantity\nOriginal Card,Riftbound,3.13,2\n','fictional.csv','trade')['receipt']

    def manual(self,name,receipt=None):
        return self.service.add_manual_item({'requestId':str(uuid.uuid4()),'game':'magic','mode':'trade',
            **({'receiptId':receipt['id'],'revision':receipt['revision']} if receipt else {}),
            'item':{'card_name':name,'qty':2,'final_price':3.13}})

    def test_original_import_is_captured_before_edits_and_cannot_be_replaced_by_save(self):
        receipt=self.csv();original=copy.deepcopy(required_manifest(receipt));key=source_key_for(receipt,receipt['cards'][0])
        self.assertEqual(original['raw_csv_sha256'],receipt['source_hash']);self.assertEqual(original['rows'][0]['quantity'],2)
        receipt['cards'][0].update(card_name='Reviewed correction',qty=1,final_price=12.13,reviewed=True)
        receipt['source_manifest']={'invented':'browser replacement'}
        saved=self.service.save_receipt(receipt['id'],receipt)
        self.assertEqual(required_manifest(saved),original);self.assertEqual(source_key_for(saved,saved['cards'][0]),key)
        self.assertNotIn('price',json.dumps(original));self.assertNotIn('reviewed',json.dumps(original))

    def test_manual_append_keeps_previous_original_facts_after_reviewed_corrections(self):
        first=self.manual('Original manual');initial=copy.deepcopy(required_manifest(first)['rows'][0])
        first['cards'][0].update(card_name='Later correction',qty=1)
        first=self.service.save_receipt(first['id'],first);second=self.manual('Another original',first)
        self.assertEqual(required_manifest(second)['rows'][0],initial)
        self.assertEqual([row['source_row'] for row in required_manifest(second)['rows']],[1,2])
        self.assertEqual(required_manifest(second)['rows'][1]['identity']['name'],'Another original')

    def test_legacy_receipt_never_gets_fabricated_source_on_save_or_manual_append(self):
        receipt=self.csv();receipt.pop('source_manifest');self.service.store.save_receipt(receipt)
        with self.assertRaisesRegex(StationError,'older batch'):required_manifest(receipt)
        saved=self.service.save_receipt(receipt['id'],receipt);self.assertNotIn('source_manifest',saved)
        added=self.manual('New manual',saved);self.assertNotIn('source_manifest',added)
        self.assertIn('source_manifest_error',added)

    def test_large_or_incomplete_source_keeps_entire_local_intake_without_partial_capture(self):
        large='Name,Game,Price,Quantity\n'+''.join(f'Fictional {index},Magic,1,1\n' for index in range(1001))
        receipt=self.csv(large);self.assertEqual(len(receipt['cards']),1001);self.assertNotIn('source_manifest',receipt)
        with self.assertRaisesRegex(StationError,'hosted intake limits'):required_manifest(receipt)
        slab=self.csv('Name,Game,Condition,Price,Quantity\nUncertified original,Pokémon,PSA 10,20,1\n')
        self.assertEqual(len(slab['cards']),1);self.assertNotIn('source_manifest',slab);self.assertIn('source_manifest_error',slab)

    def test_mixed_manual_addition_does_not_rewrite_a_csv_original(self):
        receipt=self.csv();original=copy.deepcopy(receipt['source_manifest']);added=self.manual('Separate source',receipt)
        self.assertEqual(added['source_manifest'],original);self.assertIn('separate physical intake',added['source_manifest_error'])

    def test_split_and_reordered_rows_keep_original_identity_group_keys(self):
        receipt=self.csv('Name,Game,Price,Quantity\nA,Magic,2,2\nB,Riftbound,3,1\n')
        original=copy.deepcopy(receipt['source_manifest']);first=receipt['cards'][0];extra=copy.deepcopy(first)
        extra.update(row_id=uuid.uuid4().hex,split_from=first['row_id'],qty=1);first['qty']=1
        receipt['cards']=[receipt['cards'][1],extra,first]
        saved=self.service.save_receipt(receipt['id'],receipt)
        self.assertEqual(saved['source_manifest'],original)
        self.assertEqual(saved['cards'][1]['source_line_key'],saved['cards'][2]['source_line_key'])
        other=copy.deepcopy(original);other['rows']=list(reversed(other['rows']))
        from intake_manifest import capture_manifest
        reordered=capture_manifest('csv',other['rows'],raw_csv_sha256=other['raw_csv_sha256'])
        self.assertEqual(semantic_fingerprint(original),semantic_fingerprint(reordered))
        self.assertEqual({row['source_line_key'] for row in source_lines(original)},{row['source_line_key'] for row in source_lines(reordered)})


class CentralIntakeClaimTests(unittest.TestCase):
    setUp=OriginalIntakeCaptureTests.setUp
    csv=OriginalIntakeCaptureTests.csv
    manual=OriginalIntakeCaptureTests.manual

    def connect(self):
        self.remote=TradeFixtureTransport();self.service.cloud=CloudClient(self.service.store.root,transport=self.remote)
        self.service.shared_trades.cloud=self.service.cloud
        self.session=self.service.cloud.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})

    def prepare(self,receipt,**fields):
        return self.service.intake_claims.prepare(receipt['id'],{'revision':receipt['revision'],'physicalReference':'TAG-ORIGINAL','physicalDeliveryVerified':True,**fields},self.session['sessionId'])

    def test_claim_is_encrypted_and_legacy_journal_bytes_are_never_rewritten(self):
        self.connect();receipt=self.csv();legacy=self.service.cloud.pending_vault;legacy.set(json.dumps({'version':1,'origin':'https://stock-and-sleeve-api.onrender.com','operations':[]}));before=legacy.path.read_bytes()
        self.remote.drop_after.add('claim')
        with self.assertRaises(CloudError):self.prepare(receipt)
        path=self.service.cloud.intake_vault.path;raw=path.read_bytes();self.assertTrue(raw.startswith(b'DP1'));self.assertNotIn(b'Original Card',raw)
        self.assertEqual(legacy.path.read_bytes(),before)
        self.assertEqual(self.service.cloud.pending()['pending'][0]['action'],'intake_claim')
        self.assertNotIn('source_manifest',json.dumps(self.service.cloud.pending()))

    def test_lost_claim_response_restarts_and_recovers_original_key_and_reference(self):
        self.connect();receipt=self.csv();self.remote.drop_after.add('claim')
        with self.assertRaises(CloudError):self.prepare(receipt)
        old=self.service.store.receipt(receipt['id'])['intake_claim'];self.assertEqual(len(self.remote.claims),1)
        service=StationService(core);service.cloud=CloudClient(service.store.root,transport=self.remote)
        result=service.intake_claims.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(result['receipt']['intake_claim']['request_key'],old['request_key']);self.assertEqual(len(self.remote.claims),1)
        self.assertEqual(service.cloud._intake_pending(),[])
        self.assertFalse(any(method=='POST' and path.startswith('/api/trades') for method,path in self.remote.calls))

    def test_confirmed_claim_remains_encrypted_until_local_mirror_is_durable(self):
        self.connect();receipt=self.csv();save=self.service.store.save_receipt
        def fail_mirror(value,event=None):
            if event and event[0]=='intake_claim_confirmed':raise OSError('Fictional mirror failure')
            return save(value,event)
        with patch.object(self.service.store,'save_receipt',side_effect=fail_mirror),self.assertRaises(OSError):self.prepare(receipt)
        self.assertIn('result',self.service.cloud._intake_pending()[0]);self.assertEqual(len(self.remote.claims),1)
        result=self.service.intake_claims.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(result['receipt']['intake_claim']['state'],'confirmed');self.assertEqual(self.service.cloud._intake_pending(),[])

    def test_intent_or_journal_disk_failure_prevents_any_claim_io(self):
        self.connect()
        for boundary in ('intent','journal'):
            receipt=self.csv();before=len(self.remote.calls)
            target=self.service.store if boundary=='intent' else self.service.cloud.intake_vault
            method='save_receipt' if boundary=='intent' else 'set'
            with self.subTest(boundary=boundary),patch.object(target,method,side_effect=OSError('Fictional disk failure')),self.assertRaises(OSError):self.prepare(receipt)
            self.assertEqual(len(self.remote.calls),before)

    def test_legacy_provider_binding_blocks_claim_without_rekey_or_io(self):
        self.connect()
        for field in ('square_connection','catalog_mode','journal'):
            receipt=self.service.import_csv(f'Name,Game,Price,Quantity\n{field},Magic,2,1\n',field+'.csv','trade')['receipt']
            if field=='journal':
                with self.service.store.db() as db:db.execute('INSERT INTO operations VALUES (?,?,?,NULL)',(receipt['id'],'stock:old','{"endpoint":"original-provider","payload":{"idempotency_key":"old-key"}}'))
            else:receipt[field]='legacy';self.service.store.save_receipt(receipt)
            before=len(self.remote.calls)
            with self.subTest(field=field),self.assertRaisesRegex(StationError,'original Square receiving path'):self.prepare(receipt)
            self.assertEqual(len(self.remote.calls),before)

    def test_manual_requires_explicit_physical_tag_and_delivery_confirmation(self):
        self.connect();receipt=self.manual('Fictional manual')
        with self.assertRaisesRegex(StationError,'tag/reference'):self.prepare(receipt,physicalReference='')
        with self.assertRaisesRegex(StationError,'Confirm the physical'):self.prepare(receipt,physicalDeliveryVerified=False)
        result=self.prepare(receipt,physicalReference='MANUAL-TAG-100')
        self.assertEqual(result['claim']['physical_reference'],'manual-tag-100');self.assertNotEqual(result['claim']['physical_reference'],receipt['source_hash'])

    def test_corrupt_journal_and_changed_manifest_remain_locked_without_network(self):
        self.connect();receipt=self.csv();self.remote.drop_after.add('claim')
        with self.assertRaises(CloudError):self.prepare(receipt)
        value=json.loads(self.service.cloud.intake_vault.read());value['operations'][0]['body']['physical_reference']='changed';self.service.cloud.intake_vault.set(json.dumps(value));before=len(self.remote.calls)
        with self.assertRaises(CloudError):self.service.intake_claims.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(len(self.remote.calls),before)
        with self.assertRaisesRegex(StationError,'saved physical-intake'):self.service.save_receipt(receipt['id'],{'revision':receipt['revision']})

    def test_unreadable_encrypted_journal_keeps_original_bytes_and_owner_recovery_guidance(self):
        self.connect();receipt=self.csv();self.remote.drop_after.add('claim')
        with self.assertRaises(CloudError):self.prepare(receipt)
        raw=self.service.cloud.intake_vault.path.read_bytes();before=len(self.remote.calls)
        with patch.object(self.service.cloud.intake_vault,'read',side_effect=StationError('Save a replacement token in Admin')):
            with self.assertRaises(CloudError) as caught:self.service.intake_claims.status(receipt['id'],self.session['sessionId'])
        self.assertTrue(caught.exception.pending);self.assertIn('same Windows account',str(caught.exception));self.assertNotIn('replacement token',str(caught.exception))
        self.assertEqual(self.remote.calls[before:],[]);self.assertEqual(raw,self.service.cloud.intake_vault.path.read_bytes())

    def test_server_mfa_rejection_can_void_without_changing_original_accept_body(self):
        self.connect();receipt=self.prepare(self.csv())['receipt'];receipt['cards'][0]['reviewed']=True;self.service.store.save_receipt(receipt)
        customer=self.service.cloud.create_customer({'request_key':'fixture-customer','display_name':'Fictional','permission_confirmed':True})['customer_id']
        prepared=self.service.shared_trades.prepare(receipt['id'],{'revision':receipt['revision'],'customerId':customer},self.session['sessionId']);self.remote.mfa_fresh=False
        with self.assertRaises(CloudError) as caught:self.service.shared_trades.accept(receipt['id'],{'confirmationHash':prepared['confirmationHash'],'customerAgreed':True,'itemsReceived':True,'claimReviewed':True},self.session['sessionId'])
        self.assertEqual(caught.exception.code,'MFA_STEP_UP_REQUIRED');journal=copy.deepcopy(self.service.cloud._pending()[0])
        result=self.service.shared_trades.cancel(receipt['id'],self.session['sessionId'])
        self.assertEqual(result['trade']['state'],'voided');self.assertEqual(self.remote.issuances,0);self.assertEqual(self.service.cloud._pending(),[])
        self.assertEqual(journal['body'],{'request_key':journal['request_key'],'version':0,'customer_id':customer,'offer_type':'credit','expected_offer_cents':313,'customer_agreed':True,'items_received':True,'claim_reviewed':True})

    def test_legacy_uncommitted_accept_can_void_and_retains_exact_journal_until_terminal_mirror(self):
        self.connect();receipt=self.prepare(self.csv())['receipt'];receipt['cards'][0]['reviewed']=True;self.service.store.save_receipt(receipt)
        customer=self.service.cloud.create_customer({'request_key':'legacy-fixture-customer','display_name':'Fictional','permission_confirmed':True})['customer_id']
        prepared=self.service.shared_trades.prepare(receipt['id'],{'revision':receipt['revision'],'customerId':customer},self.session['sessionId']);receipt=prepared['receipt'];intent=receipt['shared_trade']
        from station_shared_trade import fingerprint,snapshot
        for field in ('claim_binding_version','physical_intake_id','physical_manifest_hash'):intent.pop(field)
        receipt.pop('intake_claim');receipt.pop('source_manifest');intent.update(state='accepting',accept_key='legacy-original-accept',accept_version=0,expected_offer_cents=313,source_hash=fingerprint(snapshot(receipt)))
        self.service.store.save_receipt(receipt);trade=self.remote.trades[('fictional-store',intent['trade_id'])]
        trade.pop('physical_intake_id');trade.pop('physical_manifest_hash')
        original=self.service.shared_trades._body(receipt,'accept')
        with self.assertRaises(CloudError):self.service.cloud.trade_accept(intent['trade_id'],original,receipt['id'],expected_session_id=self.session['sessionId'])
        save=self.service.shared_trades._save
        def fail_terminal(value,event,detail):
            if event=='shared_trade_cancelled':raise OSError('Fictional terminal mirror failure')
            return save(value,event,detail)
        with patch.object(self.service.shared_trades,'_save',side_effect=fail_terminal),self.assertRaises(OSError):self.service.shared_trades.cancel(receipt['id'],self.session['sessionId'])
        pending=self.service.cloud._pending();old=next(row for row in pending if row['kind']=='trade_accept');self.assertEqual(old['body'],original)
        self.assertTrue(any(row['kind']=='trade_void' and 'result' in row for row in pending));self.assertEqual(trade['state'],'voided')
        result=self.service.shared_trades.status(receipt['id'],self.session['sessionId']);self.assertEqual(result['receipt']['shared_trade']['state'],'cancelled')
        self.assertEqual(self.service.cloud._pending(),[]);self.assertEqual(self.remote.issuances,0)

    def test_same_source_on_other_pc_selects_same_central_claim_and_consumed_source_cannot_reissue(self):
        self.connect();receipt=self.csv();first=self.prepare(receipt)
        with patch.object(core,'SETTINGS_FILE',str(self.root/'other-pc/settings.json')):
            other=StationService(core);other.cloud=CloudClient(other.store.root,transport=self.remote);other.shared_trades.cloud=other.cloud
            session=other.cloud.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
            copy_receipt=copy.deepcopy(receipt);other.store.save_receipt(copy_receipt)
            second=other.intake_claims.prepare(copy_receipt['id'],{'revision':copy_receipt['revision'],'physicalReference':'TAG-OTHER-PC','physicalDeliveryVerified':True},session['sessionId'])
            self.assertEqual(first['claim']['physical_intake_id'],second['claim']['physical_intake_id']);self.assertEqual(len(self.remote.claims),1)
        selected=first['claim'];self.remote.claims[('fictional-store',selected['physical_intake_id'])].update(state='consumed',bound_trade_id=str(uuid.uuid4()))
        with self.assertRaises(CloudError) as caught:self.service.intake_claims.for_trade(first['receipt'],self.session['sessionId'])
        self.assertEqual(caught.exception.code,'PHYSICAL_INTAKE_CONFLICT')

    def test_equivalent_reordered_source_requires_explicit_existing_selection(self):
        self.connect();first=self.prepare(self.csv('Name,Game,Price,Quantity\nA,Magic,2,1\nB,Riftbound,3,2\n'))
        second=self.csv('Name,Game,Price,Quantity\nB,Riftbound,9,2\nA,Magic,7,1\n')
        with self.assertRaises(CloudError) as caught:self.prepare(second,physicalReference='TAG-REORDERED')
        self.assertEqual(caught.exception.code,'PHYSICAL_INTAKE_CONFLICT')
        result=self.service.intake_claims.select_existing(second['id'],{'physicalIntakeId':first['claim']['physical_intake_id'],'physicalDeliveryVerified':True},self.session['sessionId'])
        self.assertEqual(result['receipt']['intake_claim']['physical_intake_id'],first['claim']['physical_intake_id'])
        self.assertNotEqual(result['receipt']['source_manifest']['sha256'],first['claim']['manifest_hash'])
        self.assertEqual(self.service.cloud._intake_pending(),[]);self.assertEqual(len(self.remote.claims),1)

    def test_additional_delivery_needs_link_reason_and_server_mfa_and_preserves_failed_intent(self):
        self.connect();first=self.prepare(self.csv());second=self.service.import_csv('Name,Game,Price,Quantity\nOriginal Card,Riftbound,3.13,2\n','new-delivery.csv','trade',new_delivery=True)['receipt']
        self.remote.mfa_fresh=False
        with self.assertRaises(CloudError) as caught:self.prepare(second,physicalReference='TAG-SECOND-DELIVERY',additionalDeliveryOf=first['claim']['physical_intake_id'],additionalDeliveryReason='A separate physical delivery verified by manager',additionalDeliveryConfirmed=True)
        self.assertEqual(caught.exception.code,'MFA_STEP_UP_REQUIRED');key=self.service.store.receipt(second['id'])['intake_claim']['request_key']
        with self.assertRaisesRegex(StationError,'uncertain additional delivery'):self.service.intake_claims.select_existing(second['id'],{'physicalIntakeId':first['claim']['physical_intake_id'],'physicalDeliveryVerified':True},self.session['sessionId'])
        self.remote.mfa_fresh=True;result=self.service.intake_claims.status(second['id'],self.session['sessionId'])
        self.assertEqual(result['receipt']['intake_claim']['request_key'],key);self.assertNotEqual(result['claim']['physical_intake_id'],first['claim']['physical_intake_id']);self.assertEqual(len(self.remote.claims),2)


if __name__=='__main__':unittest.main(verbosity=2)
