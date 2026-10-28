"""Desktop bridge failure/restart tests; fictional hosted transport, no providers."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid
import requests
import roca_to_square as core
from station_cloud import CloudClient,CloudError
from station_service import StationService
from station_shared_trade import fingerprint,locked,lines_for
from station_store import StationError
from test_station_cloud import SharedFixtureTransport,response
from hosted.trades import normalize_lines,calculate_offer


class TradeFixtureTransport(SharedFixtureTransport):
    """A strict local fixture; real hosted transactions have separate PG tests."""
    def __init__(self):
        super().__init__();self.lock=threading.RLock();self.trades={};self.drop_after=set();self.issuances=0

    def request(self,method,url,*,headers,json=None,**options):
        path=urlsplit(url).path
        if not path.startswith('/api/trades'):
            result=super().request(method,url,headers=headers,json=json,**options)
            if path=='/api/auth/login' and json['email'].startswith('buyer') and result.status_code==200:
                data=result.json();data['role']='buyer';self.sessions[data['access_token']]['role']='buyer';return response(200,data)
            return result
        if self.offline:raise requests.ConnectionError('Fictional outage')
        with self.lock:
            self.calls.append((method,path));actor=self.sessions.get(headers.get('Authorization','').removeprefix('Bearer '))
            if not actor:return response(401,{'error':'Sign in'})
            if actor['role'] not in ('manager','owner','buyer'):return response(403,{'error':'Trade role required'})
            store=actor['store'];key=(store,(json or {}).get('request_key'))
            if method=='POST' and key in self.requests:
                previous,result=self.requests[key]
                return response(200,result) if previous==json else response(409,{'error':'Changed saved request'})
            ident=path.split('/')[3] if len(path.split('/'))>3 else None
            trade=self.trades.get((store,ident))
            if method=='GET':return response(200,trade) if trade else response(404,{'error':'Trade missing'})
            operation='create' if path=='/api/trades' else path.rsplit('/',1)[1]
            if operation=='create':
                if json['customer_id'] not in self.customers or self.customers[json['customer_id']]['store']!=store:return response(404,{'error':'Customer missing'})
                active=next((row for (tenant,_),row in self.trades.items() if tenant==store and row['source_reference']==json['source_reference'] and row['state']!='voided'),None)
                if active:
                    result={'trade_id':active['id'],'state':active['state'],'version':active['version'],'source_deduplicated':True}
                else:
                    prior=any(row['source_reference']==json['source_reference'] for (tenant,_),row in self.trades.items() if tenant==store)
                    if prior and not json.get('replace_voided_source'):return response(409,{'error':'Confirm replacement after void'})
                    ident=str(uuid.uuid4());lines=normalize_lines(json['lines']);totals=calculate_offer(lines,3500,5000)
                    trade={'id':ident,'customer_id':json['customer_id'],'title':json['title'],'source_reference':json['source_reference'],
                           'state':'draft','version':0,'lines':lines,'lines_hash':fingerprint(lines),'cash_bps':3500,'credit_bps':5000,
                           'cash_override_cents':None,'credit_override_cents':None,'override_reason':'',**totals}
                    self.trades[(store,ident)]=trade;result={'trade_id':ident,'state':'draft','version':0,'source_deduplicated':False}
            elif not trade:return response(404,{'error':'Trade missing'})
            elif operation=='accept':
                if actor['role'] not in ('manager','owner'):return response(403,{'error':'Manager required'})
                if trade['state']=='accepted_credit':result=trade['acceptance_result']
                else:
                    if trade['state']!='draft' or trade['version']!=json['version']:return response(409,{'error':'Trade changed'})
                    if json['customer_agreed'] is not True or json['items_received'] is not True or json['customer_id']!=trade['customer_id'] or json['expected_offer_cents']!=trade['credit_offer_cents']:return response(409,{'error':'Agreement changed'})
                    amount=trade['credit_offer_cents'];customer=self.customers[trade['customer_id']];customer['balance_cents']+=amount;customer['available_cents']+=amount;customer['version']+=1;self.issuances+=1
                    entry=str(uuid.uuid4());evidence={'trade_schema_version':1,'trade_id':ident,'version_before_acceptance':trade['version'],
                        'receipt_version':trade['version']+1,'customer':{'id':customer['id'],'display_name':customer['display_name']},
                        'title':trade['title'],'source_reference':trade['source_reference'],'currency':'USD','lines':[line for line in trade['lines'] if line['included']],
                        'lines_hash':trade['lines_hash'],'intake_total_cents':trade['intake_total_cents'],'cash_offer_cents':trade['cash_offer_cents'],
                        'credit_offer_cents':amount,'selected_offer':'credit','accepted_amount_cents':amount,'accepted_at':'2026-10-28T18:00:00+00:00',
                        'accepted_by':str(uuid.uuid4()),'accepted_by_role':actor['role'],'customer_agreed':True,'items_received':True,
                        'inventory_received':False,'cash_payment_executed':False,'credit_policy_state':'unreviewed','credit_entry_id':entry}
                    result={'trade_id':ident,'state':'accepted_credit','version':trade['version']+1,'accepted_amount_cents':amount,'offer_type':'credit',
                        'receipt_hash':fingerprint(evidence),'credit_entry_id':entry,'cash_payment_executed':False,'inventory_received':False,
                        'credit_policy_state':'unreviewed','checkout_eligible':False,'balance_after_cents':customer['balance_cents']}
                    trade.update(state='accepted_credit',version=trade['version']+1,accepted_amount_cents=amount,receipt_hash=result['receipt_hash'],accepted_receipt=evidence,acceptance_result=result)
            elif operation=='void':
                if trade['state']!='draft' or trade['version']!=json['version']:return response(409,{'error':'Cannot void current state'})
                trade.update(state='voided',version=trade['version']+1);result={'trade_id':ident,'state':'voided','version':trade['version']}
            else:return response(404,{'error':'Unknown fixture route'})
            self.requests[key]=(copy.deepcopy(json),copy.deepcopy(result))
            if operation in self.drop_after:self.drop_after.remove(operation);raise requests.Timeout('Fictional response lost after commit')
            return response(200,result)


class SharedTradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.settings=patch.object(core,'SETTINGS_FILE',str(self.root/'settings.json'));self.settings.start();self.addCleanup(self.settings.stop)
        self.outputs=patch.object(core,'OUTPUT_DIR',str(self.root/'outputs'));self.outputs.start();self.addCleanup(self.outputs.stop)
        self.remote=TradeFixtureTransport();self.service=self.restart()
        self.service.store.put('preferences',self.service.preferences()|{'pricingEnabled':False})
        self.session=self.service.cloud.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
        created=self.service.cloud.create_customer({'request_key':'fixture-customer','display_name':'Fictional Trade Customer','permission_confirmed':True})
        self.customer=created['customer_id']

    def restart(self):
        service=StationService(core);service.cloud=CloudClient(service.store.root,transport=self.remote)
        service.shared_trades.cloud=service.cloud;return service

    def receipt(self):
        receipt=self.service.import_csv('Name,Game,Set,Number,Condition,Printing,Language,Price,Quantity\nFictional Trade,Riftbound,Fixture,001,Near Mint,Normal,English,8.13,2\n','fictional-trade.csv','trade',new_delivery=True)['receipt']
        receipt['cards'][0]['reviewed']=True;self.service.store.save_receipt(receipt);return receipt

    def prepare(self,receipt=None):
        receipt=receipt or self.receipt()
        return self.service.shared_trades.prepare(receipt['id'],{'revision':receipt['revision'],'customerId':self.customer},self.session['sessionId'])

    def accept(self,prepared):
        return self.service.shared_trades.accept(prepared['receipt']['id'],{'confirmationHash':prepared['confirmationHash'],'customerAgreed':True,'itemsReceived':True},self.session['sessionId'])

    def assert_no_local_credit(self):
        with self.service.store.db() as db:
            tables={row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'credit_entries' in tables:self.assertEqual(db.execute('SELECT count(*) FROM credit_entries').fetchone()[0],0)

    def test_reviewed_trade_selects_customer_and_uses_saved_hosted_offer_exactly_once(self):
        prepared=self.prepare();self.assertTrue(locked(prepared['receipt']));self.assertEqual(self.remote.issuances,0)
        self.assertEqual(prepared['trade']['credit_offer_cents'],813)
        result=self.accept(prepared);receipt=result['receipt'];self.assertFalse(locked(receipt))
        self.assertEqual(receipt['purchase']['scope'],'hosted');self.assertEqual(receipt['purchase']['amount'],8.13)
        self.assertFalse(receipt['purchase']['checkout_eligible']);self.assertNotIn('tracked_credit',receipt)
        self.accept(result);self.service.shared_trades.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(self.remote.issuances,1);self.assertEqual(self.service.cloud.pending()['pending'],[]);self.assert_no_local_credit()

    def test_unreviewed_unpriced_or_accepted_local_trade_never_creates_hosted_receipt(self):
        for modification in ('review','price','purchase','received'):
            receipt=self.receipt()
            if modification=='review':receipt['cards'][0]['reviewed']=False
            if modification=='price':receipt['cards'][0]['price_missing']=True
            if modification=='purchase':receipt['purchase']={'type':'credit','amount':5}
            if modification=='received':receipt['inventory_status']='received'
            self.service.store.save_receipt(receipt)
            with self.subTest(modification=modification),self.assertRaises(StationError):self.prepare(receipt)
        self.assertEqual(self.remote.trades,{})

    def test_pending_intent_blocks_save_manual_add_prices_delete_local_accept_and_outputs(self):
        receipt=self.prepare()['receipt'];ident=receipt['id']
        calls=(lambda:self.service.save_receipt(ident,{'revision':receipt['revision'],'title':'Changed'}),
            lambda:self.service.add_manual_item({'requestId':str(uuid.uuid4()),'receiptId':ident,'revision':receipt['revision'],'game':'magic','item':{'card_name':'Fictional extra','qty':1,'final_price':1}}),
            lambda:self.service.delete_receipt(ident,receipt['revision']),lambda:self.service.accept_buy(ident,'credit'),
            lambda:self.service.approve_price(ident,{'revision':receipt['revision']}),lambda:self.service.apply_safe_prices(ident,receipt['revision']),
            lambda:self.service.approved_cards(receipt),lambda:self.service.receive(ident),lambda:self.service.labels(ident,'side',203),lambda:self.service.prepare_online(ident))
        for callback in calls:
            with self.subTest(callback=callback),self.assertRaisesRegex(StationError,'saved shared trade'):callback()
        self.assertEqual(self.remote.issuances,0)

    def test_create_lost_response_recovers_same_draft_after_restart(self):
        receipt=self.receipt();self.remote.drop_after.add('create')
        with self.assertRaises(CloudError):self.prepare(receipt)
        pending=self.service.cloud._pending();self.assertEqual(pending[0]['kind'],'trade_create')
        self.assertTrue((self.service.store.root/'cloud.pending').read_bytes().startswith(b'DP1'))
        self.assertNotIn(b'Fictional Trade',(self.service.store.root/'cloud.pending').read_bytes())
        self.service=self.restart();result=self.service.shared_trades.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(result['trade']['state'],'draft');self.assertEqual(len(self.remote.trades),1)
        self.assertEqual(self.service.cloud.pending()['pending'],[]);self.assertEqual(self.remote.issuances,0)

    def test_accept_lost_response_restarts_and_mirrors_one_credit(self):
        prepared=self.prepare();self.remote.drop_after.add('accept')
        with self.assertRaises(CloudError):self.accept(prepared)
        self.assertEqual(self.remote.issuances,1);self.assertEqual(self.service.cloud._pending()[0]['kind'],'trade_accept')
        self.service=self.restart();result=self.service.shared_trades.status(prepared['receipt']['id'],self.session['sessionId'])
        self.assertEqual(result['receipt']['purchase']['amount'],8.13);self.assertEqual(self.remote.issuances,1)
        self.assert_no_local_credit();self.assertEqual(self.service.cloud.pending()['pending'],[])

    def test_confirmed_network_result_survives_local_mirror_save_failure(self):
        prepared=self.prepare();actual=self.service.shared_trades._save
        def fail_once(receipt,event,detail):
            if event=='shared_trade_confirmed':raise OSError('Fictional disk write interruption')
            return actual(receipt,event,detail)
        with patch.object(self.service.shared_trades,'_save',side_effect=fail_once),self.assertRaises(OSError):self.accept(prepared)
        self.assertEqual(self.remote.issuances,1);self.assertIn('result',self.service.cloud._pending()[0])
        self.service=self.restart();self.service.shared_trades.status(prepared['receipt']['id'],self.session['sessionId'])
        self.assertEqual(self.remote.issuances,1);self.assert_no_local_credit()

    def test_snapshot_or_hosted_line_mismatch_locks_for_owner_recovery(self):
        prepared=self.prepare();receipt=self.service.store.receipt(prepared['receipt']['id']);receipt['cards'][0]['qty']=5;self.service.store.save_receipt(receipt)
        with self.assertRaisesRegex(StationError,'source no longer matches'):self.accept(prepared)
        self.assertEqual(self.remote.issuances,0);self.assertTrue(locked(self.service.store.receipt(receipt['id'])))
        # Explicit draft cancellation remains possible without guessing a purchase.
        cancelled=self.service.shared_trades.cancel(receipt['id'],self.session['sessionId']);self.assertEqual(cancelled['receipt']['shared_trade']['state'],'cancelled')

    def test_changed_hosted_offer_requires_fresh_confirmation(self):
        prepared=self.prepare();trade=self.remote.trades[('fictional-store',prepared['trade']['id'])];trade['version']+=1;trade['credit_offer_cents']=900
        with self.assertRaisesRegex(StationError,'offer changed'):self.accept(prepared)
        self.assertEqual(self.remote.issuances,0)

    def test_customer_agreement_items_and_manager_role_are_all_required(self):
        prepared=self.prepare();ident=prepared['receipt']['id']
        for fields in ({'customerAgreed':False,'itemsReceived':True},{'customerAgreed':True,'itemsReceived':False}):
            with self.assertRaises(StationError):self.service.shared_trades.accept(ident,fields|{'confirmationHash':prepared['confirmationHash']},self.session['sessionId'])
        self.service.cloud.logout();self.session=self.service.cloud.login({'storeSlug':'fictional-store','email':'buyer@example.invalid','password':'fixture-staff-password'})
        with self.assertRaises(CloudError) as caught:self.accept(prepared)
        self.assertEqual(caught.exception.status,403);self.assertEqual(self.remote.issuances,0)

    def test_cancel_uncertain_create_confirms_void_before_unlock_and_explicit_replacement(self):
        receipt=self.receipt();self.remote.drop_after.add('create')
        with self.assertRaises(CloudError):self.prepare(receipt)
        cancelled=self.service.shared_trades.cancel(receipt['id'],self.session['sessionId'])
        self.assertFalse(locked(cancelled['receipt']));self.assertEqual(cancelled['trade']['state'],'voided')
        self.assertEqual(self.remote.issuances,0);replacement=self.prepare(cancelled['receipt'])
        self.assertNotEqual(replacement['trade']['id'],cancelled['trade']['id']);self.assertEqual(len(self.remote.trades),2)
        self.assertTrue(self.service.cloud.trade_journal(replacement['receipt']['shared_trade']['create_key']) is None)

    def test_void_lost_response_stays_locked_until_restart_confirms_cancel(self):
        prepared=self.prepare();self.remote.drop_after.add('void')
        with self.assertRaises(CloudError):self.service.shared_trades.cancel(prepared['receipt']['id'],self.session['sessionId'])
        self.assertTrue(locked(self.service.store.receipt(prepared['receipt']['id'])))
        self.service=self.restart();result=self.service.shared_trades.status(prepared['receipt']['id'],self.session['sessionId'])
        self.assertEqual(result['trade']['state'],'voided');self.assertFalse(locked(result['receipt']));self.assertEqual(self.remote.issuances,0)

    def test_wrong_store_or_stale_session_cannot_recover_or_change_trade(self):
        prepared=self.prepare();self.service.cloud.logout();other=self.service.cloud.login({'storeSlug':'another-store','email':'manager@example.invalid','password':'fixture-staff-password'})
        with self.assertRaises(CloudError):self.service.shared_trades.status(prepared['receipt']['id'],self.session['sessionId'])
        with self.assertRaises(CloudError):self.service.shared_trades.status(prepared['receipt']['id'],other['sessionId'])
        self.assertEqual(self.remote.issuances,0)

    def test_confirmed_shared_purchase_cannot_be_issued_into_local_wallet(self):
        receipt=self.accept(self.prepare())['receipt']
        from credit_tracker import Tracker
        tracker=Tracker(self.service);customer=tracker.add_customer({'requestId':str(uuid.uuid4()),'name':'Fictional local wallet','contact':'','square_reference':''})
        with self.assertRaisesRegex(StationError,'belongs to shared credit'):
            tracker.transaction({'requestId':str(uuid.uuid4()),'customerId':customer['id'],'version':0,'kind':'trade','receiptId':receipt['id'],'reference':receipt['id']})
        self.assert_no_local_credit();self.assertNotIn('tracked_credit',self.service.store.receipt(receipt['id']))

    def test_mismatched_accepted_receipt_evidence_never_creates_local_purchase(self):
        prepared=self.prepare();actual=self.service.shared_trades._save
        def fail_mirror(receipt,event,detail):
            if event=='shared_trade_confirmed':raise OSError('Fictional interrupted mirror')
            return actual(receipt,event,detail)
        with patch.object(self.service.shared_trades,'_save',side_effect=fail_mirror),self.assertRaises(OSError):self.accept(prepared)
        trade=self.remote.trades[('fictional-store',prepared['trade']['id'])];trade['accepted_receipt']['lines'][0]['identity']['number']='DIFFERENT'
        trade['receipt_hash']=fingerprint(trade['accepted_receipt'])
        with self.assertRaises(StationError):self.service.shared_trades.status(prepared['receipt']['id'],self.session['sessionId'])
        self.assertIsNone(self.service.store.receipt(prepared['receipt']['id'])['purchase']);self.assertEqual(self.remote.issuances,1)

    def test_cancel_after_uncertain_acceptance_recovers_acceptance_instead_of_voiding(self):
        prepared=self.prepare();self.remote.drop_after.add('accept')
        with self.assertRaises(CloudError):self.accept(prepared)
        result=self.service.shared_trades.cancel(prepared['receipt']['id'],self.session['sessionId'])
        self.assertEqual(result['receipt']['shared_trade']['state'],'confirmed');self.assertEqual(self.remote.issuances,1)
        self.assertFalse(any(path.endswith('/void') for _,path in self.remote.calls))

    def test_legacy_graded_condition_requires_explicit_slab_identity_and_certificate(self):
        receipt=self.receipt();receipt['cards'][0].update(condition='PSA 10',qty=1,grader='',grade='',cert_number='')
        self.service.store.save_receipt(receipt)
        with self.assertRaisesRegex(StationError,'slabs need'):self.prepare(receipt)
        self.assertEqual(self.remote.trades,{})

    def test_only_selected_rows_are_sent_and_excluded_uncertified_slab_does_not_block(self):
        receipt=self.receipt();excluded=copy.deepcopy(receipt['cards'][0]);excluded.update(row_id=uuid.uuid4().hex,card_name='Excluded private item',include=False,reviewed=False,condition='PSA 10',grader='',grade='',cert_number='',price_missing=True)
        receipt['cards'].append(excluded);self.service.store.save_receipt(receipt)
        result=self.prepare(receipt);self.assertEqual(len(result['trade']['lines']),1)
        self.assertNotIn('Excluded private item',json.dumps(result['trade']))
        self.assertNotIn('Excluded private item',json.dumps(self.service.shared_trades._body(result['receipt'],'create')))

    def test_selected_limit_applies_to_selected_rows_not_full_collection(self):
        receipt=self.receipt();template=receipt['cards'][0];receipt['cards']=[]
        for index in range(5000):
            card=copy.deepcopy(template);card.update(row_id=uuid.uuid4().hex,include=index<100,reviewed=index<100)
            receipt['cards'].append(card)
        self.assertEqual(len(lines_for(receipt)),100)
        for card in receipt['cards'][:1001]:card['include']=True;card['reviewed']=True
        with self.assertRaisesRegex(StationError,'1,000 included'):lines_for(receipt)

    def test_local_intent_save_failure_prevents_hosted_mutation(self):
        receipt=self.receipt()
        with patch.object(self.service.shared_trades,'_save',side_effect=OSError('Fictional local disk failure')),self.assertRaises(OSError):self.prepare(receipt)
        self.assertFalse(any(method=='POST' and path=='/api/trades' for method,path in self.remote.calls))
        self.assertEqual(self.service.cloud._pending(),[]);self.assertEqual(self.remote.issuances,0)

    def test_included_sports_slab_keeps_grade_certificate_and_serial_identity(self):
        receipt=self.receipt();card=receipt['cards'][0]
        card.update(game='sports',game_label='Sports',condition='PSA 10',grader='PSA',grade='10',cert_number='000012345',qty=1,
                    year='2026',manufacturer='Fictional Cards',player='Fictional Player',team='Fictional Team',serial_number='007/100')
        receipt['pricing_game']='sports';self.service.store.save_receipt(receipt)
        prepared=self.prepare(receipt);identity=prepared['trade']['lines'][0]['identity']
        self.assertEqual(identity['kind'],'slab');self.assertEqual(identity['certification'],'000012345')
        self.assertEqual(identity['serial_number'],'007/100');self.assertEqual(identity['year'],'2026');self.assertEqual(identity['grade'],'10')
        confirmed=self.accept(prepared);self.assertEqual(confirmed['receipt']['cards'][0]['cert_number'],'000012345')

    def test_known_hosted_constraints_fail_before_local_intent_or_remote_mutation(self):
        for kind in ('long_name','total_limit','duplicate_certificate'):
            receipt=self.receipt();card=receipt['cards'][0]
            if kind=='long_name':card['card_name']='F'*201
            elif kind=='total_limit':card.update(final_price=1_000_000,qty=2)
            else:
                card.update(condition='PSA 10',grader='PSA',grade='10',cert_number='000001',qty=1)
                extra=copy.deepcopy(card);extra['row_id']=uuid.uuid4().hex;receipt['cards'].append(extra)
            self.service.store.save_receipt(receipt)
            with self.subTest(kind=kind),self.assertRaises(StationError):self.prepare(receipt)
            self.assertNotIn('shared_trade',self.service.store.receipt(receipt['id']))
        self.assertEqual(self.remote.trades,{});self.assertEqual(self.service.cloud._pending(),[])

    def test_corrupt_trade_journal_stays_locked_without_network_or_new_credit(self):
        prepared=self.prepare();self.remote.drop_after.add('accept')
        with self.assertRaises(CloudError):self.accept(prepared)
        value=json.loads(self.service.cloud.pending_vault.read());value['operations'][0]['body']['expected_offer_cents']+=1
        self.service.cloud.pending_vault.set(json.dumps(value));before=len(self.remote.calls);self.service=self.restart()
        with self.assertRaises(CloudError) as caught:self.service.shared_trades.status(prepared['receipt']['id'],self.session['sessionId'])
        self.assertTrue(caught.exception.pending);self.assertEqual(len(self.remote.calls),before);self.assertEqual(self.remote.issuances,1)
        self.assertTrue(locked(self.service.store.receipt(prepared['receipt']['id'])))

    def test_compact_list_summary_excludes_shared_customer_and_retry_payload(self):
        prepared=self.prepare();summary=next(row for row in self.service.store.summaries() if row['id']==prepared['receipt']['id'])
        encoded=json.dumps(summary)
        self.assertNotIn(self.customer,encoded);self.assertNotIn('source_hash',summary.get('shared_trade',{}))
        self.assertNotIn('create_key',encoded);self.assertNotIn('Fictional Trade Customer',encoded);self.assertNotIn('lines',summary.get('shared_trade',{}))

    def test_source_namespace_distinguishes_id_collision_and_copy_binds_accepted_trade(self):
        original=self.receipt();first=self.accept(self.prepare(original));source=first['receipt']['shared_trade']['source_reference']
        with patch.object(core,'SETTINGS_FILE',str(self.root/'other-desktop'/'settings.json')):
            other=self.restart();other.store.put('preferences',other.preferences()|{'pricingEnabled':False})
            session=other.cloud.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
            copied=copy.deepcopy(original);copied.pop('shared_trade',None);copied['purchase']=None;other.store.save_receipt(copied)
            bound=other.shared_trades.prepare(copied['id'],{'revision':copied['revision'],'customerId':self.customer},session['sessionId'])
            self.assertEqual(bound['receipt']['shared_trade']['source_reference'],source)
            self.assertEqual(bound['receipt']['shared_trade']['trade_id'],first['trade']['id']);self.assertEqual(self.remote.issuances,1)
        # A different immutable file/manual source with the coincident local ID
        # must not be deduplicated into that unrelated accepted receipt.
        with patch.object(core,'SETTINGS_FILE',str(self.root/'collision-desktop'/'settings.json')):
            other=self.restart();other.store.put('preferences',other.preferences()|{'pricingEnabled':False})
            session=other.cloud.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
            collision=copy.deepcopy(original);collision['source_hash']='different-fictional-source';collision['cards'][0]['row_id']=uuid.uuid4().hex
            other.store.save_receipt(collision)
            result=other.shared_trades.prepare(collision['id'],{'revision':collision['revision'],'customerId':self.customer},session['sessionId'])
            self.assertNotEqual(result['receipt']['shared_trade']['source_reference'],source)
            self.assertNotEqual(result['trade']['id'],first['trade']['id']);self.assertEqual(self.remote.issuances,1)


if __name__=='__main__':unittest.main(verbosity=2)
