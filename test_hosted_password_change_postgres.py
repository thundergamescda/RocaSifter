"""Own-password acceptance on real disposable PostgreSQL; authored UNRUN.

Fictional credentials exercise actual hashes, sessions, MFA and HTTP. No email,
reset/recovery, provider request or production schema is used. The reusable base
has no test methods and does not inherit another acceptance class.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
import unicodedata
from unittest.mock import Mock, patch
import uuid

import psycopg
from psycopg import sql
from cryptography.fernet import Fernet

from hosted.auth import Auth, PasswordChangeError
from hosted.app import create_app
from hosted.database import Database
from hosted.mfa import secret_bytes, totp
from hosted.security import ServiceError, digest, password_hash, password_valid
import test_hosted_settlement_postgres as sql_fixture


class _PasswordChangeFixture(sql_fixture._PostgreSQLSettlementFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.old_password='legacy-old12'  # Existing twelve-character hashes stay valid.
            cls.old_hash=password_hash(cls.old_password)
        except Exception:
            super().tearDownClass(); raise

    def setUp(self):
        self.tenant,self.staff=uuid.uuid4(),uuid.uuid4()
        self.slug='password-fixture-'+self.tenant.hex
        self.address='fictional-password-address-'+self.tenant.hex
        self.key=Fernet.generate_key().decode(); self.now=datetime.now(timezone.utc)
        self.auth=Auth(self.database,encryption_keys=self.key)
        self.auth.mfa.clock=lambda:self.now
        self.new_password='fictional-new-password-accepted'
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional password store')",(self.tenant,self.slug))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'owner@example.invalid',%s,'owner')",(self.staff,self.tenant,self.old_hash))
            db.execute('''INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at)
                VALUES (%s,%s,'production','fictional-unchanged-ciphertext',clock_timestamp()+interval '1 day')''',(self.tenant,'fictional-'+self.tenant.hex))
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,'Fictional untouched customer','fixture-only')",(uuid.uuid4(),self.tenant))
        self.session_token,_=self.auth.login(self._credentials(self.old_password),self.address)
        self.actor=self.auth.resolve(self.session_token)
        guard=patch('requests.sessions.Session.request',side_effect=AssertionError('No real HTTP in password SQL fixtures'))
        guard.start(); self.addCleanup(guard.stop)

    def _credentials(self,password,email='owner@example.invalid'):
        return {'store_slug':self.slug,'email':email,'password':password,'mfa_supported':True}

    def _body(self,new=None,current=None):
        return {'current_password':self.old_password if current is None else current,'new_password':self.new_password if new is None else new}

    def _saved_staff(self):
        with self.database.transaction() as db:return db.execute('SELECT * FROM ss_staff WHERE tenant_id=%s AND id=%s',(self.tenant,self.staff)).fetchone()

    def _security_rows(self):
        with self.database.transaction() as db:
            rows={table:db.execute(sql.SQL('SELECT * FROM {} WHERE tenant_id=%s AND staff_id=%s ORDER BY {}').format(
                sql.Identifier(table),sql.Identifier(order)),(self.tenant,self.staff)).fetchall()
                for table,order in (('ss_sessions','token_hash'),('ss_staff_mfa','staff_id'),('ss_mfa_challenges','token_hash'),('ss_mfa_recovery_codes','code_hash'))}
            rows['staff']=db.execute('SELECT * FROM ss_staff WHERE tenant_id=%s AND id=%s',(self.tenant,self.staff)).fetchone()
        return rows

    def _business_rows(self):
        with self.database.transaction() as db:
            return {table:db.execute(sql.SQL('SELECT * FROM {} WHERE tenant_id=%s ORDER BY {}').format(sql.Identifier(table),sql.Identifier(order)),(self.tenant,)).fetchall()
                for table,order in (('ss_invites','token_hash'),('ss_square_connections','tenant_id'),('ss_customers','id'))}

    def _budget_rows(self):
        keys=[digest('password-change:ip:'+self.address),digest('password-change:staff:'+str(self.tenant)+':'+str(self.staff))]
        with self.database.transaction() as db:return db.execute('SELECT * FROM ss_login_attempts WHERE key_hash=ANY(%s) ORDER BY key_hash',(keys,)).fetchall()

    def _enable_mfa(self):
        self.setup=self.auth.mfa.begin(self.actor,{'password':self.old_password},self.address)
        code=totp(secret_bytes(self.setup['secret']),self.now.timestamp())
        result=self.auth.mfa.confirm(self.actor,{'enrollment_token':self.setup['enrollment_token'],'code':code},self.address)
        self.recovery_codes=result['recovery_codes']; self.now+=timedelta(seconds=30)
        self.actor=self.auth.resolve(self.session_token)

    def _new_client(self):
        app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://fixture.invalid','APP_ENCRYPTION_KEYS':self.key,
            'SQUARE_APPLICATION_ID':'fictional-password-app','SQUARE_APPLICATION_SECRET':'fictional-password-secret'},
            database=self.database,square_transport=Mock())
        client=app.test_client(); client.set_cookie('ss_session',self.session_token,domain='fixture.invalid')
        me=client.get('/api/me',base_url='https://fixture.invalid'); self.assertEqual(me.status_code,200)
        return app,client,me.json['csrf_token']

    def _post(self,client,csrf,**kwargs):
        headers={'Origin':'https://fixture.invalid','X-SS-CSRF':csrf}|kwargs.pop('headers',{})
        return client.post('/api/auth/password/change',base_url='https://fixture.invalid',headers=headers,environ_overrides={'REMOTE_ADDR':self.address},**kwargs)


class PostgreSQLPasswordChangeAcceptance(_PasswordChangeFixture):
    def test_all_roles_change_only_their_own_password_and_revoke_every_owned_session(self):
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_invites(token_hash,tenant_id,email,role,expires_at,created_by) VALUES (%s,%s,'future@example.invalid','buyer',now()+interval '1 day',%s)",('a'*64,self.tenant,self.staff))
        business=self._business_rows()
        fresh_sessions=[]
        for role in ('owner','manager','buyer','cashier'):
            staff=self.staff if role=='owner' else uuid.uuid4(); email='owner@example.invalid' if role=='owner' else role+'@example.invalid'
            if role!='owner':
                with self.database.transaction() as db:
                    db.execute('INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,%s)',(staff,self.tenant,email,self.old_hash,role))
            token,_=self.auth.login(self._credentials(self.old_password,email),self.address)
            second,_=self.auth.login(self._credentials(self.old_password,email),self.address)
            actor=self.auth.resolve(token)
            result=self.auth.change_password(actor,self._body(),self.address)
            self.assertEqual(result,{'password_changed':True,'sign_in_required':True,'all_sessions_revoked':True})
            for stale in (token,second):
                with self.subTest(role=role),self.assertRaises(ServiceError):self.auth.resolve(stale)
            with self.database.transaction() as db:
                saved=db.execute('SELECT password_hash,role,enabled FROM ss_staff WHERE id=%s',(staff,)).fetchone()
            self.assertTrue(password_valid(self.new_password,saved['password_hash']))
            self.assertNotEqual(saved['password_hash'],self.old_hash); self.assertEqual((saved['role'],saved['enabled']),(role,True))
            # A new ordinary login works; the password-change operation itself
            # never mints a replacement token.
            fresh,_=self.auth.login(self._credentials(self.new_password,email),self.address)
            self.assertEqual(self.auth.resolve(fresh)['staff_id'],staff)
            fresh_sessions.append(fresh)
            for unrelated in fresh_sessions:self.auth.resolve(unrelated)
        self.assertEqual(self._business_rows(),business)

    def test_new_policy_counts_unicode_codepoints_preserves_whitespace_and_rejects_same_password(self):
        original=self._security_rows()
        invalid=[self._body(new='x'*14),self._body(new='x'*129),self._body(new=True),self._body(new='\ud800'*15),
            self._body(new='x'*15)|{'staff_id':str(self.staff)},self._body(new=self.old_password,current=self.old_password)]
        # Identical values of valid new-policy length have their own clear code.
        invalid.append(self._body(new='same-current-password',current='same-current-password'))
        for body in invalid:
            with self.subTest(kind=type(body.get('new_password')).__name__),self.assertRaises(PasswordChangeError) as caught:
                self.auth.change_password(self.actor,body,self.address)
            self.assertEqual(caught.exception.status,400); self.assertFalse(caught.exception.sign_in_required)
        self.assertEqual(self._security_rows(),original); self.assertEqual(self._budget_rows(),[])
        replacement=' '+('e\u0301🃏'*5)+' '
        self.auth.change_password(self.actor,self._body(new=replacement),self.address)
        saved=self._saved_staff()['password_hash']
        self.assertTrue(password_valid(replacement,saved))
        self.assertFalse(password_valid(replacement.strip(),saved)); self.assertFalse(password_valid(unicodedata.normalize('NFC',replacement),saved))
        for boundary in ('x'*15,'🃏'*128):
            staff=uuid.uuid4(); email=staff.hex+'@example.invalid'
            with self.database.transaction() as db:
                db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,'buyer')",(staff,self.tenant,email,self.old_hash))
            token,_=self.auth.login(self._credentials(self.old_password,email),self.address)
            self.auth.change_password(self.auth.resolve(token),self._body(new=boundary),self.address)
            with self.database.transaction() as db:stored=db.execute('SELECT password_hash FROM ss_staff WHERE id=%s',(staff,)).fetchone()['password_hash']
            self.assertTrue(password_valid(boundary,stored))

    def test_fresh_mfa_preserves_active_seed_version_and_recovery_codes_and_cancels_pending_challenges(self):
        self._enable_mfa()
        _,challenge=self.auth.login(self._credentials(self.old_password),self.address)
        before=self._security_rows(); self.assertTrue(challenge['mfa_required'])
        self.auth.change_password(self.actor,self._body(),self.address)
        after=self._security_rows()
        self.assertEqual(after['ss_staff_mfa'],before['ss_staff_mfa'])
        self.assertEqual(after['ss_mfa_recovery_codes'],before['ss_mfa_recovery_codes'])
        self.assertEqual(after['ss_sessions'],[])
        self.assertTrue(all(row['cancelled_at'] is not None for row in after['ss_mfa_challenges']))
        with self.assertRaises(ServiceError):self.auth.mfa.verify_challenge({'challenge_token':challenge['challenge_token'],'code':self.recovery_codes[0]},self.address)
        self.assertEqual(self._security_rows()['ss_mfa_recovery_codes'],before['ss_mfa_recovery_codes'])

    def test_pending_enrollment_is_cleared_without_enabling_mfa_or_changing_recovery_material(self):
        setup=self.auth.mfa.begin(self.actor,{'password':self.old_password},self.address)
        before=self._security_rows()['ss_staff_mfa'][0]
        self.auth.change_password(self.actor,self._body(),self.address)
        row=self._security_rows()['ss_staff_mfa'][0]
        self.assertEqual((row['enabled'],row['version'],row['encrypted_seed']),(False,before['version'],before['encrypted_seed']))
        for field in ('pending_seed','pending_token_hash','pending_session_hash','pending_password_fingerprint','pending_expires_at'):self.assertIsNone(row[field])
        self.assertEqual(row['pending_attempts'],0); self.assertEqual(self._security_rows()['ss_mfa_recovery_codes'],[])
        with self.assertRaises(ServiceError):self.auth.mfa.confirm(self.actor,{'enrollment_token':setup['enrollment_token'],'code':'000000'},self.address)

    def test_wrong_password_guesses_and_safe_audits_commit_and_cannot_be_reset_by_an_error(self):
        before=self._security_rows()
        for _ in range(5):
            with self.assertRaises(PasswordChangeError) as caught:self.auth.change_password(self.actor,self._body(current='fictional-wrong-current'),self.address)
            self.assertEqual((caught.exception.status,caught.exception.code,caught.exception.sign_in_required),(403,'CURRENT_PASSWORD_INVALID',False))
        with self.assertRaises(PasswordChangeError) as limited:self.auth.change_password(self.actor,self._body(),self.address)
        self.assertEqual((limited.exception.status,limited.exception.code),(429,'PASSWORD_CHANGE_RATE_LIMITED'))
        self.assertEqual(sorted(row['failures'] for row in self._budget_rows()),[5,6])
        self.assertEqual(self._security_rows(),before)
        with self.database.transaction() as db:
            rows=db.execute("SELECT action,entity_id,details FROM ss_audit WHERE tenant_id=%s AND action LIKE %s",(self.tenant,'staff-password-%')).fetchall()
        self.assertEqual(len(rows),5)
        encoded=json.dumps(rows)
        for secret in (self.old_password,self.new_password,'fictional-wrong-current',self.old_hash,self.session_token):self.assertNotIn(secret,encoded)

    def test_both_actual_pbkdf2_operations_run_outside_a_database_transaction(self):
        original_transaction=self.database.transaction; original_valid=password_valid; original_hash=password_hash
        depth=0; seen=[]
        @contextmanager
        def tracked():
            nonlocal depth
            with original_transaction() as db:
                depth+=1
                try:yield db
                finally:depth-=1
        def verify(value,stored):
            self.assertEqual(depth,0); seen.append('verify'); return original_valid(value,stored)
        def encode(value):
            self.assertEqual(depth,0); seen.append('hash'); return original_hash(value)
        with patch.object(self.database,'transaction',side_effect=tracked),patch('hosted.auth.password_valid',side_effect=verify),patch('hosted.auth.password_hash',side_effect=encode):
            self.auth.change_password(self.actor,self._body(),self.address)
        self.assertEqual(seen,['verify','hash']); self.assertEqual(depth,0)

    def test_http_audit_failure_rolls_back_all_security_changes_but_preserves_committed_budget(self):
        self.auth.mfa.begin(self.actor,{'password':self.old_password},self.address)
        app,client,csrf=self._new_client(); before=self._security_rows()
        with self.database.transaction() as db:
            db.execute("CREATE FUNCTION ss_test_password_audit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action='staff-password-changed' THEN RAISE EXCEPTION 'fictional password audit failure'; END IF; RETURN NEW; END $$")
            db.execute('CREATE TRIGGER ss_test_password_audit_failure BEFORE INSERT ON ss_audit FOR EACH ROW EXECUTE FUNCTION ss_test_password_audit_failure()')
        try:
            response=self._post(client,csrf,json=self._body())
            self.assertEqual(response.status_code,500); self.assertEqual(response.json['code'],'PASSWORD_CHANGE_UNCONFIRMED')
            self.assertIs(response.json['sign_in_required'],True); self.assertNotIn('Set-Cookie',response.headers)
            self.assertEqual(self._security_rows(),before); self.assertEqual([row['failures'] for row in self._budget_rows()],[1,1])
            self.assertEqual(self.auth.resolve(self.session_token)['staff_id'],self.staff)
        finally:
            with self.database.transaction() as db:
                db.execute('DROP TRIGGER ss_test_password_audit_failure ON ss_audit'); db.execute('DROP FUNCTION ss_test_password_audit_failure()')

    def test_concurrent_password_changes_have_one_winner_and_never_reactivate_a_revoked_session(self):
        barrier=threading.Barrier(2); original_hash=password_hash
        replacements=('fictional-race-password-alpha','fictional-race-password-bravo')
        def encode(value):
            result=original_hash(value); barrier.wait(timeout=20); return result
        def change(value):
            try:return self.auth.change_password(self.actor,self._body(new=value),self.address)
            except PasswordChangeError as error:return error
        with patch('hosted.auth.password_hash',side_effect=encode),ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(change,replacements))
        self.assertEqual(sum(isinstance(row,dict) for row in outcomes),1)
        rejected=next(row for row in outcomes if isinstance(row,PasswordChangeError))
        self.assertTrue(rejected.sign_in_required); self.assertIn(rejected.status,(401,403))
        saved=self._saved_staff()['password_hash']; self.assertEqual(sum(password_valid(value,saved) for value in replacements),1)
        self.assertEqual(self._security_rows()['ss_sessions'],[]); self.assertEqual([row['failures'] for row in self._budget_rows()],[2,2])

    def test_login_that_verified_old_password_before_commit_cannot_issue_a_late_session(self):
        reached,release=threading.Event(),threading.Event(); login_thread=[]; original_valid=password_valid
        def verify(value,stored):
            valid=original_valid(value,stored)
            if login_thread and threading.get_ident()==login_thread[0]:
                reached.set(); self.assertTrue(release.wait(20),'Password race release timed out')
            return valid
        def login():
            login_thread.append(threading.get_ident())
            return self.auth.login(self._credentials(self.old_password),self.address+'-late-login')
        with patch('hosted.auth.password_valid',side_effect=verify),ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(login)
            try:
                self.assertTrue(reached.wait(10),'Old-password login did not reach its verification barrier')
                self.auth.change_password(self.actor,self._body(),self.address)
            finally:release.set()
            with self.assertRaises(ServiceError):pending.result(timeout=20)
        self.assertEqual(self._security_rows()['ss_sessions'],[])

    def test_pending_mfa_verification_after_password_commit_cannot_mint_old_credential_session(self):
        self._enable_mfa(); _,challenge=self.auth.login(self._credentials(self.old_password),self.address)
        reached,release=threading.Event(),threading.Event(); verifying=[]; original_locked=self.auth.mfa._locked_staff
        def locked(db,tenant,staff):
            if verifying and threading.get_ident()==verifying[0]:
                reached.set(); self.assertTrue(release.wait(20),'MFA race release timed out')
            return original_locked(db,tenant,staff)
        def verify():
            verifying.append(threading.get_ident())
            return self.auth.mfa.verify_challenge({'challenge_token':challenge['challenge_token'],'code':self.recovery_codes[0]},self.address)
        codes=self._security_rows()['ss_mfa_recovery_codes']
        with patch.object(self.auth.mfa,'_locked_staff',side_effect=locked),ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(verify)
            try:
                self.assertTrue(reached.wait(10),'MFA verification did not reach staff lock barrier')
                self.auth.change_password(self.actor,self._body(),self.address)
            finally:release.set()
            with self.assertRaises(ServiceError):pending.result(timeout=20)
        self.assertEqual(self._security_rows()['ss_sessions'],[]); self.assertEqual(self._security_rows()['ss_mfa_recovery_codes'],codes)

    def test_new_mfa_proof_acquired_between_phases_cannot_replace_the_captured_security_state(self):
        reached,release=threading.Event(),threading.Event(); original_hash=password_hash
        def encode(value):
            result=original_hash(value); reached.set(); self.assertTrue(release.wait(20),'MFA fence release timed out'); return result
        with patch('hosted.auth.password_hash',side_effect=encode),ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(self.auth.change_password,self.actor,self._body(),self.address)
            try:
                self.assertTrue(reached.wait(10),'Password change did not release SQL before new hash')
                self._enable_mfa()  # Retains this same session with a NEW valid fresh proof.
            finally:release.set()
            with self.assertRaises(PasswordChangeError) as caught:pending.result(timeout=20)
        self.assertEqual((caught.exception.status,caught.exception.code),(401,'PASSWORD_STATE_CHANGED'))
        self.assertEqual(self._saved_staff()['password_hash'],self.old_hash)
        self.assertTrue(self.auth.resolve(self.session_token)['mfa_authenticated'])

    def test_stale_mfa_and_changed_or_disabled_session_authority_stop_before_password_publication(self):
        self._enable_mfa(); original=self._saved_staff()['password_hash']
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        with self.assertRaises(PasswordChangeError) as stale:self.auth.change_password(self.actor,self._body(),self.address)
        self.assertEqual((stale.exception.status,stale.exception.code,stale.exception.sign_in_required),(403,'MFA_STEP_UP_REQUIRED',True))
        app,client,csrf=self._new_client()
        response=self._post(client,csrf,json=self._body())
        self.assertEqual((response.status_code,response.json['code'],response.json['sign_in_required']),(403,'MFA_STEP_UP_REQUIRED',True))
        self.assertNotIn('Set-Cookie',response.headers)
        self.assertEqual(self._budget_rows(),[])
        with self.database.transaction() as db:
            db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(self.actor['session_hash'],))
        with self.assertRaises(PasswordChangeError) as missing:self.auth.change_password(self.actor,self._body(),self.address)
        self.assertEqual((missing.exception.status,missing.exception.sign_in_required),(401,True))
        _,challenge=self.auth.login(self._credentials(self.old_password),self.address)
        token,_=self.auth.mfa.verify_challenge({'challenge_token':challenge['challenge_token'],'code':self.recovery_codes[0]},self.address)
        actor=self.auth.resolve(token)
        with self.database.transaction() as db:db.execute("UPDATE ss_staff SET role='manager' WHERE id=%s",(self.staff,))
        with self.assertRaises(PasswordChangeError) as role:self.auth.change_password(actor,self._body(),self.address)
        self.assertEqual((role.exception.status,role.exception.sign_in_required),(403,True))
        with self.database.transaction() as db:db.execute('UPDATE ss_staff SET enabled=FALSE WHERE id=%s',(self.staff,))
        with self.assertRaises(PasswordChangeError) as disabled:self.auth.change_password(actor,self._body(),self.address)
        self.assertEqual((disabled.exception.status,disabled.exception.sign_in_required),(401,True))
        self.assertEqual(self._saved_staff()['password_hash'],original)

    def test_logout_or_staff_disablement_between_phases_cannot_publish_the_replacement(self):
        original_hash=password_hash
        for changed in ('logout','disabled'):
            reached,release=threading.Event(),threading.Event()
            def encode(value):
                result=original_hash(value); reached.set(); self.assertTrue(release.wait(20),'Authority race release timed out'); return result
            if changed=='disabled':
                token,_=self.auth.login(self._credentials(self.old_password),self.address)
                self.actor=self.auth.resolve(token)
            with patch('hosted.auth.password_hash',side_effect=encode),ThreadPoolExecutor(max_workers=1) as pool:
                pending=pool.submit(self.auth.change_password,self.actor,self._body(),self.address)
                try:
                    self.assertTrue(reached.wait(10),'Password change did not reach lock-free publication gap')
                    with self.database.transaction() as db:
                        if changed=='logout':db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(self.actor['session_hash'],))
                        else:db.execute('UPDATE ss_staff SET enabled=FALSE WHERE id=%s',(self.staff,))
                finally:release.set()
                with self.assertRaises(PasswordChangeError) as caught:pending.result(timeout=20)
            self.assertTrue(caught.exception.sign_in_required)
            self.assertEqual(self._saved_staff()['password_hash'],self.old_hash)

    def test_http_strict_input_auth_csrf_and_origin_failures_are_typed_and_never_mutate_cookies(self):
        app,client,csrf=self._new_client(); before=self._security_rows()
        invalid=[b'[]',b'{"current_password":"a","new_password":"first-valid-password","new_password":"second-valid-password"}',
            b'{"current_password":"a","new_password":NaN}',b'{"current_password":"a","new_password":"abcdefghijklmnop","staff_id":"other"}',
            b'{"current_password":"a","new_password":"abcdefghijklmnop","tenant_id":"other"}',
            b'{"current_password":"a","new_password":"\\ud800abcdefghijklmnop"}',
            b'{"current_password":"a","new_password":"'+b'x'*4096+b'"}']
        for raw in invalid:
            response=self._post(client,csrf,data=raw,content_type='application/json')
            self.assertEqual(response.status_code,400); self.assertEqual(response.json['code'],'PASSWORD_INPUT_INVALID')
            self.assertIs(response.json['sign_in_required'],False); self.assertNotIn('Set-Cookie',response.headers)
        for headers,content_type in (({'X-SS-CSRF':'wrong'},'application/json'),({'Origin':'https://other.invalid'},'application/json'),({},'text/plain')):
            response=self._post(client,csrf,data=json.dumps(self._body()),content_type=content_type,headers=headers)
            self.assertIn(response.status_code,(403,415)); self.assertIs(response.json['sign_in_required'],False); self.assertNotIn('Set-Cookie',response.headers)
        wrong=self._post(client,csrf,json=self._body(current='fictional-wrong-current'))
        self.assertEqual((wrong.status_code,wrong.json['code'],wrong.json['sign_in_required']),(403,'CURRENT_PASSWORD_INVALID',False)); self.assertNotIn('Set-Cookie',wrong.headers)
        self.assertEqual(self._security_rows(),before)
        client.delete_cookie('ss_session',domain='fixture.invalid')
        missing=self._post(client,csrf,json=self._body())
        self.assertEqual((missing.status_code,missing.json['code'],missing.json['sign_in_required']),(401,'PASSWORD_SESSION_CHANGED',True)); self.assertNotIn('Set-Cookie',missing.headers)

    def test_http_success_and_lost_reply_recovery_do_not_overwrite_a_newer_signin_cookie(self):
        app,client,csrf=self._new_client()
        response=self._post(client,csrf,json=self._body())
        self.assertEqual(response.status_code,200); self.assertEqual(response.json,{'password_changed':True,'sign_in_required':True,'all_sessions_revoked':True})
        self.assertNotIn('Set-Cookie',response.headers)
        self.assertEqual(client.get('/api/me',base_url='https://fixture.invalid').status_code,401)
        # Ignore the original acknowledgement: ordinary sign-in with the new
        # password proves the outcome without replaying a saved password body.
        signed=client.post('/api/auth/login',base_url='https://fixture.invalid',json=self._credentials(self.new_password),headers={'Origin':'https://fixture.invalid'},environ_overrides={'REMOTE_ADDR':self.address})
        self.assertEqual(signed.status_code,200); self.assertIn('Set-Cookie',signed.headers)
        late=self._post(client,csrf,json=self._body(),headers={'Authorization':'Bearer '+self.session_token})
        self.assertEqual(late.status_code,401); self.assertNotIn('Set-Cookie',late.headers)
        self.assertEqual(client.get('/api/me',base_url='https://fixture.invalid').status_code,200)

    def test_exact_schema13_upgrade_adds_staff_session_seek_index_without_changing_existing_security_rows(self):
        raw=(Path(__file__).resolve().parent/'tests/fixtures/schema13.sql').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'ab19f9bc38a8b6be85a68562ee840bc5acf21440543fe06bd72cce85a0cb057e')
        namespace='ss_test_'+uuid.uuid4().hex; database=None; created=False
        try:
            with psycopg.connect(self.url,sslmode='require') as db:
                self.assertIsNone(db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(namespace,)).fetchone())
                db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
            created=True; database=Database(self.url,schema=namespace)
            tenant,staff=uuid.uuid4(),uuid.uuid4()
            with database.transaction() as db:
                db.execute(raw.decode('utf-8'),prepare=False)
                db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional schema13 password store')",(tenant,'password-old-'+tenant.hex))
                db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'legacy@example.invalid',%s,'cashier')",(staff,tenant,self.old_hash))
                db.execute("INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at) VALUES (%s,%s,%s,now()+interval '1 hour')",('f'*64,tenant,staff))
                old={table:db.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall() for table in ('ss_staff','ss_sessions')}
                self.assertIsNone(db.execute("SELECT to_regclass('ss_sessions_staff') AS id").fetchone()['id'])
            self.assertFalse(database.ready()); database.migrate(); database.migrate(); self.assertTrue(database.ready())
            with database.transaction() as db:
                for table,rows in old.items():self.assertEqual(db.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall(),rows)
                index=db.execute('''SELECT array_agg(a.attname ORDER BY k.ordinality) AS columns FROM pg_index i
                    JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                    CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ordinality)
                    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE n.nspname=%s AND c.relname='ss_sessions_staff' ''',(namespace,)).fetchone()
                self.assertEqual(index['columns'],['tenant_id','staff_id'])
                self.assertIsNotNone(db.execute('SELECT version FROM ss_schema_versions WHERE version=14').fetchone())
        finally:
            if database is not None:database.close()
            if created:
                with psycopg.connect(self.url,sslmode='require') as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(namespace)))
