"""UNRUN source: real isolated PostgreSQL artifact component acceptance.

Installs actual v9 label schema and the additive candidate only in the existing
ss_test_UUID fixture. No v9 trigger is disabled/replaced. The two policy tables
below are explicitly fictional metadata fixtures because a production policy
migration does not exist yet. They are not legal consent or staff authorization.
Opaque PDF/ZPL bytes test storage, not renderer correctness or cross-PC delivery.
No runtime is authorized by this file; no DSN means UNRUN, never a passing skip.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta, timezone
import hashlib
import threading
import unittest
import uuid

import psycopg
import intake_label_contract as contract
from scanner_adapters import normalize_manual_card
from hosted import intake_label_context as contexts
from hosted import intake_label_inputs as inputs
from hosted import intake_label_policy_records as policies
from hosted import intake_label_registry as registry
from hosted import intake_label_schema as schema_v9
from hosted import label_postgres_store as repository
from hosted.label_object_store import (ArtifactRef, LabelStorageError, LabelStorageValidationError)
import test_hosted_inventory_label_workflow_postgres as workflow_fixture
import test_hosted_receiving_postgres as receiving_fixture
import test_hosted_settlement_postgres as settlement_fixture


POLICY_FIXTURE_SQL = '''
CREATE TABLE {s}.ss_store_label_policy_heads (
  tenant_id uuid PRIMARY KEY REFERENCES {s}.ss_tenants(id), policy_revision bigint NOT NULL,
  policy_state text NOT NULL,current_version_revision bigint,current_policy_hash text,
  clock_floor_at timestamptz,clock_revision bigint NOT NULL,clock_state text NOT NULL);
CREATE TABLE {s}.ss_store_label_policy_versions (
  tenant_id uuid NOT NULL REFERENCES {s}.ss_tenants(id),revision bigint NOT NULL,policy_hash text NOT NULL,
  envelope_version integer NOT NULL,document_format text NOT NULL,document_body text NOT NULL,
  document_hash text NOT NULL,notice_body text NOT NULL,notice_hash text NOT NULL,
  accepted_enabled boolean NOT NULL,accepted_by uuid NOT NULL,accepted_at timestamptz NOT NULL,
  evidence_use_expires_at timestamptz NOT NULL,PRIMARY KEY(tenant_id,revision),UNIQUE(tenant_id,policy_hash));
'''


def stamp(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


class PostgreSQLArtifactRepositoryAcceptance(settlement_fixture._PostgreSQLSettlementFixture):
    setUp_receiving = receiving_fixture.PostgreSQLReceivingAcceptance.setUp
    approve = receiving_fixture.PostgreSQLReceivingAcceptance.approve
    wallet = receiving_fixture.PostgreSQLReceivingAcceptance.wallet
    http = workflow_fixture.PostgreSQLInventoryLabelWorkflowAcceptance.http
    stock_step = workflow_fixture.PostgreSQLInventoryLabelWorkflowAcceptance.stock_step

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The inherited fixture creates its own UUID schema and TLS connections.
        with cls.database.transaction() as db:
            state = schema_v9.install(db)
            if state.version != 9 or state.state != 'present':
                raise AssertionError('Actual v9 schema installation required')
            db.execute(POLICY_FIXTURE_SQL.format(s='"' + cls.schema + '"'))
            db.execute(repository.migration_proposal(cls.schema))

    def setUp(self):
        self.setUp_receiving()
        card = normalize_manual_card({'card_name': 'Fictional repository card', 'game': 'Sports',
            'condition': 'Near Mint', 'printing': 'Normal', 'qty': 2, 'final_price': '10.00'})
        workflow_fixture.PostgreSQLInventoryLabelWorkflowAcceptance.workflow(self, card, 'manual')
        self.policy_hash = 'a' * 64
        self.input_id = str(uuid.uuid4())
        with self.database.transaction() as db:
            rows = db.execute("SELECT id::text AS id FROM ss_receiving_operations WHERE tenant_id=%s AND state='received'",
                              (self.tenant,)).fetchall()
            self.assertEqual(len(rows), 1)
            bundle = self.service.capture_restoration_bundle(db, self.actor, rows[0]['id'])
            self.value = contract.confirmed_input(bundle, service_origin=contract.SERVICE_ORIGIN,
                                                  tenant_id=str(self.tenant))
            self.context = contexts.capture(db, self.service, self.actor, {
                'receivingId': rows[0]['id'], 'serverRevision': self.value['serverRevision'],
                'planHash': self.value['planHash'], 'expectedTerminalBasisHash': self.value['terminalBasisHash']})
            self.now = db.execute('SELECT pg_catalog.clock_timestamp() AS now').fetchone()['now']
            self.until = self.now + timedelta(days=1)
            prepared = inputs.prepare_input(self.context, None, input_id=self.input_id,
                observed_at=stamp(self.now), retention_until=stamp(self.until), policy_hash=self.policy_hash)
            self.assertIsNotNone(prepared.insert_values)
            self.insert(db, 'ss_intake_label_inputs', inputs.FIELDS, prepared.insert_values)
            document = '{"fixture":"opaque repository policy metadata only"}'
            notice = 'Fictional metadata, not a production consent record.'
            db.execute('''INSERT INTO ss_store_label_policy_versions VALUES
              (%s,1,%s,1,'ss_label_policy_document_utf8_v1',%s,%s,%s,%s,TRUE,%s,%s,%s)''',
              (self.tenant,self.policy_hash,document,hashlib.sha256(document.encode()).hexdigest(),
               notice,hashlib.sha256(notice.encode()).hexdigest(),self.actor['staff_id'],self.now,self.until))
            db.execute('''INSERT INTO ss_store_label_policy_heads VALUES
              (%s,1,'enabled',1,%s,%s,1,'healthy')''',
              (self.tenant,self.policy_hash,self.now-timedelta(seconds=1)))
        self.pdf = b'%PDF-1.7\nFictional opaque storage fixture\n%%EOF\n'
        self.zpl = b'^XA^FO1,1^FDfictional opaque storage fixture^FS^XZ\n'
        self.publication, self.generation, self.job = self.new_publication(self.pdf, self.zpl)
        self.provider_before = (list(self.provider.calls), list(self.provider.catalog_calls), self.wallet())

    def insert(self, db, table, columns, values):
        self.assertRegex(table, r'^ss_[a-z_]+$')
        db.execute('INSERT INTO "' + self.schema + '"."' + table + '"('
            + ','.join(columns) + ') VALUES(' + ','.join(['%s'] * len(values)) + ')', values)

    def new_publication(self, pdf, zpl):
        job, publication, generation, preview = (str(uuid.uuid4()) for _ in range(4))
        key, build = 'artifact_' + uuid.uuid4().hex, hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        plan = contract.build_plan(self.value)
        part = contract.part(plan, 0)
        basis = contract.job_basis_hash(self.value, plan, build)
        tenant, receiving, now, until = str(self.tenant), self.context.receiving_id, stamp(self.now), stamp(self.until)
        with self.database.transaction() as db:
            self.insert(db,'ss_intake_label_jobs',registry.ROW_FIELDS['job'], (
                tenant,job,receiving,self.input_id,self.context.input_hash,self.context.terminal_basis_hash,
                basis,build,'initial',None,plan['copyOffset'],plan['copyCount'],plan['partSize'],plan['partCount'],
                plan['renderPlanHash'],str(self.actor['staff_id']),now,self.policy_hash,until))
            self.insert(db,'ss_intake_label_publications',registry.ROW_FIELDS['publication'], (
                tenant,publication,job,0,part['partHash'],part['copyOffset'],part['copyCount'],generation,
                'planned',1,0,None,None,None,None,None,None,None,None,None,None,None,None,until,None,self.policy_hash,None))
            expires = self.now + timedelta(minutes=4)
            self.insert(db,'ss_intake_label_previews',registry.ROW_FIELDS['preview'], (
                tenant,preview,job,publication,str(self.actor['staff_id']),self.actor['session_hash'],'owner',key,
                'opaque fixture selector','b'*64,self.context.input_hash,part['partHash'],build,
                'queued',1,0,None,None,None,None,None,now,None,stamp(expires),None,1))
            token = str(uuid.uuid4())
            db.execute('''UPDATE ss_intake_label_previews SET state='checking',revision=2,lease_generation=1,
              lease_token=%s,lease_until=pg_catalog.clock_timestamp()+interval '1 minute' WHERE tenant_id=%s AND id=%s''',
              (token,self.tenant,preview))
            db.execute('''UPDATE ss_intake_label_previews SET state='ready',revision=3,lease_token=NULL,lease_until=NULL,
              ready_at=pg_catalog.clock_timestamp(),render_basis_hash=%s,overflows_body='[]',clipping_required=FALSE
              WHERE tenant_id=%s AND id=%s''', ('c'*64,self.tenant,preview))
            self.insert(db,'ss_intake_label_requests',registry.ROW_FIELDS['request'], (
                tenant,key,'confirm_part','opaque fixture intent','d'*64,preview,job,publication,None,
                str(self.actor['staff_id']),self.actor['session_hash'],stamp(db.execute(
                    'SELECT pg_catalog.clock_timestamp() AS now').fetchone()['now']),1))
            db.execute('''UPDATE ss_intake_label_publications SET state='queued',revision=2,authorization_request_key=%s
              WHERE tenant_id=%s AND id=%s''', (key,self.tenant,publication))
            db.execute('''UPDATE ss_intake_label_publications SET state='rendering',revision=3,lease_generation=1,
              lease_token=%s,lease_until=pg_catalog.clock_timestamp()+interval '10 minutes'
              WHERE tenant_id=%s AND id=%s''', (str(uuid.uuid4()),self.tenant,publication))
            db.execute('''UPDATE ss_intake_label_publications SET state='bytes_prepared',revision=4,
              render_input_hash=%s,render_basis_hash=%s,output_metadata='{}',pair_sealed_at=pg_catalog.clock_timestamp(),
              pdf_sha256=%s,pdf_bytes=%s,zpl_sha256=%s,zpl_bytes=%s WHERE tenant_id=%s AND id=%s''',
              (self.context.input_hash,'c'*64,hashlib.sha256(pdf).hexdigest(),len(pdf),
               hashlib.sha256(zpl).hexdigest(),len(zpl),self.tenant,publication))
        return publication,generation,job

    def store(self, *, write=False, database=None, publication=None):
        publication = publication or self.publication
        with self.database.transaction() as db:
            row = db.execute('SELECT job_id::text AS job,generation_id::text AS generation FROM ss_intake_label_publications '
                             'WHERE tenant_id=%s AND id=%s',(self.tenant,publication)).fetchone()
            omitted = registry.LookupSlot('omitted',None)
            chosen = {name: omitted for name in registry.SLOT_FIELDS}
            for name,key in {
                'input_by_hash':(str(self.tenant),self.context.input_hash),
                'input_by_terminal':(str(self.tenant),self.context.terminal_basis_hash),
                'input_by_id':(str(self.tenant),self.input_id),'job_by_id':(str(self.tenant),row['job']),
                'publication_by_id':(str(self.tenant),publication),
                'publication_by_generation':(str(self.tenant),row['generation'])}.items():
                chosen[name] = registry.LookupSlot('selected',key)
            selector = registry.LabelLookupSelector(str(self.tenant),*(chosen[name] for name in registry.SLOT_FIELDS))
            captured = (registry.capture_for_reservation if write else registry.capture_for_read)(db,selector,schema=self.schema)
            policy_selector = policies.PolicyRecordSelector(str(self.tenant),self.policy_hash)
            policy = (policies.capture_policy_records_for_writer if write else policies.capture_policy_records_for_read)(
                db,policy_selector,schema=self.schema)
        binding = repository.RepositoryBinding(str(self.tenant),captured,policy)
        return repository.PostgresArtifactStore(database or self.database,binding,schema=self.schema,enabled=True)

    def ref(self, kind='pdf', *, publication=None, generation=None, content=None):
        content = (self.pdf if kind == 'pdf' else self.zpl) if content is None else content
        return ArtifactRef(1,str(self.tenant),publication or self.publication,generation or self.generation,
            kind,hashlib.sha256(content).hexdigest(),len(content),
            'application/pdf' if kind == 'pdf' else 'application/octet-stream')

    def count(self):
        with self.database.transaction() as db:
            return db.execute('SELECT count(*) AS n,COALESCE(sum(bytes),0) AS bytes FROM ss_label_artifact_blobs '
                              'WHERE tenant_id=%s',(self.tenant,)).fetchone()

    def test_disabled_backend_evaluates_no_ref_content_callback_or_database(self):
        class Forbidden:
            def transaction(self):
                raise AssertionError('Disabled storage must not evaluate database')
        store = repository.PostgresArtifactStore(Forbidden())
        for operation in (lambda:store.put_if_absent(object(),object()),lambda:store.stat(object()),
                          lambda:store.open_verified(object()),lambda:store.delete_tombstoned(object(),
                              authorize=lambda _:self.fail('Deletion callback must not execute'))):
            with self.assertRaises(LabelStorageError) as error:operation()
            self.assertEqual(error.exception.code,'LABEL_STORAGE_DISABLED')
        self.assertEqual(self.count()['n'],0)

    def test_real_pair_put_full_verified_read_and_same_ref_retry(self):
        writer = self.store(write=True)
        for kind,content in (('pdf',self.pdf),('zpl',self.zpl)):
            ref=self.ref(kind)
            self.assertEqual(writer.put_if_absent(ref,content).outcome,'created')
            self.assertEqual(writer.put_if_absent(ref,content).outcome,'precondition_failed')
            reader=self.store()
            self.assertEqual(reader.stat(ref).outcome,'present')
            self.assertEqual(reader.open_verified(ref).content,content)
        self.assertEqual(self.count(),{'n':2,'bytes':len(self.pdf)+len(self.zpl)})
        self.assertEqual((self.provider.calls,self.provider.catalog_calls,self.wallet()),self.provider_before)

    def test_absence_observation_is_not_successful_get_or_repair(self):
        store=self.store()
        self.assertEqual(store.stat(self.ref()).outcome,'not_found_observed')
        with self.assertRaises(LabelStorageError) as error:store.open_verified(self.ref())
        self.assertEqual(error.exception.code,'LABEL_STORAGE_NOT_FOUND')
        self.assertEqual(self.count()['n'],0)

    def test_matching_metadata_cannot_substitute_for_full_content_integrity(self):
        ref=self.ref(); corrupt=b'x'*len(self.pdf)
        # A deliberately corrupt direct test-schema insert: no claim that an
        # accepted SHA column proves bytes. Public delivery must use full open.
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_label_artifact_blobs
              (tenant_id,publication_id,generation_id,kind,sha256,bytes,content_type,content,retention_until)
              SELECT tenant_id,id,generation_id,'pdf',pdf_sha256,pdf_bytes,'application/pdf',%s,retention_until
              FROM ss_intake_label_publications WHERE tenant_id=%s AND id=%s''',
              (corrupt,self.tenant,self.publication))
        self.assertEqual(self.store().stat(ref).outcome,'present')
        with self.assertRaises(LabelStorageError) as error:self.store().open_verified(ref)
        self.assertEqual(error.exception.code,'LABEL_STORAGE_INTEGRITY_FAILED')
        with self.assertRaises(LabelStorageError) as error:self.store(write=True).put_if_absent(ref,self.pdf)
        self.assertEqual(error.exception.code,'LABEL_STORAGE_CONFLICT')
        with self.database.transaction() as db:
            self.assertEqual(bytes(db.execute('SELECT content FROM ss_label_artifact_blobs WHERE tenant_id=%s',
                                             (self.tenant,)).fetchone()['content']),corrupt)

    def test_one_stored_file_does_not_publish_a_pair_or_dispatch_stock(self):
        self.store(write=True).put_if_absent(self.ref(),self.pdf)
        self.assertEqual(self.store().stat(self.ref('zpl')).outcome,'not_found_observed')
        with self.database.transaction() as db:
            row=db.execute('SELECT state,published_at FROM ss_intake_label_publications WHERE tenant_id=%s AND id=%s',
                           (self.tenant,self.publication)).fetchone()
        self.assertEqual(row,{'state':'bytes_prepared','published_at':None})
        self.assertEqual((self.provider.calls,self.provider.catalog_calls,self.wallet()),self.provider_before)

    def test_foreign_tenant_generation_and_hash_refs_are_denied(self):
        store=self.store(write=True); ref=self.ref()
        for changes in ({'tenant_id':str(uuid.uuid4())},{'generation_id':str(uuid.uuid4())},
                        {'publication_id':str(uuid.uuid4())},{'sha256':'e'*64}):
            values={name:getattr(ref,name) for name in ref.__dataclass_fields__};values.update(changes)
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(LabelStorageError):store.put_if_absent(ArtifactRef(**values),self.pdf)
        self.assertEqual(self.count()['n'],0)

    def test_wrong_content_and_bool_version_reject_before_insert(self):
        store=self.store(write=True)
        with self.assertRaises(LabelStorageValidationError):store.put_if_absent(self.ref(),b'different bytes')
        values={name:getattr(self.ref(),name) for name in self.ref().__dataclass_fields__};values['version']=True
        with self.assertRaises(LabelStorageValidationError):ArtifactRef(**values)
        self.assertEqual(self.count()['n'],0)

    def test_two_real_connections_racing_same_ref_store_one_immutable_row(self):
        barrier=threading.Barrier(2,timeout=20);seen=set();gate=threading.Lock();local=threading.local();owner=self
        class RacingDatabase:
            @contextmanager
            def transaction(self):
                with owner.database.transaction() as db:
                    if not getattr(local,'entered',False):
                        local.entered=True
                        pid=db.execute('SELECT pg_catalog.pg_backend_pid() AS pid').fetchone()['pid']
                        with gate:seen.add(pid)
                        barrier.wait()
                    yield db
        stores=[self.store(write=True,database=RacingDatabase()) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(store.put_if_absent,self.ref(),self.pdf) for store in stores]
            outcomes=sorted(future.result(timeout=60).outcome for future in futures)
        self.assertEqual(outcomes,['created','precondition_failed'])
        self.assertEqual(len(seen),2)
        self.assertEqual(self.count()['n'],1)

    def test_actual_sql_rollback_preserves_absence_then_same_ref_retries(self):
        owner=self;local=threading.local()
        class RollbackDatabase:
            @contextmanager
            def transaction(self):
                with owner.database.transaction() as db:
                    yield db
                    if not getattr(local,'failed',False):
                        local.failed=True
                        db.execute('SELECT 1/0')
        with self.assertRaises(LabelStorageError):
            self.store(write=True,database=RollbackDatabase()).put_if_absent(self.ref(),self.pdf)
        self.assertEqual(self.count()['n'],0)
        self.assertEqual(self.store(write=True).put_if_absent(self.ref(),self.pdf).outcome,'created')

    def test_real_policy_change_between_byte_read_and_return_denies_result(self):
        self.store(write=True).put_if_absent(self.ref(),self.pdf)
        owner=self
        class PolicyChangeDatabase:
            calls=0
            @contextmanager
            def transaction(self):
                self.calls+=1
                if self.calls==2:
                    with owner.database.transaction() as changed:
                        changed.execute("UPDATE ss_store_label_policy_heads SET policy_revision=policy_revision+1,policy_state='disabled' WHERE tenant_id=%s",(owner.tenant,))
                with owner.database.transaction() as db:yield db
        reader=self.store(database=PolicyChangeDatabase())
        with self.assertRaises(LabelStorageError):reader.open_verified(self.ref())
        self.assertEqual(self.count()['n'],1)

    def test_real_publication_revision_drift_denies_storage_write(self):
        writer=self.store(write=True)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_intake_label_publications SET state='publishing',revision=revision+1 WHERE tenant_id=%s AND id=%s",
                       (self.tenant,self.publication))
        with self.assertRaises(LabelStorageError):writer.put_if_absent(self.ref(),self.pdf)
        self.assertEqual(self.count()['n'],0)

    def test_disabled_artifact_trigger_fails_native_readiness(self):
        store=self.store()
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_label_artifact_blobs DISABLE TRIGGER ss_label_artifact_blobs_guard_v1')
        try:
            with self.assertRaises(LabelStorageError) as error:store.stat(self.ref())
            self.assertEqual(error.exception.code,'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_label_artifact_blobs ENABLE TRIGGER ss_label_artifact_blobs_guard_v1')

    def test_existing_v9_and_new_artifact_immutability_guards_remain_enforced(self):
        writer=self.store(write=True)
        writer.put_if_absent(self.ref(),self.pdf);writer.put_if_absent(self.ref('zpl'),self.zpl)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_intake_label_publications SET state='publishing',revision=revision+1 WHERE tenant_id=%s AND id=%s",(self.tenant,self.publication))
            db.execute("UPDATE ss_intake_label_publications SET state='published',revision=revision+1,published_at=pg_catalog.clock_timestamp(),lease_token=NULL,lease_until=NULL WHERE tenant_id=%s AND id=%s",(self.tenant,self.publication))
        for query,params,sqlstate in (
            ("UPDATE ss_intake_label_publications SET revision=revision+1,tombstoned_at=pg_catalog.clock_timestamp() WHERE tenant_id=%s AND id=%s",(self.tenant,self.publication),'55000'),
            ("UPDATE ss_label_artifact_blobs SET content=content WHERE tenant_id=%s",(self.tenant,),'23514'),
            ("DELETE FROM ss_label_artifact_blobs WHERE tenant_id=%s",(self.tenant,),'23514')):
            with self.assertRaises(psycopg.Error) as error:
                with self.database.transaction() as db:db.execute(query,params)
            self.assertEqual(error.exception.sqlstate,sqlstate)
        self.assertEqual(self.count()['n'],2)

    def test_logical_tenant_byte_quota_is_atomic_without_lowering_production_caps(self):
        # Explicit future resource gate:32MiB Python value and256MiB logical DB
        # data, highly compressible on disk. No such allocation has run locally.
        payload=b'x'*(32*1024*1024)
        for _ in range(4):
            publication,generation,_=self.new_publication(payload,payload)
            writer=self.store(write=True,publication=publication)
            for kind in ('pdf','zpl'):
                writer.put_if_absent(self.ref(kind,publication=publication,generation=generation,content=payload),payload)
        self.assertEqual(self.count(),{'n':8,'bytes':256*1024*1024})
        with self.assertRaises(LabelStorageError) as error:
            self.store(write=True).put_if_absent(self.ref(),self.pdf)
        self.assertEqual(error.exception.code,'LABEL_STORAGE_POLICY_BLOCKED')
        self.assertEqual(self.count(),{'n':8,'bytes':256*1024*1024})

    def test_deletion_remains_blocked_and_does_not_evaluate_callback(self):
        self.store(write=True).put_if_absent(self.ref(),self.pdf)
        with self.assertRaises(LabelStorageError) as error:
            self.store().delete_tombstoned(self.ref(),authorize=lambda _:self.fail('No lifecycle authority exists'))
        self.assertEqual(error.exception.code,'LABEL_STORAGE_POLICY_BLOCKED')
        self.assertEqual(self.store().open_verified(self.ref()).content,self.pdf)

    def test_schema_identifier_injection_is_rejected_without_database_work(self):
        binding=self.store()._binding
        with self.assertRaises(LabelStorageError):
            repository.PostgresArtifactStore(object(),binding,schema='public;DROP SCHEMA public',enabled=True)
        self.assertEqual(self.count()['n'],0)

    def assert_exact_stored_pdf(self):
        with self.database.transaction() as db:
            rows = db.execute('''SELECT publication_id::text AS publication,
              generation_id::text AS generation,kind,sha256,bytes,content_type,content
              FROM ss_label_artifact_blobs WHERE tenant_id=%s''', (self.tenant,)).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(bytes(row.pop('content')), self.pdf)
        self.assertEqual(row, {'publication': self.publication, 'generation': self.generation,
            'kind': 'pdf', 'sha256': hashlib.sha256(self.pdf).hexdigest(), 'bytes': len(self.pdf),
            'content_type': 'application/pdf'})
        self.assertEqual((self.provider.calls, self.provider.catalog_calls, self.wallet()),
                         self.provider_before)

    def test_put_final_catalog_drift_preserves_uncertain_existing_row_then_exact_retry(self):
        owner = self
        class CatalogChangeDatabase:
            calls = 0
            @contextmanager
            def transaction(self):
                self.calls += 1
                if self.calls == 2:
                    # Actual own-schema catalog change AFTER the first PUT
                    # commits; never disable or replace any existing v9 guard.
                    with owner.database.transaction() as changed:
                        changed.execute('ALTER TABLE ss_label_artifact_blobs DISABLE TRIGGER ss_label_artifact_blobs_guard_v1')
                with owner.database.transaction() as db:
                    yield db
        writer = self.store(write=True, database=CatalogChangeDatabase())
        try:
            with self.assertRaises(LabelStorageError) as error:
                writer.put_if_absent(self.ref(), self.pdf)
            self.assertEqual(error.exception.code, 'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')
            self.assertEqual(error.exception.commit_state, 'may_have_committed')
            self.assertIs(error.exception.retryable, False)
            self.assert_exact_stored_pdf()
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_label_artifact_blobs ENABLE TRIGGER ss_label_artifact_blobs_guard_v1')
        self.assertEqual(self.store(write=True).put_if_absent(self.ref(), self.pdf).outcome,
                         'precondition_failed')
        self.assert_exact_stored_pdf()

    def test_put_final_policy_drift_preserves_uncertain_existing_row(self):
        owner = self
        class PolicyChangeDatabase:
            calls = 0
            @contextmanager
            def transaction(self):
                self.calls += 1
                if self.calls == 2:
                    with owner.database.transaction() as changed:
                        changed.execute("UPDATE ss_store_label_policy_heads SET policy_revision=policy_revision+1,policy_state='disabled' WHERE tenant_id=%s", (owner.tenant,))
                with owner.database.transaction() as db:
                    yield db
        with self.assertRaises(LabelStorageError) as error:
            self.store(write=True, database=PolicyChangeDatabase()).put_if_absent(self.ref(), self.pdf)
        self.assertEqual(error.exception.commit_state, 'may_have_committed')
        self.assertIs(error.exception.retryable, False)
        self.assert_exact_stored_pdf()

    def test_put_second_transaction_native_unique_violation_preserves_existing_row(self):
        owner = self
        class DuplicateDatabase:
            calls = 0
            @contextmanager
            def transaction(self):
                self.calls += 1
                with owner.database.transaction() as db:
                    if self.calls == 2:
                        # Native existing PK violation after the first commit.
                        # The exact-row trigger accepts the immutable duplicate;
                        # the PostgreSQL unique constraint must reject it.
                        try:
                            db.execute('''INSERT INTO ss_label_artifact_blobs
                              SELECT * FROM ss_label_artifact_blobs WHERE tenant_id=%s''', (owner.tenant,))
                        except psycopg.Error as error:
                            owner.assertEqual(error.sqlstate, '23505')
                            raise
                        owner.fail('Actual second-transaction unique violation required')
                    yield db
        with self.assertRaises(LabelStorageError) as error:
            self.store(write=True, database=DuplicateDatabase()).put_if_absent(self.ref(), self.pdf)
        self.assertEqual(error.exception.code, 'LABEL_STORAGE_CONFLICT')
        self.assertEqual(error.exception.commit_state, 'may_have_committed')
        self.assertIs(error.exception.retryable, False)
        self.assert_exact_stored_pdf()

    def test_put_second_transaction_native_check_violation_preserves_existing_row(self):
        owner = self
        class ImmutableCheckDatabase:
            calls = 0
            @contextmanager
            def transaction(self):
                self.calls += 1
                with owner.database.transaction() as db:
                    if self.calls == 2:
                        # The real additive immutable-row trigger rejects even
                        # a no-op UPDATE and the second transaction rolls back.
                        try:
                            db.execute('UPDATE ss_label_artifact_blobs SET sha256=sha256 WHERE tenant_id=%s', (owner.tenant,))
                        except psycopg.Error as error:
                            owner.assertEqual(error.sqlstate, '23514')
                            raise
                        owner.fail('Actual second-transaction check violation required')
                    yield db
        with self.assertRaises(LabelStorageError) as error:
            self.store(write=True, database=ImmutableCheckDatabase()).put_if_absent(self.ref(), self.pdf)
        self.assertEqual(error.exception.code, 'LABEL_STORAGE_POLICY_BLOCKED')
        self.assertEqual(error.exception.commit_state, 'may_have_committed')
        self.assertIs(error.exception.retryable, False)
        self.assert_exact_stored_pdf()

    def test_put_first_transaction_native_check_rollback_keeps_absence(self):
        owner = self
        class FirstRollbackDatabase:
            @contextmanager
            def transaction(self):
                with owner.database.transaction() as db:
                    yield db
                    # Still inside the FIRST transaction: the inserted row must
                    # be rolled back when its real immutable trigger rejects.
                    try:
                        db.execute('UPDATE ss_label_artifact_blobs SET sha256=sha256 WHERE tenant_id=%s', (owner.tenant,))
                    except psycopg.Error as error:
                        owner.assertEqual(error.sqlstate, '23514')
                        raise
                    owner.fail('Actual first-transaction check violation required')
        with self.assertRaises(LabelStorageError) as error:
            self.store(write=True, database=FirstRollbackDatabase()).put_if_absent(self.ref(), self.pdf)
        self.assertEqual(error.exception.code, 'LABEL_STORAGE_POLICY_BLOCKED')
        self.assertEqual(error.exception.commit_state, 'not_committed')
        self.assertEqual(self.count()['n'], 0)
        self.assertEqual(self.store(write=True).put_if_absent(self.ref(), self.pdf).outcome, 'created')
        self.assert_exact_stored_pdf()


    def pg18_native_witness(self, phase):
        import json
        with self.database.transaction() as db:
            version = db.execute("SELECT pg_catalog.current_setting('server_version_num')::integer AS version").fetchone()['version']
            rows = db.execute('''SELECT conname,contype,conkey,conenforced,conperiod,convalidated,connoinherit
              FROM pg_catalog.pg_constraint WHERE conrelid='ss_label_artifact_blobs'::pg_catalog.regclass
              ORDER BY contype,conname LIMIT 14''').fetchall()
        self.assertIs(type(version),int)
        self.assertEqual(version//10000,18)
        categories = {kind:sum(row['contype']==kind for row in rows) for kind in sorted({row['contype'] for row in rows})}
        print('ARTIFACT_PG18_NATIVE_WITNESS='+json.dumps(dict(phase=phase,server_version_num=version,
            categories=categories,constraints=rows),sort_keys=True,allow_nan=False))
        return rows

    def test_pg18_native_not_null_inventory_is_exact_before_real_storage(self):
        rows = self.pg18_native_witness('initial-native')
        self.assertEqual({kind:sum(row['contype']==kind for row in rows) for kind in ('n','p','f')},
                         {'n':10,'p':1,'f':1})
        names = ('tenant_id','publication_id','generation_id','kind','sha256','bytes','content_type',
                 'content','created_at','retention_until')
        actual = {row['conname']:row['conkey'] for row in rows if row['contype']=='n'}
        self.assertEqual(actual,{'ss_label_artifact_blobs_'+name+'_not_null':[i] for i,name in enumerate(names,1)})
        self.assertTrue(all(row['conenforced'] is True and row['conperiod'] is False and row['convalidated'] is True
                            and row['connoinherit'] is (row['contype']!='n') for row in rows))
        self.assertEqual(self.store(write=True).put_if_absent(self.ref(),self.pdf).outcome,'created')
        self.assert_exact_stored_pdf()

    def test_pg18_renamed_native_not_null_is_rejected_without_ignoring_rows(self):
        self.pg18_native_witness('before-rename')
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_label_artifact_blobs RENAME CONSTRAINT ss_label_artifact_blobs_content_type_not_null TO fixture_renamed_native_nn')
        try:
            rows = self.pg18_native_witness('after-rename')
            self.assertEqual([(r['contype'],r['conkey']) for r in rows if r['conname']=='fixture_renamed_native_nn'],[('n',[7])])
            with self.assertRaises(LabelStorageError) as error:self.store().stat(self.ref())
            self.assertEqual(error.exception.code,'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')
            self.assertEqual(self.count()['n'],0)
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_label_artifact_blobs RENAME CONSTRAINT fixture_renamed_native_nn TO ss_label_artifact_blobs_content_type_not_null')

    def test_pg18_nullable_then_unvalidated_native_not_null_both_deny_storage(self):
        self.pg18_native_witness('before-nullability')
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_label_artifact_blobs ALTER COLUMN content_type DROP NOT NULL')
        try:
            rows = self.pg18_native_witness('nullable')
            self.assertFalse(any(r['contype']=='n' and r['conkey']==[7] for r in rows))
            with self.assertRaises(LabelStorageError):self.store().stat(self.ref())
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_label_artifact_blobs ADD CONSTRAINT ss_label_artifact_blobs_content_type_not_null NOT NULL content_type NOT VALID')
            rows = self.pg18_native_witness('not-valid')
            self.assertEqual([(r['conkey'],r['convalidated']) for r in rows if r['conname']=='ss_label_artifact_blobs_content_type_not_null'],[([7],False)])
            with self.assertRaises(LabelStorageError):self.store().stat(self.ref())
            self.assertEqual(self.count()['n'],0)
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_label_artifact_blobs ALTER COLUMN content_type SET NOT NULL')

    def test_pg18_same_native_foreign_key_not_enforced_is_rejected(self):
        before = self.pg18_native_witness('before-fk-enforcement')
        self.assertEqual([(r['contype'],r['conenforced']) for r in before if r['conname']=='ss_label_artifact_blobs_publication_fk_v1'],[('f',True)])
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_label_artifact_blobs ALTER CONSTRAINT ss_label_artifact_blobs_publication_fk_v1 NOT ENFORCED')
        try:
            rows = self.pg18_native_witness('not-enforced-fk')
            self.assertEqual([(r['contype'],r['conenforced']) for r in rows if r['conname']=='ss_label_artifact_blobs_publication_fk_v1'],[('f',False)])
            with self.assertRaises(LabelStorageError):self.store().stat(self.ref())
            self.assertEqual(self.count()['n'],0)
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_label_artifact_blobs ALTER CONSTRAINT ss_label_artifact_blobs_publication_fk_v1 ENFORCED')
                db.execute('ALTER TABLE ss_label_artifact_blobs VALIDATE CONSTRAINT ss_label_artifact_blobs_publication_fk_v1')

    def test_pg18_native_not_null_no_inherit_drift_is_rejected(self):
        self.pg18_native_witness('before-no-inherit')
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_label_artifact_blobs ALTER CONSTRAINT ss_label_artifact_blobs_content_type_not_null NO INHERIT')
        try:
            rows = self.pg18_native_witness('no-inherit')
            self.assertEqual([(r['contype'],r['connoinherit']) for r in rows if r['conname']=='ss_label_artifact_blobs_content_type_not_null'],[('n',True)])
            with self.assertRaises(LabelStorageError):self.store().stat(self.ref())
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_label_artifact_blobs ALTER CONSTRAINT ss_label_artifact_blobs_content_type_not_null INHERIT')

    def test_pg18_unsupported_unenforced_check_is_not_filtered_out(self):
        self.pg18_native_witness('before-extra-check')
        with self.database.transaction() as db:
            db.execute('ALTER TABLE ss_label_artifact_blobs ADD CONSTRAINT fixture_extra_unenforced CHECK (bytes>0) NOT ENFORCED')
        try:
            rows = self.pg18_native_witness('extra-unenforced-check')
            self.assertEqual([(r['contype'],r['conenforced']) for r in rows if r['conname']=='fixture_extra_unenforced'],[('c',False)])
            with self.assertRaises(LabelStorageError):self.store().stat(self.ref())
            self.assertEqual(self.count()['n'],0)
        finally:
            with self.database.transaction() as db:
                db.execute('ALTER TABLE ss_label_artifact_blobs DROP CONSTRAINT fixture_extra_unenforced')


if __name__=='__main__':
    unittest.main(verbosity=2)
