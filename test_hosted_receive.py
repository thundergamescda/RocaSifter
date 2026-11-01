"""Fictional hosted receiving transport and durable desktop acceptance tests.

The transport has no provider HTTP implementation. Hosted PostgreSQL/provider
adapter evidence is independent; these cases exercise the real desktop service.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid
import requests

import roca_to_square as core
from hosted_labels import fingerprint
from hosted.receiving_plan import normalize_plan
from intake_manifest import source_lines,source_line_id
from station_cloud import CloudClient,CloudError
from station_service import StationService
from station_store import StationError
from test_shared_trade import TradeFixtureTransport
from test_station_cloud import response


class ReceivingFixtureTransport(TradeFixtureTransport):
    def __init__(self):
        super().__init__();self.receivings={};self.write_enabled=True;self.stock_calls=0
        self.unknown=False;self.target_skus={};self.sent=[];self.plan_reject=False;self.opaque=False

    def project(self,store,body,ident,revision=0):
        domain=normalize_plan({key:item for key,item in body.items() if key!='revision'});claim=self.claims[(store,domain['physical_intake_id'])]
        accepted={}
        if domain['workflow_kind']=='accepted_trade':
            trade=self.trades[(store,domain['accepted_trade_id'])]
            accepted={row['id']:row for row in trade['accepted_receipt']['lines']}
        targets=[];selected={}
        for row in domain['lines']:
            identity=accepted[row['accepted_line_id']]['identity'] if accepted else row['identity']
            sku=self.target_skus.get(identity['name'],'SS-'+source_line_id(identity)[:8])
            target={key:copy.deepcopy(row[key]) for key in ('review_line_id','source_line_key','quantity','selling_cents')}
            if accepted:target['accepted_line_id']=row['accepted_line_id']
            target.update(identity_hash=source_line_id(identity),binding_id=str(uuid.uuid4()),binding_version=0,
                binding_evidence_hash='b'*64,source_sku=sku,target_sku=sku,variation_id='VAR-'+str(len(targets)),
                parent_id='PARENT-'+str(len(targets)),currency='USD',action='reuse_existing')
            targets.append(target);selected[row['source_line_key']]=selected.get(row['source_line_key'],0)+row['quantity']
            if self.opaque:target.update(variation_id='VAR:/opaque?x=%2F',parent_id='PARENT:/opaque#exact')
        value={key:domain.get(key) for key in ('physical_intake_id','physical_manifest_hash','source_snapshot_hash','workflow_kind','accepted_trade_id','accepted_receipt_hash','client_label_profile')}
        value.update(receiving_id=ident,revision=revision,state='ready',plan_hash=fingerprint(domain),
            provider={'merchant_id':'FIXTURE-MERCHANT','environment':'sandbox','application_id':'FIXTURE-APP','location_id':domain['location_id']},
            targets=targets,selected_quantities=selected,excluded_quantities={row['source_line_key']:row['quantity']-selected.get(row['source_line_key'],0) for row in source_lines(claim['source_manifest'])},
            inventory_confirmed=False,receipt=None,blocked_reasons=[])
        value.update(catalog_confirmed=False,catalog_proof=None,catalog_proof_hash=None,catalog_resolutions=[],catalog_resolutions_hash=None)
        if self.opaque:value['provider'].update(merchant_id='MERCHANT:/opaque?x=%2F',application_id='APP:/opaque#exact')
        return value

    def complete(self,value):
        value.update(state='received',inventory_confirmed=True)
        proof={key:copy.deepcopy(value.get(key)) for key in ('receiving_id','revision','plan_hash','physical_intake_id','physical_manifest_hash','source_snapshot_hash','workflow_kind','accepted_trade_id','accepted_receipt_hash','provider','selected_quantities','excluded_quantities','client_label_profile')}
        proof.update(receipt_schema_version=1,confirmed_targets=[dict(row,confirmed_quantity=row['quantity']) for row in value['targets']],
            inventory_confirmed=True,catalog_write_performed=False,received_at='2026-10-30T18:00:00+00:00',provider_proof_hash='e'*64)
        proof['receipt_hash']=fingerprint(proof);value['receipt']=proof

    def request(self,method,url,*,headers,json=None,**options):
        path=urlsplit(url).path
        if not path.startswith('/api/receiving'):return super().request(method,url,headers=headers,json=json,**options)
        if self.offline:raise requests.ConnectionError('Fictional outage')
        with self.lock:
            self.calls.append((method,path));self.sent.append((method,path,copy.deepcopy(json)))
            actor=self.sessions.get(headers.get('Authorization','').removeprefix('Bearer '))
            if not actor:return response(401,{'error':'Sign in'})
            if actor['role'] not in ('owner','manager'):return response(403,{'error':'Manager required'})
            store=actor['store']
            if path=='/api/receiving/readiness':return response(200,{'protocol_version':1,'write_enabled':self.write_enabled,
                'server_write_enabled':self.write_enabled,'tenant_write_enabled':self.write_enabled,'same_price_existing_supported':True,
                'new_price_supported':False,'new_parent_enabled':False,'fresh_mfa_required':True,'max_targets':100,
                'location_selection_required':True,'grants_verified':False,'required_scopes':['ITEMS_READ','INVENTORY_WRITE']})
            if path=='/api/receiving/locations':return response(200,{'locations':[{'id':'FIXTURE-LOC','name':'Fictional store','currency':'USD','status':'ACTIVE'}]})
            if path.startswith('/api/receiving/requests/') and path.endswith('/retire'):
                original=json['original_plan'];key=(store,path.split('/')[4]);saved=self.requests.get(key)
                active=next((row for (tenant,_),row in self.receivings.items() if tenant==store and row['physical_intake_id']==original['physical_intake_id']),None)
                if active:return response(409,{'error':'Original claim already has an operation','code':'CLAIM_WORKFLOW_CONFLICT','receiving_id':active['receiving_id']})
                if json.get('retire_uncreated_request') is not True or original['request_key']!=key[1]:return response(400,{'error':'Original exact retirement required'})
                if saved and saved[0]!=original:return response(409,{'error':'Original wire changed','code':'PLAN_CHANGED'})
                result={'request_key':key[1],'retired':True,'state':'retired_before_creation','receiving_id':None,'original_plan_sha256':fingerprint(original),'retirement_basis':'registered_original_wire'}
                self.requests[key]=(copy.deepcopy(original),copy.deepcopy(result))
                if 'retire' in self.drop_after:self.drop_after.remove('retire');raise requests.Timeout('Fictional retirement reply lost')
                return response(200,result)
            if path.startswith('/api/receiving/requests/'):
                saved=self.requests.get((store,path.rsplit('/',1)[1]));return response(200,{'found':bool(saved),'result':saved[1] if saved else None})
            ident=path.split('/')[3] if len(path.split('/'))>3 else None
            value=self.receivings.get((store,ident))
            if method=='GET':return response(200,value) if value else response(404,{'error':'Original receiving missing'})
            key=(store,json['request_key'])
            if key in self.requests:
                prior,result=self.requests[key]
                if result.get('retired'):return response(409,{'error':'Original key permanently retired','code':'PLAN_REQUEST_RETIRED'})
                return response(200,result) if prior==json else response(409,{'error':'Exact original key/body required'})
            operation='plan' if path.endswith('/plans') else 'revise' if method=='PUT' else path.rsplit('/',1)[1]
            if operation=='plan':
                if self.plan_reject:return response(409,{'error':'Approve exact catalog binding before planning','code':'RECEIVING_REVIEW_REQUIRED'})
                existing=next((row for (tenant,_),row in self.receivings.items() if tenant==store and row['physical_intake_id']==json['physical_intake_id']),None)
                if existing:return response(409,{'error':'Open the original operation','code':'RECEIVING_SOURCE_CONFLICT','receiving_id':existing['receiving_id']})
                ident=str(uuid.uuid4());value=self.project(store,json,ident);self.receivings[(store,ident)]=value
            elif not value:return response(404,{'error':'Original operation missing'})
            elif json.get('revision')!=value['revision'] or json.get('plan_hash',value['plan_hash'])!=value['plan_hash']:return response(409,{'error':'Plan changed'})
            elif operation=='revise':
                if value['state']!='cancelled_before_write':return response(409,{'error':'Provider admission locks revision'})
                value=self.project(store,json,ident,value['revision']+1);self.receivings[(store,ident)]=value
            elif operation=='commit':
                if not self.write_enabled:return response(409,{'error':'Writes disabled','code':'RECEIVING_DISABLED'})
                if not self.mfa_fresh:return response(403,{'error':'Fresh enrolled MFA required','code':'MFA_STEP_UP_REQUIRED'})
                if value['state']!='ready':return response(409,{'error':'No second provider admission'})
                self.stock_calls+=1
                if self.unknown:value['state']='outcome_unknown'
                else:self.complete(value)
            elif operation=='recover':
                if value['state']=='outcome_unknown' and not self.unknown:self.complete(value)
            elif operation=='cancel':
                if value['state']!='ready':return response(409,{'error':'Admitted operations cannot cancel'})
                value['state']='cancelled_before_write'
            elif operation!='authorize':return response(404,{'error':'Unsupported fixture action'})
            self.requests[key]=(copy.deepcopy(json),copy.deepcopy(value))
            if operation in self.drop_after:self.drop_after.remove(operation);raise requests.Timeout('Fictional receiving response lost')
            return response(200,value)


class HostedReceivingTests(unittest.TestCase):
    transport_type=ReceivingFixtureTransport
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for key,name in (('SETTINGS_FILE','settings.json'),('OUTPUT_DIR','outputs')):
            item=patch.object(core,key,str(self.root/name));item.start();self.addCleanup(item.stop)
        item=patch('requests.sessions.Session.request',side_effect=AssertionError('No real provider HTTP'));item.start();self.addCleanup(item.stop)
        self.remote=self.transport_type();self.service=self.restart()
        self.service.store.put('preferences',self.service.preferences()|{'pricingEnabled':False})
        self.session=self.service.cloud.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
        self.profile={'stock':'2x1','layout':'side','dpi':203}

    def restart(self):
        service=StationService(core);service.cloud=CloudClient(service.store.root,transport=self.remote)
        service.shared_trades.cloud=service.cloud;return service

    def receipt(self,text=None):
        receipt=self.service.import_csv(text or 'Name,Game,Price,Quantity\nFictional A,Magic,3.13,2\nFictional B,Riftbound,4.13,1\n','fictional-receiving.csv','labels',new_delivery=True)['receipt']
        receipt['inventory_intent']='new'
        for card in receipt['cards']:card.update(reviewed=True,operator_price=True)
        self.service.store.save_receipt(receipt)
        return receipt

    def claim(self,receipt):
        return self.service.intake_claims.prepare(receipt['id'],{'revision':receipt['revision'],'physicalReference':'TAG-'+receipt['id'],'physicalDeliveryVerified':True},self.session['sessionId'])['receipt']

    def prepare(self,receipt=None,**changes):
        receipt=receipt or self.claim(self.receipt())
        return self.service.hosted_receiving.prepare(receipt['id'],{'revision':receipt['revision'],'workflowKind':'inventory_only',
            'locationId':'FIXTURE-LOC','labelProfile':self.profile,'newInventoryConfirmed':True,'excludedNotReceivedAck':True,
            'externalOwnershipConfirmed':True,**changes},self.session['sessionId'])

    def commit(self,value):
        return self.service.hosted_receiving.act(value['receipt']['id'],{'confirmed':True,'excludedNotReceivedAck':True,'planHash':value['operation']['plan_hash']},self.session['sessionId'],'commit')

    def test_plan_is_read_only_and_commit_mirrors_only_exact_proof_and_invalidates_files(self):
        receipt=self.claim(self.receipt());receipt['files']={'pdf':'stale.pdf','zpl':'stale.zpl','listing':'stale.csv','ticket':'stale-ticket.pdf'};self.service.store.save_receipt(receipt)
        value=self.prepare(receipt);self.assertEqual(self.remote.stock_calls,0)
        with patch.object(self.service.vault,'read',side_effect=AssertionError('Local token fallback')),patch.object(core,'upload_to_square',side_effect=AssertionError('Legacy upload')):
            received=self.commit(value)['receipt']
        self.assertEqual(self.remote.stock_calls,1);self.assertEqual(received['inventory_status'],'received');self.assertEqual(received['files'],{})
        self.assertTrue(all(card['sku'].startswith('SS-') for card in received['cards']));self.assertFalse(received['purchase'])
        self.assertEqual(self.service.cloud.receiving.read(),[])
        self.service.hosted_receiving.status(received['id'],self.session['sessionId']);self.assertEqual(self.remote.stock_calls,1)

    def test_lost_plan_and_commit_response_restart_original_key_no_second_stock(self):
        self.remote.drop_after.add('plan');receipt=self.claim(self.receipt())
        with self.assertRaises(CloudError):self.prepare(receipt)
        before=self.service.store.receipt(receipt['id'])['hosted_receive'];self.assertEqual(len(self.remote.receivings),1)
        self.assertTrue(self.service.cloud.receiving.vault.path.read_bytes().startswith(b'DP1'))
        self.assertNotIn(b'Fictional',self.service.cloud.receiving.vault.path.read_bytes())
        self.service=self.restart();value=self.service.hosted_receiving.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(before['actions']['plan'],value['receipt']['hosted_receive']['actions']['plan'])
        self.remote.drop_after.add('commit')
        with self.assertRaises(CloudError):self.commit(value)
        key=self.service.store.receipt(receipt['id'])['hosted_receive']['actions']['commit']['body']['request_key']
        self.service=self.restart();received=self.service.hosted_receiving.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(received['receipt']['hosted_receive']['actions']['commit']['body']['request_key'],key)
        self.assertEqual(self.remote.stock_calls,1);self.assertEqual(len(self.remote.receivings),1)

    def test_intent_and_encrypted_journal_failures_prevent_mutation(self):
        for boundary in ('intent','journal'):
            receipt=self.claim(self.receipt());before=len([item for item in self.remote.sent if item[0]!='GET'])
            target=self.service.store if boundary=='intent' else self.service.cloud.receiving.vault
            with patch.object(target,'save_receipt' if boundary=='intent' else 'set',side_effect=OSError('Fictional disk failure')),self.assertRaises(OSError):self.prepare(receipt)
            self.assertEqual(len([item for item in self.remote.sent if item[0]!='GET']),before)

    def test_terminal_result_retained_until_local_mirror_is_durable(self):
        value=self.prepare();save=self.service.store.save_receipt
        def fail(receipt,event=None):
            if event and event[0]=='hosted_received':raise OSError('Fictional SQLite failure')
            return save(receipt,event)
        with patch.object(self.service.store,'save_receipt',side_effect=fail),self.assertRaises(OSError):self.commit(value)
        self.assertTrue(any('result' in row and row['kind']=='commit' for row in self.service.cloud.receiving.read()))
        self.service=self.restart();result=self.service.hosted_receiving.status(value['receipt']['id'],self.session['sessionId'])
        self.assertEqual(result['receipt']['inventory_status'],'received');self.assertEqual(self.remote.stock_calls,1)

    def test_unknown_operation_never_replans_rekeys_commits_cancels_or_issues_credit(self):
        value=self.prepare();self.remote.unknown=True;unknown=self.commit(value);intent=unknown['receipt']['hosted_receive']
        for action in ('commit','cancel'):
            with self.subTest(action=action),self.assertRaises(CloudError):self.service.hosted_receiving.act(unknown['receipt']['id'],{'confirmed':True,'excludedNotReceivedAck':True,'planHash':intent['plan_hash'],'reason':'Not an escape hatch'},self.session['sessionId'],action)
        with self.assertRaises(StationError):self.service.accept_buy(unknown['receipt']['id'],'credit')
        self.remote.unknown=False;result=self.service.hosted_receiving.act(unknown['receipt']['id'],{},self.session['sessionId'],'recover')
        self.assertEqual(result['receipt']['inventory_status'],'received');self.assertEqual(self.remote.stock_calls,1)
        self.assertEqual(result['receipt']['hosted_receive']['actions']['commit'],intent['actions']['commit'])

    def test_disabled_flags_label_fit_and_server_mfa_prevent_stock(self):
        value=self.prepare();self.remote.write_enabled=False
        with self.assertRaisesRegex(CloudError,'disabled'):self.commit(value)
        self.remote.write_enabled=True;self.remote.mfa_fresh=False
        with self.assertRaises(CloudError) as caught:self.commit(value)
        self.assertEqual(caught.exception.code,'MFA_STEP_UP_REQUIRED');self.assertEqual(self.remote.stock_calls,0)
        self.remote.mfa_fresh=True;receipt=self.claim(self.receipt('Name,Game,Price,Quantity\nLong SKU,Magic,3,1\n'));self.remote.target_skus['Long SKU']='X'*100
        bad=self.prepare(receipt);self.assertTrue(bad['labelError'])
        with self.assertRaises(ValueError):self.commit(bad)
        self.assertEqual(self.remote.stock_calls,0)

    def test_source_profile_session_and_store_changes_cannot_transfer_intent(self):
        value=self.prepare();receipt=value['receipt'];original=copy.deepcopy(receipt)
        with self.assertRaises(StationError):self.service.save_receipt(receipt['id'],receipt)
        receipt['cards'][0]['qty']+=1;self.service.store.save_receipt(receipt)
        with self.assertRaises((CloudError,StationError)):self.service.hosted_receiving.status(receipt['id'],self.session['sessionId'])
        self.service.store.save_receipt(original);self.service.cloud.logout();other=self.service.cloud.login({'storeSlug':'another-store','email':'manager@example.invalid','password':'fixture-staff-password'})
        for nonce in (self.session['sessionId'],other['sessionId']):
            with self.assertRaises(CloudError):self.service.hosted_receiving.status(receipt['id'],nonce)
        self.assertEqual(self.remote.stock_calls,0)

    def test_corrupt_journal_is_owner_recovery_not_token_replacement(self):
        value=self.prepare()
        with patch.object(self.service.cloud.receiving.vault,'read',side_effect=StationError('DPAPI failed')):
            with self.assertRaises(CloudError) as caught:self.service.cloud.pending()
        self.assertTrue(caught.exception.pending);self.assertIn('Windows account',str(caught.exception));self.assertNotIn('Save replacement',str(caught.exception))
        self.assertEqual(self.remote.stock_calls,0)

    def test_malformed_terminal_proof_never_sets_local_received(self):
        value=self.prepare();self.remote.unknown=True;self.commit(value);operation=self.remote.receivings[('fictional-store',value['operation']['receiving_id'])]
        self.remote.complete(operation);operation['receipt']['confirmed_targets'][0]['confirmed_quantity']+=1
        operation['receipt']['receipt_hash']=fingerprint({key:item for key,item in operation['receipt'].items() if key!='receipt_hash'})
        with self.assertRaises(CloudError):self.service.hosted_receiving.status(value['receipt']['id'],self.session['sessionId'])
        self.assertEqual(self.service.store.receipt(value['receipt']['id'])['inventory_status'],'not_received');self.assertTrue(self.service.cloud.receiving.read())

    def test_fresh_reimport_shuffled_order_new_row_ids_recovers_original_profile_no_key_or_stock(self):
        received=self.commit(self.prepare());old=received['receipt'];keys=set(self.remote.requests);count=self.remote.stock_calls
        fresh=self.receipt('Name,Game,Price,Quantity\nFictional B,Riftbound,4.13,1\nFictional A,Magic,3.13,2\n')
        fresh=self.service.intake_claims.select_existing(fresh['id'],{'revision':fresh['revision'],'physicalIntakeId':old['intake_claim']['physical_intake_id'],'physicalDeliveryVerified':True},self.session['sessionId'])['receipt']
        self.assertNotEqual([row['row_id'] for row in fresh['cards']],[row['row_id'] for row in old['cards']])
        result=self.service.hosted_receiving.recover_existing(fresh['id'],{'revision':fresh['revision'],'receivingId':received['operation']['receiving_id']},self.session['sessionId'])
        self.assertEqual(result['receipt']['inventory_status'],'received');self.assertEqual(result['receipt']['hosted_label_profile'],self.profile)
        self.assertEqual({row['card_name']:row['sku'] for row in result['receipt']['cards']},{row['card_name']:row['sku'] for row in old['cards']})
        self.assertEqual(set(self.remote.requests),keys);self.assertEqual(self.remote.stock_calls,count)

    def test_fresh_csv_prices_cannot_replace_frozen_retail_plan(self):
        received=self.commit(self.prepare());old=received['receipt'];fresh=self.receipt('Name,Game,Price,Quantity\nFictional A,Magic,99,2\nFictional B,Riftbound,4.13,1\n')
        fresh=self.service.intake_claims.select_existing(fresh['id'],{'revision':fresh['revision'],'physicalIntakeId':old['intake_claim']['physical_intake_id'],'physicalDeliveryVerified':True},self.session['sessionId'])['receipt']
        before=copy.deepcopy(fresh);keys=set(self.remote.requests)
        result=self.service.hosted_receiving.recover_existing(fresh['id'],{'revision':fresh['revision'],'receivingId':received['operation']['receiving_id']},self.session['sessionId'])
        self.assertTrue(result['readOnly']);self.assertEqual(self.service.store.receipt(fresh['id']),before)
        self.assertEqual(set(self.remote.requests),keys);self.assertEqual(self.remote.stock_calls,1)

    def test_explicit_cancel_correction_revises_same_operation_and_frozen_profile(self):
        value=self.prepare();ident=value['receipt']['id'];operation=value['operation']['receiving_id']
        cancelled=self.service.hosted_receiving.act(ident,{'reason':'Wrong stock selected before write'},self.session['sessionId'],'cancel')['receipt']
        cancelled['cards'][0]['final_price']=7.13;cancelled['cards'][0]['operator_price']=True
        cancelled=self.service.save_receipt(ident,cancelled)
        revised=self.service.hosted_receiving.revise(ident,{'revision':cancelled['revision'],'workflowKind':'inventory_only','locationId':'FIXTURE-LOC',
            'labelProfile':{'stock':'3x1','layout':'standard','dpi':300},'newInventoryConfirmed':True,'excludedNotReceivedAck':True,'externalOwnershipConfirmed':True},self.session['sessionId'])
        self.assertEqual(revised['operation']['receiving_id'],operation);self.assertEqual(revised['operation']['revision'],1)
        self.assertEqual(revised['operation']['client_label_profile']['stock'],'3x1');self.assertEqual(len(self.remote.receivings),1);self.assertEqual(self.remote.stock_calls,0)

    def test_marker_injection_is_stripped_and_received_storage_save_restores_proof(self):
        receipt=self.receipt();forged=dict(receipt['cards'][0],hosted_sku_resolution={'state':'received'},sku='FORGED')
        self.assertNotIn('hosted_sku_resolution',self.service.validate_card(forged))
        received=self.commit(self.prepare())['receipt'];received['cards'][0]['storage_location']='Fictional bin'
        saved=self.service.save_receipt(received['id'],received);approved=self.service.approved_cards(saved)
        self.assertEqual([core.barcode_value(row) for row in approved],[row['sku'] for row in received['cards']])
        self.service.labels(saved['id'],'side',203)
        with self.assertRaises(StationError):self.service.labels(saved['id'],'standard',300)

    def test_accepted_trade_uses_immutable_financial_lines_and_copied_import_does_not_issue_credit(self):
        receipt=self.receipt();receipt['mode']='trade';receipt['requires_purchase']=True;self.service.store.save_receipt(receipt);receipt=self.claim(receipt)
        customer=self.service.cloud.create_customer({'request_key':'fixture-trade-customer','display_name':'Fictional Customer','permission_confirmed':True})['customer_id']
        draft=self.service.shared_trades.prepare(receipt['id'],{'revision':receipt['revision'],'customerId':customer},self.session['sessionId'])
        accepted=self.service.shared_trades.accept(receipt['id'],{'confirmationHash':draft['confirmationHash'],'customerAgreed':True,'itemsReceived':True,'claimReviewed':True},self.session['sessionId'])
        plan=self.prepare(accepted['receipt'],workflowKind='accepted_trade');body=plan['receipt']['hosted_receive']['plan_body']
        self.assertTrue(all('accepted_line_id' in row and 'identity' not in row for row in body['lines']))
        result=self.commit(plan);self.assertEqual(result['receipt']['purchase'],accepted['receipt']['purchase']);self.assertEqual(self.remote.issuances,1)
        fresh=self.receipt('Name,Game,Price,Quantity\nFictional B,Riftbound,4.13,1\nFictional A,Magic,3.13,2\n')
        fresh=self.service.intake_claims.select_existing(fresh['id'],{'revision':fresh['revision'],'physicalIntakeId':receipt['intake_claim']['physical_intake_id'],'physicalDeliveryVerified':True},self.session['sessionId'])['receipt']
        keys=set(self.remote.requests);bound=self.service.hosted_receiving.recover_existing(fresh['id'],{'revision':fresh['revision'],'receivingId':result['operation']['receiving_id']},self.session['sessionId'])
        self.assertEqual(bound['receipt']['inventory_status'],'received');self.assertFalse(bound['receipt']['purchase']);self.assertEqual(self.remote.issuances,1);self.assertEqual(set(self.remote.requests),keys)
        self.assertEqual(len(self.service.approved_cards(bound['receipt'])),2)

    def test_listing_retains_exact_ascii_sku_but_blocks_formula_prefix_without_rewriting(self):
        import csv
        self.remote.target_skus['Fictional A']=' lead>5^~ '
        received=self.commit(self.prepare())['receipt'];received['mode']='online';received['storage_location']='Fictional bin';self.service.store.save_receipt(received)
        prepared=self.service.prepare_online(received['id']);path=Path(core.OUTPUT_DIR)/prepared['files']['listing']
        with path.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.reader(stream))
        self.assertIn(' lead>5^~ ',[row[0] for row in rows[1:]])
        other=self.claim(self.receipt('Name,Game,Price,Quantity\nFormula SKU,Magic,2,1\n'));self.remote.target_skus['Formula SKU']='=exact-provider-sku'
        received=self.commit(self.prepare(other))['receipt'];received.update(mode='online',storage_location='Fictional bin');self.service.store.save_receipt(received)
        with self.assertRaisesRegex(StationError,'would change the SKU'):self.service.prepare_online(received['id'])
        self.assertEqual(received['cards'][0]['sku'],'=exact-provider-sku')

    def test_legacy_journal_and_pending_bytes_are_unchanged_and_claimed_financial_route_is_locked(self):
        receipt=self.claim(self.receipt());legacy=self.service.cloud.pending_vault;legacy.set(json.dumps({'version':1,'origin':'https://stock-and-sleeve-api.onrender.com','operations':[]}));before=legacy.path.read_bytes()
        value=self.prepare(receipt);self.assertEqual(legacy.path.read_bytes(),before)
        public=self.service.cloud.pending();self.assertTrue(any(row['action']=='hosted_receiving' for row in public['pending']));self.assertNotIn('plan_body',json.dumps(public));self.assertNotIn('source_manifest',json.dumps(public))
        customer=self.service.cloud.create_customer({'request_key':'another-customer','display_name':'Fictional Customer','permission_confirmed':True})['customer_id']
        with self.assertRaisesRegex(StationError,'financial route is locked'):self.service.shared_trades.prepare(receipt['id'],{'revision':value['receipt']['revision'],'customerId':customer},self.session['sessionId'])
        self.assertEqual(self.remote.issuances,0)

    def test_server_retirement_lost_reply_unlocks_only_after_exact_tombstone_and_fences_old_key(self):
        self.remote.plan_reject=True;receipt=self.claim(self.receipt())
        with self.assertRaises(CloudError):self.prepare(receipt)
        original=copy.deepcopy(self.service.store.receipt(receipt['id'])['hosted_receive']['actions']['plan']['body'])
        self.remote.offline=True
        with self.assertRaises(CloudError):self.service.hosted_receiving.retire(receipt['id'],{'retireUncreatedRequest':True},self.session['sessionId'])
        self.assertEqual(self.service.store.receipt(receipt['id'])['hosted_receive']['state'],'retiring')
        self.remote.offline=False;self.remote.drop_after.add('retire')
        with self.assertRaises(CloudError):self.service.hosted_receiving.retire(receipt['id'],{},self.session['sessionId'])
        self.assertEqual(len(self.service.cloud.receiving.read()),2)
        self.service=self.restart();result=self.service.hosted_receiving.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(result['receipt']['hosted_receive']['state'],'retired_before_creation')
        self.assertEqual(result['receipt']['hosted_receive']['server_retirement']['original_plan_sha256'],fingerprint(original));self.assertEqual(self.service.cloud.receiving.read(),[])
        session=self.service.cloud._saved();late=self.remote.request('POST','https://stock-and-sleeve-api.onrender.com/api/receiving/plans',headers={'Authorization':'Bearer '+session['token']},json=original)
        self.assertEqual(late.status_code,409);self.assertEqual(late.json()['code'],'PLAN_REQUEST_RETIRED');self.assertEqual(self.remote.receivings,{})
        receipt=result['receipt'];receipt['cards'][0]['final_price']=7.13;receipt=self.service.save_receipt(receipt['id'],receipt);self.remote.plan_reject=False
        new=self.prepare(receipt);self.assertNotEqual(new['receipt']['hosted_receive']['actions']['plan']['body']['request_key'],original['request_key'])
        self.assertEqual(new['receipt']['hosted_receiving_history'][-1]['actions']['plan']['body'],original);self.assertEqual(self.remote.stock_calls,0)

    def test_retirement_mirror_failure_keeps_proof_and_existing_operation_cannot_be_retired(self):
        self.remote.plan_reject=True;receipt=self.claim(self.receipt())
        with self.assertRaises(CloudError):self.prepare(receipt)
        save=self.service.store.save_receipt
        def fail(value,event=None):
            if event and event[0]=='hosted_receiving_retired':raise OSError('Fictional failed retirement mirror')
            return save(value,event)
        with patch.object(self.service.store,'save_receipt',side_effect=fail),self.assertRaises(OSError):self.service.hosted_receiving.retire(receipt['id'],{'retireUncreatedRequest':True},self.session['sessionId'])
        self.assertTrue(any(row['kind']=='retire' and 'result' in row for row in self.service.cloud.receiving.read()))
        self.service=self.restart();self.service.hosted_receiving.status(receipt['id'],self.session['sessionId']);self.remote.plan_reject=False
        plan=self.prepare(self.service.store.receipt(receipt['id']))
        with self.assertRaisesRegex(CloudError,'already has a receiving operation'):self.service.hosted_receiving.retire(receipt['id'],{'retireUncreatedRequest':True},self.session['sessionId'])
        self.assertEqual(plan['operation']['state'],'ready');self.assertEqual(self.remote.stock_calls,0)

    def test_existing_operation_conflict_can_explicitly_handoff_without_retiring_or_redispatch(self):
        original=self.prepare();claim=original['receipt']['intake_claim'];fresh=self.receipt('Name,Game,Price,Quantity\nFictional B,Riftbound,4.13,1\nFictional A,Magic,3.13,2\n')
        fresh=self.service.intake_claims.select_existing(fresh['id'],{'revision':fresh['revision'],'physicalIntakeId':claim['physical_intake_id'],'physicalDeliveryVerified':True},self.session['sessionId'])['receipt']
        with self.assertRaises(CloudError) as caught:self.prepare(fresh)
        self.assertEqual(caught.exception.receiving_id,original['operation']['receiving_id'])
        with self.assertRaises(CloudError):self.service.hosted_receiving.retire(fresh['id'],{'retireUncreatedRequest':True},self.session['sessionId'])
        fresh=self.service.store.receipt(fresh['id']);bound=self.service.hosted_receiving.recover_existing(fresh['id'],{'revision':fresh['revision'],'receivingId':original['operation']['receiving_id']},self.session['sessionId'])
        self.assertTrue(bound['receipt']['hosted_receive']['recovered_reference']);self.commit(original)
        result=self.service.hosted_receiving.status(fresh['id'],self.session['sessionId']);self.assertEqual(result['receipt']['inventory_status'],'received');self.assertEqual(self.remote.stock_calls,1)
        self.assertEqual(len(self.remote.receivings),1);self.assertEqual(self.service.cloud.receiving.read(),[])

    def test_exact_opaque_provider_ids_are_preserved_without_decoding_or_regex_adoption(self):
        from station_hosted_receive import provider_id
        self.remote.opaque=True;value=self.prepare();result=self.commit(value)
        self.assertEqual(result['operation']['provider']['merchant_id'],'MERCHANT:/opaque?x=%2F')
        self.assertEqual(result['receipt']['hosted_receive']['server_receipt']['confirmed_targets'][0]['variation_id'],'VAR:/opaque?x=%2F')
        for invalid in (' x','x ','..','a\nb','a'*256):
            with self.subTest(invalid=repr(invalid)),self.assertRaises(ValueError):provider_id(invalid)

    def test_excluded_financial_lines_and_unconfirmed_zero_fail_before_receiving_intent(self):
        from station_intake_claim import original_identity
        from station_shared_trade import fingerprint as trade_fingerprint
        receipt=self.claim(self.receipt());claim=self.remote.claims[('fictional-store',receipt['intake_claim']['physical_intake_id'])]
        trade_id=str(uuid.uuid4());claim.update(state='consumed',bound_trade_id=trade_id)
        evidence={'physical_intake_id':claim['physical_intake_id'],'physical_manifest_hash':claim['manifest_hash'],
            'lines':[{'id':str(uuid.uuid4()),'identity':original_identity(card),'source_line_key':card['source_line_key'],'quantity':card['qty'],'included':False} for card in receipt['cards']]}
        self.remote.trades[('fictional-store',trade_id)]={'id':trade_id,'state':'accepted_credit','accepted_receipt':evidence,'receipt_hash':trade_fingerprint(evidence)}
        with self.assertRaisesRegex(StationError,'exact accepted receipt line'):self.prepare(receipt,workflowKind='accepted_trade')
        self.assertNotIn('hosted_receive',self.service.store.receipt(receipt['id']));self.assertFalse(self.service.cloud.receiving.read())
        other=self.claim(self.receipt('Name,Game,Price,Quantity\nZero Confirmation,Magic,2,1\n'))
        other['cards'][0].update(final_price=0,price_confirmed=False,price_missing=False,reviewed=True);self.service.store.save_receipt(other)
        with self.assertRaisesRegex(StationError,'confirm each zero'):self.prepare(other)
        self.assertNotIn('hosted_receive',self.service.store.receipt(other['id']))


if __name__=='__main__':unittest.main()
