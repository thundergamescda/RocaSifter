"""Platform-issued, email-bound invitations for independent new store owners.

No public signup, email send, automatic session, grant/financial policy approval
or existing-store takeover is implemented by this invitation-first slice.
"""
from dataclasses import dataclass,field
from datetime import datetime,timezone
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from typing import Protocol
from psycopg.types.json import Jsonb
from .database import Database,audit
from .security import ServiceError,digest,email_address,password_hash,identifier


class EnrollmentError(ServiceError):
    def __init__(self,message='This owner invitation is unavailable. Check the invitation and intended email, or contact the issuer.',status=403,*,code='INVITATION_UNAVAILABLE'):
        super().__init__(message,status);self.code=code


def _text(value,label,minimum,maximum):
    if not isinstance(value,str) or not minimum<=len(value.strip())<=maximum or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ServiceError(label+' is invalid.')
    return value.strip()


def _keys(data,allowed):
    if not isinstance(data,dict) or set(data)-set(allowed):raise ServiceError('Unsupported owner invitation fields.')


def _canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def _token_hash(token):return digest('stock-sleeve-owner-invitation-v1:'+token)


def _owner_email(value):
    # Existing Auth normalization remains untouched. New invitation delivery is
    # explicitly bounded until international mailbox semantics are verified;
    # case-folding must not silently turn a Unicode local part into another one.
    if not isinstance(value,str) or not value.isascii() or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ServiceError('This owner-invitation preview supports ASCII email addresses. Internationalized mailboxes need a verified delivery path; no invitation was issued.')
    return email_address(value)


@dataclass(frozen=True)
class OperatorAuthority:
    """Internal service-operator capability. Never deserialize an HTTP actor."""
    reference:str
    def __post_init__(self):object.__setattr__(self,'reference',_text(self.reference,'Operator reference',1,100))


@dataclass(frozen=True)
class InvitationMessage:
    recipient:str=field(repr=False)
    invitation_url:str=field(repr=False)
    invitation_id:str


class InvitationDelivery(Protocol):
    def send(self,message:InvitationMessage)->dict:...


class NoDelivery:
    def send(self,message):
        if not isinstance(message,InvitationMessage):raise TypeError('A typed invitation message is required.')
        return {'sent':False,'delivery_configured':False,'status':'not_configured'}


