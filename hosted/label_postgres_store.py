"""SOURCE CANDIDATE / UNRUN: private immutable label bytes, default disabled.

No service/config/routes or migration are activated here. Typed snapshots are
comparison data, not staff authorization. A later coordinator must own live
actor/source/consent/download gates before and after these private operations.
Never call storage while retaining a financial/source transaction or OS guard.
"""
from dataclasses import dataclass
import hashlib
import re

from .label_object_store import (ArtifactRef, DeleteObservation, LabelStorageError,
    PutObservation, StatObservation, VerifiedArtifact, validate_ref)
from .intake_label_registry import (CapturedLabelRows, ExpectedLabelPublication,
    assert_current as assert_registry_current)
from .intake_label_policy_records import (CapturedPolicyRecords,
    assert_policy_records_current_for_read, assert_policy_records_current_for_writer)
from .intake_label_schema import inspect as inspect_label_schema

ARTIFACT_LIMIT = 32 * 1024 * 1024
TENANT_BYTES = 256 * 1024 * 1024
TOTAL_BYTES = 2 * 1024 * 1024 * 1024
TENANT_ROWS = 1024
TOTAL_ROWS = 8192
LOCK_DOMAIN = 1397967938
_SCHEMA = re.compile(r'(?:public|ss_test_[a-f0-9]{32})')


def _error(operation, code='LABEL_STORAGE_POLICY_BLOCKED', commit='not_started', retry=False):
    return LabelStorageError(code, operation, commit, retry)


def _schema(value):
    if type(value) is not str or not _SCHEMA.fullmatch(value):
        raise _error('stat', 'LABEL_STORAGE_CONFIGURATION_INVALID')
    return '"' + value + '"'


@dataclass(frozen=True, slots=True, repr=False)
class RepositoryBinding:
    tenant_id: str
    registry: CapturedLabelRows
    policy: CapturedPolicyRecords

    def __repr__(self):
        return '<RepositoryBinding>'


def _publication(binding, operation='stat'):
    if type(binding) is not RepositoryBinding or type(binding.registry) is not CapturedLabelRows \
            or type(binding.policy) is not CapturedPolicyRecords:
        raise _error(operation)
    observation = binding.registry.publication_by_id
    publication = observation.row
    if observation.state != 'present' or type(publication) is not ExpectedLabelPublication \
            or publication.tenant_id != binding.tenant_id \
            or binding.registry.lookup_selector.tenant_id != binding.tenant_id \
            or binding.policy.selector.tenant_id != binding.tenant_id:
        raise _error(operation)
    if publication.pair_sealed_at is None or publication.tombstoned_at is not None \
            or publication.state not in ('bytes_prepared', 'publishing', 'published'):
        raise _error(operation)
    head, current, original = (binding.policy.head, binding.policy.current_version,
                               binding.policy.original_version)
    if any(item.state != 'present' for item in (head, current, original)) \
            or head.row.policy_state != 'enabled' or head.row.clock_state != 'healthy' \
            or current.row.accepted_enabled is not True or original.row.accepted_enabled is not True \
            or publication.policy_hash != original.row.policy_hash:
        raise _error(operation)
    return publication


