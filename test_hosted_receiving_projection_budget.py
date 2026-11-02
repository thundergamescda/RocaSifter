"""Independent typed receiving wire-budget checks; no SQL or provider HTTP.

These use maximal declared public dimensions, not a claim that an actual Square
account has issued these fictional opaque IDs. Admission side effects are tested
separately with disposable PostgreSQL by the domain suite.
"""
from copy import deepcopy
import json
import unittest
import uuid
from unittest.mock import Mock

from cryptography.fernet import Fernet
from hosted.app import create_app
from hosted.receiving import Receiving
from hosted.receiving_pipeline import CatalogReceiving
from hosted.receiving_plan import (PUBLIC_RESPONSE_BYTES, ReceivingError,
    canonical, fingerprint, normalize_plan, public_response_bytes)
from hosted.security import provider_id
from station_cloud import CloudClient
from test_hosted_receiving import plan_data


def uid(number):return str(uuid.UUID(int=number+1))


def opaque(number):
    """255 characters with four-byte Unicode, symbols and interior whitespace."""
    return f'{number:04d}'+('\U0001f0a1'*246)+' /?>~'


def large_plan(*,catalog=False):
    """1,000 price-split reviews on 100 selected of 1,000 original groups."""
    targets=[];selected={f'{i:064x}':10 for i in range(100)}
    excluded={f'{i:064x}':0 if i<100 else 1 for i in range(1000)}
    for i in range(1000):
        group=i%100
        target={'review_line_id':uid(i),'source_line_key':f'{group:064x}',
            'accepted_line_id':uid(i+5000),'identity_hash':f'{group:064x}',
            'quantity':1,'selling_cents':100_000_000,'currency':'USD',
            'binding_id':uid(group+10000),'binding_version':2**31-1,
            'binding_evidence_hash':'a'*64,'source_sku':'\U0001f0a1'*100,
            'target_sku':f'{group:012d}' if catalog else '\U0001f0a1'*97+f'{group:03d}',
            'variation_id':None if catalog else opaque(group),
            'parent_id':opaque(group+100),'action':'create_price' if catalog else 'reuse_existing'}
        if catalog:
            target.update(binding_role='source_family',template_id=uid(group+20000),
                template_version=2**31-1,template_evidence_hash='b'*64)
        targets.append(target)
    result={'physical_intake_id':uid(30000),'physical_manifest_hash':'c'*64,
        'source_snapshot_hash':'d'*64,'workflow_kind':'accepted_trade',
        'accepted_trade_id':uid(30001),'accepted_receipt_hash':'e'*64,
        'client_label_profile':{'stock':'4x2','layout':'side','dpi':300},
        'provider':{'merchant_id':opaque(201),'application_id':opaque(202),
            'location_id':opaque(203),'environment':'production'},
        'selected_quantities':selected,'excluded_quantities':excluded,'targets':targets}
    if catalog:
        result.update(catalog_required=True,catalog_aliases={t['review_line_id']:targets[i%100]['review_line_id'] for i,t in enumerate(targets)},
            catalog_additions=[{key:t[key] for key in ('review_line_id','identity_hash','parent_id','target_sku',
                'selling_cents','template_id','template_version')} for t in targets[:100]])
    return result


class CapturedReceiving(Receiving):
    def _receipt(self,row,*args,**kwargs):
        self.projected_row=row
        return super()._receipt(row,*args,**kwargs)


class CapturedCatalogReceiving(CatalogReceiving):
    def _receipt(self,row,*args,**kwargs):
        self.projected_row=row
        return super()._receipt(row,*args,**kwargs)


