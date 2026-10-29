"""Desktop MFA session/challenge boundaries; fictional transport and proofs only."""
import copy
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid
import requests
from station_cloud import CloudClient,CloudError
from test_station_cloud import SharedFixtureTransport,response

PASSWORD='fixture-staff-password'
RECOVERY_A='0123456789abcdef0123456789abcdef'
RECOVERY_B='fedcba9876543210fedcba9876543210'


class MfaFixtureTransport(SharedFixtureTransport):
    """Exercises desktop protocol, not the independently tested TOTP algorithm."""
    def __init__(self):
        super().__init__();self.lock=threading.RLock();self.now=1000;self.challenges={};self.current_code='123456'
        self.used_totp=set();self.recovery={RECOVERY_A,RECOVERY_B};self.enabled=True;self.auth_version=1
        self.drop_after_verify=False;self.login_override=None;self.verify_override=None;self.mfa_supported=False
        self.verify_entered=None;self.verify_release=None
        self.cancel_status=None

    def request(self,method,url,*,headers,json=None,**options):
        path=url.split('onrender.com',1)[-1]
        if path=='/api/auth/login' and json.get('email','').startswith('mfa-'):
            if self.offline:raise requests.ConnectionError('Fictional outage')
            with self.lock:
                self.calls.append((method,path));self.mfa_supported=json.get('mfa_supported') is True
                if json.get('password')!=PASSWORD:return response(401,{'error':'Sign-in failed.'})
                if not self.mfa_supported:return response(426,{'error':'Update Stock & Sleeve before signing in with account verification.'})
                token=secrets.token_urlsafe(32);self.challenges[token]={'store':json['store_slug'],'role':'manager','expires':self.now+300,
                    'state':'pending','attempts':0,'auth_version':self.auth_version,'issued_token':None}
                result={'mfa_required':True,'challenge_token':token,'expires_in':300,'methods':['totp','recovery_code'],'store':json['store_slug']}
                if self.login_override:result.update(self.login_override)
                return response(200,result)
        if path not in ('/api/auth/mfa/challenge','/api/auth/mfa/cancel'):
            return super().request(method,url,headers=headers,json=json,**options)
        if self.offline:raise requests.ConnectionError('Fictional outage')
        with self.lock:
            self.calls.append((method,path));challenge=self.challenges.get(json.get('challenge_token'))
            if path.endswith('/cancel'):
                if self.cancel_status:return response(self.cancel_status,{'error':'Cancellation could not be confirmed.'})
                if challenge:
                    challenge['state']='cancelled';self.sessions.pop(challenge.get('issued_token'),None)
                return response(200,{'cancelled':True})
            if not challenge or challenge['state']!='pending' or challenge['expires']<=self.now or not self.enabled or challenge['auth_version']!=self.auth_version:
                return response(401,{'error':'Verification failed.'})
            proof=json.get('code','').strip();recovery=proof.replace('-','').lower()
            valid=(proof==self.current_code and proof not in self.used_totp) or recovery in self.recovery
            if not valid:
                challenge['attempts']+=1
                if challenge['attempts']>=5:challenge['state']='exhausted';return response(429,{'error':'Wait before trying again.'})
                return response(401,{'error':'Verification failed.'})
            if recovery in self.recovery:self.recovery.remove(recovery)
            else:self.used_totp.add(proof)
            token='fixture-mfa-session-'+uuid.uuid4().hex;challenge.update(state='complete',issued_token=token)
            self.sessions[token]={'store':challenge['store'],'role':challenge['role'],'mfa_authenticated':True}
            result={'access_token':token,'csrf_token':'fixture-csrf-'+uuid.uuid4().hex,'store':challenge['store'],'role':challenge['role'],
                    'expires_in':28800,'mfa_authenticated':True}
            if self.verify_override:result.update(self.verify_override)
            if self.verify_entered:self.verify_entered.set()
            if self.verify_release:assert self.verify_release.wait(5)
            if self.drop_after_verify:self.drop_after_verify=False;raise requests.Timeout('Fictional lost verification response')
            return response(200,result)


class DesktopMfaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.remote=MfaFixtureTransport();self.now=1000;self.client=CloudClient(self.root,transport=self.remote,clock=lambda:self.now)

    def login(self):return self.client.login({'storeSlug':'fictional-store','email':'mfa-manager@example.invalid','password':PASSWORD})
    def verify(self,challenge,code='123456'):return self.client.verify_mfa({'challengeId':challenge['challengeId'],'code':code})
    def assert_no_session(self):self.assertFalse(self.client.vault.path.exists());self.assertIsNone(self.client._saved())

    def test_password_only_challenge_is_memory_only_and_public_result_has_no_credentials(self):
        challenge=self.login();self.assertTrue(challenge['mfaRequired']);self.assertFalse(challenge['signedIn']);self.assertTrue(self.remote.mfa_supported)
        self.assert_no_session();self.assertFalse(self.client.pending_vault.path.exists())
        public=json.dumps(challenge);self.assertNotIn('challenge_token',public);self.assertNotIn('access_token',public);self.assertNotIn(PASSWORD,public)
        self.assertNotIn('mfa-manager@example.invalid',public);self.assertNotIn(self.client._mfa['token'],public)
        with self.assertRaises(CloudError):self.client.customers()
        self.assertEqual(self.client.status()['challengeId'],challenge['challengeId'])

    def test_verified_session_is_encrypted_durable_and_codes_are_not_persisted(self):
        challenge=self.login();token=next(iter(self.remote.challenges));result=self.verify(challenge)
        self.assertTrue(result['signedIn']);self.assertNotIn('access_token',result);self.assertNotIn('csrf_token',result)
        content=self.client.vault.path.read_bytes();self.assertTrue(content.startswith(b'DP1'))
        for secret in (PASSWORD,'123456',RECOVERY_A,token,self.client._saved()['token']):self.assertNotIn(secret.encode(),content)
        reopened=CloudClient(self.root,transport=self.remote,clock=lambda:self.now)
        self.assertTrue(reopened.status()['signedIn']);self.assertTrue(reopened._saved()['mfa_authenticated']);self.assertIsNone(reopened._mfa)

    def test_unenrolled_existing_pilot_login_stays_compatible(self):
        result=self.client.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':PASSWORD})
        self.assertTrue(result['signedIn']);self.assertFalse(self.client._saved()['mfa_authenticated'])

    def test_wrong_code_invalid_format_and_replayed_totp_never_issue_another_session(self):
        challenge=self.login();before=len(self.remote.calls)
        with self.assertRaises(CloudError):self.verify(challenge,'bad code')
        self.assertEqual(len(self.remote.calls),before)
        with self.assertRaises(CloudError) as caught:self.verify(challenge,'000000')
        self.assertEqual(caught.exception.status,401);self.assert_no_session();self.assertIsNotNone(self.client._mfa)
        self.verify(challenge);self.client.logout();again=self.login()
        with self.assertRaises(CloudError):self.verify(again)
        self.assert_no_session()

    def test_recovery_code_is_consumed_by_server_and_cannot_be_reused(self):
        challenge=self.login();formatted='-'.join(RECOVERY_A[index:index+4] for index in range(0,32,4)).upper()
        self.verify(challenge,' '+formatted+' ');self.assertNotIn(RECOVERY_A,self.remote.recovery)
        self.client.logout();again=self.login()
        with self.assertRaises(CloudError):self.verify(again,RECOVERY_A)
        self.assert_no_session();self.verify(again,RECOVERY_B);self.assertTrue(self.client.status()['signedIn'])

    def test_expired_local_challenge_restarts_without_transmitting_a_code(self):
        challenge=self.login();self.now+=301;before=len(self.remote.calls)
        with self.assertRaises(CloudError) as caught:self.verify(challenge)
        self.assertTrue(caught.exception.mfa_restart);self.assertEqual(len(self.remote.calls),before);self.assert_no_session();self.assertIsNone(self.client._mfa)

    def test_server_expiry_disabled_staff_or_auth_version_change_never_save_session(self):
        for kind in ('expiry','disabled','version'):
            self.remote.enabled=True;self.remote.now=self.now
            challenge=self.login()
            if kind=='expiry':self.remote.now+=301
            if kind=='disabled':self.remote.enabled=False
            if kind=='version':self.remote.auth_version+=1
            with self.subTest(kind=kind),self.assertRaises(CloudError):self.verify(challenge)
            self.assert_no_session()

    def test_exhausted_attempts_clear_local_challenge_and_require_wait(self):
        challenge=self.login()
        for _ in range(4):
            with self.assertRaises(CloudError):self.verify(challenge,'000000')
        with self.assertRaises(CloudError) as caught:self.verify(challenge,'000000')
        self.assertEqual(caught.exception.status,429);self.assertTrue(caught.exception.mfa_restart);self.assertIsNone(self.client._mfa);self.assert_no_session()

    def test_malformed_challenge_store_methods_expiry_or_mixed_session_fail_closed(self):
        for change in ({'store':'another-store'},{'challenge_token':'short'},{'expires_in':True},{'expires_in':301},
                       {'methods':[{}]},{'methods':['totp','totp']},{'methods':['unsupported']},{'access_token':'unexpected-token'}):
            self.remote.login_override=change
            with self.subTest(change=change),self.assertRaises(CloudError):self.login()
            self.assert_no_session();self.assertIsNone(self.client._mfa)

    def test_malformed_verified_store_role_strength_or_session_revokes_issued_challenge(self):
        for change in ({'store':'another-store'},{'role':'administrator'},{'mfa_authenticated':False},{'expires_in':True},{'access_token':'short'}):
            self.remote.current_code=secrets.choice(('223344','445566','667788'))
            self.remote.used_totp.clear();self.remote.verify_override=change;challenge=self.login()
            with self.subTest(change=change),self.assertRaises(CloudError):self.verify(challenge,self.remote.current_code)
            self.assert_no_session();self.assertIsNone(self.client._mfa);self.assertEqual(self.remote.sessions,{})

    def test_stale_nonce_cannot_verify_or_cancel_a_new_flow(self):
        old=self.login();current=self.login();before=len(self.remote.calls)
        for action in (lambda:self.verify(old),lambda:self.client.cancel_mfa({'challengeId':old['challengeId']})):
            with self.assertRaises(CloudError):action()
        self.assertEqual(len(self.remote.calls),before);self.assertEqual(self.client.status()['challengeId'],current['challengeId']);self.assert_no_session()

    def test_cancel_signout_and_restart_clear_challenge_without_touching_credit_journal(self):
        body={'request_key':'fictional-pending','display_name':'Fictional saved customer','permission_confirmed':True}
        self.client._save_pending([{'kind':'customer','customer_id':None,'store':'fictional-store','request_key':body['request_key'],
            'body':body,'fingerprint':self.client._fingerprint(body),'created_at':self.now}]);content=self.client.pending_vault.path.read_bytes()
        challenge=self.login();self.client.cancel_mfa({'challengeId':challenge['challengeId']});self.assertIsNone(self.client._mfa)
        challenge=self.login();self.client.logout(expected_challenge_id=challenge['challengeId']);self.assertIsNone(self.client._mfa)
        self.login();restarted=CloudClient(self.root,transport=self.remote,clock=lambda:self.now)
        self.assertFalse(restarted.status()['signedIn']);self.assertFalse(restarted.status().get('mfaRequired'));self.assertEqual(content,self.client.pending_vault.path.read_bytes());self.assert_no_session()

    def test_lost_verify_response_keeps_no_session_and_cancel_revokes_exact_server_session(self):
        challenge=self.login();self.remote.drop_after_verify=True
        with self.assertRaises(CloudError) as caught:self.verify(challenge,RECOVERY_A)
        self.assertTrue(caught.exception.offline);self.assertIn('may already have been used',str(caught.exception));self.assert_no_session()
        self.assertEqual(len(self.remote.sessions),1);self.assertNotIn(RECOVERY_A,self.remote.recovery)
        self.client.cancel_mfa({'challengeId':challenge['challengeId']});self.assertEqual(self.remote.sessions,{});self.assertIsNone(self.client._mfa)

    def test_replacement_login_cannot_silently_abandon_an_uncertain_verification(self):
        challenge=self.login();self.remote.drop_after_verify=True
        with self.assertRaises(CloudError):self.verify(challenge,RECOVERY_A)
        self.remote.offline=True
        with self.assertRaises(CloudError):self.login()
        self.assertEqual(self.client._mfa['nonce'],challenge['challengeId']);self.assert_no_session()
        self.remote.offline=False;new=self.login()
        self.assertNotEqual(new['challengeId'],challenge['challengeId']);self.assertEqual(self.remote.sessions,{})

    def test_unreadable_success_keeps_exact_cancel_handle_for_issued_session(self):
        challenge=self.login();actual=self.remote.request
        def unreadable(method,url,**options):
            result=actual(method,url,**options)
            if url.endswith('/api/auth/mfa/challenge') and result.status_code==200:result._content=b'not-json'
            return result
        with patch.object(self.remote,'request',side_effect=unreadable),self.assertRaises(CloudError):self.verify(challenge,RECOVERY_A)
        self.assertIsNotNone(self.client._mfa);self.assertEqual(self.client._mfa['nonce'],challenge['challengeId']);self.assert_no_session()
        self.assertEqual(len(self.remote.sessions),1);self.client.cancel_mfa({'challengeId':challenge['challengeId']});self.assertFalse(self.remote.sessions)

    def test_uncertain_response_keeps_cancel_handle_after_local_challenge_expiry(self):
        challenge=self.login();self.remote.drop_after_verify=True
        with self.assertRaises(CloudError):self.verify(challenge,RECOVERY_A)
        self.now+=301;status=self.client.status();self.assertTrue(status['mfaRequired']);self.assertTrue(status['verificationExpired'])
        with self.assertRaises(CloudError):self.verify(challenge,RECOVERY_B)
        self.client.cancel_mfa({'challengeId':challenge['challengeId']});self.assertFalse(self.remote.sessions);self.assert_no_session()

    def test_session_file_write_interruption_remains_cancelable(self):
        challenge=self.login();actual=self.client.vault.set
        def saved_then_interrupted(value):actual(value);raise OSError('Fictional interruption after atomic session write')
        with patch.object(self.client.vault,'set',side_effect=saved_then_interrupted),self.assertRaises(OSError):self.verify(challenge)
        self.assertIsNotNone(self.client._mfa);self.assertIsNotNone(self.client._saved())
        self.client.cancel_mfa({'challengeId':challenge['challengeId']});self.assert_no_session();self.assertFalse(self.remote.sessions)

    def test_mixed_session_challenge_payloads_fail_closed_before_vault_save(self):
        mixed={'access_token':'fictional-full-token-1234567890','expires_in':28800,'store':'fictional-store','role':'manager','challenge_token':'A'*43}
        with patch.object(self.remote,'request',return_value=response(200,mixed)),self.assertRaises(CloudError):
            self.client.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':PASSWORD})
        self.assert_no_session()
        challenge=self.login();self.remote.verify_override={'challenge_token':'A'*43}
        with self.assertRaises(CloudError):self.verify(challenge)
        self.assert_no_session();self.assertFalse(self.remote.sessions)

    def test_failed_cancel_retains_exact_handle_until_reconnect_and_one_revocation(self):
        challenge=self.login();self.remote.drop_after_verify=True
        with self.assertRaises(CloudError):self.verify(challenge,RECOVERY_A)
        self.remote.cancel_status=503;result=self.client.cancel_mfa({'challengeId':challenge['challengeId']})
        self.assertFalse(result['serverRevoked']);self.assertTrue(result['cancelRequired']);self.assertEqual(result['challengeId'],challenge['challengeId']);self.assert_no_session()
        with self.assertRaises(CloudError):self.login()
        with self.assertRaises(CloudError):self.verify(challenge,RECOVERY_B)
        self.assertEqual(len(self.remote.sessions),1);self.remote.cancel_status=None
        result=self.client.cancel_mfa({'challengeId':challenge['challengeId']});self.assertTrue(result['serverRevoked']);self.assertFalse(self.remote.sessions)
        self.assertIsNone(self.client._mfa)

    def test_wrong_address_cancel_has_explicit_local_clear_escape_without_claiming_revocation(self):
        challenge=self.login();self.verify(challenge);self.remote.cancel_status=401
        result=self.client.cancel_mfa({'challengeId':challenge['challengeId']});self.assertFalse(result['serverRevoked']);self.assertTrue(result['cancelRequired']);self.assert_no_session()
        self.assertTrue(self.client.status()['cancelRequired']);self.assertEqual(len(self.remote.sessions),1)
        with self.assertRaises(CloudError):self.client.clear_mfa_local({'challengeId':'0'*32})
        result=self.client.clear_mfa_local({'challengeId':challenge['challengeId']})
        self.assertFalse(result['serverRevoked']);self.assertIn('eight hours',result['message']);self.assertIsNone(self.client._mfa)
        self.remote.cancel_status=None;new=self.login();self.assertNotEqual(new['challengeId'],challenge['challengeId'])

    def test_cancel_after_completed_verify_clears_only_that_session(self):
        challenge=self.login();self.verify(challenge);self.client.cancel_mfa({'challengeId':challenge['challengeId']});self.assert_no_session();self.assertEqual(self.remote.sessions,{})
        current=self.client.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':PASSWORD})
        with self.assertRaises(CloudError):self.client.cancel_mfa({'challengeId':challenge['challengeId']})
        self.assertEqual(self.client._saved()['session_id'],current['sessionId'])

    def test_cancel_racing_verification_does_not_leave_durable_session(self):
        challenge=self.login();self.remote.verify_entered=threading.Event();self.remote.verify_release=threading.Event();errors=[]
        def verify():
            try:self.verify(challenge)
            except Exception as error:errors.append(type(error).__name__)
        def cancel():
            try:self.client.cancel_mfa({'challengeId':challenge['challengeId']})
            except Exception as error:errors.append(type(error).__name__)
        first=threading.Thread(target=verify);first.start();self.assertTrue(self.remote.verify_entered.wait(3));second=threading.Thread(target=cancel);second.start()
        self.remote.verify_release.set();first.join(5);second.join(5);self.assertFalse(first.is_alive() or second.is_alive());self.assertEqual(errors,[])
        self.assert_no_session();self.assertEqual(self.remote.sessions,{})

    def test_authentication_error_messages_never_reflect_supplied_password_or_proof(self):
        with patch.object(self.remote,'request',return_value=response(401,{'error':'Rejected '+PASSWORD})),self.assertRaises(CloudError) as caught:self.login()
        self.assertNotIn(PASSWORD,str(caught.exception));self.assert_no_session()
        challenge=self.login()
        with patch.object(self.remote,'request',return_value=response(400,{'error':'Rejected '+RECOVERY_A})),self.assertRaises(CloudError) as caught:self.verify(challenge,RECOVERY_A)
        self.assertNotIn(RECOVERY_A,str(caught.exception));self.assert_no_session()

    def test_old_client_gets_actionable_upgrade_error_without_challenge_or_session(self):
        result=self.remote.request('POST','https://stock-and-sleeve-api.onrender.com/api/auth/login',headers={},
            json={'store_slug':'fictional-store','email':'mfa-manager@example.invalid','password':PASSWORD},allow_redirects=False)
        self.assertEqual(result.status_code,426);self.assertIn('Update Stock & Sleeve',result.json()['error']);self.assertEqual(self.remote.challenges,{})
        with patch.object(self.remote,'request',return_value=response(426,{'error':'Upgrade needed'})),self.assertRaises(CloudError) as caught:self.login()
        self.assertIn('Update Stock & Sleeve',str(caught.exception));self.assert_no_session()