def guard_source(schema):
    """Exact additive trigger proposal; no v9 guard is changed or bypassed."""
    table = _schema(schema)
    return '''
DECLARE
  p record;
  existing record;
  n_all bigint;
  b_all bigint;
  n_tenant bigint;
  b_tenant bigint;
BEGIN
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='LABEL_ARTIFACT_IMMUTABLE';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(1397967938,pg_catalog.hashtext(TG_TABLE_SCHEMA));
  IF NEW.kind NOT IN ('pdf','zpl') OR NEW.bytes NOT BETWEEN 1 AND 33554432
     OR NEW.sha256 !~ '^[0-9a-f]{64}$' OR pg_catalog.octet_length(NEW.content) <> NEW.bytes
     OR NEW.content_type IS DISTINCT FROM CASE WHEN NEW.kind='pdf' THEN 'application/pdf' ELSE 'application/octet-stream' END
     THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='LABEL_ARTIFACT_INVALID'; END IF;
  SELECT generation_id,state,pair_sealed_at,pdf_sha256,pdf_bytes,zpl_sha256,zpl_bytes,retention_until,tombstoned_at
    INTO p FROM __SCHEMA__.ss_intake_label_publications
    WHERE tenant_id=NEW.tenant_id AND id=NEW.publication_id FOR KEY SHARE;
  IF NOT FOUND OR p.generation_id IS DISTINCT FROM NEW.generation_id OR p.pair_sealed_at IS NULL
     OR p.tombstoned_at IS NOT NULL OR p.retention_until <= pg_catalog.clock_timestamp()
     OR NEW.retention_until IS DISTINCT FROM p.retention_until
     OR NOT pg_catalog.isfinite(NEW.created_at)
     OR NEW.created_at > pg_catalog.clock_timestamp() OR NEW.created_at >= NEW.retention_until
     OR NEW.sha256 IS DISTINCT FROM CASE WHEN NEW.kind='pdf' THEN p.pdf_sha256 ELSE p.zpl_sha256 END
     OR NEW.bytes IS DISTINCT FROM CASE WHEN NEW.kind='pdf' THEN p.pdf_bytes ELSE p.zpl_bytes END
     THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='LABEL_ARTIFACT_BASIS_CHANGED'; END IF;
  SELECT * INTO existing FROM __SCHEMA__.ss_label_artifact_blobs
    WHERE tenant_id=NEW.tenant_id AND publication_id=NEW.publication_id
      AND generation_id=NEW.generation_id AND kind=NEW.kind;
  IF FOUND THEN
    IF ROW(existing.sha256,existing.bytes,existing.content_type,existing.content,existing.retention_until)
       IS DISTINCT FROM ROW(NEW.sha256,NEW.bytes,NEW.content_type,NEW.content,NEW.retention_until)
       THEN RAISE EXCEPTION USING ERRCODE='23505', MESSAGE='LABEL_ARTIFACT_CONFLICT'; END IF;
    RETURN NEW;
  END IF;
  IF p.state NOT IN ('bytes_prepared','publishing') THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='LABEL_ARTIFACT_NO_PUBLISHED_REPAIR';
  END IF;
  SELECT count(*),COALESCE(sum(bytes),0),count(*) FILTER(WHERE tenant_id=NEW.tenant_id),
    COALESCE(sum(bytes) FILTER(WHERE tenant_id=NEW.tenant_id),0)
    INTO n_all,b_all,n_tenant,b_tenant FROM __SCHEMA__.ss_label_artifact_blobs;
  IF n_all >= 8192 OR n_tenant >= 1024 OR b_all+NEW.bytes > 2147483648 OR b_tenant+NEW.bytes > 268435456
     THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='LABEL_ARTIFACT_QUOTA'; END IF;
  RETURN NEW;
END;
'''.replace('__SCHEMA__', table)


def migration_proposal(schema):
    """Pure SQL text only. Operator review/exact migration admission required."""
    table = _schema(schema)
    return '''CREATE TABLE __SCHEMA__.ss_label_artifact_blobs (
  tenant_id uuid NOT NULL, publication_id uuid NOT NULL, generation_id uuid NOT NULL,
  kind text NOT NULL, sha256 text NOT NULL, bytes integer NOT NULL,
  content_type text NOT NULL, content bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(), retention_until timestamptz NOT NULL,
  CONSTRAINT ss_label_artifact_blobs_pk_v1 PRIMARY KEY(tenant_id,publication_id,generation_id,kind),
  CONSTRAINT ss_label_artifact_blobs_publication_fk_v1 FOREIGN KEY(tenant_id,publication_id)
    REFERENCES __SCHEMA__.ss_intake_label_publications(tenant_id,id)
);
CREATE FUNCTION __SCHEMA__.ss_label_artifact_guard_v1() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $ss_artifact$__BODY__$ss_artifact$;
CREATE TRIGGER ss_label_artifact_blobs_guard_v1 BEFORE INSERT OR UPDATE OR DELETE
ON __SCHEMA__.ss_label_artifact_blobs FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.ss_label_artifact_guard_v1();
REVOKE ALL ON __SCHEMA__.ss_label_artifact_blobs FROM PUBLIC;
REVOKE ALL ON FUNCTION __SCHEMA__.ss_label_artifact_guard_v1() FROM PUBLIC;
'''.replace('__SCHEMA__', table).replace('__BODY__', guard_source(schema))


