"""Capture real typed Flask/PG projections using only explicit fictional data.

This is contract evidence for the desktop verifier, not live provider acceptance.
The fixture wrapper supplies a private loopback DSN; it is never emitted.
"""
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hosted.app import create_app
from hosted.security import digest
from intake_manifest import source_line_id
import test_hosted_receiving_postgres as receiving_fixtures
from test_hosted_receiving import plan_data
from test_hosted_receiving_pipeline_postgres import PostgreSQLReceivingPipelineAcceptance
from tools.user_acceptance_receiving_templates_postgres import fingerprints as browser_sources


def fingerprints():
    value=browser_sources()
    for name in ('test_hosted_receiving_pipeline_postgres.py','test_hosted_receiving.py',
                 'tools/export_fictional_receiving_contract.py'):
        value[name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    return value


def run():
    destination=ROOT/'Testing/receiving-saga'/('contract-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    destination.mkdir(parents=True,exist_ok=False)
    before=fingerprints()
    fixture_class=PostgreSQLReceivingPipelineAcceptance
    cases=[];prepared=False;active=[]
    try:
        fixture_class.setUpClass();prepared=True
        for name in ('inventory_single','inventory_coalesced','accepted_trade_single','inventory_lost_catalog','inventory_desktop_canonical'):
            case=fixture_class('test_new_price_creates_preserved_sibling_then_freshly_admits_exact_stock')
            active.append(case)
            original_card=receiving_fixtures.card_line
            def desktop_card(**kwargs):
                value=original_card(**kwargs)
                value['identity'].update(game='pokemon',game_label='Pok\u00e9mon',condition='Near Mint',printing='Normal',language='English')
                return value
            with patch.object(receiving_fixtures,'card_line',desktop_card) if name=='inventory_desktop_canonical' else nullcontext():
                case.setUp()
            token='fictional-contract-session-'+uuid.uuid4().hex
            with case.database.transaction() as db:
                db.execute("""INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                              VALUES (%s,%s,%s,clock_timestamp()+interval '1 hour',1,clock_timestamp())""",
                           (digest(token),case.tenant,case.actor['staff_id']))
            case.actor['session_hash']=digest(token)
            body=plan_data(case.claim)
            if name=='inventory_desktop_canonical':
                body['lines'][0].update(identity=deepcopy(case.original['identity']),source_line_key=source_line_id(case.original['identity']))
            body['lines'][0]['selling_cents']=1250
            item={'name':name,'physical_claim':case.claim,'source_manifest':case.manifest,'original_request':body}
            if name=='inventory_coalesced':
                body['lines'][0]['quantity']=1
                body['lines'].append(deepcopy(body['lines'][0])|{'review_line_id':str(uuid.uuid4())})
            if name=='accepted_trade_single':
                trade=case.trade();case.accept_trade(trade)
                accepted=case.trades.get(case.actor,trade['trade_id'])
                item['accepted_trade']=accepted
                body.pop('external_ownership_confirmed')
                body.update(workflow_kind='accepted_trade',accepted_trade_id=trade['trade_id'],accepted_receipt_hash=accepted['receipt_hash'])
                body['lines'][0].pop('identity')
                body['lines'][0]['accepted_line_id']=accepted['accepted_receipt']['lines'][0]['id']
            app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://contract.fixture.invalid',
                            'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode(),'BOOTSTRAP_TOKEN':'',
                            'SQUARE_APPLICATION_ID':'','SQUARE_APPLICATION_SECRET':'','SQUARE_WEBHOOK_SIGNATURE_KEY':'',
                            'SQUARE_ENVIRONMENT':'sandbox','RECEIVING_WRITES_ENABLED':True},database=case.database)
            service=app.extensions['ss_receiving'];service.provider=case.provider;service.templates.provider=case.provider
            client=app.test_client();client.set_cookie('ss_session',token,domain='contract.fixture.invalid')
            base='https://contract.fixture.invalid'
            me=client.get('/api/me',base_url=base)
            assert me.status_code==200
            headers={'X-SS-CSRF':me.get_json()['csrf_token']}
            def post(path,payload,expected=200):
                result=client.post(path,json=payload,headers=headers,base_url=base)
                assert result.status_code==expected,(result.status_code,result.get_json())
                return result.get_json()
            plan=post('/api/receiving/plans',body,201)
            assert plan['targets'][0]['action']=='create_price' and plan['state']=='ready'
            item['ready']=plan
            terms={'request_key':'fictional-commit-'+uuid.uuid4().hex,'revision':plan['revision'],
                   'plan_hash':plan['plan_hash'],'confirmed':True,'excluded_not_received_ack':True}
            if name=='inventory_lost_catalog':case.provider.catalog_behavior='lost_response'
            committed=post('/api/receiving/'+plan['receiving_id']+'/commit',terms)
            if name=='inventory_lost_catalog':
                assert committed['state']=='outcome_unknown';item['catalog_unknown']=committed
                committed=post('/api/receiving/'+plan['receiving_id']+'/recover',
                    {'request_key':'fictional-recover-'+uuid.uuid4().hex,'revision':plan['revision'],'plan_hash':plan['plan_hash']})
            assert committed['state']=='catalog_confirmed' and committed['catalog_confirmed'] and not committed['inventory_confirmed']
            assert (len(case.provider.catalog_calls),len(case.provider.calls))==(1,0)
            item['catalog_confirmed']=committed
            final=post('/api/receiving/'+plan['receiving_id']+'/authorize',
                {'request_key':'fictional-authorize-'+uuid.uuid4().hex,'revision':plan['revision'],
                 'plan_hash':plan['plan_hash'],'resume_same_operation':True})
            assert final['state']=='received' and final['inventory_confirmed']
            assert (len(case.provider.catalog_calls),len(case.provider.calls))==(1,1)
            recovered=client.get('/api/receiving/'+plan['receiving_id'],base_url=base)
            assert recovered.status_code==200 and recovered.get_json()==final
            item['received']=final
            item['provider_fixture_counts']={'catalog_calls':1,'inventory_calls':1}
            cases.append(item)
    finally:
        for case in reversed(active):case.doCleanups()
        if prepared:fixture_class.tearDownClass()
    after=fingerprints()
    document={'generated_utc':datetime.now(timezone.utc).isoformat(),
              'scope':'Five actual Flask API/PostgreSQL scenarios in a disposable ss_test namespace with fictional MFA sessions and injected fictional Square provider. No live Square or login/enrollment proof.',
              'source_stable':before==after,'sources_before':before,'sources_after':after,'cases':cases}
    wire=json.dumps(document,ensure_ascii=False,indent=2,default=str)+'\n'
    assert 'fictional-server-token-never-to-client' not in wire and 'encrypted_body' not in wire and 'csrf_token' not in wire
    assert before==after,'Source changed during contract generation; regenerate before using as current proof.'
    path=destination/'fictional-newprice-api-contract.json'
    path.write_text(wire,encoding='utf-8')
    print(json.dumps({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'cases':len(cases),'source_stable':True}))


if __name__=='__main__':run()
