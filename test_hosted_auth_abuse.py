"""Public invite abuse and race boundaries without real accounts or network calls."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import patch
import uuid
from hosted.auth import Auth
from hosted.security import ServiceError, digest


class InviteDatabase:
    """Small transactional fixture exposing only the invitation/rate-limit SQL."""
    def __init__(self,token,row):
        self.token_hash=digest(token);self.row=row;self.attempts={}
        self.depth=0;self.transactions=0;self.invite_reads=0;self.staff=[];self.result=None

    @contextmanager
    def transaction(self):
        self.transactions+=1;self.depth+=1
        try:yield self
        finally:self.depth-=1

    def execute(self,statement,params=()):
        self.result=None
        if statement.startswith('DELETE FROM ss_login_attempts'):
            cutoff=datetime.now(timezone.utc)-timedelta(days=1)
            self.attempts={key:row for key,row in self.attempts.items() if row['window_start']>=cutoff}
        elif statement.startswith('INSERT INTO ss_login_attempts'):
            self.attempts.setdefault(params[0],{'failures':0,'window_start':datetime.now(timezone.utc)})
        elif statement.startswith('SELECT * FROM ss_login_attempts'):
            self.result=dict(self.attempts[params[0]])
        elif statement.startswith('UPDATE ss_login_attempts SET failures=1,'):
            self.attempts[params[0]]={'failures':1,'window_start':datetime.now(timezone.utc)}
        elif statement.startswith('UPDATE ss_login_attempts SET failures=failures+1'):
            self.attempts[params[0]]['failures']+=1
        elif statement.startswith('SELECT * FROM ss_invites'):
            self.invite_reads+=1
            self.result=dict(self.row) if params[0]==self.token_hash and self.row else None
        elif statement.startswith('INSERT INTO ss_staff'):
            self.staff.append(params)
        elif statement.startswith('UPDATE ss_invites SET used_at=now()'):
            self.row['used_at']=datetime.now(timezone.utc)
        elif statement.startswith('INSERT INTO ss_audit'):
            pass
        else:raise AssertionError('Unexpected fixture SQL: '+statement)
        return self

    def fetchone(self):return self.result


class InviteAbuseTests(unittest.TestCase):
    def setUp(self):
        self.token='V'*43
        self.row={'email':'staff@example.invalid','used_at':None,
                  'expires_at':datetime.now(timezone.utc)+timedelta(hours=1),
                  'tenant_id':uuid.uuid4(),'role':'cashier'}
        self.db=InviteDatabase(self.token,self.row)
        self.auth=Auth.__new__(Auth);self.auth.database=self.db
        self.data={'invite_token':self.token,'email':self.row['email'],'password':'fixture-password-1234'}

    def reject(self,data,status=403):
        with self.assertRaises(ServiceError) as caught:
            self.auth.accept_invite(data,'fixture-address')
        self.assertEqual(caught.exception.status,status)

    def test_malformed_tokens_are_rejected_without_database_or_hash_work(self):
        with patch('hosted.auth.password_hash') as hasher:
            for token in ('',None,17,'x'*42,'x'*44,'\u2603'*43,'../'+('x'*40)):
                with self.subTest(token=token):self.reject(self.data|{'invite_token':token})
            hasher.assert_not_called()
        self.assertEqual(self.db.transactions,0)

    def test_unknown_wrong_email_used_and_expired_invites_never_hash_passwords(self):
        with patch('hosted.auth.password_hash') as hasher:
            self.reject(self.data|{'invite_token':'U'*43})
            self.reject(self.data|{'email':'other@example.invalid'})
            self.row['used_at']=datetime.now(timezone.utc)
            self.reject(self.data)
            self.row['used_at']=None;self.row['expires_at']=datetime.now(timezone.utc)-timedelta(seconds=1)
            self.reject(self.data)
            hasher.assert_not_called()
        self.assertEqual(self.db.staff,[])

    def test_valid_invite_hashes_outside_transaction_then_is_consumed_once(self):
        def hash_password(password):
            self.assertEqual(self.db.depth,0,'Do not hold a DB connection/row lock during PBKDF2.')
            self.assertEqual(password,self.data['password'])
            return 'fixture-hash'
        with patch('hosted.auth.password_hash',side_effect=hash_password) as hasher:
            self.assertEqual(self.auth.accept_invite(self.data,'fixture-address'),{'staff_created':True})
            self.reject(self.data)
            hasher.assert_called_once()
        self.assertEqual(len(self.db.staff),1)
        self.assertIsNotNone(self.row['used_at'])

    def test_concurrent_use_during_hash_is_rejected_by_final_locked_recheck(self):
        def concurrent_use(_):
            self.row['used_at']=datetime.now(timezone.utc)
            return 'fixture-hash'
        with patch('hosted.auth.password_hash',side_effect=concurrent_use):self.reject(self.data)
        self.assertEqual(self.db.staff,[])

    def test_expiry_during_hash_is_rejected_before_staff_creation(self):
        def expires(_):
            self.row['expires_at']=datetime.now(timezone.utc)-timedelta(seconds=1)
            return 'fixture-hash'
        with patch('hosted.auth.password_hash',side_effect=expires):self.reject(self.data)
        self.assertEqual(self.db.staff,[])

    def test_unknown_invite_attempts_commit_shared_budget_and_throttle_before_lookup(self):
        data=self.data|{'invite_token':'U'*43}
        with patch('hosted.auth.password_hash') as hasher:
            for _ in range(30):self.reject(data)
            reads=self.db.invite_reads
            self.reject(data,status=429)
            self.assertEqual(self.db.invite_reads,reads)
            hasher.assert_not_called()
        attempt=self.db.attempts[digest('invite-accept:fixture-address')]
        self.assertEqual(attempt['failures'],30)
        with self.assertRaises(ServiceError) as caught:self.auth.accept_invite(data,'different-address')
        self.assertEqual(caught.exception.status,403)
        self.assertEqual(self.db.invite_reads,reads+1)

    def test_throttle_window_resets_and_does_not_share_signin_namespace(self):
        key=digest('invite-accept:fixture-address')
        self.db.attempts[key]={'failures':30,'window_start':datetime.now(timezone.utc)-timedelta(minutes=16)}
        signin=digest('all-signins:fixture-address')
        self.db.attempts[signin]={'failures':30,'window_start':datetime.now(timezone.utc)}
        self.reject(self.data|{'invite_token':'U'*43})
        self.assertEqual(self.db.attempts[key]['failures'],1)
        self.assertEqual(self.db.attempts[signin]['failures'],30)


if __name__=='__main__':unittest.main()
