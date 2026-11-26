"""UNRUN private TLS-PG source proposals, not legal/staff/feature acceptance.

Uses only an owned ss_test_UUID schema. Explicit fictional policy text, staff
IDs and historical dates are data fixtures; they do not prove consent or current
owner authority. No provider, customer, balance, journal or app route is invoked.
No DATABASE_URL means acceptance is UNRUN, never a passing skipped proof.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import os
import threading
import uuid
import unittest

import psycopg
from psycopg import sql
import intake_label_policy_evidence as codec
from hosted import intake_label_policy_records as records
from hosted import intake_label_policy_store as store
from hosted import intake_label_schema as schema_v9
import test_hosted_settlement_postgres as pg_fixture


AT = '2026-11-01T01:02:03.000004Z'
LATER = '2026-11-02T01:02:03.000004Z'
EARLIER = '2026-10-31T01:02:03.000004Z'


class PostgreSQLPolicyPersistenceAcceptance(pg_fixture._PostgreSQLSettlementFixture):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get('DATABASE_URL'):
            raise RuntimeError('DATABASE_URL missing: private PG acceptance is UNRUN.')
        super().setUpClass()
        try:
            with cls.database.transaction() as db:
                cls.v9_before = schema_v9.install(db)
                if cls.v9_before.state != 'present' or cls.v9_before.version != 9:
                    raise AssertionError('Exact existing v9 schema required')
                db.execute(store.migration_proposal(cls.schema))
                actual = store.inspect_schema(db, schema=cls.schema)
                if actual.state != 'present' or actual.table_count != 2 or actual.feature_enabled is not False:
                    raise AssertionError('Exact empty policy catalog required')
        except Exception:
            cls.tearDownClass()
            raise

    def setUp(self):
        # Deliberately do NOT call the base financial fixture's setUp.
        self.tenant, self.staff = str(uuid.uuid4()), str(uuid.uuid4())
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES(%s,%s,%s)',
                       (self.tenant,'policy-'+self.tenant,'Fictional policy store'))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES(%s,%s,%s,'fictional-no-password','owner')",
                       (self.staff,self.tenant,self.staff+'@example.invalid'))
        self.repository = store.PolicyRepository(self.database,schema=self.schema,enabled=True)

    def capture(self, *, tenant=None, original=None):
        with self.database.transaction() as db:
            return records.capture_policy_records_for_writer(db,
                records.PolicyRecordSelector(tenant or self.tenant,original),schema=self.schema)

    def counts(self):
        with self.database.transaction() as db:
            return tuple(db.execute('SELECT count(*) AS n FROM '+name+' WHERE tenant_id=%s',
                                     (self.tenant,)).fetchone()['n'] for name in
                         ('ss_store_label_policy_heads','ss_store_label_policy_versions'))

    def initialize(self):
        return self.repository.apply_prepared(store.prepare_initialize(self.capture()))

    def evidence(self, *, revision=1, enabled=False, staff=None, tenant=None, text=None):
        document = text or 'Fictional exact document: café.\r\nNo legal approval asserted.'
        notice = 'Fictional notice only — not a consent flow.\n'
        atoms = dict(envelope_version=1,tenant_id=tenant or self.tenant,revision=revision,
            document_format='ss_label_policy_document_utf8_v1',document_hash=codec.document_hash(document),
            notice_hash=codec.notice_hash(notice),accepted_enabled=enabled,accepted_by=staff or self.staff,
            accepted_at=AT,evidence_use_expires_at=LATER)
        return codec.PolicyEvidence(policy_hash=codec.evidence_hash(**atoms),document_body=document,
                                    notice_body=notice,**atoms)

    def append_prepared(self, *, expected=None, evidence=None):
        expected = expected or self.capture()
        evidence = evidence or self.evidence()
        head = replace(expected.head.row,policy_revision=expected.head.row.policy_revision+1,
            policy_state='enabled' if evidence.accepted_enabled else 'disabled',
            current_version_revision=evidence.revision,current_policy_hash=evidence.policy_hash)
        return store.prepare_append(expected,evidence,head)

    def test_empty_install_does_not_create_acceptance_clock_or_feature_defaults(self):
        self.assertEqual(self.counts(),(0,0))
        with self.database.transaction() as db:
            state = store.inspect_schema(db,schema=self.schema)
        self.assertIs(state.feature_enabled,False)
        self.assertEqual(self.capture().head.state,'absent')

    def test_absent_and_partial_schema_are_not_implicitly_installed(self):
        scratch = 'ss_test_'+uuid.uuid4().hex
        with self.database.transaction() as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(scratch)))
        try:
            with self.database.transaction() as db:
                self.assertEqual(store.inspect_schema(db,schema=scratch).state,'absent')
                db.execute(sql.SQL('CREATE TABLE {}.ss_store_label_policy_heads(tenant_id uuid PRIMARY KEY)').format(sql.Identifier(scratch)))
            with self.database.transaction() as db:
                with self.assertRaises(store.PolicyPersistenceError) as error:
                    store.inspect_schema(db,schema=scratch)
                self.assertEqual(error.exception.code,'INTAKE_LABEL_POLICY_STORE_CATALOG')
        finally:
            with self.database.transaction() as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(scratch)))
        self.assertEqual(self.counts(),(0,0))

    def test_default_disabled_does_not_evaluate_prepared_or_database(self):
        class Forbidden:
            def transaction(self):
                raise AssertionError('Disabled persistence must not evaluate database')
        with self.assertRaises(store.PolicyPersistenceError) as error:
            store.PolicyRepository(Forbidden(),schema=self.schema).apply_prepared(object())
        self.assertEqual(error.exception.code,'INTAKE_LABEL_POLICY_STORE_DISABLED')
        self.assertEqual(self.counts(),(0,0))

    def test_explicit_initialization_is_unaccepted_and_uninitialized_only(self):
        observed = self.initialize()
        current = self.capture()
        self.assertEqual((observed.policy_revision,current.head.row.policy_state), (1,'unaccepted'))
        self.assertEqual((current.head.row.current_version_revision,current.head.row.current_policy_hash,
            current.head.row.clock_floor_at,current.head.row.clock_revision,current.head.row.clock_state),
            (None,None,None,0,'uninitialized'))
        self.assertEqual(self.counts(),(1,0))

    def test_exact_unicode_evidence_roundtrip_and_expired_history_are_data_only(self):
        self.initialize()
        evidence = self.evidence(enabled=True)
        self.repository.apply_prepared(self.append_prepared(evidence=evidence))
        captured = self.capture(original=evidence.policy_hash)
        selected = captured.current_version.row
        verified = codec.verify_evidence(codec.PolicyEvidence(*(getattr(selected,key) for key in records.VERSION_FIELDS)))
        self.assertEqual(verified.document_body.encode('utf-8'), evidence.document_body.encode('utf-8'))
        self.assertEqual(verified.notice_body.encode('utf-8'), evidence.notice_body.encode('utf-8'))
        self.assertEqual(verified.policy_hash,evidence.policy_hash)
        self.assertEqual(captured.head.row.clock_state,'uninitialized')
        self.assertIsNone(captured.head.row.clock_floor_at)
        self.assertEqual(self.counts(),(1,1))
        # Historical timestamps and accepted_enabled are raw evidence fields.
        # No current-clock, enabled staff, consent, routes or feature gate ran.

    def test_versions_are_native_immutable_and_head_delete_is_denied(self):
        self.initialize(); self.repository.apply_prepared(self.append_prepared())
        for statement in ('UPDATE ss_store_label_policy_versions SET notice_body=notice_body WHERE tenant_id=%s',
                          'DELETE FROM ss_store_label_policy_versions WHERE tenant_id=%s',
                          'DELETE FROM ss_store_label_policy_heads WHERE tenant_id=%s'):
            with self.subTest(statement=statement.split()[0:2]):
                with self.assertRaises(psycopg.Error) as error:
                    with self.database.transaction() as db:
                        db.execute(statement,(self.tenant,))
                self.assertEqual(error.exception.sqlstate,'23514')
        self.assertEqual(self.counts(),(1,1))

    def test_second_version_appends_without_replacing_original_evidence(self):
        self.initialize()
        first = self.evidence()
        self.repository.apply_prepared(self.append_prepared(evidence=first))
        second = self.evidence(revision=2,text='Second fictional policy document; no legal approval implied.')
        self.repository.apply_prepared(self.append_prepared(evidence=second))
        captured = self.capture(original=first.policy_hash)
        self.assertEqual((captured.head.row.policy_revision,captured.current_version.row.revision,
                          captured.original_version.row.revision),(3,2,1))
        self.assertEqual(captured.original_version.row.document_body,first.document_body)
        self.assertEqual(captured.original_version.row.policy_hash,first.policy_hash)
        self.assertEqual(captured.current_version.row.policy_hash,second.policy_hash)
        self.assertEqual(self.counts(),(1,2))

    def test_stale_head_cas_cannot_overwrite_new_revision(self):
        self.initialize()
        expected = self.capture()
        first = store.prepare_head_cas(expected,replace(expected.head.row,policy_revision=2,policy_state='disabled'))
        second = store.prepare_head_cas(expected,replace(expected.head.row,policy_revision=2,policy_state='restricted'))
        self.repository.apply_prepared(first)
        with self.assertRaises(store.PolicyPersistenceError) as error:self.repository.apply_prepared(second)
        self.assertEqual(error.exception.code,'INTAKE_LABEL_POLICY_STORE_CHANGED')
        current = self.capture().head.row
        self.assertEqual((current.policy_revision,current.policy_state),(2,'disabled'))
        self.assertEqual(self.counts(),(1,0))

    def race(self, prepared):
        barrier,gate,pids = threading.Barrier(2,timeout=20),threading.Lock(),set()
        owner = self
        class RacingDatabase:
            @contextmanager
            def transaction(self):
                with owner.database.transaction() as db:
                    pid = db.execute('SELECT pg_catalog.pg_backend_pid() AS pid').fetchone()['pid']
                    with gate:pids.add(pid)
                    barrier.wait()
                    yield db
        repositories = [store.PolicyRepository(RacingDatabase(),schema=self.schema,enabled=True) for _ in range(2)]
        def invoke(repository):
            try:
                repository.apply_prepared(prepared)
                return 'committed'
            except store.PolicyPersistenceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(invoke,repository) for repository in repositories]
            outcomes = sorted(future.result(timeout=60) for future in futures)
        self.assertEqual(len(pids),2)
        self.assertEqual(outcomes,['INTAKE_LABEL_POLICY_STORE_CHANGED','committed'])

    def test_two_real_connections_serialize_absent_initialization(self):
        self.race(store.prepare_initialize(self.capture()))
        self.assertEqual(self.counts(),(1,0))
        self.assertEqual(self.capture().head.row.policy_revision,1)

    def test_two_real_connections_append_one_version_and_one_head_advance(self):
        self.initialize(); self.race(self.append_prepared())
        self.assertEqual(self.counts(),(1,1))
        self.assertEqual(self.capture().head.row.policy_revision,2)

    def test_foreign_tenant_staff_fk_and_mismatched_prepared_tenant_are_denied(self):
        self.initialize()
        other,foreign = str(uuid.uuid4()),str(uuid.uuid4())
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES(%s,%s,'Fictional other store')",(other,'policy-'+other))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES(%s,%s,%s,'fictional','owner')",
                       (foreign,other,foreign+'@example.invalid'))
        with self.assertRaises(store.PolicyPersistenceError) as error:
            self.repository.apply_prepared(self.append_prepared(evidence=self.evidence(staff=foreign)))
        self.assertEqual(error.exception.commit_state,'not_committed')
        expected = self.capture()
        with self.assertRaises(store.PolicyPersistenceError):
            store.prepare_append(expected,self.evidence(tenant=other),replace(expected.head.row,
                policy_revision=2,policy_state='disabled',current_version_revision=1,current_policy_hash='a'*64))
        self.assertEqual(self.counts(),(1,0))
        self.assertEqual(self.capture().head.row.policy_revision,1)

    def test_wrong_evidence_hash_and_bool_metadata_fail_before_any_write(self):
        self.initialize()
        wrong = replace(self.evidence(),policy_hash='f'*64)
        with self.assertRaises(codec.LabelPolicyEvidenceError):self.append_prepared(evidence=wrong)
        with self.assertRaises(codec.LabelPolicyEvidenceError):replace(self.evidence(),accepted_enabled=1)
        with self.assertRaises(records.LabelPolicyRecordsError):replace(self.capture().head.row,policy_revision=True)
        self.assertEqual(self.counts(),(1,0))

    def test_clock_floor_regression_or_reset_cannot_erase_reviewed_head(self):
        self.initialize()
        expected = self.capture()
        self.repository.apply_prepared(store.prepare_head_cas(expected,replace(expected.head.row,
            policy_revision=2,policy_state='disabled',clock_floor_at=AT,clock_revision=1,clock_state='healthy')))
        expected = self.capture()
        for changes in (dict(clock_floor_at=EARLIER,clock_revision=2,clock_state='healthy'),
                        dict(clock_floor_at=None,clock_revision=0,clock_state='uninitialized')):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(store.PolicyPersistenceError):
                    store.prepare_head_cas(expected,replace(expected.head.row,policy_revision=3,**changes))
        self.assertEqual(self.capture().head.row.clock_floor_at,AT)

    def test_native_trigger_disable_is_catalog_drift_before_mutation(self):
        expected = self.capture()
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_store_label_policy_heads DISABLE TRIGGER ss_label_policy_heads_guard_v1')
        try:
            with self.assertRaises(store.PolicyPersistenceError) as error:
                self.repository.apply_prepared(store.prepare_initialize(expected))
            self.assertEqual(error.exception.code,'INTAKE_LABEL_POLICY_STORE_CATALOG')
            self.assertEqual(self.counts(),(0,0))
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_store_label_policy_heads ENABLE TRIGGER ss_label_policy_heads_guard_v1')

    def test_native_security_definer_function_drift_is_rejected(self):
        with self.database.transaction() as db:
            db.execute('ALTER FUNCTION ss_label_policy_heads_guard_v1() SECURITY DEFINER')
        try:
            with self.database.transaction() as db:
                with self.assertRaises(store.PolicyPersistenceError):store.inspect_schema(db,schema=self.schema)
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER FUNCTION ss_label_policy_heads_guard_v1() SECURITY INVOKER')

    def test_atomic_append_then_head_cas_rolls_back_and_exact_prepared_retry_works(self):
        self.initialize(); prepared = self.append_prepared(); owner = self
        class RollbackDatabase:
            @contextmanager
            def transaction(self):
                with owner.database.transaction() as db:
                    yield db
                    db.execute('SELECT 1/0')
        with self.assertRaises(store.PolicyPersistenceError):
            store.PolicyRepository(RollbackDatabase(),schema=self.schema,enabled=True).apply_prepared(prepared)
        self.assertEqual(self.counts(),(1,0))
        self.assertEqual(self.capture().head.row.policy_revision,1)
        self.repository.apply_prepared(prepared)
        self.assertEqual(self.counts(),(1,1))
        self.assertEqual(self.capture().head.row.policy_revision,2)

    def test_public_acl_drift_is_rejected_without_granting_product_authority(self):
        with self.database.transaction() as db:
            db.execute('GRANT INSERT ON ss_store_label_policy_heads TO PUBLIC')
        try:
            with self.database.transaction() as db:
                with self.assertRaises(store.PolicyPersistenceError):store.inspect_schema(db,schema=self.schema)
            self.assertEqual(self.counts(),(0,0))
        finally:
            with self.database.transaction() as db:
                db.execute('REVOKE ALL ON ss_store_label_policy_heads FROM PUBLIC')

    def test_existing_v9_guards_remain_exact_and_schema_injection_is_denied(self):
        with self.database.transaction() as db:
            actual = schema_v9.inspect(db)
        self.assertEqual((actual.version,actual.state,actual.table_count),(9,'present',5))
        with self.assertRaises(store.PolicyPersistenceError):
            store.PolicyRepository(object(),schema='public;DROP SCHEMA public',enabled=True)
        self.assertEqual(self.counts(),(0,0))


    def pg18_native_witness(self, phase):
        import json
        with self.database.transaction() as db:
            version = db.execute("SELECT pg_catalog.current_setting('server_version_num')::integer AS version").fetchone()['version']
            rows = db.execute('''SELECT c.relname,k.conname,k.contype,k.conkey,k.conenforced,k.conperiod,k.convalidated,k.connoinherit
              FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid
              JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname=%s AND c.relname=ANY(%s) ORDER BY c.relname,k.contype,k.conname LIMIT 28''',
              (self.schema,['ss_store_label_policy_heads','ss_store_label_policy_versions'])).fetchall()
        self.assertIs(type(version),int)
        self.assertEqual(version//10000,18)
        categories = {kind:sum(row['contype']==kind for row in rows) for kind in sorted({row['contype'] for row in rows})}
        print('POLICY_PG18_NATIVE_WITNESS='+json.dumps(dict(phase=phase,server_version_num=version,
            categories=categories,constraints=rows),sort_keys=True,allow_nan=False))
        return rows

    def test_pg18_native_not_null_inventory_is_exact_before_initialization(self):
        rows = self.pg18_native_witness('initial-native')
        self.assertEqual({kind:sum(row['contype']==kind for row in rows) for kind in ('n','p','u','f')},
                         {'n':18,'p':2,'u':2,'f':4})
        heads = {'tenant_id':1,'policy_revision':2,'policy_state':3,'clock_revision':7,'clock_state':8}
        versions = ('tenant_id','revision','policy_hash','envelope_version','document_format','document_body',
                    'document_hash','notice_body','notice_hash','accepted_enabled','accepted_by','accepted_at',
                    'evidence_use_expires_at')
        expected = {'ss_store_label_policy_heads_'+name+'_not_null':[number] for name,number in heads.items()}
        expected.update({'ss_store_label_policy_versions_'+name+'_not_null':[number]
                         for number,name in enumerate(versions,1)})
        self.assertEqual({r['conname']:r['conkey'] for r in rows if r['contype']=='n'},expected)
        self.assertTrue(all(row['conenforced'] is True and row['conperiod'] is False and row['convalidated'] is True
                            and row['connoinherit'] is (row['contype']!='n') for row in rows))
        self.initialize()
        self.assertEqual(self.counts(),(1,0))
        self.assertIsNone(self.capture().head.row.current_policy_hash)

    def test_pg18_renamed_native_not_null_is_rejected_without_ignoring_rows(self):
        self.pg18_native_witness('before-rename')
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_store_label_policy_heads RENAME CONSTRAINT ss_store_label_policy_heads_policy_state_not_null TO fixture_renamed_native_nn')
        try:
            rows = self.pg18_native_witness('after-rename')
            self.assertEqual([(r['contype'],r['conkey']) for r in rows if r['conname']=='fixture_renamed_native_nn'],[('n',[3])])
            with self.database.transaction() as db:
                with self.assertRaises(store.PolicyPersistenceError):store.inspect_schema(db,schema=self.schema)
            self.assertEqual(self.counts(),(0,0))
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_store_label_policy_heads RENAME CONSTRAINT fixture_renamed_native_nn TO ss_store_label_policy_heads_policy_state_not_null')

    def test_pg18_nullable_then_unvalidated_native_not_null_both_deny_persistence(self):
        self.pg18_native_witness('before-nullability')
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_store_label_policy_heads ALTER COLUMN policy_state DROP NOT NULL')
        try:
            rows = self.pg18_native_witness('nullable')
            self.assertFalse(any(r['relname']=='ss_store_label_policy_heads' and r['contype']=='n' and r['conkey']==[3] for r in rows))
            with self.database.transaction() as db:
                with self.assertRaises(store.PolicyPersistenceError):store.inspect_schema(db,schema=self.schema)
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_store_label_policy_heads ADD CONSTRAINT ss_store_label_policy_heads_policy_state_not_null NOT NULL policy_state NOT VALID')
            rows = self.pg18_native_witness('not-valid')
            self.assertEqual([(r['conkey'],r['convalidated']) for r in rows if r['conname']=='ss_store_label_policy_heads_policy_state_not_null'],[([3],False)])
            with self.database.transaction() as db:
                with self.assertRaises(store.PolicyPersistenceError):store.inspect_schema(db,schema=self.schema)
            self.assertEqual(self.counts(),(0,0))
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_store_label_policy_heads ALTER COLUMN policy_state SET NOT NULL')

    def test_pg18_same_native_foreign_key_not_enforced_is_rejected(self):
        before = self.pg18_native_witness('before-fk-enforcement')
        self.assertEqual([(r['contype'],r['conenforced']) for r in before if r['conname']=='ss_label_policy_heads_tenant_fk_v1'],[('f',True)])
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_store_label_policy_heads ALTER CONSTRAINT ss_label_policy_heads_tenant_fk_v1 NOT ENFORCED')
        try:
            rows = self.pg18_native_witness('not-enforced-fk')
            self.assertEqual([(r['contype'],r['conenforced']) for r in rows if r['conname']=='ss_label_policy_heads_tenant_fk_v1'],[('f',False)])
            with self.database.transaction() as db:
                with self.assertRaises(store.PolicyPersistenceError):store.inspect_schema(db,schema=self.schema)
            self.assertEqual(self.counts(),(0,0))
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_store_label_policy_heads ALTER CONSTRAINT ss_label_policy_heads_tenant_fk_v1 ENFORCED')
                db.execute('ALTER TABLE ss_store_label_policy_heads VALIDATE CONSTRAINT ss_label_policy_heads_tenant_fk_v1')

    def test_pg18_native_not_null_no_inherit_drift_is_rejected(self):
        self.pg18_native_witness('before-no-inherit')
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_store_label_policy_heads ALTER CONSTRAINT ss_store_label_policy_heads_policy_state_not_null NO INHERIT')
        try:
            rows = self.pg18_native_witness('no-inherit')
            self.assertEqual([(r['contype'],r['connoinherit']) for r in rows if r['conname']=='ss_store_label_policy_heads_policy_state_not_null'],[('n',True)])
            with self.database.transaction() as db:
                with self.assertRaises(store.PolicyPersistenceError):store.inspect_schema(db,schema=self.schema)
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_store_label_policy_heads ALTER CONSTRAINT ss_store_label_policy_heads_policy_state_not_null INHERIT')

    def test_pg18_unsupported_unenforced_check_is_not_filtered_out(self):
        self.pg18_native_witness('before-extra-check')
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_store_label_policy_heads ADD CONSTRAINT fixture_extra_unenforced CHECK (policy_revision>0) NOT ENFORCED')
        try:
            rows = self.pg18_native_witness('extra-unenforced-check')
            self.assertEqual([(r['contype'],r['conenforced']) for r in rows if r['conname']=='fixture_extra_unenforced'],[('c',False)])
            with self.database.transaction() as db:
                with self.assertRaises(store.PolicyPersistenceError):store.inspect_schema(db,schema=self.schema)
            self.assertEqual(self.counts(),(0,0))
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_store_label_policy_heads DROP CONSTRAINT fixture_extra_unenforced')


if __name__=='__main__':
    unittest.main(verbosity=2)
