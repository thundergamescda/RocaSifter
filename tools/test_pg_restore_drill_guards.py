"""Pure mocked restore-drill preflight tests; never connect or invoke PG tools."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,Mock,patch
from cryptography.fernet import Fernet
from tools import pg_logical_restore_drill as drill


class RestoreDrillGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.suffix='.exe' if os.name=='nt' else '';pins={}
        for program in ('pg_dump','pg_restore'):
            name=program+self.suffix;path=self.root/name;path.write_bytes(('fictional binary, never executed: '+program).encode());pins[name]=drill.sha(path)
        self.environment={'SS_DISPOSABLE_PG_FIXTURE':'1','SS_PG_BIN_DIR':str(self.root),'SS_PG_BIN_SHA256_JSON':json.dumps(pins),
            'DATABASE_URL':'postgresql://ss_fixture:fictional-fixture-password-1234@127.0.0.1:5432/postgres?sslmode=require',
            'SS_RESTORE_MFA_KEY':Fernet.generate_key().decode()}
        self.name='ss_test_'+'a'*32

    def config(self):return drill.Config.from_env(self.environment)

    def test_no_marker_refuses_before_database_or_binary_execution(self):
        with patch.object(drill.psycopg,'connect') as connect,patch.object(drill.subprocess,'run') as run:
            with self.assertRaises(drill.DrillError):drill.Config.from_env(self.environment|{'SS_DISPOSABLE_PG_FIXTURE':''})
            connect.assert_not_called();run.assert_not_called()

    def test_remote_routing_wrong_role_database_and_plaintext_are_rejected(self):
        changes=('postgresql://ss_fixture:password@remote.invalid:5432/postgres?sslmode=require',
                 'postgresql://postgres:password@127.0.0.1:5432/postgres?sslmode=require',
                 'postgresql://ss_fixture:password@127.0.0.1:5432/real_store?sslmode=require',
                 'postgresql://ss_fixture:password@127.0.0.1:5432/postgres?sslmode=disable',
                 'postgresql://ss_fixture:password@127.0.0.1:5432/postgres?options=unsafe')
        with patch.object(drill.psycopg,'connect') as connect,patch.object(drill.subprocess,'run') as run:
            for value in changes:
                with self.subTest(kind=value.split('?')[-1]),self.assertRaises(drill.DrillError):drill.Config.from_env(self.environment|{'DATABASE_URL':value})
            connect.assert_not_called();run.assert_not_called()

    def test_missing_key_binary_pin_and_inherited_service_fail_closed(self):
        for changes in ({'SS_RESTORE_MFA_KEY':''},{'SS_PG_BIN_SHA256_JSON':'{}'},{'PGSERVICE':'unexpected'}):
            with self.subTest(fields=list(changes)),self.assertRaises(drill.DrillError):drill.Config.from_env(self.environment|changes)

    def test_credentials_are_not_in_config_repr_and_binary_is_rechecked(self):
        config=self.config();self.assertNotIn(config.key,repr(config));self.assertNotIn(config.connection['password'],repr(config))
        (self.root/('pg_dump'+self.suffix)).write_bytes(b'changed')
        with self.assertRaises(drill.DrillError):config.binary('pg_dump'+self.suffix)
        with self.assertRaises(drill.DrillError):config.binary('../pg_dump'+self.suffix)

    def test_child_environment_only_has_exact_fixture_connection_and_os_values(self):
        config=self.config()
        with patch.dict(os.environ,{'AWS_SECRET_ACCESS_KEY':'must-not-inherit','APP_ENCRYPTION_KEYS':'must-not-inherit','DATABASE_URL':'must-not-inherit','PGSERVICE':'must-not-inherit'}):
            environment=config.child_env(self.name)
        self.assertEqual(environment['PGDATABASE'],self.name);self.assertEqual(environment['PGUSER'],'ss_fixture')
        self.assertEqual(environment['PGPASSWORD'],config.connection['password']);self.assertEqual(environment['PGHOSTADDR'],'127.0.0.1')
        for key in ('AWS_SECRET_ACCESS_KEY','APP_ENCRYPTION_KEYS','DATABASE_URL','PGSERVICE','SS_RESTORE_MFA_KEY'):self.assertNotIn(key,environment)
        with self.assertRaises(drill.DrillError):config.child_env('postgres')

    def test_credentials_are_environment_only_in_dump_restore_calls_and_logs_redacted(self):
        config=self.config();log=drill.RedactedLog();log.private(config.connection['password'],self.environment['DATABASE_URL'])
        result=Mock(returncode=0,stdout=b'',stderr=b'')
        with patch.object(drill.subprocess,'run',return_value=result) as run,redirect_stdout(io.StringIO()):
            drill.run_binary(config,'pg_restore',['--dbname=application_name=ss_disposable_restore','fictional.dump'],self.name,log)
        argv=run.call_args.args[0];environment=run.call_args.kwargs['env']
        self.assertNotIn(config.connection['password'],' '.join(argv));self.assertNotIn('ss_fixture',' '.join(argv));self.assertNotIn(self.environment['DATABASE_URL'],' '.join(argv))
        self.assertEqual(environment['PGPASSWORD'],config.connection['password']);self.assertEqual(environment['PGDATABASE'],self.name)
        result=Mock(returncode=1,stdout=b'',stderr=('fatal '+config.connection['password']).encode())
        with patch.object(drill.subprocess,'run',return_value=result),redirect_stdout(io.StringIO()),self.assertRaises(drill.DrillError):drill.run_binary(config,'pg_dump',[],self.name,log)
        self.assertNotIn(config.connection['password'],log.text());self.assertIn('[redacted]',log.text())

    def test_cleanup_requires_exact_recorded_uuid_name_before_any_connection(self):
        config=Mock()
        for name in ('postgres','public','ss_test_*',self.name):
            with self.subTest(name=name),self.assertRaises(drill.DrillError):drill.drop_owned(config,name,{})
        config.connect.assert_not_called()

    def test_changed_oid_or_ownership_comment_never_issues_drop(self):
        for row in ({'oid':12,'marker':'owned-marker'},{'oid':11,'marker':'another-run'}):
            connection=Mock();connection.execute.return_value.fetchone.return_value=row;context=MagicMock();context.__enter__.return_value=connection
            config=Mock();config.connect.return_value=context
            with self.subTest(kind=row['oid']),self.assertRaises(drill.DrillError):drill.drop_owned(config,self.name,{self.name:{'oid':11,'marker':'owned-marker'}})
            self.assertEqual(connection.execute.call_count,1)

    def test_exact_owned_oid_and_comment_only_drop_that_database(self):
        connection=Mock();connection.execute.return_value.fetchone.return_value={'oid':11,'marker':'owned-marker'}
        context=MagicMock();context.__enter__.return_value=connection;config=Mock();config.connect.return_value=context
        result=drill.drop_owned(config,self.name,{self.name:{'oid':11,'marker':'owned-marker'}})
        self.assertEqual(result,'dropped_exact_owned_database');self.assertEqual(connection.execute.call_count,2)
        statement=connection.execute.call_args.args[0].as_string()
        self.assertEqual(statement,'DROP DATABASE "'+self.name+'"');self.assertNotIn('FORCE',statement)

    def test_binary_versions_are_checked_without_credentials_or_database_calls(self):
        config=self.config();result=Mock(returncode=0,stdout=b'PostgreSQL 18.6',stderr=b'');log=drill.RedactedLog()
        with patch.object(drill.subprocess,'run',return_value=result) as run,patch.object(drill.psycopg,'connect') as connect,redirect_stdout(io.StringIO()):
            drill.verify_versions(config,log)
        self.assertEqual(run.call_count,2);connect.assert_not_called()
        for call in run.call_args_list:self.assertNotIn('PGPASSWORD',call.kwargs['env'])

    def test_bounded_wrapper_timeout_is_allowed_but_connections_keep_fixed_timeout(self):
        environment=self.environment|{'DATABASE_URL':self.environment['DATABASE_URL']+'&connect_timeout=5'}
        config=drill.Config.from_env(environment);connection=Mock()
        connection.execute.side_effect=(Mock(fetchone=Mock(return_value={'name':'postgres','role':'ss_fixture','host':'127.0.0.1','version':180006})),Mock(fetchone=Mock(return_value={'ssl':True})))
        with patch.object(drill.psycopg,'connect',return_value=connection) as connect:config.connect('postgres')
        self.assertEqual(connect.call_args.kwargs['connect_timeout'],10)
        for value in ('0','11','bad','5.5'):
            with self.subTest(value=value),self.assertRaises(drill.DrillError):drill.Config.from_env(self.environment|{'DATABASE_URL':self.environment['DATABASE_URL']+'&connect_timeout='+value})

    def test_connected_inet_address_is_normalized_by_postgres_before_loopback_check(self):
        config=self.config();connection=Mock()
        def execute(statement):
            # PostgreSQL inet::text can carry /32; host(inet) returns the address.
            if 'inet_server_addr()' in statement:
                host='127.0.0.1' if 'host(inet_server_addr()) AS host' in statement else '127.0.0.1/32'
                return Mock(fetchone=Mock(return_value={'name':'postgres','role':'ss_fixture','host':host,'version':180006}))
            return Mock(fetchone=Mock(return_value={'ssl':True}))
        connection.execute.side_effect=execute
        with patch.object(drill.psycopg,'connect',return_value=connection):self.assertIs(config.connect('postgres'),connection)
        connection.close.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