class ReceivingProjectionBudgetTests(unittest.TestCase):
    def service(self,catalog=False):
        return (CapturedCatalogReceiving if catalog else CapturedReceiving)(Mock(),Mock(),Fernet(Fernet.generate_key()))

    def test_maximal_same_price_rows_and_opaque_unicode_are_not_dropped_or_ascii_escaped(self):
        plan=large_plan();service=self.service();amount=service._projection_budget(plan,uid(40000))
        result=service._view(service.projected_row);wire=public_response_bytes({'found':True,'result':result})
        self.assertEqual(amount,len(wire));self.assertLessEqual(amount,PUBLIC_RESPONSE_BYTES)
        parsed=json.loads(wire);public=parsed['result'];self.assertTrue(parsed['found'])
        self.assertEqual(len(public['targets']),1000);self.assertEqual(len(public['receipt']['confirmed_targets']),1000)
        self.assertEqual(len({t['variation_id'] for t in public['targets']}),100)
        self.assertEqual(sum(t['confirmed_quantity'] for t in public['receipt']['confirmed_targets']),1000)
        self.assertEqual(len(public['excluded_quantities']),1000)
        self.assertEqual(public['receipt']['provider'],plan['provider'])
        for value in list(plan['provider'].values())[:-1]+[opaque(99)]:self.assertEqual(provider_id(value),value)
        self.assertIn('\U0001f0a1'.encode(),wire);self.assertNotIn(b'\\ud83c',wire)
        # Default ASCII serialization is larger than the supported wire format.
        self.assertGreater(len(json.dumps(parsed,sort_keys=True,separators=(',',':')).encode()),len(wire))

    def test_catalog_projection_reserves_generated_ids_aliases_receipt_and_status_envelope(self):
        plan=large_plan(catalog=True);service=self.service(True)
        conservative=service._projection_budget(plan,uid(40001),revision=7)
        worst=deepcopy(service.projected_row);view=service._view(worst)
        self.assertEqual(len(worst['catalog_resolutions']),100)
        self.assertEqual(sum(len(row['review_line_ids']) for row in worst['catalog_resolutions']),1000)
        self.assertEqual(len(view['receipt']['confirmed_targets']),1000)
        self.assertEqual(len(view['receipt']['catalog_resolutions']),100)
        self.assertEqual(conservative,len(canonical({'found':True,'result':view})))
        self.assertGreater(conservative,len(canonical(view)))
        self.assertLessEqual(conservative,PUBLIC_RESPONSE_BYTES)
        # An actual supported generated result may be shorter, never longer than
        # the pre-admission ID/version/date reservation for the same facts.
        actual=deepcopy(worst);actual['revision']=7
        for i,row in enumerate(actual['catalog_resolutions']):
            row.update(variation_id=opaque(i+400),binding_id=uid(i+50000))
        actual['receipt']=service._receipt(actual,service._final_targets(actual),actual['inventory_proof'],
            received_at='2026-11-01T22:00:00.000001+00:00')
        actual_view=service._view(actual);actual_wire=public_response_bytes({'found':True,'result':actual_view})
        self.assertLessEqual(len(actual_wire),conservative)
        self.assertEqual(len(json.loads(actual_wire)['result']['receipt']['confirmed_targets']),1000)

    def test_normalized_1000_row_input_is_still_bounded_independently(self):
        body=plan_data();base=body['lines'][0];body['workflow_kind']='accepted_trade'
        body.pop('external_ownership_confirmed');body.update(accepted_trade_id=uid(60000),accepted_receipt_hash='f'*64)
        body['lines']=[]
        for i in range(1000):
            row={k:v for k,v in base.items() if k!='identity'}
            row.update(review_line_id=uid(i),accepted_line_id=uid(i+1000));body['lines'].append(row)
        self.assertEqual(len(normalize_plan(body)['lines']),1000)
        self.assertLess(len(canonical(body)),512*1024)

    def test_exact_compact_utf8_limit_accepts_all_bytes_and_plus_one_rejects(self):
        payload={'wire':''};payload['wire']='x'*(PUBLIC_RESPONSE_BYTES-len(canonical(payload)))
        exact=public_response_bytes(payload)
        self.assertEqual(len(exact),PUBLIC_RESPONSE_BYTES);self.assertEqual(json.loads(exact),payload)
        payload['wire']+='\U0001f0a1'
        self.assertEqual(len(canonical(payload)),PUBLIC_RESPONSE_BYTES+4)
        with self.assertRaises(ReceivingError) as caught:public_response_bytes(payload)
        self.assertEqual((caught.exception.status,caught.exception.code),(409,'RECEIVING_PROJECTION_LIMIT'))
        payload['wire']=payload['wire'][:-1]+'x'
        with self.assertRaises(ReceivingError):public_response_bytes(payload)


class ReceivingProjectionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.actor={'tenant_id':uuid.uuid4(),'staff_id':uuid.uuid4(),'role':'owner','slug':'fictional-store'}
        self.app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://wire.fixture.invalid',
            'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=Mock())
        self.app.extensions['ss_auth'].resolve=Mock(return_value=self.actor)
        self.service=self.app.extensions['ss_receiving'];self.client=self.app.test_client()
        self.base='https://wire.fixture.invalid';self.headers={'Authorization':'Bearer fictional-session'}
        self.ident=uid(70000)

    def test_all_operation_routes_emit_exact_same_compact_utf8_bytes_including_wrapper(self):
        payload={'value':'\U0001f0a1 /?>~','targets':[{'review_line_id':uid(i)} for i in range(1000)]}
        routes=[('post','/api/receiving/plans','create_plan'),('get','/api/receiving/'+self.ident,'get'),
            ('put','/api/receiving/'+self.ident+'/plan','revise')]
        routes += [('post','/api/receiving/'+self.ident+'/'+action,action) for action in ('commit','recover','authorize','cancel')]
        routes += [('post','/api/receiving/requests/key/retire','retire_plan_request')]
        for method,path,name in routes:
            setattr(self.service,name,Mock(return_value=payload))
            with self.subTest(path=path):
                r=getattr(self.client,method)(path,base_url=self.base,headers=self.headers,**({'json':{}} if method!='get' else {}))
                self.assertIn(r.status_code,(200,201));self.assertEqual(r.data,canonical(payload));self.assertEqual(r.json,payload)
        self.service.request_status=Mock(return_value={'found':True,'result':payload})
        r=self.client.get('/api/receiving/requests/key',base_url=self.base,headers=self.headers)
        self.assertEqual(r.data,canonical({'found':True,'result':payload}));self.assertEqual(len(r.json['result']['targets']),1000)

    def test_request_status_wrapper_cannot_push_complete_result_past_wire_limit(self):
        payload={'wire':''};payload['wire']='x'*(PUBLIC_RESPONSE_BYTES-len(canonical(payload)))
        self.service.get=Mock(return_value=payload);self.service.request_status=Mock(return_value={'found':True,'result':payload})
        r=self.client.get('/api/receiving/'+self.ident,base_url=self.base,headers=self.headers)
        self.assertEqual((r.status_code,len(r.data)),(200,PUBLIC_RESPONSE_BYTES));self.assertEqual(r.json,payload)
        r=self.client.get('/api/receiving/requests/key',base_url=self.base,headers=self.headers)
        self.assertEqual(r.status_code,409);self.assertEqual(r.json['code'],'RECEIVING_PROJECTION_LIMIT')
        self.assertNotIn('wire',r.json);self.assertLess(len(r.data),1000)

    def test_request_input_limit_and_unrelated_json_serialization_are_unchanged(self):
        self.assertEqual(self.app.config['MAX_CONTENT_LENGTH'],512*1024)
        self.service.create_plan=Mock(return_value={'unexpected':'not admitted'})
        oversized=canonical({'padding':'x'*(512*1024)})
        r=self.client.post('/api/receiving/plans',base_url=self.base,headers=self.headers,data=oversized,content_type='application/json')
        self.assertEqual(r.status_code,413);self.service.create_plan.assert_not_called()
        self.service.readiness=Mock(return_value={'label':'\U0001f0a1'})
        r=self.client.get('/api/receiving/readiness',base_url=self.base,headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json,{'label':'\U0001f0a1'})
        self.assertNotEqual(r.data,canonical(r.json));self.assertIn(b'\\ud83c',r.data)

    def test_hosted_exact_status_wrapper_is_consumable_by_desktop_without_second_action(self):
        payload={'found':True,'result':{'state':'received','receipt':{'all_rows':[uid(i) for i in range(1000)]}}}
        self.service.request_status=Mock(return_value=payload)
        r=self.client.get('/api/receiving/requests/key',base_url=self.base,headers=self.headers)
        class Response:
            status_code=r.status_code
            content=r.data
            def json(self):return json.loads(self.content)
        transport=Mock();transport.request.return_value=Response()
        client=CloudClient.__new__(CloudClient);client.transport=transport
        got=client._call('GET','/api/receiving/requests/key')
        self.assertEqual(got,payload);transport.request.assert_called_once()


if __name__=='__main__':unittest.main()
