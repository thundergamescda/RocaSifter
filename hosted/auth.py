"""Invitation-only staff accounts and revocable opaque sessions."""
from datetime import datetime, timezone, timedelta
import hmac
import re
import secrets
import uuid
from .security import ServiceError, digest, email_address, password_hash, password_valid
from .database import audit


class Auth:
    def __init__(self,database,bootstrap_token=''):
        self.database,self.bootstrap_token=database,bootstrap_token
        self.dummy_hash=password_hash('dummy-login-not-an-account')

    def bootstrap(self,data):
        if not self.bootstrap_token:raise ServiceError('Pilot onboarding is not enabled.',503)
        supplied=data.get('bootstrap_token','')
        if not isinstance(supplied,str) or not hmac.compare_digest(supplied,self.bootstrap_token):
            raise ServiceError('Pilot onboarding authorization is invalid.',403)
        slug=str(data.get('store_slug','')).strip().lower()
        name=str(data.get('store_name','')).strip()
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',slug) or not 1<=len(name)<=150:
            raise ServiceError('Use a store name and a 3-50 character lowercase store slug.')
        email=email_address(data.get('email'));hashed=password_hash(data.get('password'))
        tenant,staff=uuid.uuid4(),uuid.uuid4()
        with self.database.transaction() as db:
            db.execute('SELECT pg_advisory_xact_lock(81726002)')
            if db.execute("SELECT 1 FROM ss_bootstrap WHERE key='initial-owner'").fetchone():
                raise ServiceError('Pilot owner already exists. An owner must invite additional staff.',409)
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',(tenant,slug,name))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,'owner')",(staff,tenant,email,hashed))
            db.execute("INSERT INTO ss_bootstrap(key) VALUES ('initial-owner')")
            audit(db,tenant,staff,'store-created',tenant)
        return {'store_slug':slug,'mode':'pilot','owner_created':True}

    def login(self,data,client_address):
        email=email_address(data.get('email'));slug=str(data.get('store_slug','')).strip().lower()
        password=data.get('password','')
        rate_key=digest(client_address+'\0'+slug+'\0'+email)
        ip_key=digest('all-signins:'+client_address)
        # Commit the failure counter even when credentials fail; never log raw IP/email.
        with self.database.transaction() as db:
            db.execute("DELETE FROM ss_login_attempts WHERE window_start<now()-interval '1 day'")
            db.execute('INSERT INTO ss_login_attempts(key_hash) VALUES (%s) ON CONFLICT DO NOTHING',(ip_key,))
            ip_attempt=db.execute('SELECT * FROM ss_login_attempts WHERE key_hash=%s FOR UPDATE',(ip_key,)).fetchone()
            if ip_attempt['window_start']<datetime.now(timezone.utc)-timedelta(minutes=15):
                db.execute('UPDATE ss_login_attempts SET failures=0,window_start=now() WHERE key_hash=%s',(ip_key,))
                ip_attempt['failures']=0
            if ip_attempt['failures']>=30:raise ServiceError('Too many sign-in attempts. Try again later.',429)
            db.execute('INSERT INTO ss_login_attempts(key_hash) VALUES (%s) ON CONFLICT DO NOTHING',(rate_key,))
            attempt=db.execute('SELECT * FROM ss_login_attempts WHERE key_hash=%s FOR UPDATE',(rate_key,)).fetchone()
            if attempt['window_start']<datetime.now(timezone.utc)-timedelta(minutes=15):
                db.execute('UPDATE ss_login_attempts SET failures=0,window_start=now() WHERE key_hash=%s',(rate_key,))
                attempt['failures']=0
            if attempt['failures']>=5:raise ServiceError('Too many sign-in attempts. Try again later.',429)
            staff=db.execute('''SELECT s.*,t.slug,t.name FROM ss_staff s JOIN ss_tenants t ON t.id=s.tenant_id
                                WHERE t.slug=%s AND s.email=%s''',(slug,email)).fetchone()
            valid=password_valid(password,staff['password_hash'] if staff else self.dummy_hash)
            if not staff or not valid or not staff['enabled']:
                db.execute('UPDATE ss_login_attempts SET failures=failures+1 WHERE key_hash=%s',(rate_key,))
                db.execute('UPDATE ss_login_attempts SET failures=failures+1 WHERE key_hash=%s',(ip_key,))
                failure=True
            else:
                failure=False;db.execute('DELETE FROM ss_login_attempts WHERE key_hash=%s',(rate_key,))
                token=secrets.token_urlsafe(32)
                db.execute('DELETE FROM ss_sessions WHERE expires_at<now()')
                db.execute("INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at) VALUES (%s,%s,%s,now()+interval '8 hours')",
                           (digest(token),staff['tenant_id'],staff['id']))
                audit(db,staff['tenant_id'],staff['id'],'staff-login',staff['id'])
        if failure:raise ServiceError('Store, email or password is incorrect.',401)
        return token,{'store':slug,'store_name':staff['name'],'role':staff['role'],'expires_in':28800}

    def resolve(self,token):
        if not token or len(token)>256:raise ServiceError('Sign in to your store.',401)
        with self.database.transaction() as db:
            row=db.execute('''SELECT s.tenant_id,s.staff_id,u.role,t.slug FROM ss_sessions s
                              JOIN ss_staff u ON u.id=s.staff_id AND u.tenant_id=s.tenant_id
                              JOIN ss_tenants t ON t.id=s.tenant_id
                              WHERE s.token_hash=%s AND s.expires_at>now() AND u.enabled=TRUE''',(digest(token),)).fetchone()
        if not row:raise ServiceError('Your session expired. Sign in again.',401)
        return dict(row)|{'session_hash':digest(token)}

    def logout(self,token):
        with self.database.transaction() as db:db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(digest(token),))

    def invite(self,actor,data):
        if actor['role']!='owner':raise ServiceError('Only the store owner can invite staff.',403)
        email=email_address(data.get('email'));role=data.get('role')
        if role not in ('manager','buyer','cashier'):raise ServiceError('Select a staff role.')
        token=secrets.token_urlsafe(32)
        with self.database.transaction() as db:
            if db.execute('SELECT 1 FROM ss_staff WHERE tenant_id=%s AND email=%s',(actor['tenant_id'],email)).fetchone():
                raise ServiceError('This staff account already exists.',409)
            db.execute("INSERT INTO ss_invites(token_hash,tenant_id,email,role,expires_at,created_by) VALUES (%s,%s,%s,%s,now()+interval '24 hours',%s)",
                       (digest(token),actor['tenant_id'],email,role,actor['staff_id']))
            audit(db,actor['tenant_id'],actor['staff_id'],'staff-invited',digest(email),{'role':role})
        return {'invite_token':token,'expires_in':86400,'delivery':'Share privately with the intended staff member. Email delivery is not connected.'}

    def _invite_attempt(self,client_address):
        # Separate namespace from sign-in counters, with a short transaction.
        # Unknown/replayed invitations still consume this budget before lookup.
        key=digest('invite-accept:'+str(client_address))
        with self.database.transaction() as db:
            db.execute("DELETE FROM ss_login_attempts WHERE window_start<now()-interval '1 day'")
            db.execute('INSERT INTO ss_login_attempts(key_hash) VALUES (%s) ON CONFLICT DO NOTHING',(key,))
            attempt=db.execute('SELECT * FROM ss_login_attempts WHERE key_hash=%s FOR UPDATE',(key,)).fetchone()
            if attempt['window_start']<datetime.now(timezone.utc)-timedelta(minutes=15):
                db.execute('UPDATE ss_login_attempts SET failures=1,window_start=now() WHERE key_hash=%s',(key,))
                limited=False
            elif attempt['failures']>=30:
                limited=True
            else:
                db.execute('UPDATE ss_login_attempts SET failures=failures+1 WHERE key_hash=%s',(key,))
                limited=False
        if limited:raise ServiceError('Too many invitation attempts. Try again later.',429)

    @staticmethod
    def _live_invite(row,email):
        return bool(row and row['email']==email and not row['used_at']
                    and row['expires_at']>datetime.now(timezone.utc))

    def accept_invite(self,data,client_address=''):
        token=data.get('invite_token','');email=email_address(data.get('email'))
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
            raise ServiceError('The invitation is invalid, expired or already used.',403)
        self._invite_attempt(client_address)
        token_hash=digest(token)
        # Authenticate the opaque invitation before spending password-hash CPU.
        # Do not hold a database connection or row lock during the hash.
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM ss_invites WHERE token_hash=%s',(token_hash,)).fetchone()
        if not self._live_invite(row,email):
            raise ServiceError('The invitation is invalid, expired or already used.',403)
        hashed=password_hash(data.get('password'));staff=uuid.uuid4()
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM ss_invites WHERE token_hash=%s FOR UPDATE',(token_hash,)).fetchone()
            if not self._live_invite(row,email):
                raise ServiceError('The invitation is invalid, expired or already used.',403)
            db.execute('INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,%s)',(staff,row['tenant_id'],email,hashed,row['role']))
            db.execute('UPDATE ss_invites SET used_at=now() WHERE token_hash=%s',(token_hash,))
            audit(db,row['tenant_id'],staff,'invite-accepted',staff)
        return {'staff_created':True}

    def disable_staff(self,actor,staff_id):
        if actor['role']!='owner':raise ServiceError('Only the owner can disable staff.',403)
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM ss_staff WHERE tenant_id=%s AND id=%s FOR UPDATE',(actor['tenant_id'],staff_id)).fetchone()
            if not row or row['role']=='owner':raise ServiceError('Select a non-owner staff member.')
            db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',(actor['tenant_id'],staff_id))
            db.execute('DELETE FROM ss_sessions WHERE tenant_id=%s AND staff_id=%s',(actor['tenant_id'],staff_id))
            audit(db,actor['tenant_id'],actor['staff_id'],'staff-disabled',staff_id)