class OwnerOnboarding:
    def __init__(self,database):self.database=database

    @staticmethod
    def _operator(operator):
        if type(operator) is not OperatorAuthority:raise ServiceError('Platform operator authority is required.',403)
        return operator.reference

    @staticmethod
    def _platform_audit(db,ident,operator,action):
        db.execute('INSERT INTO ss_owner_enrollment_audit(id,enrollment_id,operator_reference,action) VALUES (%s,%s,%s,%s)',
                   (uuid.uuid4(),ident,operator,action))

    def _budget(self,address,token=None):
        # Committed independently, including failures. New tokens and successful
        # password validation never reset the IP budget; all IPs share token budget.
        budgets=([(digest('owner-enrollment-ip:'+str(address)),50)] if token is None
                 else [(digest('owner-enrollment-token:'+_token_hash(token)),10)])
        limited=False
        with self.database.transaction() as db:
            for key,maximum in sorted(budgets):
                db.execute('INSERT INTO ss_login_attempts(key_hash) VALUES (%s) ON CONFLICT DO NOTHING',(key,))
                row=db.execute('SELECT *,clock_timestamp() AS server_now FROM ss_login_attempts WHERE key_hash=%s FOR UPDATE',(key,)).fetchone()
                if (row['server_now']-row['window_start']).total_seconds()>=900:
                    db.execute('UPDATE ss_login_attempts SET failures=1,window_start=clock_timestamp() WHERE key_hash=%s',(key,))
                elif row['failures']>=maximum:limited=True
                else:db.execute('UPDATE ss_login_attempts SET failures=failures+1 WHERE key_hash=%s',(key,))
        if limited:raise EnrollmentError('Too many owner invitation attempts. Wait before retrying.',429,code='ENROLLMENT_RATE_LIMITED')

    def issue(self,operator,data):
        operator=self._operator(operator)
        _keys(data,('request_key','email','store_slug','store_name','expires_seconds','reason'))
        key=identifier(data.get('request_key'),'Issuance request key');email=_owner_email(data.get('email'))
        slug=_text(data.get('store_slug'),'Store slug',3,50).lower()
        if not re.fullmatch('[a-z0-9][a-z0-9-]{2,49}',slug):raise ServiceError('Use a 3 to 50 character lowercase store slug.')
        name=_text(data.get('store_name'),'Store name',1,150);reason=_text(data.get('reason'),'Issuance reason',10,500)
        ttl=data.get('expires_seconds',86400)
        if type(ttl) is not int or not 1<=ttl<=259200:raise ServiceError('Invitation expiry must be 1 second to 72 hours.')
        intent=digest(_canonical({'email':email,'slug':slug,'name':name,'reason':reason,'ttl':ttl}).decode())
        raw=secrets.token_urlsafe(32);ident,tenant,owner=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
        with self.database.transaction() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('owner-enrollment-issue:'+operator+':'+key,))
            old=db.execute('SELECT * FROM ss_owner_enrollments WHERE operator_reference=%s AND issue_key=%s',(operator,key)).fetchone()
            if old:
                if old['issue_hash']!=intent:raise EnrollmentError('This platform issuance key has different immutable terms.',409,code='ISSUANCE_CHANGED')
                return self._issued_view(old,None)
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('owner-enrollment-slug:'+slug,))
            if db.execute('SELECT 1 FROM ss_tenants WHERE slug=%s',(slug,)).fetchone():raise EnrollmentError('That store already exists; use the separate verified ownership/recovery process.',409,code='STORE_ALREADY_EXISTS')
            if db.execute('SELECT 1 FROM ss_owner_enrollments WHERE store_slug=%s AND revoked_at IS NULL',(slug,)).fetchone():raise EnrollmentError('An owner invitation already reserves that store. Review/revoke the unused invitation explicitly.',409,code='INVITATION_ALREADY_ISSUED')
            row=db.execute('''INSERT INTO ss_owner_enrollments(id,token_hash,email,store_slug,store_name,tenant_id,owner_id,operator_reference,issue_key,issue_hash,issue_reason,expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second')) RETURNING *''',
                (ident,_token_hash(raw),email,slug,name,tenant,owner,operator,key,intent,reason,ttl)).fetchone()
            self._platform_audit(db,ident,operator,'issued')
            return self._issued_view(row,raw)

    @staticmethod
    def _issued_view(row,token):
        return {'invitation_id':str(row['id']),'invitation_token':token,'token_available':token is not None,
                'invitation_fragment':('owner_invite='+token+'&invitation_id='+str(row['id'])) if token else None,
                'store':{'slug':row['store_slug'],'name':row['store_name']},'email':row['email'],'expires_at':row['expires_at'].isoformat(),
                'state':row['state'],'delivery_configured':False,'delivery_status':'not_sent',
                'next_action':'deliver_privately_after_approval' if token else 'use_original_secret_or_revoke_unused_and_issue_new_key'}

    def revoke(self,operator,ident,reason):
        operator=self._operator(operator);reason=_text(reason,'Revocation reason',10,500)
        try:ident=uuid.UUID(str(ident))
        except (ValueError,TypeError):raise ServiceError('Invitation ID is invalid.') from None
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM ss_owner_enrollments WHERE id=%s FOR UPDATE',(ident,)).fetchone()
            if not row:raise ServiceError('Invitation not found.',404)
            if row['state']=='consumed':raise EnrollmentError('A completed enrollment cannot be revoked as an unused invitation. Use the separate account lifecycle process.',409,code='ENROLLMENT_ALREADY_COMPLETED')
            if row['state']=='issued':
                db.execute("UPDATE ss_owner_enrollments SET state='revoked',revoked_at=clock_timestamp(),revoked_by=%s,revoke_reason=%s WHERE id=%s",(operator,reason,ident))
                self._platform_audit(db,ident,operator,'revoked')
            return {'invitation_id':str(ident),'revoked':True}

    def _input(self,data,address,allowed):
        self._budget(address)
        _keys(data,allowed);token=data.get('invitation_token')
        if not isinstance(token,str) or not re.fullmatch('[A-Za-z0-9_-]{43}',token):raise EnrollmentError()
        self._budget(address,token)
        try:email=_owner_email(data.get('email'))
        except ServiceError:raise EnrollmentError() from None
        return token,email

    @staticmethod
    def _row(db,token,*,lock=False):
        row=db.execute('SELECT * FROM ss_owner_enrollments WHERE token_hash=%s'+(' FOR UPDATE' if lock else ''),(_token_hash(token),)).fetchone()
        # A projected clock value can be evaluated before a row-lock wait. Take
        # the final authority time only after that lock has actually returned.
        if row:row['server_now']=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
        return row

    @staticmethod
    def _matched(row,email):return bool(row and hmac.compare_digest(row['email'].encode(),email.encode()))

    @staticmethod
    def _live(row):return row['state']=='issued' and row['revoked_at'] is None and row['expires_at']>row['server_now']

    def inspect(self,data,address):
        token,email=self._input(data,address,('invitation_token','email'))
        with self.database.transaction() as db:
            row=self._row(db,token)
            if not self._matched(row,email) or not self._live(row):raise EnrollmentError()
            return {'protocol_version':1,'invitation_id':str(row['id']),'store':{'slug':row['store_slug'],'name':row['store_name']},
                    'email':row['email'],'expires_at':row['expires_at'].isoformat(),'signed_out_required':True,
                    'public_signup_enabled':False,'delivery_configured':False}

    def status(self,data,address):
        token,email=self._input(data,address,('invitation_token','email','request_key'));key=identifier(data.get('request_key'),'Enrollment request key')
        with self.database.transaction() as db:
            row=self._row(db,token)
            if not self._matched(row,email):raise EnrollmentError()
            if row['state']=='consumed' and row['accepted_key']==key:
                return {'found':True,'state':'completed','result':row['result']}
            if not self._live(row):raise EnrollmentError()
            return {'found':False,'state':'not_completed','result':None}

    @staticmethod
    def _accept_hash(token,email,password,key):
        return hmac.new(token.encode(),_canonical({'domain':'owner-enrollment-accept-v1','email':email,'password':password,'request_key':key,'store_details_confirmed':True}),hashlib.sha256).hexdigest()

    def accept(self,data,address):
        token,email=self._input(data,address,('invitation_token','email','password','request_key','store_details_confirmed'))
        key=identifier(data.get('request_key'),'Enrollment request key');password=data.get('password')
        if data.get('store_details_confirmed') is not True:raise ServiceError('Confirm the intended store details before creating its owner account.')
        if not isinstance(password,str) or not 12<=len(password)<=128:raise ServiceError('Use a password between 12 and 128 characters.')
        intent=self._accept_hash(token,email,password,key)
        with self.database.transaction() as db:
            row=self._row(db,token)
            if not self._matched(row,email):raise EnrollmentError()
            if row['state']=='consumed':return self._replay(row,key,intent)
            if not self._live(row):raise EnrollmentError()
        # Expensive hashing only after a live email-bound secret is proven. No
        # connection/row lock spans the hash; all grant facts are checked again.
        hashed=password_hash(password)
        with self.database.transaction() as db:
            # Serialize with the unchanged initial-owner bootstrap's tenant insert.
            db.execute('SELECT pg_advisory_xact_lock(81726002)')
            row=self._row(db,token,lock=True)
            if not self._matched(row,email):raise EnrollmentError()
            if row['state']=='consumed':return self._replay(row,key,intent)
            if not self._live(row):raise EnrollmentError()
            if db.execute('SELECT 1 FROM ss_tenants WHERE slug=%s OR id=%s',(row['store_slug'],row['tenant_id'])).fetchone():
                raise EnrollmentError('This store identity now exists. No owner was attached or replaced; contact the platform issuer.',409,code='STORE_ALREADY_EXISTS')
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',(row['tenant_id'],row['store_slug'],row['store_name']))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,'owner')",(row['owner_id'],row['tenant_id'],email,hashed))
            result={'protocol_version':1,'owner_created':True,'store':{'slug':row['store_slug'],'name':row['store_name']},'owner_email':email,
                'next_action':'sign_in','mfa_enrollment_required_for_trade_and_square_writes':True,'square_connected':False,'receiving_enabled':False,'checkout_enabled':False}
            db.execute("UPDATE ss_owner_enrollments SET state='consumed',consumed_at=clock_timestamp(),accepted_key=%s,accepted_hash=%s,result=%s WHERE id=%s",(key,intent,Jsonb(result),row['id']))
            audit(db,row['tenant_id'],row['owner_id'],'store-owner-enrolled',row['id'],{'purpose':'independent-store-owner-v1'})
            self._platform_audit(db,row['id'],row['operator_reference'],'consumed')
            return result

    @staticmethod
    def _replay(row,key,intent):
        if row['accepted_key']!=key or not hmac.compare_digest(row['accepted_hash'],intent):
            raise EnrollmentError('This invitation already completed with different immutable acceptance terms. Sign in or recover the original request.',409,code='ENROLLMENT_ALREADY_COMPLETED')
        return row['result']