class DesktopMfaHTTPTests(unittest.TestCase):
    def test_loopback_mfa_requires_nonce_proof_and_same_origin_before_session(self):
        import roca_to_square as core
        from station_service import StationService
        with tempfile.TemporaryDirectory() as folder,patch.object(core,'SETTINGS_FILE',str(Path(folder)/'settings.json')):
            server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);service=server.station=StationService(core);remote=MfaFixtureTransport()
            service.cloud=CloudClient(service.store.root,transport=remote,clock=lambda:remote.now)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();client=requests.Session();client.trust_env=False;base=f'http://127.0.0.1:{server.server_port}'
            try:
                signed=client.post(base+'/api/cloud/login',json={'storeSlug':'fictional-store','email':'mfa-manager@example.invalid','password':PASSWORD})
                self.assertEqual(signed.status_code,200);challenge=signed.json();self.assertTrue(challenge['mfaRequired']);self.assertNotIn('challenge_token',challenge);self.assertFalse(service.cloud.vault.path.exists())
                calls=len(remote.calls);body={'challengeId':challenge['challengeId'],'code':'123456'}
                self.assertEqual(client.post(base+'/api/cloud/mfa-verify',json=body,headers={'Origin':'https://foreign.invalid'}).status_code,403)
                self.assertEqual(client.post(base+'/api/cloud/mfa-verify',json=body|{'challengeId':'0'*32}).status_code,409);self.assertEqual(len(remote.calls),calls)
                result=client.post(base+'/api/cloud/mfa-verify',json=body);self.assertEqual(result.status_code,200);self.assertTrue(result.json()['signedIn'])
                self.assertNotIn('access_token',result.json());self.assertTrue(service.cloud._saved()['mfa_authenticated'])
                cancelled=client.post(base+'/api/cloud/mfa-cancel',json={'challengeId':challenge['challengeId']});self.assertEqual(cancelled.status_code,200)
                self.assertFalse(service.cloud.vault.path.exists());self.assertFalse(remote.sessions)
            finally:client.close();server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':unittest.main(verbosity=2)
