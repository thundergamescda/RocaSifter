"""Isolated desktop/shared-service relay tests. All identities and tokens are fictional."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
import uuid
import requests
from station_cloud import CloudClient, CloudError, ORIGIN

TOKEN='fictional-session-token-1234567890'


def response(status,body):
    result=requests.Response();result.status_code=status
    result._content=json.dumps(body).encode();result.headers['Content-Type']='application/json'
    return result


class FixtureVault:
    def __init__(self,root):self.path=Path(root)/'fictional-test-session'
    def set(self,value):self.path.write_text(value)
    def read(self):return self.path.read_text() if self.path.exists() else ''


class SharedFixtureTransport:
    """Deterministic fake API for two desktop clients and browser walkthroughs.

    The real PostgreSQL service is tested independently. This fake never contacts
    a provider or stores real people; it exercises client/desktop UI behavior.
    """
    def __init__(self):
        self.trust_env=False;self.lock=threading.Lock();self.offline=False
        self.sessions={};self.customers={};self.entries={};self.requests={};self.calls=[]
    def request(self,method,url,*,headers,json=None,**options):
        if self.offline:raise requests.ConnectionError('fictional outage')
        uri=urlsplit(url)
        assert url.startswith(ORIGIN+'/') and options['allow_redirects'] is False
        with self.lock:
            self.calls.append((method,uri.path))
            if uri.path=='/api/auth/login':
                if json['password']!='fixture-staff-password':return response(401,{'error':'Store, email or password is incorrect.'})
                token='fixture-session-'+uuid.uuid4().hex
                role='cashier' if json['email'].startswith('cashier') else 'manager'
                self.sessions[token]={'store':json['store_slug'],'role':role}
                return response(200,{'store':json['store_slug'],'role':role,'expires_in':28800,'access_token':token})
            token=headers.get('Authorization','').removeprefix('Bearer ')
            actor=self.sessions.get(token)
            if not actor:return response(401,{'error':'Sign in to your store.'})
            store=actor['store']
            if uri.path=='/api/me':return response(200,actor)
            if uri.path=='/api/auth/logout':
                self.sessions.pop(token);return response(200,{'signed_out':True})
            if uri.path.startswith('/api/credit/requests/'):
                record=self.requests.get((store,uri.path.rsplit('/',1)[1]))
                return response(200,{'found':bool(record),'result':record[1] if record else None})
            if uri.path=='/api/credit/customers' and method=='GET':
                query=parse_qs(uri.query).get('q',[''])[0].casefold()
                return response(200,{'customers':[c for c in self.customers.values() if c['store']==store and query in (c['display_name']+' '+c['contact']).casefold()]})
            key=(store,(json or {}).get('request_key'))
            if method=='POST' and key[1] in (None,''):return response(400,{'error':'Request key is required.'})
            if method=='POST' and key in self.requests:
                old_body,old_response=self.requests[key]
                if old_body!=json:return response(409,{'error':'Request key was reused with different details.'})
                return response(200,old_response)
            if uri.path=='/api/credit/customers' and method=='POST':
                if json.get('permission_confirmed') is not True:return response(400,{'error':'Confirm authority to record this customer.'})
                ident=str(uuid.uuid4());self.customers[ident]={'id':ident,'store':store,'display_name':json['display_name'],'contact':json.get('contact') or '',
                    'square_customer_id':json.get('square_customer_id'),'balance_cents':0,'available_cents':0,'reserved_cents':0,'version':0}
                self.entries[ident]=[];result={'customer_id':ident};self.requests[key]=(dict(json),result)
                return response(201,result)
            parts=uri.path.split('/');ident=parts[4] if len(parts)>4 else ''
            customer=self.customers.get(ident)
            if not customer or customer['store']!=store:return response(404,{'error':'Customer not found.'})
            if method=='GET':return response(200,{'customer':customer,'entries':self.entries[ident]})
            if actor['role'] not in ('owner','manager'):return response(403,{'error':'Your store role cannot perform this action.'})
            if json['version']!=customer['version']:return response(409,{'error':'The balance changed. Refresh before adjusting.'})
            amount=json['amount_cents']*(-1 if json['kind']=='adjust_remove' else 1)
            if customer['available_cents']+amount<0:return response(409,{'error':'Insufficient available credit.'})
            customer['balance_cents']+=amount;customer['available_cents']+=amount;customer['version']+=1
            entry={'id':str(uuid.uuid4()),'at':'2026-10-28T12:00:00+00:00','kind':json['kind'],'amount_cents':amount,
                   'balance_after_cents':customer['balance_cents'],'reference':json['reference'],'reason':json['reason']}
            self.entries[ident].insert(0,entry);result={'entry_id':entry['id'],'balance_cents':customer['balance_cents'],'version':customer['version']}
            self.requests[key]=(dict(json),result);return response(200,result)


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.vault=FixtureVault(self.root)
        self.transport=Mock();self.transport.request.return_value=response(200,{'access_token':TOKEN,'store':'fictional-store','role':'manager','expires_in':28800})
        self.client=CloudClient(self.root,transport=self.transport,vault=self.vault,clock=lambda:1000)
    def login(self):return self.client.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
    def test_login_fixed_origin_no_password_or_token_in_public_result(self):
        result=self.login();self.assertTrue(result['signedIn']);self.assertNotIn(TOKEN,json.dumps(result))
        self.assertNotIn('fixture-staff-password',self.vault.read())
        args,options=self.transport.request.call_args
        self.assertEqual(args,('POST',ORIGIN+'/api/auth/login'));self.assertFalse(options['allow_redirects']);self.assertFalse(self.transport.trust_env)
        self.assertEqual(json.loads(self.vault.read())['expires_at'],29800)
    def test_redirect_and_malformed_login_never_save_session(self):
        self.transport.request.return_value=response(302,{'location':'https://attacker.invalid'})
        with self.assertRaises(CloudError):self.login()
        self.assertFalse(self.vault.path.exists())
        self.transport.request.return_value=response(200,{'access_token':TOKEN,'store':'other-store','role':'manager','expires_in':28800})
        with self.assertRaises(CloudError):self.login()
        self.assertFalse(self.vault.path.exists())
    def test_expired_and_revoked_sessions_clear_and_do_not_reuse(self):
        self.login();self.client.clock=lambda:30000
        self.assertFalse(self.client.status()['signedIn']);self.assertFalse(self.vault.path.exists())
        self.client.clock=lambda:1000;self.login();self.transport.request.return_value=response(401,{'error':'Expired'})
        self.assertFalse(self.client.status()['signedIn']);self.assertFalse(self.vault.path.exists())
    def test_tampered_origin_never_transmits_token(self):
        self.login();saved=json.loads(self.vault.read());saved['origin']='https://attacker.invalid';self.vault.set(json.dumps(saved))
        self.transport.request.reset_mock()
        with self.assertRaises(CloudError):self.client.customers()
        self.transport.request.assert_not_called()
    def test_uncertain_write_keeps_session_and_request_key_for_retry(self):
        self.login();self.transport.request.side_effect=requests.Timeout('fictional sensitive error')
        ident=str(uuid.uuid4());body={'request_key':'same-key','amount_cents':500,'kind':'adjust_add','reference':'FIXTURE','reason':'Fixture correction','version':1}
        with self.assertRaises(CloudError) as error:self.client.adjust(ident,body)
        self.assertTrue(error.exception.offline);self.assertNotIn('sensitive',str(error.exception));self.assertTrue(self.vault.path.exists())
        self.transport.request.side_effect=None;self.transport.request.return_value=response(200,{'balance_cents':500})
        self.client.adjust(ident,body)
        self.assertEqual(self.transport.request.call_args.kwargs['json']['request_key'],'same-key')
    def test_offline_logout_clears_local_session_without_claiming_revocation(self):
        self.login();self.transport.request.side_effect=requests.ConnectionError('fixture')
        result=self.client.logout();self.assertFalse(result['serverRevoked']);self.assertFalse(self.vault.path.exists())
        self.assertIn('expires within eight hours',result['message'])
    def test_query_is_encoded_invalid_id_and_trade_do_not_call_remote(self):
        self.login();self.transport.request.return_value=response(200,{'customers':[]})
        self.client.customers('a&tenant=other?')
        self.assertTrue(self.transport.request.call_args.args[1].endswith('q=a%26tenant%3Dother%3F'))
        self.transport.request.reset_mock()
        for call in (lambda:self.client.account('../other'),lambda:self.client.adjust(str(uuid.uuid4()),{'kind':'trade_credit'})):
            with self.assertRaises(CloudError):call()
        self.transport.request.assert_not_called()
    def test_windows_vault_encrypts_staff_session_and_reopens(self):
        import os
        if os.name!='nt':self.skipTest('Windows DPAPI acceptance')
        client=CloudClient(self.root,transport=self.transport,clock=lambda:1000)
        client.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
        content=(self.root/'cloud.session').read_bytes();self.assertTrue(content.startswith(b'DP1'));self.assertNotIn(TOKEN.encode(),content)
        self.assertFalse((self.root/'square.secret').exists());self.assertEqual(client._saved()['token'],TOKEN)
    def test_two_desktops_share_state_and_other_store_cannot_read_it(self):
        remote=SharedFixtureTransport();other_root=self.root/'till-b';other_root.mkdir()
        first=CloudClient(self.root,transport=remote,vault=self.vault)
        second=CloudClient(other_root,transport=remote,vault=FixtureVault(other_root))
        credentials={'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'}
        first.login(credentials);second.login(credentials)
        created=first.create_customer({'request_key':'customer','display_name':'Fictional customer','permission_confirmed':True})
        ident=created['customer_id'];entry={'request_key':'opening','amount_cents':5000,'kind':'opening','reference':'FIXTURE','reason':'Fixture opening','version':0}
        first.adjust(ident,entry);self.assertEqual(second.account(ident)['customer']['balance_cents'],5000)
        second.adjust(ident,{'request_key':'remove','amount_cents':2000,'kind':'adjust_remove','reference':'FIXTURE','reason':'Fixture correction','version':1})
        self.assertEqual(first.account(ident)['customer']['balance_cents'],3000)
        second.logout();second.login(credentials|{'storeSlug':'another-store'})
        with self.assertRaises(CloudError) as error:second.account(ident)
        self.assertEqual(error.exception.status,404)

    def test_crash_after_server_commit_recovers_original_key_without_double_correction(self):
        remote=SharedFixtureTransport();client=CloudClient(self.root,transport=remote,vault=self.vault)
        client.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
        ident=client.create_customer({'request_key':'fixture-customer','display_name':'Fictional account','permission_confirmed':True})['customer_id']
        original=remote.request
        def commit_then_lose_response(method,url,**options):
            result=original(method,url,**options)
            if url.endswith('/adjust'):raise requests.Timeout('Fictional lost response after commit')
            return result
        remote.request=commit_then_lose_response
        body={'request_key':'durable-original','amount_cents':5000,'kind':'opening','reference':'FIXTURE','reason':'Fixture opening','version':0}
        with self.assertRaises(CloudError):client.adjust(ident,body)
        self.assertEqual(remote.customers[ident]['balance_cents'],5000)
        self.assertTrue((self.root/'cloud.pending').exists())
        remote.request=original
        restarted=CloudClient(self.root,transport=remote,vault=self.vault)
        self.assertEqual(restarted.pending()['pending'][0]['requestKey'],'durable-original')
        with self.assertRaises(CloudError):restarted.adjust(ident,body|{'request_key':'dangerous-new-key','version':1})
        result=restarted.recover('durable-original',retry=True)
        self.assertTrue(result['alreadyCompleted']);self.assertEqual(remote.customers[ident]['balance_cents'],5000)
        self.assertEqual(restarted.pending()['pending'],[]);self.assertFalse((self.root/'cloud.pending').exists())

    def test_pending_retry_after_no_commit_and_new_staff_session_uses_same_key(self):
        remote=SharedFixtureTransport();client=CloudClient(self.root,transport=remote,vault=self.vault)
        credentials={'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'}
        client.login(credentials)
        remote.offline=True
        with self.assertRaises(CloudError):client.create_customer({'request_key':'pending-customer','display_name':'Fictional account','permission_confirmed':True})
        client.logout();remote.offline=False;client.login(credentials)
        self.assertFalse(client.recover('pending-customer')['resolved'])
        result=client.recover('pending-customer',retry=True)
        self.assertTrue(result['resolved']);self.assertFalse(result['alreadyCompleted']);self.assertEqual(len(remote.customers),1)

    def test_pending_actions_are_not_exposed_or_replayed_to_another_store(self):
        remote=SharedFixtureTransport();client=CloudClient(self.root,transport=remote,vault=self.vault)
        credentials={'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'}
        client.login(credentials);remote.offline=True
        with self.assertRaises(CloudError):client.create_customer({'request_key':'pending-customer','display_name':'Private fictional account','permission_confirmed':True})
        client.logout();remote.offline=False;client.login(credentials|{'storeSlug':'another-store'})
        result=client.pending();self.assertEqual(result['pending'],[]);self.assertTrue(result['hasOtherPending'])
        with self.assertRaises(CloudError):client.recover('pending-customer',retry=True)

    def test_corrupt_pending_journal_blocks_mutations_and_signed_out_details_stay_private(self):
        remote=SharedFixtureTransport();client=CloudClient(self.root,transport=remote,vault=self.vault)
        session=client.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
        remote.offline=True
        with self.assertRaises(CloudError):
            client.create_customer({'request_key':'original-private-key','display_name':'Private fictional pending customer','permission_confirmed':True})
        original=client.pending_vault.read();payload=json.loads(original)
        altered=json.loads(original);altered['operations'][0]['body']['display_name']='Changed without its original fingerprint'
        corruptions={'malformed_json':'not JSON',
                     'wrong_origin':json.dumps(payload|{'origin':'https://attacker.invalid'}),
                     'changed_body':json.dumps(altered)}
        calls=len(remote.calls)
        for name,corrupt in corruptions.items():
            with self.subTest(corruption=name):
                client.pending_vault.set(corrupt)
                encrypted_before=client.pending_vault.path.read_bytes()
                with self.assertRaises(CloudError) as error:
                    client.create_customer({'request_key':'must-not-send','display_name':'Must not be transmitted','permission_confirmed':True})
                self.assertEqual(error.exception.status,409);self.assertTrue(error.exception.pending)
                self.assertEqual(len(remote.calls),calls,'A corrupt recovery journal must stop new network mutations.')
                self.assertEqual(client.pending_vault.path.read_bytes(),encrypted_before,'Keep the journal unchanged for recovery.')
        client.pending_vault.set(original)
        client.logout(expected_session_id=session['sessionId'])
        details=client.pending(expected_session_id=session['sessionId'])
        self.assertEqual(details,{'pending':[],'hasOtherPending':True})
        self.assertNotIn('original-private-key',json.dumps(details))
        self.assertNotIn('Private fictional pending customer',json.dumps(details))
        self.assertEqual(client.pending_vault.read(),original,'Signing out must not discard an uncertain action.')

    def test_stale_window_cannot_write_read_or_sign_out_new_staff_session(self):
        remote=SharedFixtureTransport();client=CloudClient(self.root,transport=remote,vault=self.vault)
        credentials={'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'}
        old=client.login(credentials);client.logout(expected_session_id=old['sessionId'])
        fresh=client.login(credentials|{'storeSlug':'another-store'})
        calls=len(remote.calls)
        for action in (lambda:client.create_customer({'request_key':'stale','display_name':'Wrong store','permission_confirmed':True},expected_session_id=old['sessionId']),
                       lambda:client.customers(expected_session_id=old['sessionId']),
                       lambda:client.logout(expected_session_id=old['sessionId'])):
            with self.assertRaises(CloudError) as error:action()
            self.assertTrue(error.exception.session_changed)
        self.assertEqual(len(remote.calls),calls);self.assertEqual(client.status()['sessionId'],fresh['sessionId'])
        self.assertFalse((self.root/'cloud.pending').exists());self.assertEqual(remote.customers,{})

    def test_expired_session_cannot_reveal_pending_action_details(self):
        remote=SharedFixtureTransport();client=CloudClient(self.root,transport=remote,vault=self.vault,clock=lambda:1000)
        session=client.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
        remote.offline=True
        with self.assertRaises(CloudError):client.create_customer({'request_key':'private-pending','display_name':'Private fictional customer','permission_confirmed':True})
        client.clock=lambda:100000
        result=client.pending(expected_session_id=session['sessionId'])
        self.assertEqual(result['pending'],[]);self.assertNotIn('Private',json.dumps(result));self.assertFalse(self.vault.path.exists())


if __name__=='__main__':unittest.main(verbosity=2)
