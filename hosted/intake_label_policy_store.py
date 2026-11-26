"""UNRUN private policy persistence; stored evidence never proves human consent.

Pure preparation verifies the existing exact D1 codec outside our transaction.
The explicitly enabled repository owns one atomic transaction; captured rows are
comparison data only. No actor/session/MFA, document interpretation, current
clock decision, retention default, feature activation or application wiring.
Migration is returned as a proposal; no import, constructor or write installs it.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from psycopg.pq import TransactionStatus
import intake_label_policy_evidence as codec
from . import intake_label_policy_records as records


MAX_INT8 = 9223372036854775807
LOCK_DOMAIN = 1397967940
CATALOG_LIMIT = 256
_SCHEMA = re.compile(r'(?:public|ss_test_[a-f0-9]{32})')
_TABLES = ('ss_store_label_policy_heads', 'ss_store_label_policy_versions')
_KINDS = ('DISABLED', 'INVALID', 'ABSENT', 'CATALOG', 'CHANGED', 'TRANSACTION', 'DATABASE')


class PolicyPersistenceError(ValueError):
    __slots__ = ('code', 'commit_state')

    def __init__(self, kind='INVALID', commit_state='not_started'):
        self.code = 'INTAKE_LABEL_POLICY_STORE_' + (kind if kind in _KINDS else 'INVALID')
        self.commit_state = commit_state if commit_state in ('not_started', 'not_committed', 'may_have_committed') else 'may_have_committed'
        super().__init__('Shared label policy persistence requires a fresh review.')

    def __repr__(self):
        return 'PolicyPersistenceError()'


def _need(value, kind='INVALID'):
    if not value:
        raise PolicyPersistenceError(kind)


def _schema(value):
    _need(type(value) is str and _SCHEMA.fullmatch(value))
    return '"' + value + '"'


def _instant(value):
    _need(type(value) is str and len(value) == 27)
    try:
        result = datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
        canonical = (f'{result.year:04d}-{result.month:02d}-{result.day:02d}T'
                     f'{result.hour:02d}:{result.minute:02d}:{result.second:02d}.{result.microsecond:06d}Z')
        _need(canonical == value)
        return result
    except PolicyPersistenceError:
        raise
    except Exception:
        pass
    raise PolicyPersistenceError()


def _head_copy(value):
    _need(type(value) is records.ExpectedPolicyHead)
    row = records.ExpectedPolicyHead(*(getattr(value, key) for key in records.HEAD_FIELDS))
    if row.clock_floor_at is not None:
        _instant(row.clock_floor_at)
    return row


def _version_copy(value):
    _need(type(value) in (records.ExpectedPolicyVersion, codec.PolicyEvidence))
    row = codec.PolicyEvidence(*(getattr(value, key) for key in records.VERSION_FIELDS))
    return codec.verify_evidence(row)


def _capture_copy(value):
    _need(type(value) is records.CapturedPolicyRecords and value.capture_kind == 'writer')
    _need(type(value.selector) is records.PolicyRecordSelector)
    selector = records.PolicyRecordSelector(value.selector.tenant_id, value.selector.original_policy_hash)
    observations = []
    for name in ('head', 'current_version', 'original_version'):
        item = getattr(value, name)
        _need(type(item) is records.PolicyRecordObservation)
        row = item.row
        if item.state == 'present':
            if name == 'head':
                row = _head_copy(row)
            else:
                checked = _version_copy(row)
                row = records.ExpectedPolicyVersion(*(getattr(checked, key) for key in records.VERSION_FIELDS))
        observations.append(records.PolicyRecordObservation(item.state, row))
    return records.CapturedPolicyRecords(selector, 'writer', *observations)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class PreparedPolicyWrite:
    kind: str
    expected: records.CapturedPolicyRecords
    new_head: records.ExpectedPolicyHead
    new_version: codec.PolicyEvidence | None

    def __repr__(self):
        return '<PreparedPolicyWrite>'

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class PolicySchemaState:
    state: str
    table_count: int
    feature_enabled: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class PolicyWriteObservation:
    tenant_id: str
    policy_revision: int
    version_revision: int | None
    policy_hash: str | None

    def __repr__(self):
        return '<PolicyWriteObservation>'


def _advance(expected, new_head):
    old = expected.head.row
    _need(expected.head.state == 'present' and type(old) is records.ExpectedPolicyHead)
    _need(old.tenant_id == new_head.tenant_id == expected.selector.tenant_id
          and old.policy_revision < MAX_INT8 and new_head.policy_revision == old.policy_revision + 1)
    changed = (old.clock_floor_at, old.clock_state) != (new_head.clock_floor_at, new_head.clock_state)
    _need(new_head.clock_revision == old.clock_revision + (1 if changed else 0)
          and new_head.clock_revision <= MAX_INT8)
    if old.clock_floor_at is not None:
        _need(new_head.clock_floor_at is not None and _instant(new_head.clock_floor_at) >= _instant(old.clock_floor_at)
              and new_head.clock_state != 'uninitialized')


def prepare_initialize(expected):
    """Explicit empty metadata initialization only; never acceptance/enablement."""
    captured = _capture_copy(expected)
    _need(captured.head.state == 'absent' and captured.selector.original_policy_hash is None)
    head = records.ExpectedPolicyHead(captured.selector.tenant_id, 1, 'unaccepted', None, None, None, 0, 'uninitialized')
    return PreparedPolicyWrite('initialize', captured, head, None)


def prepare_append(expected, evidence, new_head):
    """Prepare exact caller-supplied evidence and explicit next HEAD; no clock IO."""
    captured, version, head = _capture_copy(expected), _version_copy(evidence), _head_copy(new_head)
    _advance(captured, head)
    old = captured.head.row
    next_revision = (old.current_version_revision or 0) + 1
    _need(version.tenant_id == head.tenant_id == captured.selector.tenant_id
          and next_revision <= MAX_INT8 and version.revision == next_revision
          and (head.current_version_revision, head.current_policy_hash) == (version.revision, version.policy_hash)
          and (head.policy_state != 'enabled' or version.accepted_enabled is True))
    return PreparedPolicyWrite('append', captured, head, version)


def prepare_head_cas(expected, new_head):
    """Change explicit HEAD/clock metadata without rewriting or selecting history."""
    captured, head = _capture_copy(expected), _head_copy(new_head)
    _advance(captured, head)
    old = captured.head.row
    _need((head.current_version_revision, head.current_policy_hash) ==
          (old.current_version_revision, old.current_policy_hash))
    if head.policy_state == 'enabled':
        _need(captured.current_version.state == 'present' and captured.current_version.row.accepted_enabled is True)
    return PreparedPolicyWrite('head_cas', captured, head, None)


def _prepared_copy(value):
    _need(type(value) is PreparedPolicyWrite)
    _need(type(value.expected) is records.CapturedPolicyRecords and type(value.new_head) is records.ExpectedPolicyHead
          and (value.new_version is None or type(value.new_version) is codec.PolicyEvidence))
    if value.kind == 'initialize':
        copy = prepare_initialize(value.expected)
    elif value.kind == 'append':
        copy = prepare_append(value.expected, value.new_version, value.new_head)
    elif value.kind == 'head_cas':
        copy = prepare_head_cas(value.expected, value.new_head)
    else:
        raise PolicyPersistenceError()
    _need(tuple(getattr(copy.new_head, key) for key in records.HEAD_FIELDS) ==
          tuple(getattr(value.new_head, key) for key in records.HEAD_FIELDS)
          and (value.new_version is None) == (copy.new_version is None))
    return copy


def _version_guard(schema):
    table = _schema(schema)
    return '''
DECLARE n bigint;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_VERSION_IMMUTABLE'; END IF;
  IF NOT pg_catalog.pg_try_advisory_xact_lock(1397967940,pg_catalog.hashtext(NEW.tenant_id::pg_catalog.text)) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_WRITE_BUSY'; END IF;
  PERFORM tenant_id FROM __S__.ss_store_label_policy_heads WHERE tenant_id=NEW.tenant_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_HEAD_REQUIRED'; END IF;
  SELECT COALESCE(max(revision),0) INTO n FROM __S__.ss_store_label_policy_versions WHERE tenant_id=NEW.tenant_id;
  IF n>=9223372036854775807 OR NEW.revision<>n+1 OR NEW.envelope_version<>1
     OR NEW.document_format<>'ss_label_policy_document_utf8_v1'
     OR pg_catalog.current_setting('server_encoding')<>'UTF8' OR pg_catalog.current_setting('client_encoding')<>'UTF8'
     OR pg_catalog.octet_length(NEW.document_body) NOT BETWEEN 1 AND 65536
     OR pg_catalog.octet_length(NEW.notice_body) NOT BETWEEN 1 AND 65536
     OR pg_catalog.octet_length(NEW.policy_hash)<>64 OR pg_catalog.octet_length(NEW.document_hash)<>64
     OR pg_catalog.octet_length(NEW.notice_hash)<>64
     OR NEW.policy_hash!~'^[0-9a-f]{64}$' OR NEW.document_hash!~'^[0-9a-f]{64}$' OR NEW.notice_hash!~'^[0-9a-f]{64}$'
     OR NOT pg_catalog.isfinite(NEW.accepted_at) OR NOT pg_catalog.isfinite(NEW.evidence_use_expires_at)
     OR NEW.accepted_at<'0001-01-01 00:00:00+00'::pg_catalog.timestamptz
     OR NEW.evidence_use_expires_at>'9999-12-31 23:59:59.999999+00'::pg_catalog.timestamptz
     OR NEW.evidence_use_expires_at<=NEW.accepted_at THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_VERSION_INVALID'; END IF;
  RETURN NEW;
END;
'''.replace('__S__', table)


def _head_guard(schema):
    table = _schema(schema)
    return '''
DECLARE accepted boolean;
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_HEAD_DELETE_DENIED'; END IF;
  IF NOT pg_catalog.pg_try_advisory_xact_lock(1397967940,pg_catalog.hashtext(NEW.tenant_id::pg_catalog.text)) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_WRITE_BUSY'; END IF;
  IF NEW.policy_revision<1 OR NEW.policy_state NOT IN ('unaccepted','enabled','disabled','restricted','restore_review')
     OR (NEW.current_version_revision IS NULL)<>(NEW.current_policy_hash IS NULL)
     OR (NEW.current_policy_hash IS NOT NULL AND (pg_catalog.octet_length(NEW.current_policy_hash)<>64 OR NEW.current_policy_hash!~'^[0-9a-f]{64}$'))
     OR (NEW.current_version_revision IS NOT NULL AND NEW.current_version_revision<1)
     OR NEW.clock_state NOT IN ('uninitialized','healthy','blocked_regression','restore_review')
     OR NEW.clock_revision<0
     OR (NEW.clock_state='uninitialized')<>(NEW.clock_floor_at IS NULL)
     OR (NEW.clock_state='uninitialized' AND NEW.clock_revision<>0)
     OR (NEW.clock_state<>'uninitialized' AND NEW.clock_revision<1)
     OR (NEW.policy_state='unaccepted' AND NEW.current_version_revision IS NOT NULL)
     OR (NEW.policy_state='enabled' AND NEW.current_version_revision IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_HEAD_INVALID'; END IF;
  IF NEW.clock_floor_at IS NOT NULL AND (NOT pg_catalog.isfinite(NEW.clock_floor_at)
     OR NEW.clock_floor_at<'0001-01-01 00:00:00+00'::pg_catalog.timestamptz
     OR NEW.clock_floor_at>'9999-12-31 23:59:59.999999+00'::pg_catalog.timestamptz) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_CLOCK_INVALID'; END IF;
  IF TG_OP='INSERT' THEN
    IF NEW.policy_revision<>1 OR NEW.policy_state<>'unaccepted' OR NEW.current_version_revision IS NOT NULL
       OR NEW.clock_state<>'uninitialized' OR NEW.clock_revision<>0 THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_INITIAL_HEAD_INVALID'; END IF;
  ELSE
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR OLD.policy_revision>=9223372036854775807
       OR NEW.policy_revision<>OLD.policy_revision+1
       OR (OLD.clock_floor_at IS NOT NULL AND (NEW.clock_floor_at IS NULL OR NEW.clock_floor_at<OLD.clock_floor_at))
       OR (ROW(NEW.clock_floor_at,NEW.clock_state) IS DISTINCT FROM ROW(OLD.clock_floor_at,OLD.clock_state)
           AND (OLD.clock_revision>=9223372036854775807 OR NEW.clock_revision<>OLD.clock_revision+1))
       OR (ROW(NEW.clock_floor_at,NEW.clock_state) IS NOT DISTINCT FROM ROW(OLD.clock_floor_at,OLD.clock_state)
           AND NEW.clock_revision<>OLD.clock_revision) THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_HEAD_CAS_INVALID'; END IF;
  END IF;
  IF NEW.current_version_revision IS NOT NULL THEN
    SELECT accepted_enabled INTO accepted FROM __S__.ss_store_label_policy_versions
      WHERE tenant_id=NEW.tenant_id AND revision=NEW.current_version_revision AND policy_hash=NEW.current_policy_hash;
    IF NOT FOUND OR (NEW.policy_state='enabled' AND accepted IS NOT TRUE) THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='LABEL_POLICY_VERSION_REFERENCE_INVALID'; END IF;
  END IF;
  RETURN NEW;
END;
'''.replace('__S__', table)


def migration_proposal(schema='public'):
    """Return additive empty-table SQL only. Caller/operator owns any future install."""
    table = _schema(schema)
    ddl = '''
CREATE TABLE __S__.ss_store_label_policy_heads (
 tenant_id pg_catalog.uuid NOT NULL,
 policy_revision pg_catalog.int8 NOT NULL,
 policy_state pg_catalog.text COLLATE pg_catalog."C" NOT NULL,
 current_version_revision pg_catalog.int8,
 current_policy_hash pg_catalog.text COLLATE pg_catalog."C",
 clock_floor_at pg_catalog.timestamptz,
 clock_revision pg_catalog.int8 NOT NULL,
 clock_state pg_catalog.text COLLATE pg_catalog."C" NOT NULL,
 CONSTRAINT ss_label_policy_heads_pk_v1 PRIMARY KEY(tenant_id),
 CONSTRAINT ss_label_policy_heads_tenant_fk_v1 FOREIGN KEY(tenant_id) REFERENCES __S__.ss_tenants(id));
CREATE TABLE __S__.ss_store_label_policy_versions (
 tenant_id pg_catalog.uuid NOT NULL,revision pg_catalog.int8 NOT NULL,
 policy_hash pg_catalog.text COLLATE pg_catalog."C" NOT NULL,envelope_version pg_catalog.int4 NOT NULL,
 document_format pg_catalog.text COLLATE pg_catalog."C" NOT NULL,document_body pg_catalog.text COLLATE pg_catalog."C" NOT NULL,
 document_hash pg_catalog.text COLLATE pg_catalog."C" NOT NULL,notice_body pg_catalog.text COLLATE pg_catalog."C" NOT NULL,
 notice_hash pg_catalog.text COLLATE pg_catalog."C" NOT NULL,accepted_enabled pg_catalog.bool NOT NULL,
 accepted_by pg_catalog.uuid NOT NULL,accepted_at pg_catalog.timestamptz NOT NULL,evidence_use_expires_at pg_catalog.timestamptz NOT NULL,
 CONSTRAINT ss_label_policy_versions_pk_v1 PRIMARY KEY(tenant_id,revision),
 CONSTRAINT ss_label_policy_versions_hash_uq_v1 UNIQUE(tenant_id,policy_hash),
 CONSTRAINT ss_label_policy_versions_identity_uq_v1 UNIQUE(tenant_id,revision,policy_hash),
 CONSTRAINT ss_label_policy_versions_head_fk_v1 FOREIGN KEY(tenant_id) REFERENCES __S__.ss_store_label_policy_heads(tenant_id),
 CONSTRAINT ss_label_policy_versions_staff_fk_v1 FOREIGN KEY(tenant_id,accepted_by) REFERENCES __S__.ss_staff(tenant_id,id));
ALTER TABLE __S__.ss_store_label_policy_heads ADD CONSTRAINT ss_label_policy_heads_version_fk_v1
 FOREIGN KEY(tenant_id,current_version_revision,current_policy_hash) REFERENCES __S__.ss_store_label_policy_versions(tenant_id,revision,policy_hash);
'''.replace('__S__', table)
    for name, source in (('heads', _head_guard(schema)), ('versions', _version_guard(schema))):
        function = table + '.ss_label_policy_' + name + '_guard_v1'
        ddl += 'CREATE FUNCTION ' + function + '() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $ss_policy$' + source + '$ss_policy$;\n'
        ddl += 'CREATE TRIGGER ss_label_policy_' + name + '_guard_v1 BEFORE INSERT OR UPDATE OR DELETE ON ' + table + '.ss_store_label_policy_' + name + ' FOR EACH ROW EXECUTE FUNCTION ' + function + '();\n'
        ddl += 'REVOKE ALL ON FUNCTION ' + function + '() FROM PUBLIC;\n'
    ddl += 'REVOKE ALL ON TABLE ' + table + '.ss_store_label_policy_heads,' + table + '.ss_store_label_policy_versions FROM PUBLIC;\n'
    return ddl


_COLUMNS = {
    _TABLES[0]: (('tenant_id','uuid',False),('policy_revision','int8',False),('policy_state','text',False),
        ('current_version_revision','int8',True),('current_policy_hash','text',True),
        ('clock_floor_at','timestamptz',True),('clock_revision','int8',False),('clock_state','text',False)),
    _TABLES[1]: (('tenant_id','uuid',False),('revision','int8',False),('policy_hash','text',False),
        ('envelope_version','int4',False),('document_format','text',False),('document_body','text',False),
        ('document_hash','text',False),('notice_body','text',False),('notice_hash','text',False),
        ('accepted_enabled','bool',False),('accepted_by','uuid',False),('accepted_at','timestamptz',False),
        ('evidence_use_expires_at','timestamptz',False)),
}
_CONSTRAINTS = {
    'ss_label_policy_heads_pk_v1': (_TABLES[0],'p',('tenant_id',),None,()),
    'ss_label_policy_heads_tenant_fk_v1': (_TABLES[0],'f',('tenant_id',),'ss_tenants',('id',)),
    'ss_label_policy_heads_version_fk_v1': (_TABLES[0],'f',('tenant_id','current_version_revision','current_policy_hash'),_TABLES[1],('tenant_id','revision','policy_hash')),
    'ss_label_policy_versions_pk_v1': (_TABLES[1],'p',('tenant_id','revision'),None,()),
    'ss_label_policy_versions_hash_uq_v1': (_TABLES[1],'u',('tenant_id','policy_hash'),None,()),
    'ss_label_policy_versions_identity_uq_v1': (_TABLES[1],'u',('tenant_id','revision','policy_hash'),None,()),
    'ss_label_policy_versions_head_fk_v1': (_TABLES[1],'f',('tenant_id',),_TABLES[0],('tenant_id',)),
    'ss_label_policy_versions_staff_fk_v1': (_TABLES[1],'f',('tenant_id','accepted_by'),'ss_staff',('tenant_id','id')),
}


def _transaction(db):
    valid = False
    try:
        valid = (type(db.autocommit) is bool and db.autocommit is False
                 and type(db.info.transaction_status) is TransactionStatus
                 and db.info.transaction_status is TransactionStatus.INTRANS)
    except Exception:
        pass
    _need(valid, 'TRANSACTION')


def _rows(db, sql, params=()):
    _transaction(db)
    result = db.execute(sql, params).fetchall()
    _need(type(result) is list and len(result) <= CATALOG_LIMIT
          and all(type(row) is dict for row in result), 'CATALOG')
    _transaction(db)
    return result


def _inspect_schema(db, schema):
    _schema(schema)
    _transaction(db)
    encoding = db.execute("SELECT pg_catalog.current_setting('server_encoding')='UTF8' AND pg_catalog.current_setting('client_encoding')='UTF8' AS ok, pg_catalog.current_setting('server_version_num')::integer AS version").fetchone()
    _need(type(encoding) is dict and encoding.get('ok') is True
          and type(encoding.get('version')) is int and encoding['version']//10000 in (17,18), 'CATALOG')
    major = encoding['version']//10000
    tables = _rows(db, '''SELECT c.oid,c.relname,c.relowner,c.relkind,c.relpersistence,c.relrowsecurity,c.relforcerowsecurity,
      c.relispartition,c.relhasrules,c.reloptions IS NULL AS no_options,
      NOT EXISTS(SELECT 1 FROM pg_catalog.pg_inherits x WHERE x.inhrelid=c.oid OR x.inhparent=c.oid) AS no_inheritance,
      NOT EXISTS(SELECT 1 FROM pg_catalog.aclexplode(COALESCE(c.relacl,pg_catalog.acldefault('r',c.relowner))) x WHERE x.grantee=0) AS no_public_acl
      FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname=%s AND c.relname=ANY(%s) ORDER BY c.relname LIMIT 257''', (schema,list(_TABLES)))
    functions = _rows(db, '''SELECT p.oid,p.proname,p.proowner,p.pronargs,p.prokind,p.proretset,p.provolatile,p.proparallel,
      p.prosecdef,p.proisstrict,p.proleakproof,t.typname AS result_type,tn.nspname AS result_namespace,l.lanname,
      NOT EXISTS(SELECT 1 FROM pg_catalog.aclexplode(COALESCE(p.proacl,pg_catalog.acldefault('f',p.proowner))) x WHERE x.grantee=0) AS no_public_acl,
      (p.prosupport=0 AND p.provariadic=0 AND p.pronargdefaults=0 AND p.proargdefaults IS NULL
       AND p.proallargtypes IS NULL AND p.proargmodes IS NULL AND p.proargnames IS NULL AND p.prosqlbody IS NULL) AS plain_arguments,
      pg_catalog.octet_length(p.prosrc)<=65536 AS source_bound,
      CASE WHEN pg_catalog.octet_length(p.prosrc)<=65536 THEN p.prosrc ELSE NULL END AS source,
      CASE WHEN pg_catalog.pg_column_size(p.proconfig)<=8192 THEN p.proconfig ELSE NULL END AS config
      FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
      JOIN pg_catalog.pg_type t ON t.oid=p.prorettype JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace
      JOIN pg_catalog.pg_language l ON l.oid=p.prolang WHERE n.nspname=%s
      AND p.proname=ANY(%s) ORDER BY p.proname,p.oid LIMIT 257''',
      (schema,['ss_label_policy_heads_guard_v1','ss_label_policy_versions_guard_v1']))
    if not tables and not functions:
        return PolicySchemaState('absent',0)
    _need(len(tables)==2 and {row['relname'] for row in tables}==set(_TABLES) and len(functions)==2,'CATALOG')
    owners = {row['relowner'] for row in tables}
    _need(len(owners)==1 and all(row['relkind']=='r' and row['relpersistence']=='p'
          and row['relrowsecurity'] is False and row['relforcerowsecurity'] is False and row['relispartition'] is False
          and row['relhasrules'] is False and row['no_options'] is True and row['no_inheritance'] is True
          and row['no_public_acl'] is True for row in tables),'CATALOG')
    oids = [row['oid'] for row in tables]
    columns = _rows(db, '''SELECT c.relname,a.attnum,a.attname,t.typname,n.nspname,a.attnotnull,a.attisdropped,
      a.attidentity,a.attgenerated,a.atthasdef,a.attcollation,co.collname,cn.nspname AS collation_namespace,co.collisdeterministic
      FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
      LEFT JOIN pg_catalog.pg_type t ON t.oid=a.atttypid LEFT JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
      LEFT JOIN pg_catalog.pg_collation co ON co.oid=a.attcollation LEFT JOIN pg_catalog.pg_namespace cn ON cn.oid=co.collnamespace
      WHERE a.attrelid=ANY(%s) AND a.attnum>0 ORDER BY c.relname,a.attnum LIMIT 257''',(oids,))
    _need(len(columns)==21,'CATALOG')
    collations = {}
    for table, specs in _COLUMNS.items():
        selected = [row for row in columns if row['relname']==table]
        _need(len(selected)==len(specs),'CATALOG')
        for index,(actual,(name,kind,nullable)) in enumerate(zip(selected,specs,strict=True),1):
            _need(actual['attnum']==index and actual['attname']==name and actual['typname']==kind
                  and actual['nspname']=='pg_catalog' and actual['attnotnull'] is (not nullable)
                  and actual['attisdropped'] is False and actual['attidentity']==actual['attgenerated']==''
                  and actual['atthasdef'] is False,'CATALOG')
            _need((kind=='text' and actual['collname']=='C' and actual['collation_namespace']=='pg_catalog'
                   and actual['collisdeterministic'] is True) or (kind!='text' and actual['attcollation']==0),'CATALOG')
            collations[(table,name)] = actual['attcollation']
    constraint_sql = '''SELECT k.oid,k.conname,k.contype,c.relname,n.nspname,k.convalidated,k.condeferrable,k.condeferred,
      k.conislocal,k.coninhcount,k.conparentid,k.confmatchtype,k.confupdtype,k.confdeltype,
      k.connoinherit,k.conindid,k.contypid,k.confrelid,k.conbin IS NULL AS no_expression,
      NULL::boolean AS enforced,NULL::boolean AS period,
      rc.relname AS referenced,rn.nspname AS referenced_namespace,
      ARRAY(SELECT a.attname FROM pg_catalog.unnest(k.conkey) WITH ORDINALITY x(num,ord)
        JOIN pg_catalog.pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.num ORDER BY x.ord) AS columns,
      ARRAY(SELECT a.attname FROM pg_catalog.unnest(k.confkey) WITH ORDINALITY x(num,ord)
        JOIN pg_catalog.pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=x.num ORDER BY x.ord) AS referenced_columns
      FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid
      JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_catalog.pg_class rc ON rc.oid=k.confrelid
      LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid=rc.relnamespace
      WHERE k.conrelid=ANY(%s) OR k.confrelid=ANY(%s) ORDER BY k.conname,k.oid LIMIT 257'''
    if major == 18:
        constraint_sql = constraint_sql.replace('NULL::boolean AS enforced,NULL::boolean AS period',
                                               'k.conenforced AS enforced,k.conperiod AS period')
    constraints = _rows(db,constraint_sql,(oids,oids))
    expected_constraints = dict(_CONSTRAINTS)
    if major == 18:
        expected_constraints.update({table+'_'+name+'_not_null':(table,'n',(name,),None,())
            for table,specs in _COLUMNS.items() for name,_,nullable in specs if not nullable})
    _need(len(constraints)==len(expected_constraints)
          and {row['conname'] for row in constraints}==set(expected_constraints),'CATALOG')
    fk = {}
    for row in constraints:
        table,kind,names,ref,ref_names = expected_constraints[row['conname']]
        _need(row['relname']==table and row['nspname']==schema and row['contype']==kind
              and tuple(row['columns'])==names and row['convalidated'] is True and row['condeferrable'] is False
              and row['condeferred'] is False and row['conislocal'] is True and row['coninhcount']==row['conparentid']==0
              and row['connoinherit'] is (kind in ('p','u','f')) and row['contypid']==0
              and row['no_expression'] is True,'CATALOG')
        _need((row['enforced'] is True and row['period'] is False) if major == 18
              else (row['enforced'] is None and row['period'] is None),'CATALOG')
        if kind=='n':
            _need(row['conindid']==0 and row['confrelid']==0 and row['referenced'] is None
                  and row['referenced_namespace'] is None and tuple(row['referenced_columns'])==()
                  and row['confmatchtype']==row['confupdtype']==row['confdeltype']==' ','CATALOG')
        if kind=='f':
            _need(row['referenced']==ref and row['referenced_namespace']==schema
                  and tuple(row['referenced_columns'])==ref_names and row['confmatchtype']=='s'
                  and row['confupdtype']==row['confdeltype']=='a','CATALOG')
            fk[row['oid']]=(table,ref)
    indexes = _rows(db, '''SELECT c.relname,ic.relname AS index_name,i.indisunique,i.indisprimary,i.indisvalid,i.indisready,
      i.indimmediate,i.indisexclusion,i.indnullsnotdistinct,i.indnatts,i.indnkeyatts,am.amname,
      i.indexprs IS NULL AS no_expression,i.indpred IS NULL AS no_predicate,ic.reloptions IS NULL AS no_options,
      ARRAY(SELECT a.attname FROM pg_catalog.unnest(i.indkey) WITH ORDINALITY x(num,ord)
        JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=x.num ORDER BY x.ord) AS columns,
      ARRAY(SELECT n.nspname||'.'||o.opcname FROM pg_catalog.unnest(i.indclass) WITH ORDINALITY x(num,ord)
        JOIN pg_catalog.pg_opclass o ON o.oid=x.num JOIN pg_catalog.pg_namespace n ON n.oid=o.opcnamespace ORDER BY x.ord) AS opclasses,
      ARRAY(SELECT num FROM pg_catalog.unnest(i.indcollation) WITH ORDINALITY x(num,ord) ORDER BY x.ord) AS collations
      FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid=i.indrelid
      JOIN pg_catalog.pg_class ic ON ic.oid=i.indexrelid JOIN pg_catalog.pg_am am ON am.oid=ic.relam
      WHERE i.indrelid=ANY(%s) ORDER BY ic.relname LIMIT 257''',(oids,))
    expected_indexes = {name:spec for name,spec in _CONSTRAINTS.items() if spec[1] in ('p','u')}
    _need(len(indexes)==len(expected_indexes) and {row['index_name'] for row in indexes}==set(expected_indexes),'CATALOG')
    for row in indexes:
        table,kind,names,_,_ = expected_indexes[row['index_name']]
        kinds = dict((item[0],item[1]) for item in _COLUMNS[table])
        _need(row['relname']==table and tuple(row['columns'])==names and row['amname']=='btree'
              and row['indisunique'] is True and row['indisprimary'] is (kind=='p')
              and row['indisvalid'] is True and row['indisready'] is True and row['indimmediate'] is True
              and row['indisexclusion'] is False and row['indnullsnotdistinct'] is False
              and row['indnatts']==row['indnkeyatts']==len(names) and row['no_expression'] is True
              and row['no_predicate'] is True and row['no_options'] is True
              and row['opclasses']==['pg_catalog.'+kinds[name]+'_ops' for name in names]
              and row['collations']==[collations[(table,name)] for name in names],'CATALOG')
    function_oids = {}
    for row in functions:
        family = 'heads' if row['proname']=='ss_label_policy_heads_guard_v1' else 'versions'
        expected = _head_guard(schema) if family=='heads' else _version_guard(schema)
        _need(row['proowner'] in owners and row['pronargs']==0 and row['prokind']=='f' and row['proretset'] is False
              and row['provolatile']=='v' and row['proparallel']=='u' and row['prosecdef'] is False
              and row['proisstrict'] is False and row['proleakproof'] is False and row['result_type']=='trigger'
              and row['result_namespace']=='pg_catalog' and row['lanname']=='plpgsql' and row['source_bound'] is True
              and row['source']==expected and row['config']==['search_path=pg_catalog'] and row['plain_arguments'] is True
              and row['no_public_acl'] is True,'CATALOG')
        function_oids[row['oid']]=family
    triggers = _rows(db, '''SELECT g.oid,g.tgrelid,g.tgname,g.tgfoid,g.tgisinternal,g.tgtype,g.tgenabled,g.tgconstraint,
      g.tgdeferrable,g.tginitdeferred,g.tgqual IS NULL AS no_when,pg_catalog.octet_length(g.tgargs) AS arg_bytes,
      g.tgnargs,g.tgattr::pg_catalog.text AS columns,c.relname,n.nspname,p.proname,pn.nspname AS function_namespace
      FROM pg_catalog.pg_trigger g JOIN pg_catalog.pg_class c ON c.oid=g.tgrelid
      JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace JOIN pg_catalog.pg_proc p ON p.oid=g.tgfoid
      JOIN pg_catalog.pg_namespace pn ON pn.oid=p.pronamespace
      WHERE g.tgrelid=ANY(%s) OR g.tgconstraint=ANY(%s) ORDER BY g.oid LIMIT 257''',(oids,list(fk)))
    own = [row for row in triggers if row['tgisinternal'] is False]
    internal = [row for row in triggers if row['tgisinternal'] is True]
    _need(len(own)==2 and len(internal)==4*len(fk),'CATALOG')
    for row in own:
        family = function_oids.get(row['tgfoid'])
        _need(family in ('heads','versions') and row['tgname']=='ss_label_policy_'+family+'_guard_v1'
              and row['relname']=='ss_store_label_policy_'+family and row['nspname']==schema
              and row['function_namespace']==schema and row['tgtype']==31 and row['tgenabled']=='O'
              and row['tgconstraint']==0 and row['tgdeferrable'] is False and row['tginitdeferred'] is False
              and row['no_when'] is True and row['arg_bytes']==row['tgnargs']==0 and row['columns']=='','CATALOG')
    for constraint,(source,target) in fk.items():
        selected = [row for row in internal if row['tgconstraint']==constraint]
        want = {(source,'RI_FKey_check_ins',5),(source,'RI_FKey_check_upd',17),
                (target,'RI_FKey_noaction_del',9),(target,'RI_FKey_noaction_upd',17)}
        _need(len(selected)==4 and {(row['relname'],row['proname'],row['tgtype']) for row in selected}==want,'CATALOG')
        _need(all(row['nspname']==schema and row['function_namespace']=='pg_catalog' and row['tgenabled']=='O'
                  and row['tgdeferrable'] is False and row['tginitdeferred'] is False and row['no_when'] is True
                  and row['arg_bytes']==row['tgnargs']==0 and row['columns']=='' for row in selected),'CATALOG')
    return PolicySchemaState('present',2)


def inspect_schema(db, *, schema='public'):
    """Read exact own native catalog only; absent is data, never installation."""
    try:
        return _inspect_schema(db,schema)
    except PolicyPersistenceError:
        raise
    except Exception:
        pass
    raise PolicyPersistenceError('CATALOG') from None


class PolicyRepository:
    def __init__(self, database=None, *, schema='public', enabled=False):
        _schema(schema)
        _need(type(enabled) is bool)
        self._database,self._schema,self._enabled = database,schema,enabled

    def apply_prepared(self,value):
        if self._enabled is not True:
            raise PolicyPersistenceError('DISABLED')
        prepared = _prepared_copy(value)  # All full evidence hashes before our SQL transaction.
        _need(self._database is not None)
        tenant = prepared.expected.selector.tenant_id
        table = _schema(self._schema)
        issued,completed,failure = False,False,None
        try:
            with self._database.transaction() as db:
                _need(inspect_schema(db,schema=self._schema).state=='present','ABSENT')
                db.execute('LOCK TABLE '+table+'.'+_TABLES[0]+','+table+'.'+_TABLES[1]+' IN ROW EXCLUSIVE MODE')
                _need(inspect_schema(db,schema=self._schema).state=='present','CATALOG')
                db.execute('SELECT pg_catalog.pg_advisory_xact_lock(%s,pg_catalog.hashtext(%s))',(LOCK_DOMAIN,tenant))
                records.assert_policy_records_current_for_writer(db,prepared.expected,schema=self._schema)
                head = prepared.new_head
                if prepared.kind=='initialize':
                    issued=True
                    db.execute('INSERT INTO '+table+'.'+_TABLES[0]+'('+','.join(records.HEAD_FIELDS)+') VALUES('+','.join(['%s']*8)+')',
                               tuple(getattr(head,key) for key in records.HEAD_FIELDS))
                else:
                    if prepared.new_version is not None:
                        version = prepared.new_version
                        issued=True
                        db.execute('INSERT INTO '+table+'.'+_TABLES[1]+'('+','.join(records.VERSION_FIELDS)+') VALUES('+','.join(['%s']*13)+')',
                                   tuple(getattr(version,key) for key in records.VERSION_FIELDS))
                    old = prepared.expected.head.row
                    fields = records.HEAD_FIELDS
                    casts = ('uuid','int8','text','int8','text','timestamptz','int8','text')
                    issued=True
                    result = db.execute('UPDATE '+table+'.'+_TABLES[0]+' SET '+','.join(name+'=%s' for name in fields[1:])+
                        ' WHERE ROW('+','.join(fields)+') IS NOT DISTINCT FROM ROW('+','.join('%s::pg_catalog.'+kind for kind in casts)+') RETURNING policy_revision',
                        tuple(getattr(head,key) for key in fields[1:])+tuple(getattr(old,key) for key in fields)).fetchone()
                    _need(type(result) is dict and result.get('policy_revision')==head.policy_revision,'CHANGED')
                observation = PolicyWriteObservation(tenant,head.policy_revision,head.current_version_revision,head.current_policy_hash)
            completed=True
            return observation
        except PolicyPersistenceError as error:
            failure=(error.code.removeprefix('INTAKE_LABEL_POLICY_STORE_'),'may_have_committed' if completed else 'not_committed' if issued else 'not_started')
        except records.LabelPolicyRecordsError as error:
            failure=('CHANGED' if error.code.endswith('_CHANGED') else 'DATABASE','not_committed' if issued else 'not_started')
        except Exception as error:
            known_rollback = getattr(error,'sqlstate',None) in ('23503','23505','23514')
            failure=('DATABASE','not_committed' if known_rollback and not completed else 'may_have_committed')
        raise PolicyPersistenceError(*failure) from None
