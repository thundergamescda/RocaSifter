"""Desktop catalog→inventory state/recovery tests, fictional transport only."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid
import requests
import roca_to_square as core
from hosted_labels import fingerprint
from square_variations import variant_sku
from station_cloud import CloudClient,CloudError
from station_service import StationService
from station_store import StationError
import test_hosted_receive as base_tests
from test_hosted_receive import ReceivingFixtureTransport
from test_station_cloud import response


class CatalogReceivingFixtureTransport(ReceivingFixtureTransport):
    def __init__(self):
        super().__init__();self.new_price_names=set();self.new_price_enabled=True;self.catalog_calls=0;self.catalog_unknown=False;self.families={};self.concurrent_read=None

    def project(self,store,body,ident,revision=0):
        value=super().project(store,body,ident,revision)
        accepted={row['id']:row for row in self.trades.get((store,body.get('accepted_trade_id')),{}).get('accepted_receipt',{}).get('lines',[])}
        for line,target in zip(sorted(body['lines'],key=lambda row:row['review_line_id']),value['targets']):
            identity=accepted[line['accepted_line_id']]['identity'] if accepted else line['identity']
            if identity['name'] not in self.new_price_names:continue
            family=self.families.setdefault(target['identity_hash'],{'binding_id':str(uuid.uuid4()),'template_id':str(uuid.uuid4())})
            base=self.target_skus.get(identity['name'],'Existing >5^~ '+target['identity_hash'][:6])
            target.update(action='create_price',binding_role='source_family',binding_id=family['binding_id'],binding_version=1,
                source_sku=base,target_sku=variant_sku(base,target['selling_cents']),variation_id=None,parent_id='PARENT-'+target['identity_hash'][:8],
                template_id=family['template_id'],template_version=1,template_evidence_hash='c'*64)
        return value

    def catalog_complete(self,value,history=False):
        groups={}
        for target in value['targets']:
            if target['action']=='create_price':groups.setdefault((target['template_id'],target['parent_id'],target['target_sku'],target['selling_cents']),[]).append(target)
        resolutions=[]
        for _,targets in sorted(groups.items()):
            targets.sort(key=lambda row:row['review_line_id']);first=targets[0]
            result={key:first[key] for key in ('review_line_id','identity_hash','target_sku','parent_id','selling_cents','template_id','template_version')}
            result.update(review_line_ids=[row['review_line_id'] for row in targets],variation_id='RESOLVED-'+uuid.uuid4().hex,
                currency='USD',binding_id=str(uuid.uuid4()),binding_version=1,binding_evidence_hash='d'*64);resolutions.append(result)
        proof={'kind':'catalog_state_match' if history else 'catalog_batch_ack','request_hash':'a'*64,'evidence_hash' if history else 'response_hash':'b'*64}
        value.update(state='catalog_confirmed',catalog_confirmed=True,catalog_proof=proof,catalog_proof_hash=fingerprint(proof),
            catalog_resolutions=resolutions,catalog_resolutions_hash=fingerprint(resolutions))

    def complete(self,value):
        if not any(row['action']=='create_price' for row in value['targets']):return super().complete(value)
        assert value['catalog_confirmed'] is True
        super().complete(value);proof=value['receipt'];by_alias={alias:row for row in value['catalog_resolutions'] for alias in row['review_line_ids']};confirmed=[]
        for frozen in value['targets']:
            target=copy.deepcopy(frozen)
            if frozen['action']=='create_price':
                resolved=by_alias[frozen['review_line_id']]
                target.update(source_binding_id=frozen['binding_id'],source_binding_version=frozen['binding_version'],source_binding_evidence_hash=frozen['binding_evidence_hash'],
                    binding_id=resolved['binding_id'],binding_version=resolved['binding_version'],binding_evidence_hash=resolved['binding_evidence_hash'],binding_role='resolved_target',variation_id=resolved['variation_id'])
            target['confirmed_quantity']=target['quantity'];confirmed.append(target)
        proof.update(confirmed_targets=confirmed,catalog_write_performed=True,**{key:copy.deepcopy(value[key]) for key in ('catalog_proof_hash','catalog_resolutions_hash','catalog_resolutions')})
        proof['receipt_hash']=fingerprint({key:item for key,item in proof.items() if key!='receipt_hash'})

    def request(self,method,url,*,headers,json=None,**options):
        path=urlsplit(url).path
        if path=='/api/receiving/readiness':
            result=super().request(method,url,headers=headers,json=json,**options)
            if result.status_code==200:return response(200,result.json()|{'new_price_supported':True,'new_price_write_enabled':self.write_enabled and self.new_price_enabled})
            return result
        ident=path.split('/')[3] if path.startswith('/api/receiving/') and len(path.split('/'))>3 else None
        actor=self.sessions.get(headers.get('Authorization','').removeprefix('Bearer '));store=(actor or {}).get('store');value=self.receivings.get((store,ident))
        if method=='GET' and value and path=='/api/receiving/'+ident and self.concurrent_read:
            snapshot=copy.deepcopy(value);self.concurrent_read.wait(timeout=10);return response(200,snapshot)
        operation=path.rsplit('/',1)[1]
        if not value or not any(row['action']=='create_price' for row in value['targets']) or method!='POST' or operation not in ('commit','recover','authorize'):
            return super().request(method,url,headers=headers,json=json,**options)
        if self.offline:raise requests.ConnectionError('Fictional outage')
        with self.lock:
            self.calls.append((method,path));self.sent.append((method,path,copy.deepcopy(json)))
            if not actor:return response(401,{'error':'Sign in'})
            if actor['role'] not in ('owner','manager'):return response(403,{'error':'Manager required'})
            key=(store,json['request_key'])
            if key in self.requests:
                previous,result=self.requests[key];return response(200,result) if previous==json else response(409,{'error':'Original exact key required'})
            if json['revision']!=value['revision'] or json['plan_hash']!=value['plan_hash']:return response(409,{'error':'Original plan changed'})
            if operation=='commit' and value['state']=='ready':
                if not self.new_price_enabled or not self.write_enabled:return response(403,{'error':'New price disabled','code':'RECEIVING_DISABLED'})
                if not self.mfa_fresh:return response(403,{'error':'Fresh enrolled MFA required','code':'MFA_STEP_UP_REQUIRED'})
                self.catalog_calls+=1
                if self.catalog_unknown:value['state']='outcome_unknown'
                else:self.catalog_complete(value)
            elif operation=='recover':
                if value['state']=='outcome_unknown' and not value['catalog_confirmed'] and not self.catalog_unknown:self.catalog_complete(value,True)
                elif value['state']=='outcome_unknown' and value['catalog_confirmed'] and not self.unknown:self.complete(value)
            elif operation=='authorize' and value['state']=='catalog_confirmed':
                if not self.new_price_enabled or not self.write_enabled:return response(403,{'error':'New price disabled','code':'RECEIVING_DISABLED'})
                if not self.mfa_fresh:return response(403,{'error':'Fresh enrolled MFA required','code':'MFA_STEP_UP_REQUIRED'})
                self.stock_calls+=1
                if self.unknown:value['state']='outcome_unknown'
                else:self.complete(value)
            self.requests[key]=(copy.deepcopy(json),copy.deepcopy(value))
            if operation in self.drop_after:self.drop_after.remove(operation);raise requests.Timeout('Fictional catalog/stock reply lost')
            return response(200,value)


class CatalogReceivingTests(unittest.TestCase):
    transport_type=CatalogReceivingFixtureTransport
    setUp=base_tests.HostedReceivingTests.setUp
    restart=base_tests.HostedReceivingTests.restart
    receipt=base_tests.HostedReceivingTests.receipt
    claim=base_tests.HostedReceivingTests.claim
    prepare=base_tests.HostedReceivingTests.prepare
    commit=base_tests.HostedReceivingTests.commit

    def new_plan(self,split=False):
        self.remote.new_price_names.add('Fictional new price');receipt=self.receipt('Name,Game,Price,Quantity\nFictional new price,Magic,12.50,2\n')
        if split:
            other=copy.deepcopy(receipt['cards'][0]);other.update(row_id=uuid.uuid4().hex,qty=1);receipt['cards'][0]['qty']=1;receipt['cards'].append(other);self.service.store.save_receipt(receipt)
        return self.prepare(self.claim(receipt))

    def authorize(self,value):return self.service.hosted_receiving.act(value['receipt']['id'],{'stockContinuationConfirmed':True},self.session['sessionId'],'authorize')

    def test_catalog_confirmation_is_not_inventory_and_explicit_fresh_stock_authorization_is_separate(self):
        plan=self.new_plan();frozen=copy.deepcopy(plan['operation']);catalog=self.commit(plan)
        self.assertEqual(catalog['operation']['state'],'catalog_confirmed');self.assertFalse(catalog['operation']['inventory_confirmed']);self.assertIsNone(catalog['operation']['receipt']);self.assertEqual(catalog['receipt']['inventory_status'],'not_received')
        self.assertEqual((self.remote.catalog_calls,self.remote.stock_calls),(1,0));self.assertEqual(catalog['operation']['targets'],frozen['targets'])
        with self.assertRaises(StationError):self.service.hosted_receiving.act(plan['receipt']['id'],{},self.session['sessionId'],'authorize')
        self.remote.mfa_fresh=False
        with self.assertRaises(CloudError):self.authorize(catalog)
        action=copy.deepcopy(self.service.store.receipt(plan['receipt']['id'])['hosted_receive']['actions']['authorize']);self.remote.mfa_fresh=True
        received=self.authorize(catalog);self.assertEqual(received['receipt']['inventory_status'],'received');self.assertEqual((self.remote.catalog_calls,self.remote.stock_calls),(1,1));self.assertEqual(received['receipt']['hosted_receive']['actions']['authorize'],action)
        self.assertEqual(received['receipt']['cards'][0]['sku'],variant_sku(frozen['targets'][0]['source_sku'],1250));self.assertEqual(received['operation']['targets'],frozen['targets'])

    def test_unknown_catalog_recovers_read_only_then_lost_stock_response_restarts_without_resend(self):
        plan=self.new_plan();self.remote.catalog_unknown=True;unknown=self.commit(plan)
        self.assertEqual(unknown['operation']['state'],'outcome_unknown');self.assertEqual(self.remote.stock_calls,0)
        self.remote.catalog_unknown=False;catalog=self.service.hosted_receiving.act(plan['receipt']['id'],{},self.session['sessionId'],'recover')
        self.assertEqual(catalog['operation']['state'],'catalog_confirmed');self.assertEqual((self.remote.catalog_calls,self.remote.stock_calls),(1,0))
        self.remote.drop_after.add('authorize')
        with self.assertRaises(CloudError):self.authorize(catalog)
        original=copy.deepcopy(self.service.store.receipt(plan['receipt']['id'])['hosted_receive']['actions']['authorize']);self.service=self.restart()
        received=self.service.hosted_receiving.status(plan['receipt']['id'],self.session['sessionId']);self.assertEqual(received['receipt']['inventory_status'],'received');self.assertEqual(received['receipt']['hosted_receive']['actions']['authorize'],original);self.assertEqual((self.remote.catalog_calls,self.remote.stock_calls),(1,1))

    def test_two_price_split_rows_share_one_resolved_variation_and_each_quantity_is_proved(self):
        plan=self.new_plan(True);catalog=self.commit(plan);self.assertEqual(len(catalog['operation']['catalog_resolutions']),1)
        self.assertEqual(len(catalog['operation']['catalog_resolutions'][0]['review_line_ids']),2)
        received=self.authorize(catalog);self.assertEqual([row['qty'] for row in received['receipt']['cards']],[1,1]);self.assertEqual(len({row['sku'] for row in received['receipt']['cards']}),1)
        self.assertEqual(self.remote.stock_calls,1);self.service.approved_cards(received['receipt'])

    def test_new_price_store_flag_and_frozen_template_proof_gate_all_provider_steps(self):
        plan=self.new_plan();self.remote.new_price_enabled=False
        with self.assertRaises(CloudError):self.commit(plan)
        self.assertEqual((self.remote.catalog_calls,self.remote.stock_calls),(0,0));self.remote.new_price_enabled=True;catalog=self.commit(plan)
        original=copy.deepcopy(self.remote.receivings[('fictional-store',plan['operation']['receiving_id'])]);bad=self.remote.receivings[('fictional-store',plan['operation']['receiving_id'])]
        bad['catalog_resolutions'][0]['template_version']+=1;bad['catalog_resolutions_hash']=fingerprint(bad['catalog_resolutions'])
        with self.assertRaises(CloudError):self.authorize(catalog)
        self.assertEqual(self.remote.stock_calls,0);self.remote.receivings[('fictional-store',plan['operation']['receiving_id'])]=original

    def test_source_sku_spaces_punctuation_and_unicode_are_preserved_in_deterministic_new_price_family(self):
        source='  Pok\u00e9mon >5^~ family  ';self.remote.target_skus['Fictional new price']=source
        plan=self.new_plan();target=plan['operation']['targets'][0]
        self.assertEqual(target['source_sku'],source);self.assertEqual(target['target_sku'],variant_sku(source,1250))
        received=self.authorize(self.commit(plan));self.assertEqual(received['operation']['targets'][0]['source_sku'],source)
        self.assertEqual(received['receipt']['cards'][0]['sku'],target['target_sku'])

    def test_two_computers_explicitly_authorize_same_catalog_operation_once(self):
        catalog=self.commit(self.new_plan());old=catalog['receipt'];other_root=self.root/'other-pc';other_root.mkdir()
        with patch.object(core,'SETTINGS_FILE',str(other_root/'settings.json')):other=StationService(core)
        other.store.put('preferences',other.preferences()|{'pricingEnabled':False});other.cloud=CloudClient(other.store.root,transport=self.remote)
        session=other.cloud.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
        fresh=other.import_csv('Name,Game,Price,Quantity\nFictional new price,Magic,12.50,2\n','independent.csv','labels')['receipt'];fresh['inventory_intent']='new'
        for card in fresh['cards']:card.update(reviewed=True,operator_price=True)
        other.store.save_receipt(fresh);fresh=other.intake_claims.select_existing(fresh['id'],{'revision':fresh['revision'],'physicalIntakeId':old['intake_claim']['physical_intake_id'],'physicalDeliveryVerified':True},session['sessionId'])['receipt']
        count=self.remote.stock_calls;keys=set(self.remote.requests)
        bound=other.hosted_receiving.recover_existing(fresh['id'],{'revision':fresh['revision'],'receivingId':catalog['operation']['receiving_id']},session['sessionId'])
        self.assertEqual((self.remote.stock_calls,set(self.remote.requests)),(count,keys));self.remote.concurrent_read=threading.Barrier(2)
        def first():return self.authorize(catalog)
        def second():return other.hosted_receiving.act(fresh['id'],{'stockContinuationConfirmed':True},session['sessionId'],'authorize')
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(first);b=pool.submit(second);results=[a.result(timeout=20),b.result(timeout=20)]
        finally:self.remote.concurrent_read=None
        self.assertEqual(self.remote.stock_calls,1);self.assertEqual(self.remote.catalog_calls,1)
        self.assertTrue(all(result['receipt']['inventory_status']=='received' for result in results));self.assertEqual({result['receipt']['cards'][0]['sku'] for result in results},{catalog['operation']['targets'][0]['target_sku']})


if __name__=='__main__':unittest.main()
