"""Owned Linux HTTPS/SQL portal fixture, never merchant or desktop acceptance.

The caller owns the sandboxed nonroot browser and must close it before exiting
this context. Credentials remain private fixture memory. Cases use the real UI,
Auth/MFA and production cookie/CSRF boundary; no session/cookie is seeded here.
Private control methods are Python-only, never HTTP routes. Gate PASS on zero
provider_attempts, successful cases and cleanup_complete. Source authored UNRUN.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from urllib.request import build_opener, HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from cryptography.fernet import Fernet
from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from hosted.app import create_app
from hosted.database import Database
from hosted.security import password_hash
from hosted.mfa import secret_bytes, totp


ROLES=('owner','manager','buyer','cashier')
_DSN=re.compile(r'postgresql://postgres:[0-9a-f]{48}@db:5432/(ss_ci_[0-9a-f]{16})\?sslmode=require')
_NSS_FILES=frozenset(('cert9.db','key4.db','pkcs11.txt','cert9.db-journal','key4.db-journal','cert9.db-wal','key4.db-wal','cert9.db-shm','key4.db-shm'))


class PortalFixtureError(RuntimeError):
    def __init__(self,code):super().__init__(code); self.code=code


@dataclass(frozen=True)
class Account:
    slug:str
    email:str
    password:str=field(repr=False)
    role:str
    staff_id:uuid.UUID
    tenant_id:uuid.UUID


class _DeniedTransport:
    trust_env=False
    def __init__(self,fixture):self.fixture=fixture
    def request(self,*unused,**ignored):return self.fixture._deny_provider()
    get=request
    post=request


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*unused,**ignored):raise PortalFixtureError('fixture_probe_redirect')


class _SafeHandler(WSGIRequestHandler):
    # Werkzeug's default messages include the request target. The runner owns
    # a separate sanitized method/path/status ledger; never log headers/query.
    def log(self,*unused,**ignored):pass
    def log_request(self,*unused,**ignored):pass
    def log_error(self,*unused,**ignored):pass
    def log_message(self,*unused,**ignored):pass
    def make_environ(self):
        environ=super().make_environ()
        environ['ss.fixture.connection']=self.connection
        return environ


class _TLSServer(ThreadedWSGIServer):
    """Prebind first; bounded real TLS handshake, then ordinary WSGI dispatch."""
    def __init__(self,application,context):
        self._owned=threading.Condition(); self._connections={}; self._handlers=[]
        super().__init__('127.0.0.1',0,application,_SafeHandler,ssl_context=None)
        self.ssl_context=context
    def get_request(self):
        raw,address=self.socket.accept(); raw.settimeout(5); connection=None
        try:
            connection=self.ssl_context.wrap_socket(raw,server_side=True,do_handshake_on_connect=False)
            with self._owned:self._connections[connection]=None
            connection.do_handshake()
            return connection,address
        except Exception:
            (connection or raw).close()
            with self._owned:self._connections.pop(connection,None); self._owned.notify_all()
            raise
    def process_request_thread(self,request,client_address):
        with self._owned:
            self._connections[request]=threading.current_thread()
            self._handlers=[thread for thread in self._handlers if thread.is_alive()]
            self._handlers.append(threading.current_thread())
        try:super().process_request_thread(request,client_address)
        finally:
            with self._owned:self._connections.pop(request,None); self._owned.notify_all()
    def close_owned_requests(self):
        with self._owned:connections=list(self._connections)
        for connection in connections:
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            connection.close()
        deadline=time.monotonic()+6
        with self._owned:
            while self._connections and time.monotonic()<deadline:self._owned.wait(deadline-time.monotonic())
            remaining=bool(self._connections); handlers=list(self._handlers)
        for thread in handlers:
            if thread.ident is not None:thread.join(timeout=max(0,deadline-time.monotonic()))
        return not remaining and not any(thread.is_alive() for thread in handlers)


class ReplyGate:
    """One actual password POST; hold only its completed, committed reply.

    No request body or cookie is read/copied. release delivers the original
    bytes/status/headers. drop_delivery closes only this request's socket before
    any response headers are delivered. Neither method modifies cookies.
    """
    def __init__(self,fixture):
        self._fixture=fixture; self._committed=threading.Event(); self._decision=threading.Event()
        self._lock=threading.Lock(); self._metadata=None; self._error=None; self._action=None
    def __enter__(self):return self
    def __exit__(self,*unused):self.release(); return False
    def wait_committed(self,timeout=10):
        if type(timeout) not in (int,float) or not 0<timeout<=20:raise PortalFixtureError('fixture_gate_timeout_input')
        if not self._committed.wait(timeout):raise PortalFixtureError('fixture_gate_not_committed')
        if self._error:raise PortalFixtureError(self._error)
        return dict(self._metadata)
    def _choose(self,action,*,cleanup=False):
        with self._lock:
            if self._action is None:self._action=action
            self._decision.set()
            error=self._error
        self._fixture._disarm(self)
        if error and not cleanup:raise PortalFixtureError(error)
    def release(self):self._choose('deliver')
    def drop_delivery(self):self._choose('drop')
    def _ready(self,metadata,error=None):
        with self._lock:self._metadata=metadata; self._error=error
        self._committed.set()
    def _await(self):
        if not self._decision.wait(15):
            with self._lock:self._error='fixture_gate_delivery_deadline'; self._action='deliver'
            self._decision.set()
        return self._action


class PortalFixture:
    def __init__(self,evidence_dir):
        self._evidence=Path(evidence_dir); self._nonce=uuid.uuid4().hex
        self.accounts={}; self.database=None; self.auth=None
        self.origin=self.untrusted_origin=self.hostname_mismatch_origin=''
        self.ca_path=None; self.tls_probe={}; self.cleanup_complete=False; self.cleanup_errors=[]
        self._entered=False; self._uid=None; self._home=None; self._private=None; self._private_inode=None
        self._owned_files=set(); self._servers=[]; self._threads=[]; self._guard=None
        self._schema=None; self._schema_oid=None; self._schema_touched=False; self._dsn=None
        self._nss=None; self._nss_inode=None; self._created_pki=None
        self._initial_hashes={}; self._tenant=None; self._application=None
        self._lock=threading.Lock(); self._active=threading.Condition(); self._active_requests=0
        self._gate=None; self._gates=[]; self._provider_attempts=0

    @property
    def provider_attempts(self):
        with self._lock:return self._provider_attempts

    def _deny_provider(self,*unused,**ignored):
        with self._lock:self._provider_attempts+=1
        raise PortalFixtureError('fixture_provider_request_denied')

    def _contract(self):
        if sys.platform!='linux' or ROOT!=Path('/workspace') or os.getuid()!=10001 or os.getgid()!=10001:
            raise PortalFixtureError('fixture_linux_nonroot_required')
        if os.environ.get('SS_HOSTED_BROWSER_FIXTURE')!='1' or not re.fullmatch(r'[0-9a-f]{40}',os.environ.get('SS_CI_SOURCE_SHA','')):
            raise PortalFixtureError('fixture_admission_required')
        self._dsn=os.environ.get('DATABASE_URL',''); match=_DSN.fullmatch(self._dsn)
        if not match:raise PortalFixtureError('fixture_owned_database_required')
        if any(os.environ.get(name) for name in ('PGSERVICE','PGSERVICEFILE','PGOPTIONS','PGHOSTADDR','PGHOST','PGPORT','PGDATABASE','PGUSER','PGPASSWORD','PGSSLMODE','PGSSLROOTCERT','PGPASSFILE')):
            raise PortalFixtureError('fixture_database_environment_override')
        self._database_name=match.group(1); self._uid=os.getuid(); self._home=Path(pwd.getpwuid(self._uid).pw_dir)
        if not self._home.is_absolute() or (os.environ.get('HOME') and os.environ['HOME']!=str(self._home)):
            raise PortalFixtureError('fixture_private_home_required')
        if not self._evidence.is_absolute() or not self._evidence.is_relative_to(Path('/ci-evidence')):
            raise PortalFixtureError('fixture_evidence_root_required')
        current=Path('/ci-evidence')
        for name in ('',*self._evidence.relative_to(current).parts):
            if name:current=current/name
            info=current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=self._uid:raise PortalFixtureError('fixture_evidence_ownership')
        home_info=self._home.lstat()
        if not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid!=self._uid or home_info.st_mode&0o077:raise PortalFixtureError('fixture_home_ownership')
        runtime=Path('/ci-runtime').lstat()
        if not stat.S_ISDIR(runtime.st_mode) or runtime.st_uid!=self._uid or runtime.st_mode&0o077:
            raise PortalFixtureError('fixture_private_runtime_required')

    def __enter__(self):
        if self._entered:raise PortalFixtureError('fixture_context_reused')
        self._entered=True
        try:
            self._contract()
            # Failure capture copies /ci-evidence. Keys/NSS runtime material
            # must remain elsewhere even if owned cleanup cannot finish.
            self._private=Path('/ci-runtime')/('portal-fixture-'+self._nonce)
            self._private.mkdir(mode=0o700); self._private_inode=self._private.lstat().st_ino
            self._guard=patch('requests.sessions.Session.request',new=lambda *args,**kwargs:self._deny_provider())
            self._guard.start()
            self._create_schema(); self._seed_accounts(); self._certificates(); self._trust()
            primary=self._tls_context(self._private/'leaf.crt',self._private/'leaf.key')
            untrusted=self._tls_context(self._private/'untrusted-leaf.crt',self._private/'untrusted-leaf.key')
            for context in (primary,untrusted,primary):self._servers.append(_TLSServer(self._dispatch,context))
            self.origin='https://127.0.0.1:'+str(self._servers[0].server_port)
            self.untrusted_origin='https://127.0.0.1:'+str(self._servers[1].server_port)
            self.hostname_mismatch_origin='https://localhost:'+str(self._servers[2].server_port)
            key=Fernet.generate_key().decode()
            app=create_app({'TESTING':False,'DEBUG':False,'PUBLIC_BASE_URL':self.origin,'APP_ENCRYPTION_KEYS':key,
                'BOOTSTRAP_TOKEN':'','SQUARE_APPLICATION_ID':'fictional-browser-app-'+self._nonce,
                'SQUARE_APPLICATION_SECRET':'fictional-browser-server-secret-'+self._nonce,'SQUARE_WEBHOOK_SIGNATURE_KEY':'',
                'SQUARE_ENVIRONMENT':'sandbox','RECEIVING_WRITES_ENABLED':False,'CLEANUP_OBSERVER_ENABLED':False},
                database=self.database,square_transport=_DeniedTransport(self))
            self.auth=app.extensions['ss_auth']; self._application=app
            for server in self._servers:
                thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.1},daemon=True)
                self._threads.append(thread); thread.start()
            self.tls_probe=self.probe_tls()
            return self
        except BaseException as error:
            self._cleanup()
            if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
            raise PortalFixtureError(error.code if isinstance(error,PortalFixtureError) else 'fixture_setup_failed') from None

    def __exit__(self,*unused):self._cleanup(); return False

    def _admin(self):
        return psycopg.connect(self._dsn,sslmode='require',connect_timeout=10,row_factory=dict_row,
            options='-c statement_timeout=10000 -c lock_timeout=3000')

    def _database_probe(self,db,*,scoped=False):
        row=db.execute('''SELECT current_database() AS database_name,current_user AS role,current_schema() AS schema_name,
            current_setting('server_version') AS server_version,(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()) AS ssl''').fetchone()
        if row['database_name']!=self._database_name or row['role']!='postgres' or row['ssl'] is not True:
            raise PortalFixtureError('fixture_database_tls_identity')
        if scoped and row['schema_name']!=self._schema:raise PortalFixtureError('fixture_database_schema_identity')
        return {'ssl':True,'sslmode':'require','server_version':row['server_version'],'schema_scoped':scoped}

    def _create_schema(self):
        self._schema='ss_test_'+self._nonce; self._schema_marker='ss-hosted-browser:'+self._nonce
        with self._admin() as db:
            self._database_probe(db)
            if db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(self._schema,)).fetchone():raise PortalFixtureError('fixture_schema_exists')
            self._schema_touched=True
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self._schema)))
            db.execute(sql.SQL('COMMENT ON SCHEMA {} IS {}').format(sql.Identifier(self._schema),sql.Literal(self._schema_marker)))
            self._schema_oid=db.execute('SELECT oid FROM pg_namespace WHERE nspname=%s',(self._schema,)).fetchone()['oid']
        self.database=Database(self._dsn,schema=self._schema); self.database.migrate()
        if not self.database.ready():raise PortalFixtureError('fixture_schema_not_ready')

    def _seed_accounts(self):
        self._tenant=uuid.uuid4(); slug='browser-fixture-'+self._tenant.hex
        for role in ROLES:
            password='Fictional '+role+' original password '+secrets.token_hex(12)
            self.accounts[role]=Account(slug,role+'@example.invalid',password,role,uuid.uuid4(),self._tenant)
            self._initial_hashes[role]=password_hash(password)
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional HTTPS browser store')",(self._tenant,slug))
            for role,account in self.accounts.items():
                db.execute('INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,%s)',
                    (account.staff_id,account.tenant_id,account.email,self._initial_hashes[role],role))

    def _command(self,executable,*arguments):
        try:
            result=subprocess.run([executable,*map(str,arguments)],cwd=self._private,stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,check=False,close_fds=True,
                env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','TMPDIR':str(self._private)})
            if result.returncode:raise PortalFixtureError('fixture_certificate_command_failed')
        except (OSError,subprocess.TimeoutExpired):raise PortalFixtureError('fixture_certificate_command_failed') from None

    def _certificates(self):
        for prefix in ('','untrusted-'):
            names=tuple(prefix+name for name in ('ca.key','ca.crt','leaf.key','leaf.csr','leaf.crt','leaf.ext'))
            for name in names:self._owned_files.add(self._private/name)
            ca_key,ca_cert,leaf_key,csr,leaf_cert,extensions=(self._private/name for name in names)
            extensions.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\n',encoding='ascii')
            self._command('/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-sha256','-nodes','-days','1',
                '-subj','/CN=SS Browser CA '+prefix+self._nonce,
                '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign',
                '-keyout',ca_key,'-out',ca_cert)
            self._command('/usr/bin/openssl','req','-new','-newkey','rsa:2048','-sha256','-nodes','-subj','/CN=127.0.0.1','-keyout',leaf_key,'-out',csr)
            self._command('/usr/bin/openssl','x509','-req','-sha256','-days','1','-set_serial',str(secrets.randbits(128)+1),
                '-in',csr,'-CA',ca_cert,'-CAkey',ca_key,'-extfile',extensions,'-out',leaf_cert)
            for path in (ca_key,leaf_key):path.chmod(0o600)
        self.ca_path=self._private/'ca.crt'

    def _trust(self):
        pki=self._home/'.pki'; self._nss=pki/'nssdb'
        if pki.exists():
            info=pki.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=self._uid:raise PortalFixtureError('fixture_nss_parent_ownership')
        else:
            pki.mkdir(mode=0o700); self._created_pki=(pki,pki.lstat().st_ino)
        if self._nss.exists() or self._nss.is_symlink():raise PortalFixtureError('fixture_existing_nss_refused')
        self._nss.mkdir(mode=0o700); self._nss_inode=self._nss.lstat().st_ino
        self._command('/usr/bin/certutil','-N','--empty-password','-d','sql:'+str(self._nss))
        self._command('/usr/bin/certutil','-A','-d','sql:'+str(self._nss),'-n','ss-ephemeral-'+self._nonce,'-t','C,,','-i',self.ca_path)
        for path in self._nss.iterdir():
            info=path.lstat()
            if path.name not in _NSS_FILES or not stat.S_ISREG(info.st_mode) or info.st_uid!=self._uid:raise PortalFixtureError('fixture_nss_contents')
            path.chmod(0o600)

    @staticmethod
    def _tls_context(certificate,key):
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(certificate),str(key)); return context

    def probe_tls(self):
        with self.database.transaction() as db:database=self._database_probe(db,scoped=True)
        context=ssl.create_default_context(cafile=str(self.ca_path))
        opener=build_opener(ProxyHandler({}),HTTPSHandler(context=context),_NoRedirect())
        try:
            with opener.open(Request(self.origin+'/readyz',headers={'Accept':'application/json'}),timeout=5) as response:
                raw=response.read(4097); status=response.status; hsts=response.headers.get('Strict-Transport-Security','')
            value=json.loads(raw.decode('utf-8'))
            if len(raw)>4096 or status!=200 or value.get('database_ready') is not True or value.get('service_ready') is not True or value.get('commercial_release') is not False or not hsts:
                raise ValueError()
        except Exception:raise PortalFixtureError('fixture_verified_https_readiness') from None
        return {'database_client':database,'web_client':{'https':True,'certificate_verified':True,'hostname_verified':True,
            'status':status,'hsts_present':True,'ca_sha256':hashlib.sha256(self.ca_path.read_bytes()).hexdigest()},
            'testing':False,'browser_certificate_acceptance_proven':False,'trust_scope':'fresh-private-user-nss','uid':self._uid}

    def next_password(self,role):
        if role not in self.accounts:raise PortalFixtureError('fixture_role_invalid')
        return ' '+role+' e\u0301 🃏 new password '+secrets.token_hex(12)+' '

    def current_mfa_code(self,role):
        """Private actual TOTP for an already-enrolled owned fictional account.

        HUI05 deliberately clears its displayed recovery material. Later cases
        may prove the same real MFA through this read-only fixture helper; it
        creates no session, changes no SQL/clock/counter and exposes no endpoint.
        If this real time step was used, wait at most 35 seconds for the next real step.
        """
        if role not in self.accounts or not self._entered:
            raise PortalFixtureError('fixture_role_invalid')
        account=self.accounts[role]
        if account.tenant_id!=self._tenant:raise PortalFixtureError('fixture_role_invalid')
        deadline=time.monotonic()+35
        while True:
            with self.database.transaction() as db:
                state=db.execute('''SELECT enabled,version,encrypted_seed,last_totp_step,clock_timestamp() AS checked_at
                    FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s''',
                    (account.tenant_id,account.staff_id)).fetchone()
            if not state or not state['enabled']:raise PortalFixtureError('fixture_mfa_not_enrolled')
            if int(state['checked_at'].timestamp()//30)>state['last_totp_step']:
                seed=self.auth.mfa.cipher.decrypt(state['encrypted_seed'],account.tenant_id,account.staff_id,'active',state['version'])
                try:return totp(secret_bytes(seed),state['checked_at'].timestamp())
                finally:seed=''
            if time.monotonic()>=deadline:raise PortalFixtureError('fixture_mfa_real_step_deadline')
            time.sleep(0.2)

    def read_auth_state(self,role):
        if role not in self.accounts:raise PortalFixtureError('fixture_role_invalid')
        account=self.accounts[role]
        with self.database.transaction() as db:
            row=db.execute('''SELECT s.enabled AS staff_enabled,s.password_hash,COALESCE(m.enabled,FALSE) AS mfa_enabled,
                COALESCE(m.version,0) AS mfa_version,COALESCE(m.pending_expires_at>clock_timestamp(),FALSE) AS pending_enrollment,
                (SELECT count(*) FROM ss_sessions x WHERE x.tenant_id=s.tenant_id AND x.staff_id=s.id) AS sessions,
                (SELECT count(*) FROM ss_mfa_challenges x WHERE x.tenant_id=s.tenant_id AND x.staff_id=s.id
                    AND x.used_at IS NULL AND x.cancelled_at IS NULL AND x.expires_at>clock_timestamp()) AS pending_challenges,
                (SELECT count(*) FROM ss_mfa_challenges x WHERE x.tenant_id=s.tenant_id AND x.staff_id=s.id AND x.cancelled_at IS NOT NULL) AS cancelled_challenges,
                (SELECT count(*) FROM ss_mfa_recovery_codes x WHERE x.tenant_id=s.tenant_id AND x.staff_id=s.id
                    AND x.used_at IS NULL AND x.mfa_version=m.version AND m.enabled) AS recovery_codes_remaining,
                (SELECT count(*) FROM ss_audit x WHERE x.tenant_id=s.tenant_id AND x.staff_id=s.id AND x.action='staff-password-changed') AS password_change_audits,
                (SELECT count(*) FROM ss_audit x WHERE x.tenant_id=s.tenant_id AND x.staff_id=s.id AND x.action='staff-password-change-rejected') AS password_rejection_audits
                FROM ss_staff s LEFT JOIN ss_staff_mfa m ON m.tenant_id=s.tenant_id AND m.staff_id=s.id
                WHERE s.tenant_id=%s AND s.id=%s''',(account.tenant_id,account.staff_id)).fetchone()
        if not row:raise PortalFixtureError('fixture_account_missing')
        row=dict(row); saved=row.pop('password_hash')
        row['password_changed']=not hmac.compare_digest(saved,self._initial_hashes[role])
        return row

    def password_reply_gate(self):
        gate=ReplyGate(self)
        with self._lock:
            if self._gate is not None:raise PortalFixtureError('fixture_gate_already_armed')
            self._gate=gate; self._gates.append(gate)
        return gate

    def _disarm(self,gate):
        with self._lock:
            if self._gate is gate:self._gate=None

    def _dispatch(self,environ,start_response):
        with self._active:self._active_requests+=1
        gate=None
        if environ.get('REQUEST_METHOD')=='POST' and environ.get('PATH_INFO')=='/api/auth/password/change':
            with self._lock:gate=self._gate; self._gate=None
        if gate is None:
            try:reply=self._application(environ,start_response)
            except BaseException:
                with self._active:self._active_requests-=1; self._active.notify_all()
                raise
            return self._tracked(reply)
        captured={}; reply=None; chunks=[]; size=0
        try:
            with self.database.transaction() as db:
                before=db.execute('SELECT COALESCE(max(id),0) AS id FROM ss_audit WHERE tenant_id=%s',(self._tenant,)).fetchone()['id']
            def append(chunk):
                nonlocal size
                if type(chunk) is not bytes:raise PortalFixtureError('fixture_reply_type')
                size+=len(chunk)
                if size>65536:raise PortalFixtureError('fixture_reply_bound')
                chunks.append(chunk)
            def capture(status,headers,exc_info=None):
                captured.update(status=status,headers=list(headers)); return append
            reply=self._application(environ,capture)
            for chunk in reply:append(chunk)
            if hasattr(reply,'close'):reply.close()
            reply=None; raw=b''.join(chunks); code=int(captured['status'].split(' ',1)[0])
            has_cookie=any(name.lower()=='set-cookie' for name,value in captured['headers'])
            try:
                value=json.loads(raw.decode('utf-8'))
                if code!=200 or type(value) is not dict or set(value)!={'password_changed','sign_in_required','all_sessions_revoked'} or any(item is not True for item in value.values()):
                    raise PortalFixtureError('fixture_password_not_committed')
                with self.database.transaction() as db:
                    audits=db.execute("SELECT staff_id FROM ss_audit WHERE tenant_id=%s AND id>%s AND action='staff-password-changed' ORDER BY id LIMIT 2",(self._tenant,before)).fetchall()
                    if len(audits)!=1 or not any(account.staff_id==audits[0]['staff_id'] for account in self.accounts.values()):raise PortalFixtureError('fixture_commit_audit')
                    if db.execute('SELECT count(*) AS n FROM ss_sessions WHERE tenant_id=%s AND staff_id=%s',(self._tenant,audits[0]['staff_id'])).fetchone()['n']!=0:
                        raise PortalFixtureError('fixture_commit_sessions')
                if has_cookie:raise PortalFixtureError('fixture_password_cookie_mutation')
            except Exception as error:
                # Report a failed barrier without replacing a real application
                # rejection/malformed reply with a synthetic auth response.
                gate._ready({},error.code if isinstance(error,PortalFixtureError) else 'fixture_reply_not_verified')
                start_response(captured['status'],captured['headers']); return [raw]
            gate._ready({'status':code,'has_set_cookie':has_cookie,'body_bytes':size})
            action=gate._await()  # All application/verification SQL is closed.
            if action=='drop':
                connection=environ['ss.fixture.connection']
                try:connection.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                connection.close(); raise ConnectionAbortedError('fixture_expected_delivery_drop')
            start_response(captured['status'],captured['headers']); return [raw]
        except BaseException as error:
            if not gate._committed.is_set():gate._ready({},error.code if isinstance(error,PortalFixtureError) else 'fixture_reply_failed')
            raise
        finally:
            if reply is not None and hasattr(reply,'close'):reply.close()
            with self._active:self._active_requests-=1; self._active.notify_all()

    def _tracked(self,reply):
        try:yield from reply
        finally:
            if hasattr(reply,'close'):reply.close()
            with self._active:self._active_requests-=1; self._active.notify_all()

    def _cleanup_error(self,code):
        if code not in self.cleanup_errors:self.cleanup_errors.append(code)

    def _remove_directory(self,directory,inode,names):
        if directory is None or inode is None:return
        try:
            info=directory.lstat()
        except FileNotFoundError:return
        if not stat.S_ISDIR(info.st_mode) or info.st_ino!=inode or info.st_uid!=self._uid:raise PortalFixtureError('fixture_cleanup_directory_identity')
        for path in directory.iterdir():
            info=path.lstat()
            if path.name not in names or not stat.S_ISREG(info.st_mode) or info.st_uid!=self._uid:raise PortalFixtureError('fixture_cleanup_file_identity')
            path.unlink()
        directory.rmdir()

    def _cleanup(self):
        if self.cleanup_complete:return
        for gate in self._gates:gate._choose('deliver',cleanup=True)
        for server,thread in zip(self._servers,self._threads):
            try:
                if thread.is_alive():server.shutdown()
                server.server_close()
                if not server.close_owned_requests():self._cleanup_error('fixture_request_threads_alive')
                if thread.ident is not None:thread.join(timeout=6)
                if thread.is_alive():self._cleanup_error('fixture_server_thread_alive')
            except Exception:self._cleanup_error('fixture_server_cleanup_failed')
        for server in self._servers[len(self._threads):]:
            try:server.server_close()
            except Exception:self._cleanup_error('fixture_server_cleanup_failed')
        with self._active:
            deadline=time.monotonic()+6
            while self._active_requests and time.monotonic()<deadline:self._active.wait(deadline-time.monotonic())
            if self._active_requests:self._cleanup_error('fixture_requests_still_active')
        if self.database is not None:
            try:self.database.close()
            except Exception:self._cleanup_error('fixture_pool_cleanup_failed')
        if self._schema_touched:
            try:
                with self._admin() as db:
                    self._database_probe(db)
                    row=db.execute("SELECT n.oid,obj_description(n.oid,'pg_namespace') AS marker,r.rolname AS owner FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname=%s",(self._schema,)).fetchone()
                    if row:
                        if row['oid']!=self._schema_oid or row['marker']!=self._schema_marker or row['owner']!='postgres':raise PortalFixtureError('fixture_cleanup_schema_identity')
                        db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self._schema)))
                self._schema_touched=False
            except Exception:self._cleanup_error('fixture_schema_cleanup_failed')
        try:self._remove_directory(self._nss,self._nss_inode,_NSS_FILES)
        except Exception:self._cleanup_error('fixture_nss_cleanup_failed')
        if self._created_pki:
            try:self._remove_directory(self._created_pki[0],self._created_pki[1],frozenset())
            except Exception:self._cleanup_error('fixture_pki_cleanup_failed')
        try:self._remove_directory(self._private,self._private_inode,{path.name for path in self._owned_files})
        except Exception:self._cleanup_error('fixture_certificate_cleanup_failed')
        if self._guard and not self._active_requests and not any(thread.is_alive() for thread in self._threads):
            try:self._guard.stop(); self._guard=None
            except Exception:self._cleanup_error('fixture_transport_guard_cleanup_failed')
        self.cleanup_complete=not self.cleanup_errors and not self._schema_touched and self._guard is None
