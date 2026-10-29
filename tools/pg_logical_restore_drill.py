"""Explicitly disposable loopback PostgreSQL logical restore drill.

Prepared for the fixture owner to run. Never accepts an existing application DB,
an input archive, a remote host, or a caller-chosen cleanup namespace.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass,field
from datetime import datetime,timedelta,timezone,date
from decimal import Decimal
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import uuid
from unittest.mock import patch
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from cryptography.fernet import Fernet

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
NAMESPACE=re.compile(r'ss_test_[a-f0-9]{32}')
TABLES=('ss_schema_versions','ss_tenants','ss_staff','ss_sessions','ss_customers',
        'ss_credit_entries','ss_credit_lots','ss_credit_lot_mutations','ss_trade_receipts',
        'ss_trade_receipt_lines','ss_requests','ss_staff_mfa','ss_mfa_recovery_codes',
        'ss_mfa_challenges','ss_audit','ss_restore_drill_marker')
BUSINESS=('ss_customers','ss_credit_entries','ss_credit_lots','ss_credit_lot_mutations',
          'ss_trade_receipts','ss_trade_receipt_lines','ss_requests')


class DrillError(RuntimeError):pass


def owned_name(value):
    if not isinstance(value,str) or not NAMESPACE.fullmatch(value):raise DrillError('Only an exact ss_test_<uuid> namespace is allowed.')
    return value


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RedactedLog:
    def __init__(self):self.secrets=set();self.lines=[]
    def private(self,*values):self.secrets.update(value for value in values if isinstance(value,str) and value)
    def scrub(self,value):
        for secret in sorted(self.secrets,key=len,reverse=True):value=value.replace(secret,'[redacted]')
        return value
    def add(self,event,**facts):
        value=self.scrub(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'event':event,**facts},sort_keys=True))
        self.lines.append(value);print(value,flush=True)
    def text(self):return '\n'.join(self.lines)+'\n'


@dataclass(frozen=True)
class Config:
    connection:dict=field(repr=False)
    key:str=field(repr=False)
    binary_dir:Path
    hashes:dict
    version:str='18.6'

    @classmethod
    def from_env(cls,environment):
        if environment.get('SS_DISPOSABLE_PG_FIXTURE')!='1':raise DrillError('SS_DISPOSABLE_PG_FIXTURE=1 is required; no connection was attempted.')
        try:settings=conninfo_to_dict(environment.get('DATABASE_URL',''))
        except Exception:raise DrillError('Private fixture connection settings are invalid.') from None
        if (settings.get('host')!='127.0.0.1' or settings.get('user')!='ss_fixture'
            or settings.get('dbname')!='postgres' or not settings.get('password')):
            raise DrillError('Only the marked loopback ss_fixture role and postgres maintenance database are allowed.')
        allowed={'host','hostaddr','port','user','password','dbname','sslmode','sslrootcert','sslcert','sslkey','connect_timeout'}
        if set(settings)-allowed or settings.get('hostaddr','127.0.0.1')!='127.0.0.1':raise DrillError('Unexpected connection routing settings are not allowed.')
        if any(environment.get(name) for name in ('PGSERVICE','PGSERVICEFILE','PGOPTIONS')):raise DrillError('Inherited service/search-path routing must be cleared by the fixture launcher.')
        port=settings.get('port','5432')
        if not str(port).isdecimal() or not 1<=int(port)<=65535:raise DrillError('The fixture port is invalid.')
        timeout=settings.get('connect_timeout','10')
        if not str(timeout).isdecimal() or not 1<=int(timeout)<=10:raise DrillError('The connection timeout must be a whole number from 1 to 10 seconds.')
        if settings.get('sslmode','require') not in ('require','verify-ca','verify-full'):raise DrillError('The local fixture must use TLS.')
        key=environment.get('SS_RESTORE_MFA_KEY','')
        try:Fernet(key.encode())
        except Exception:raise DrillError('A dedicated disposable MFA encryption key must be provided privately.') from None
        directory=Path(environment.get('SS_PG_BIN_DIR',''))
        if not directory.is_absolute() or not directory.is_dir():raise DrillError('A verified absolute PostgreSQL binary directory is required.')
        directory=directory.resolve()
        try:pins=json.loads(environment.get('SS_PG_BIN_SHA256_JSON',''))
        except Exception:raise DrillError('Independently verified binary SHA256 pins are required.') from None
        suffix='.exe' if os.name=='nt' else ''
        expected={'pg_dump'+suffix,'pg_restore'+suffix}
        if not isinstance(pins,dict) or set(pins)!=expected or any(not isinstance(v,str) or not re.fullmatch(r'[a-f0-9]{64}',v) for v in pins.values()):raise DrillError('Both exact PostgreSQL binary hashes must be pinned.')
        version=environment.get('SS_PG_EXPECT_VERSION','18.6')
        if not re.fullmatch(r'18\.\d+',version):raise DrillError('This drill is prepared only for the local PostgreSQL 18 fixture.')
        settings=dict(settings,hostaddr='127.0.0.1',port=str(port),sslmode=settings.get('sslmode','require'))
        result=cls(settings,key,directory,pins,version)
        for name in pins:result.binary(name)
        return result

    def binary(self,name):
        if name not in self.hashes:raise DrillError('The requested binary is not allowlisted.')
        path=(self.binary_dir/name).resolve(strict=True)
        if path.parent!=self.binary_dir or not path.is_file() or sha(path)!=self.hashes[name]:raise DrillError('A PostgreSQL binary does not match its independently verified pin.')
        return path

    def child_env(self,database):
        owned_name(database)
        safe={'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','COMSPEC','PATHEXT','APPDATA','LOCALAPPDATA','USERPROFILE','HOME','LANG','LC_ALL','LC_CTYPE','TZ'}
        result={k:v for k,v in os.environ.items() if k.upper() in safe}
        names={'host':'PGHOST','hostaddr':'PGHOSTADDR','port':'PGPORT','user':'PGUSER','password':'PGPASSWORD',
               'sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY'}
        result.update({target:str(self.connection[key]) for key,target in names.items() if key in self.connection})
        result.update(PGDATABASE=database,PGCONNECT_TIMEOUT='10',PGAPPNAME='ss_disposable_restore')
        return result

    def connect(self,database,*,schema=None,autocommit=False):
        if database!='postgres':owned_name(database)
        if schema:owned_name(schema)
        settings=dict(self.connection,dbname=database,connect_timeout=10,
            application_name='ss_disposable_restore',options='-c search_path='+(schema+',pg_catalog' if schema else 'pg_catalog'))
        connection=psycopg.connect(**settings,row_factory=dict_row,autocommit=autocommit)
        try:
            facts=connection.execute("SELECT current_database() AS name,current_user AS role,host(inet_server_addr()) AS host,current_setting('server_version_num')::int AS version").fetchone()
            major,minor=map(int,self.version.split('.'))
            if facts['name']!=database or facts['role']!='ss_fixture' or not ipaddress.ip_address(facts['host']).is_loopback or facts['version']!=major*10000+minor:raise DrillError('The connected server does not match the marked local fixture.')
            if not connection.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()['ssl']:raise DrillError('The fixture connection is not encrypted.')
            return connection
        except BaseException:connection.close();raise


class FixtureDatabase:
    def __init__(self,config,name,schema):self.config=config;self.name=owned_name(name);self.schema=owned_name(schema)
    @contextmanager
    def transaction(self):
        with self.config.connect(self.name,schema=self.schema) as connection:yield connection
    def migrate_fresh_only(self):
        with self.config.connect(self.name,autocommit=True) as connection:
            if connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(self.schema,)).fetchone():raise DrillError('The application schema must be fresh, never an existing namespace.')
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        with self.transaction() as connection:
            connection.execute((ROOT/'hosted/schema.sql').read_text(encoding='utf-8'),prepare=False)
    def close(self):pass  # Each short transaction owns and closes its connection.


def write_json(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp');temporary.write_text(json.dumps(value,indent=2),encoding='utf-8');os.replace(temporary,path)


def create_owned(config,name,marker,owned):
    owned_name(name)
    with config.connect('postgres',autocommit=True) as connection:
        if connection.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone():raise DrillError('An owned database name unexpectedly already exists; refusing reuse.')
        connection.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
        row=connection.execute('SELECT oid FROM pg_database WHERE datname=%s',(name,)).fetchone();owned[name]={'oid':row['oid'],'marker':marker}
        connection.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(sql.Identifier(name),sql.Literal(marker)))


def drop_owned(config,name,owned):
    owned_name(name)
    if name not in owned:raise DrillError('Cleanup refuses a namespace not created by this run.')
    with config.connect('postgres',autocommit=True) as connection:
        row=connection.execute("SELECT oid,shobj_description(oid,'pg_database') AS marker FROM pg_database WHERE datname=%s",(name,)).fetchone()
        if not row:return 'already_absent'
        if row['oid']!=owned[name]['oid'] or row['marker']!=owned[name]['marker']:raise DrillError('Cleanup ownership/OID changed; refusing to drop this database.')
        connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
    return 'dropped_exact_owned_database'


def run_binary(config,program,arguments,database,log):
    name=program+('.exe' if os.name=='nt' else '');path=config.binary(name);started=time.monotonic()
    result=subprocess.run([str(path),*arguments],env=config.child_env(database),capture_output=True,timeout=180,check=False)
    log.add('postgres_binary',program=program,returncode=result.returncode,elapsed_seconds=round(time.monotonic()-started,3),
            stdout=result.stdout.decode('utf-8','replace'),stderr=result.stderr.decode('utf-8','replace'))
    if result.returncode:raise DrillError('A PostgreSQL dump/restore tool failed; see the redacted stage log.')


def verify_versions(config,log):
    for program in ('pg_dump','pg_restore'):
        name=program+('.exe' if os.name=='nt' else '');path=config.binary(name)
        safe_env={key:value for key,value in os.environ.items() if key.upper() in ('SYSTEMROOT','WINDIR','PATH','TEMP','TMP','COMSPEC','PATHEXT')}
        result=subprocess.run([str(path),'--version'],env=safe_env,capture_output=True,timeout=10,check=False)
        output=result.stdout.decode('utf-8','replace').strip()
        if result.returncode or not re.search(r'\b'+re.escape(config.version)+r'\b',output):raise DrillError('A pinned PostgreSQL binary has an unexpected version.')
        log.add('pinned_binary_version',program=program,version=config.version,sha256=config.hashes[name])


def canonical(value):
    if isinstance(value,dict):return {str(k):canonical(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [canonical(v) for v in value]
    if isinstance(value,(uuid.UUID,datetime,date,Decimal)):return str(value)
    if isinstance(value,(bytes,memoryview)):return bytes(value).hex()
    return value


def snapshot(database):
    result={}
    with database.transaction() as connection:
        for name in TABLES:
            rows=connection.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(database.schema,name))).fetchall()
            result[name]=sorted(json.dumps(canonical(row),sort_keys=True,separators=(',',':')) for row in rows)
        sequences=connection.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind='S' ORDER BY c.relname",(database.schema,)).fetchall()
        for row in sequences:
            state=connection.execute(sql.SQL('SELECT last_value,is_called FROM {}').format(sql.Identifier(database.schema,row['relname']))).fetchone()
            result['sequence:'+row['relname']]=[json.dumps(state,sort_keys=True)]
    return result


def snapshot_hashes(value):return {name:{'rows':len(rows),'sha256':hashlib.sha256('\n'.join(rows).encode()).hexdigest()} for name,rows in value.items()}


def seed_fixture(database,key,run_id,log):
    from hosted.auth import Auth
    from hosted.ledger import Ledger
    from hosted.trades import Trades
    from hosted.security import password_hash
    from hosted.mfa import secret_bytes,totp
    tenant,owner,disabled=uuid.uuid4(),uuid.uuid4(),uuid.uuid4();password=secrets.token_urlsafe(24);log.private(password)
    slug='restore-fixture-'+run_id[:16];address='fictional-restore-'+run_id;now=datetime.now(timezone.utc)
    with database.transaction() as connection:
        connection.execute('CREATE TABLE ss_restore_drill_marker(run_id UUID PRIMARY KEY,fixture_only BOOLEAN NOT NULL CHECK(fixture_only=TRUE))')
        connection.execute('INSERT INTO ss_restore_drill_marker VALUES (%s,TRUE)',(uuid.UUID(run_id),))
        connection.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',(tenant,slug,'Fictional restore store'))
        hashed=password_hash(password)
        for ident,role,email in ((owner,'owner','owner@example.invalid'),(disabled,'manager','disabled@example.invalid')):
            connection.execute('INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,%s)',(ident,tenant,email,hashed,role))
    auth=Auth(database,encryption_keys=key);auth.mfa.clock=lambda:now
    credentials={'store_slug':slug,'email':'owner@example.invalid','password':password,'mfa_supported':True}
    token,_=auth.login(credentials,address);log.private(token);actor=auth.resolve(token)
    setup=auth.mfa.begin(actor,{'password':password},address);log.private(setup['secret'],setup['enrollment_token'])
    confirmed=auth.mfa.confirm(actor,{'enrollment_token':setup['enrollment_token'],'code':totp(secret_bytes(setup['secret']),now.timestamp())},address)
    log.private(*confirmed['recovery_codes']);actor=auth.resolve(token)
    disabled_token,_=auth.login(credentials|{'email':'disabled@example.invalid'},address);log.private(disabled_token);auth.disable_staff(actor,disabled)
    ledger=Ledger(database);customer=ledger.create_customer(actor,{'request_key':'restore-create-customer','display_name':'Fictional restore customer','permission_confirmed':True})['customer_id']
    opening={'request_key':'restore-opening','amount_cents':5000,'kind':'opening','reference':'FICTIONAL RESTORE','reason':'Disposable logical restore fixture','version':0}
    opening_result=ledger.adjust(actor,customer,opening)
    trade=Trades(database);draft=trade.create(actor,{'request_key':'restore-create-trade','customer_id':customer,'title':'Fictional restored trade',
        'source_reference':'fixture:'+run_id,'lines':[{'identity':{'name':'Fictional restore card','game':'riftbound','kind':'card','number':'001'},'quantity':2,'unit_intake_cents':1000,'included':True,'reviewed':True}]})
    accept={'request_key':'restore-accept-trade','version':draft['version'],'customer_id':customer,'offer_type':'credit','expected_offer_cents':1000,'customer_agreed':True,'items_received':True}
    accepted=trade.accept(actor,draft['trade_id'],accept)
    return {'tenant':tenant,'owner':owner,'disabled':disabled,'customer':customer,'actor':actor,'credentials':credentials,
        'token':token,'disabled_token':disabled_token,'seed':setup['secret'],'recovery':confirmed['recovery_codes'][0],
        'time':now,'address':address,'trade_id':draft['trade_id'],'accept':accept,'accepted':accepted,'opening':opening,'opening_result':opening_result}


def check_business(database,fixture):
    from hosted.ledger import Ledger
    from hosted.trades import Trades
    from hosted.settlement import evidence_hash
    from hosted.security import ServiceError
    ledger=Ledger(database);account=ledger.account(fixture['actor'],fixture['customer']);customer=account['customer']
    with database.transaction() as connection:
        lots=connection.execute('SELECT * FROM ss_credit_lots WHERE tenant_id=%s AND customer_id=%s',(fixture['tenant'],fixture['customer'])).fetchall()
        enabled=connection.execute('SELECT credit_checkout_enabled FROM ss_tenants WHERE id=%s',(fixture['tenant'],)).fetchone()['credit_checkout_enabled']
    if customer['balance_cents']!=6000 or sum(row['amount_cents'] for row in account['entries'])!=6000 or sum(row['remaining_cents'] for row in lots)!=6000 or len(lots)!=2:raise DrillError('Restored credit/lot conservation failed.')
    if enabled or any(row['policy_id'] is not None for row in lots) or set(row['origin_code'] for row in lots)!={'unknown','trade_unreviewed'}:raise DrillError('Restored credit policy gates changed.')
    trade=Trades(database);accepted=trade.get(fixture['actor'],fixture['trade_id'])
    if evidence_hash(accepted['accepted_receipt'])!=accepted['receipt_hash']:raise DrillError('Accepted-trade evidence hash changed.')
    if trade.accept(fixture['actor'],fixture['trade_id'],fixture['accept'])!=fixture['accepted']:raise DrillError('Restored trade replay changed its exact result.')
    if ledger.adjust(fixture['actor'],fixture['customer'],fixture['opening'])!=fixture['opening_result']:raise DrillError('Restored ledger replay changed its exact result.')
    try:ledger.reserve(fixture['actor'],fixture['customer'],{'request_key':'restore-gate','amount_cents':100,'order_id':'fictional-order'})
    except ServiceError as error:
        if error.status!=409:raise DrillError('Restored checkout denial had the wrong status.') from None
    else:raise DrillError('Restored checkout incorrectly became enabled.')
    return {'balance_cents':6000,'lots':2,'entries':len(account['entries']),'accepted_trade_replay_exact':True,'checkout_still_disabled':True}


def check_security(database,key,fixture,log):
    from hosted.auth import Auth
    from hosted.mfa import SeedCipher,secret_bytes,totp
    from hosted.security import ServiceError,digest
    auth=Auth(database,encryption_keys=key);now=fixture['time']+timedelta(seconds=30);auth.mfa.clock=lambda:now
    with database.transaction() as connection:state=connection.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s',(fixture['tenant'],fixture['owner'])).fetchone()
    seed=SeedCipher(key).decrypt(state['encrypted_seed'],fixture['tenant'],fixture['owner'],'active',state['version'])
    if seed!=fixture['seed']:raise DrillError('Restored encrypted seed/key continuity failed.')
    try:SeedCipher(Fernet.generate_key().decode()).decrypt(state['encrypted_seed'],fixture['tenant'],fixture['owner'],'active',state['version'])
    except ServiceError:pass
    else:raise DrillError('A wrong encryption key decrypted the restored MFA seed.')
    if not auth.resolve(fixture['token'])['mfa_authenticated']:raise DrillError('The restored verified session lost its proof binding.')
    try:auth.resolve(fixture['disabled_token'])
    except ServiceError as error:
        if error.status!=401:raise DrillError('Disabled-session denial had the wrong status.') from None
    else:raise DrillError('A disabled staff session became valid after restore.')
    try:
        with database.transaction() as connection:connection.execute("INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at) VALUES (%s,%s,%s,now()+interval '1 hour')",(digest(secrets.token_urlsafe(32)),fixture['tenant'],fixture['disabled']))
    except psycopg.errors.CheckViolation:pass
    else:raise DrillError('The restored disabled-session database guard was not enforced.')
    _,challenge=auth.login(fixture['credentials'],fixture['address']);log.private(challenge['challenge_token'])
    verified,_=auth.mfa.verify_challenge({'challenge_token':challenge['challenge_token'],'code':totp(secret_bytes(seed),now.timestamp())},fixture['address']);log.private(verified)
    if not auth.resolve(verified)['mfa_authenticated']:raise DrillError('Restored TOTP verification did not establish MFA scope.')
    _,challenge=auth.login(fixture['credentials'],fixture['address']);log.private(challenge['challenge_token'])
    recovered,_=auth.mfa.verify_challenge({'challenge_token':challenge['challenge_token'],'code':fixture['recovery']},fixture['address']);log.private(recovered)
    _,replay=auth.login(fixture['credentials'],fixture['address']);log.private(replay['challenge_token'])
    try:auth.mfa.verify_challenge({'challenge_token':replay['challenge_token'],'code':fixture['recovery']},fixture['address'])
    except ServiceError as error:
        if error.status!=401:raise DrillError('Restored recovery replay had the wrong status.') from None
    else:raise DrillError('A restored recovery code could be reused.')
    return {'same_key_decrypts':True,'wrong_key_denied':True,'totp_after_restore':True,'recovery_once_after_restore':True,'disabled_session_denied':True,'database_session_guard_enforced':True}


def fingerprints():
    paths=[p for p in (ROOT/'hosted').rglob('*') if p.suffix in ('.py','.sql') and p.is_file()]
    paths.append(Path(__file__))
    return {str(p.relative_to(ROOT)):sha(p) for p in sorted(paths)}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--execute-local-disposable',action='store_true');args=parser.parse_args()
    run_id=uuid.uuid4().hex;output=ROOT/'Testing'/'next-security'/'restore-drills'/run_id;output.mkdir(parents=True,exist_ok=False)
    try:output.chmod(0o700)
    except OSError:pass
    log=RedactedLog();log.private(os.environ.get('DATABASE_URL',''),os.environ.get('SS_RESTORE_MFA_KEY',''))
    summary={'run_id':run_id,'started_utc':datetime.now(timezone.utc).isoformat(),'scope':'Disposable loopback logical backup/restore only; not Render PITR or production RPO/RTO evidence.','passed':False,'executed':False,'cleanup':{}}
    config=None;owned={};before=fingerprints();source_name='ss_test_'+uuid.uuid4().hex;target_name='ss_test_'+uuid.uuid4().hex;schema='ss_test_'+uuid.uuid4().hex
    marker='stock-sleeve-disposable-restore:'+run_id;manifest=output/'owned-namespaces.json'
    write_json(manifest,{'run_id':run_id,'planned_databases':[source_name,target_name],'schema':schema,'owned':owned})
    try:
        if not args.execute_local_disposable:raise DrillError('Execution requires --execute-local-disposable and the fixture environment guards.')
        config=Config.from_env(os.environ);log.private(config.connection['password'],config.key,'ss_fixture');verify_versions(config,log)
        with config.connect('postgres',autocommit=True) as connection:
            role=connection.execute('SELECT rolcreatedb,rolsuper FROM pg_roles WHERE rolname=current_user').fetchone()
            if not role or not(role['rolcreatedb'] or role['rolsuper']):raise DrillError('The disposable fixture role needs CREATEDB; this drill never changes roles.')
        summary.update(executed=True,source_database=source_name,restored_database=target_name,schema=schema,binary_sha256=config.hashes)
        for name in (source_name,target_name):
            create_owned(config,name,marker,owned);write_json(manifest,{'run_id':run_id,'planned_databases':[source_name,target_name],'schema':schema,'owned':owned});log.add('created_exact_owned_database',database=name)
        source=FixtureDatabase(config,source_name,schema);target=FixtureDatabase(config,target_name,schema);source.migrate_fresh_only()
        with patch('requests.sessions.Session.request',side_effect=AssertionError('Provider HTTP is forbidden in the local restore drill.')):
            fixture=seed_fixture(source,config.key,run_id,log);check_business(source,fixture);initial=snapshot(source);summary['source_snapshot']=snapshot_hashes(initial)
            archive=output/'fictional-fixture.dump'
            run_binary(config,'pg_dump',['--format=custom','--no-owner','--no-privileges','--no-password','--schema='+schema,'--file='+str(archive)],source_name,log)
            if not archive.is_file() or archive.is_symlink():raise DrillError('The dump archive is missing or is not an owned regular file.')
            summary['archive_sha256']=sha(archive);summary['archive_bytes']=archive.stat().st_size
            content=archive.read_bytes()
            if any(value.encode() in content for value in log.secrets if len(value)>=16):raise DrillError('A plaintext fixture credential unexpectedly appeared in the owned dump archive.')
            summary['plaintext_fixture_secrets_absent_from_archive']=True
            try:archive.chmod(0o600)
            except OSError:pass
            if snapshot(source)!=initial:raise DrillError('The source fixture changed during the logical backup.')
            # A harmless connection-string parameter selects direct-connect mode;
            # all database routing/user/password values remain in child env.
            run_binary(config,'pg_restore',['--exit-on-error','--single-transaction','--no-owner','--no-privileges','--no-password','--dbname=application_name=ss_disposable_restore',str(archive)],target_name,log)
            if sha(archive)!=summary['archive_sha256']:raise DrillError('The owned dump archive changed during restore.')
            restored=snapshot(target);summary['restored_initial_snapshot']=snapshot_hashes(restored)
            if restored!=initial:raise DrillError('Selected exact rows or sequence states changed during logical restore.')
            summary['exact_initial_rows_and_sequences_equal']=True;summary['business_invariants']=check_business(target,fixture)
            summary['security_invariants']=check_security(target,config.key,fixture,log)
            after_checks=snapshot(target)
            if any(after_checks[name]!=initial[name] for name in BUSINESS):raise DrillError('Functional security/replay probes changed restored business rows.')
            if snapshot(source)!=initial:raise DrillError('The source fixture changed while the separate restored database was tested.')
            summary.update(source_untouched_after_backup=True,restored_business_rows_unchanged=True,passed=True)
            log.add('logical_restore_checks_passed',exact_rows_equal=True,source_unchanged=True)
    except BaseException as error:
        code=getattr(error,'sqlstate',None);facts={'error_type':type(error).__name__}
        if isinstance(error,DrillError):facts['message']=str(error)
        if isinstance(code,str) and re.fullmatch(r'[0-9A-Z]{5}',code):facts['sqlstate']=code
        log.add('drill_failed',**facts);summary['error']=facts
    finally:
        for name in reversed(list(owned)):
            try:summary['cleanup'][name]=drop_owned(config,name,owned);log.add('cleanup_exact_owned_database',database=name,status=summary['cleanup'][name])
            except BaseException as error:summary['cleanup'][name]='refused_or_failed:'+type(error).__name__;summary['passed']=False;log.add('cleanup_needs_owner_review',database=name,error_type=type(error).__name__)
        final=fingerprints();summary['source_sha256_at_start']=before;summary['source_sha256']=final;summary['source_snapshot_stable']=before==final
        if before!=final:summary['passed']=False
        summary['finished_utc']=datetime.now(timezone.utc).isoformat();log_text=log.text();summary['log_sha256']=hashlib.sha256(log_text.encode()).hexdigest()
        (output/'restore-drill.log').write_text(log_text,encoding='utf-8');write_json(output/'restore-drill.json',summary)
        print('Logical restore drill passed='+str(summary['passed'])+'; evidence='+str(output),flush=True)
    return 0 if summary['passed'] else 1


if __name__=='__main__':sys.exit(main())