class PostgresArtifactStore:
    """Private storage only. Does not authorize staff or publish a pair.

    Default disabled fails before ref/content/database evaluation. No generic
    callback, SDK, credential lookup, migration or service activation occurs.
    """
    def __init__(self, database=None, binding=None, *, schema='public', enabled=False):
        if type(enabled) is not bool:
            raise _error('stat', 'LABEL_STORAGE_CONFIGURATION_INVALID')
        self._enabled = enabled
        self._database, self._binding, self._schema = database, binding, schema
        if enabled:
            _schema(schema)
            _publication(binding)
            if not callable(getattr(database, 'transaction', None)):
                raise _error('stat', 'LABEL_STORAGE_CONFIGURATION_INVALID')

    def _ref(self, value, operation):
        if not self._enabled:
            raise _error(operation, 'LABEL_STORAGE_DISABLED')
        ref = validate_ref(value)
        publication = _publication(self._binding, operation)
        expected = ArtifactRef(1, publication.tenant_id, publication.id, publication.generation_id,
            ref.kind, getattr(publication, ref.kind + '_sha256'), getattr(publication, ref.kind + '_bytes'),
            'application/pdf' if ref.kind == 'pdf' else 'application/octet-stream')
        if ref != expected:
            raise _error(operation, 'LABEL_STORAGE_FORBIDDEN')
        return ref

    def _catalog(self, db, operation):
        schema = self._schema
        version = db.execute("SELECT pg_catalog.current_setting('server_version_num')::integer AS version").fetchone()
        if type(version) is not dict or type(version.get('version')) is not int or version['version']//10000 not in (17,18):
            raise _error(operation, 'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')
        major = version['version']//10000
        row = db.execute('''SELECT c.oid AS table_oid,c.relkind,c.relpersistence,c.relrowsecurity,c.relhasrules,
          c.relispartition,EXISTS(SELECT 1 FROM pg_catalog.pg_inherits h WHERE h.inhrelid=c.oid OR h.inhparent=c.oid) AS inheritance,
          CASE WHEN pg_catalog.octet_length(p.prosrc)<=16384 THEN p.prosrc ELSE NULL END AS prosrc,
          p.prosecdef,CASE WHEN pg_catalog.cardinality(p.proconfig)=1 AND pg_catalog.octet_length(p.proconfig::text)<=4096
            THEN p.proconfig ELSE NULL END AS proconfig,p.proname,p.pronargs,p.proretset,p.provolatile,p.proparallel,
          p.prorettype='pg_catalog.trigger'::pg_catalog.regtype AS trigger_return,pn.nspname AS function_schema,
          l.lanname,t.tgname,t.tgtype,t.tgenabled,t.tgconstraint,t.tgnargs,
          CASE WHEN pg_catalog.octet_length(t.tgargs)<=4096 THEN pg_catalog.encode(t.tgargs,'hex') ELSE NULL END AS args,
          (t.tgqual IS NOT NULL) AS has_when,
          t.tgdeferrable,t.tginitdeferred,
          pg_catalog.current_setting('session_replication_role') AS replication
          FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
          JOIN pg_catalog.pg_trigger t ON t.tgrelid=c.oid AND NOT t.tgisinternal
          JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid JOIN pg_catalog.pg_language l ON l.oid=p.prolang
          JOIN pg_catalog.pg_namespace pn ON pn.oid=p.pronamespace
          WHERE n.nspname=%s AND c.relname='ss_label_artifact_blobs' LIMIT 2''', (schema,)).fetchall()
        if len(row) != 1 or row[0]['relkind'] != 'r' or row[0]['relpersistence'] != 'p' \
                or row[0]['relrowsecurity'] is not False or row[0]['relhasrules'] is not False \
                or row[0]['relispartition'] is not False or row[0]['inheritance'] is not False \
                or row[0]['prosrc'] != guard_source(schema) or row[0]['function_schema'] != schema \
                or row[0]['proname'] != 'ss_label_artifact_guard_v1' or row[0]['pronargs'] != 0 \
                or row[0]['proretset'] is not False or row[0]['trigger_return'] is not True \
                or row[0]['provolatile'] != 'v' or row[0]['proparallel'] != 'u' \
                or row[0]['prosecdef'] is not False or row[0]['proconfig'] != ['search_path=pg_catalog'] \
                or row[0]['lanname'] != 'plpgsql' or row[0]['tgtype'] != 31 or row[0]['tgenabled'] != 'O' \
                or row[0]['tgname'] != 'ss_label_artifact_blobs_guard_v1' \
                or row[0]['tgconstraint'] != 0 or row[0]['tgnargs'] != 0 or row[0]['args'] != '' \
                or row[0]['has_when'] is not False or row[0]['tgdeferrable'] is not False \
                or row[0]['tginitdeferred'] is not False or row[0]['replication'] != 'origin':
            raise _error(operation, 'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')
        table_oid = row[0]['table_oid']
        columns = db.execute('''SELECT a.attnum,a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod) AS type,
          a.attnotnull,a.attgenerated,a.attidentity,pg_catalog.pg_get_expr(d.adbin,d.adrelid) AS default_value
          FROM pg_catalog.pg_attribute a LEFT JOIN pg_catalog.pg_attrdef d ON (d.adrelid,d.adnum)=(a.attrelid,a.attnum)
          WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum LIMIT 11''',(table_oid,)).fetchall()
        expected = (('tenant_id','uuid'),('publication_id','uuid'),('generation_id','uuid'),('kind','text'),
                    ('sha256','text'),('bytes','integer'),('content_type','text'),('content','bytea'),
                    ('created_at','timestamp with time zone'),('retention_until','timestamp with time zone'))
        if len(columns) != 10 or any(value['attnum'] != number or (value['attname'],value['type']) != shape
                or value['attnotnull'] is not True or value['attgenerated'] != '' or value['attidentity'] != ''
                or (value['default_value'] not in ('clock_timestamp()','pg_catalog.clock_timestamp()')
                    if shape[0]=='created_at' else value['default_value'] is not None)
                for number,(value,shape) in enumerate(zip(columns,expected),1)):
            raise _error(operation, 'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')
        # PostgreSQL18 represents each NOT NULL in pg_constraint as kind n.
        # The17 projection preserves absence of those native-only fields;
        # neither an unexpected major nor an unrecognized constraint is ignored.
        native_fields = ('c.conenforced,c.conperiod' if major == 18 else
                         'NULL::boolean AS conenforced,NULL::boolean AS conperiod')
        constraints = db.execute(('''SELECT c.oid,c.conname,c.contype,c.conkey,c.confkey,c.confrelid,
          c.convalidated,c.condeferrable,c.condeferred,c.confupdtype,c.confdeltype,c.confmatchtype,
          c.conislocal,c.coninhcount,c.conparentid,c.connoinherit,c.conindid,c.contypid,
          c.conbin IS NULL AS no_expression,cn.nspname AS constraint_namespace,__NATIVE_FIELDS__,
          (SELECT x.oid FROM pg_catalog.pg_class x JOIN pg_catalog.pg_namespace n ON n.oid=x.relnamespace
            WHERE n.nspname=%s AND x.relname='ss_intake_label_publications') AS expected_target
          FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_namespace cn ON cn.oid=c.connamespace
          WHERE c.conrelid=%s ORDER BY c.contype,c.conname LIMIT 13''').replace('__NATIVE_FIELDS__',native_fields),
          (schema,table_oid)).fetchall()
        not_null = {('ss_label_artifact_blobs_'+shape[0]+'_not_null'):number
                    for number,shape in enumerate(expected,1)} if major == 18 else {}
        actual_null = [value for value in constraints if value['contype']=='n']
        by_kind = {value['contype']:value for value in constraints if value['contype']!='n'}
        if (len(constraints)!=2+len(not_null) or len(actual_null)!=len(not_null)
                or {value['conname'] for value in actual_null}!=set(not_null) or set(by_kind)!={'p','f'}
                or any(value['convalidated'] is not True or value['condeferrable'] is not False
                    or value['condeferred'] is not False or value['conislocal'] is not True
                    or value['coninhcount']!=0 or value['conparentid']!=0 or value['contypid']!=0
                    or value['constraint_namespace']!=schema or value['no_expression'] is not True
                    or value['connoinherit'] is not (value['contype'] in ('p','f'))
                    or (value['conenforced'] is not True or value['conperiod'] is not False
                        if major == 18 else value['conenforced'] is not None or value['conperiod'] is not None)
                    for value in constraints)):
            raise _error(operation, 'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')
        for value in actual_null:
            if (value['conkey']!=[not_null[value['conname']]] or value['conindid']!=0
                    or value['confrelid']!=0 or value['confkey'] is not None
                    or value['confupdtype']!=' ' or value['confdeltype']!=' ' or value['confmatchtype']!=' '):
                raise _error(operation, 'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')
        primary,foreign=by_kind['p'],by_kind['f']
        if primary['conname']!='ss_label_artifact_blobs_pk_v1' or primary['conkey']!=[1,2,3,4] \
                or foreign['conname']!='ss_label_artifact_blobs_publication_fk_v1' \
                or foreign['conkey']!=[1,2] or foreign['confkey']!=[1,2] \
                or foreign['confrelid']!=foreign['expected_target'] \
                or foreign['confupdtype']!='a' or foreign['confdeltype']!='a' or foreign['confmatchtype']!='s':
            raise _error(operation, 'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')
        ri = db.execute('''SELECT p.proname,n.nspname,t.tgtype,t.tgenabled,t.tgnargs,
          CASE WHEN pg_catalog.octet_length(t.tgargs)<=4096 THEN pg_catalog.encode(t.tgargs,'hex') ELSE NULL END AS args,
          (t.tgqual IS NOT NULL) AS has_when,
          t.tgdeferrable,t.tginitdeferred FROM pg_catalog.pg_trigger t
          JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
          WHERE t.tgisinternal AND t.tgconstraint=%s LIMIT 5''',(foreign['oid'],)).fetchall()
        if sorted((value['proname'],value['tgtype']) for value in ri) != sorted((
                ('RI_FKey_check_ins',5),('RI_FKey_check_upd',17),
                ('RI_FKey_noaction_del',9),('RI_FKey_noaction_upd',17))) \
                or any(value['nspname']!='pg_catalog' or value['tgenabled']!='O' or value['tgnargs']!=0
                       or value['args']!='' or value['has_when'] is not False
                       or value['tgdeferrable'] is not False or value['tginitdeferred'] is not False for value in ri):
            raise _error(operation, 'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')

    def _guard(self, db, ref, operation, *, write=False):
        if write:
            db.execute('SELECT pg_catalog.pg_advisory_xact_lock(%s,pg_catalog.hashtext(%s))',
                       (LOCK_DOMAIN, self._schema))
        state = inspect_label_schema(db)
        if state.version != 9 or state.state != 'present' or state.table_count != 5:
            raise _error(operation, 'LABEL_STORAGE_DEPENDENCY_UNAVAILABLE')
        actual = db.execute('SELECT pg_catalog.current_schema() AS name').fetchone()
        if actual['name'] != self._schema:
            raise _error(operation, 'LABEL_STORAGE_CONFIGURATION_INVALID')
        self._catalog(db, operation)
        binding = self._binding
        policy_guard = (assert_policy_records_current_for_writer if binding.policy.capture_kind == 'writer'
                        else assert_policy_records_current_for_read)
        if write and (binding.policy.capture_kind != 'writer' or binding.registry.capture_kind != 'reservation'):
            raise _error(operation)
        policy_guard(db, binding.policy, schema=self._schema)
        assert_registry_current(db, binding.registry, schema=self._schema)
        table = _schema(self._schema)
        eligible = db.execute('''SELECT p.retention_until>pg_catalog.clock_timestamp()
          AND j.retention_until>pg_catalog.clock_timestamp() AND i.retention_until>pg_catalog.clock_timestamp()
          AND i.content_state='active' AND i.input_body IS NOT NULL AND p.tombstoned_at IS NULL
          AND (p.state='published' OR p.lease_until>pg_catalog.clock_timestamp())
          AND pg_catalog.clock_timestamp()<%s::timestamptz AND pg_catalog.clock_timestamp()<%s::timestamptz
          AND pg_catalog.clock_timestamp()>=%s::timestamptz AS eligible
          FROM __SCHEMA__.ss_intake_label_publications p
          JOIN __SCHEMA__.ss_intake_label_jobs j ON (j.tenant_id,j.id)=(p.tenant_id,p.job_id)
          JOIN __SCHEMA__.ss_intake_label_inputs i ON (i.tenant_id,i.id)=(j.tenant_id,j.input_id)
          WHERE p.tenant_id=%s AND p.id=%s AND p.generation_id=%s'''.replace('__SCHEMA__', table),
          (binding.policy.current_version.row.evidence_use_expires_at,
           binding.policy.original_version.row.evidence_use_expires_at,
           binding.policy.head.row.clock_floor_at, ref.tenant_id, ref.publication_id, ref.generation_id)).fetchone()
        if eligible is None or eligible['eligible'] is not True:
            raise _error(operation)

    @staticmethod
    def _params(ref):
        return (ref.tenant_id, ref.publication_id, ref.generation_id, ref.kind)

    def _row(self, db, ref, *, content=False):
        fields = 'sha256,bytes,content_type' + (',CASE WHEN bytes BETWEEN 1 AND 33554432 '
                  'AND pg_catalog.octet_length(content)=bytes THEN content ELSE NULL END AS content' if content else '')
        return db.execute('SELECT ' + fields + ' FROM ' + _schema(self._schema)
            + '.ss_label_artifact_blobs WHERE tenant_id=%s AND publication_id=%s AND generation_id=%s AND kind=%s',
            self._params(ref)).fetchone()

    @staticmethod
    def _matches(row, ref):
        return row is not None and row['sha256'] == ref.sha256 and type(row['bytes']) is int \
            and row['bytes'] == ref.bytes and row['content_type'] == ref.content_type

    def _finish(self, ref, operation):
        with self._database.transaction() as db:
            self._guard(db, ref, operation)
            if not self._matches(self._row(db, ref), ref):
                raise _error(operation, 'LABEL_STORAGE_INTEGRITY_FAILED', 'not_applicable')

    def put_if_absent(self, value, content):
        ref = self._ref(value, 'put')
        verified = VerifiedArtifact(ref, content)  # Full hash outside any SQL/source/OS guard.
        code = None
        first_transaction_complete = False
        try:
            with self._database.transaction() as db:
                self._guard(db, ref, 'put', write=True)
                table = _schema(self._schema)
                inserted = db.execute('''INSERT INTO __SCHEMA__.ss_label_artifact_blobs
                  (tenant_id,publication_id,generation_id,kind,sha256,bytes,content_type,content,retention_until)
                  SELECT %s,%s,%s,%s,%s,%s,%s,%s,p.retention_until
                  FROM __SCHEMA__.ss_intake_label_publications p WHERE p.tenant_id=%s AND p.id=%s
                  ON CONFLICT(tenant_id,publication_id,generation_id,kind) DO NOTHING RETURNING kind'''
                  .replace('__SCHEMA__', table), (*self._params(ref), ref.sha256, ref.bytes, ref.content_type,
                    verified.content, ref.tenant_id, ref.publication_id)).fetchone()
                row = self._row(db, ref, content=True)
                if not self._matches(row, ref):
                    raise _error('put', 'LABEL_STORAGE_CONFLICT', 'not_committed')
            first_transaction_complete = True
            observed = VerifiedArtifact(ref, bytes(row['content']))
            if observed.content != verified.content:
                raise _error('put', 'LABEL_STORAGE_CONFLICT', 'may_have_committed')
            self._finish(ref, 'put')
            return PutObservation('created' if inserted is not None else 'precondition_failed', ref)
        except LabelStorageError as error:
            if not first_transaction_complete:
                raise
            # The first PUT transaction already exited successfully. A failed
            # final check cannot turn its durable same-ref row into absence.
            code = (error.code, 'may_have_committed')
        except Exception as error:
            sqlstate = getattr(error, 'sqlstate', None)
            code = ('LABEL_STORAGE_CONFLICT', 'not_committed') if sqlstate == '23505' else (
                ('LABEL_STORAGE_POLICY_BLOCKED', 'not_committed') if sqlstate == '23514' else
                ('LABEL_STORAGE_OUTCOME_UNKNOWN', 'may_have_committed'))
            if first_transaction_complete:
                code = (code[0], 'may_have_committed')
        raise _error('put', code[0], code[1], False)  # Outside owned driver handler; no raw context.

    def stat(self, value):
        ref = self._ref(value, 'stat')
        failed = False
        try:
            with self._database.transaction() as db:
                self._guard(db, ref, 'stat')
                row = self._row(db, ref)
            if row is None:
                return StatObservation('not_found_observed', ref, None, None, None)
            if not self._matches(row, ref):
                raise _error('stat', 'LABEL_STORAGE_INTEGRITY_FAILED', 'not_applicable')
            return StatObservation('present', ref, ref.bytes, ref.content_type, ref.sha256)
        except LabelStorageError:
            raise
        except Exception:
            failed = True
        if failed:
            raise _error('stat', 'LABEL_STORAGE_RESPONSE_INVALID', 'not_applicable', False)

    def open_verified(self, value):
        ref = self._ref(value, 'get')
        failed = False
        try:
            with self._database.transaction() as db:
                self._guard(db, ref, 'get')
                row = self._row(db, ref, content=True)
            if row is None:
                raise _error('get', 'LABEL_STORAGE_NOT_FOUND', 'not_applicable')
            if not self._matches(row, ref):
                raise _error('get', 'LABEL_STORAGE_INTEGRITY_FAILED', 'not_applicable')
            artifact = VerifiedArtifact(ref, bytes(row['content']))  # Hash after transaction closes.
            self._finish(ref, 'get')
            return artifact
        except LabelStorageError:
            raise
        except Exception:
            failed = True
        if failed:
            raise _error('get', 'LABEL_STORAGE_INTEGRITY_FAILED', 'not_applicable', False)

    def delete_tombstoned(self, ref, *, authorize):
        if not self._enabled:
            raise _error('delete', 'LABEL_STORAGE_DISABLED')
        # No callback evaluation: this compartment has no durable lifecycle authority.
        raise _error('delete', 'LABEL_STORAGE_POLICY_BLOCKED')