def main():
    parser=argparse.ArgumentParser(description='Privileged platform owner-invitation issuance. Never expose as a web endpoint.')
    parser.add_argument('--confirm-platform-operator',action='store_true');parser.add_argument('--operator-reference',required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    issue=sub.add_parser('issue');issue.add_argument('--request-key',required=True);issue.add_argument('--email',required=True)
    issue.add_argument('--store-slug',required=True);issue.add_argument('--store-name',required=True);issue.add_argument('--reason',required=True)
    issue.add_argument('--expires-seconds',type=int,default=86400)
    revoke=sub.add_parser('revoke');revoke.add_argument('--invitation-id',required=True);revoke.add_argument('--reason',required=True)
    args=parser.parse_args()
    if not args.confirm_platform_operator or os.environ.get('SS_OWNER_INVITATIONS_OPERATOR')!='1':
        raise SystemExit('Operator issuance is disabled. Use the reviewed platform service-account boundary; store-owner sessions/bootstrap tokens do not grant it.')
    url=os.environ.get('DATABASE_URL')
    if not url:raise SystemExit('Private database configuration is required.')
    database=Database(url)
    try:
        if not database.ready():raise SystemExit('The reviewed owner-invitation schema is required.')
        service=OwnerOnboarding(database);operator=OperatorAuthority(args.operator_reference)
        if args.action=='issue':result=service.issue(operator,{key:getattr(args,key) for key in ('request_key','email','store_slug','store_name','reason','expires_seconds')})
        else:result=service.revoke(operator,args.invitation_id,args.reason)
        # Deliberate privileged delivery output, never copied into server logs.
        # The raw token is shown once, cannot be recovered from its saved hash,
        # and no email is sent. Protect this console like a credential.
        print(json.dumps(result,ensure_ascii=False))
    except ServiceError as error:raise SystemExit(str(error)) from None
    finally:database.close()


if __name__=='__main__':main()
