"""New scripted-catalog unit definitions, not PostgreSQL DDL acceptance.

No old case bodies, database driver, server, fixture or test runner is reused.
Catalog fixtures encode declared source facts; real PG15..18 catalog output,
DDL guards, native constraints and concurrency remain separately UNRUN.
"""
import copy
from dataclasses import FrozenInstanceError
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from hosted import intake_label_schema as schema


def catalog(major=18, *, present=True, name='public'):
    context = [{'schema_oid':99,'schema_name':name,'timezone':'UTC','server_version_num':major*10000,
                'date_style':'ISO, MDY','interval_style':'postgres','replication_role':'origin',
                'server_encoding':'UTF8','client_encoding':'UTF8'}]
    objects = []; columns = []; constraints = []; indexes = []; functions = []; triggers = []; ri = []
    base_keys = {'ss_schema_versions':('version',),'ss_tenants':('id',),
                 'ss_staff':('tenant_id','id'),'ss_receiving_operations':('tenant_id','id')}
    oid_map = {}
    def obj(n, ident, kind='r'):
        oid_map[n] = ident
        objects.append({'oid':ident,'name':n,'kind':kind,'persistence':'p','partitioned':False,'rls':False,'force_rls':False,
                        'has_rules':False,'has_inheritance':False})
    def col(table, n, typ, nullable, position):
        return {'table_name':table,'position':position,'name':n,'type_name':typ,'not_null':not nullable,
                'type_schema':'pg_catalog','type_kind':'b','type_modifier':-1,
                'dropped':False,'generated':'','identity':'','has_default':False,'local':True,'inheritance_count':0,
                'collation':'pg_catalog.default' if typ=='text' else '', 'deterministic_collation':True}
    for ident, (table, keys) in enumerate(base_keys.items(), 1):
        obj(table, ident)
        for pos, key in enumerate(keys, 1):
            columns.append(col(table,key,'integer' if key=='version' else 'uuid',False,pos))
    for ident, spec in enumerate(schema.SCHEMA_SPEC, 100):
        if present:
            obj(spec.name,ident)
    next_index = 200
    if present:
        for spec in schema.SCHEMA_SPEC:
            for index in spec.indexes:
                obj(index.name,next_index,'i'); next_index += 1
    next_constraint = 500
    def con(table, n, kind, cols, *, ref=None, ref_cols=(), expression=None, index_oid=0):
        nonlocal next_constraint
        next_constraint += 1
        return {'oid':next_constraint,'table_name':table,'name':n,'kind':kind,'columns':list(cols),
            'ref_schema':name if ref else None,'ref_table':ref,'ref_oid':oid_map[ref] if ref else 0,
            'ref_columns':list(ref_cols),'deferrable':False,'deferred':False,'validated':True,
            'update_action':'r' if ref else ' ','delete_action':'r' if ref else ' ','match_type':'s' if ref else ' ',
            'local':True,'inheritance_count':0,'no_inherit':kind in ('p','u','f'), 'parent_oid':0,
            'index_oid':index_oid,'enforced':True if major==18 else None,'period':False if major==18 else None,
            'supporting_index_valid':True if kind in ('p','u','f') else None,
            'check_definition':'CHECK ('+expression+')' if kind=='c' else None,'builtin_dependencies':True}
    for table, keys in base_keys.items():
        constraints.append(con(table,table+'_required_key','p',keys,index_oid=9000+oid_map[table]))
    if present:
        for spec in schema.SCHEMA_SPEC:
            for pos, column in enumerate(spec.columns, 1):
                columns.append(col(spec.name,column.name,column.type_name,column.nullable,pos))
            definitions = spec.constraints + (schema.NOT_NULL_SPEC[spec.name] if major==18 else ())
            for constraint in definitions:
                cols = constraint.columns
                if constraint.kind=='c':
                    cols = tuple(c.name for c in spec.columns if re.search(r'\b'+re.escape(c.name)+r'\b',constraint.expression))
                index_oid = oid_map[constraint.name] if constraint.kind in ('p','u') else 9900 if constraint.kind=='f' else 0
                constraints.append(con(spec.name,constraint.name,constraint.kind,cols,ref=constraint.ref_table,
                    ref_cols=constraint.ref_columns,expression=constraint.expression,index_oid=index_oid))
            types = {column.name:column.type_name for column in spec.columns}
            for index in spec.indexes:
                indexes.append({'table_name':spec.name,'name':index.name,'method':'btree','unique':index.unique,
                    'primary':index.primary,'immediate':True,'valid':True,'ready':True,'live':True,
                    'nulls_not_distinct':False,'key_count':len(index.columns),'total_count':len(index.columns),
                    'has_expression':False,'has_predicate':False,'columns':list(index.columns),
                    'opclasses':[schema._OPCLASSES[types[n]] for n in index.columns],
                    'collations':['pg_catalog.default' if types[n]=='text' else '' for n in index.columns],
                    'options':[0]*len(index.columns)})
        for ident, spec in enumerate(schema.SCHEMA_SPEC, 400):
            function = spec.name+'_guard_v9'
            functions.append({'oid':ident,'name':function,'language':'plpgsql','returns':'trigger',
                'return_schema':'pg_catalog','return_type_name':'trigger','nargs':0,
                'returns_set':False,'security_definer':False,'leakproof':False,'strict':False,'volatility':'v',
                'parallel':'u','kind':'f','config':['search_path=pg_catalog'],'cost':100.0,'rows':0.0,'plain_args':True,
                'source':spec.guard_source.replace('__SS_LABEL_SCHEMA__','"'+name+'"'),
                'source_bounded':True,'unexpected_acl':False})
            triggers.append({'table_name':spec.name,'name':spec.name+'_guard_trg_v9','function_name':function,
                'function_schema':name,'function_oid':ident,'type':31,'enabled':'O','nargs':0,'args':'',
                'has_when':False,'constraint_oid':0,'deferrable':False,'deferred':False})
        for constraint in constraints:
            if constraint['table_name'] not in schema.TABLES or constraint['kind']!='f':
                continue
            variants = ((oid_map[constraint['table_name']],'RI_FKey_check_ins',5),
                        (oid_map[constraint['table_name']],'RI_FKey_check_upd',17),
                        (constraint['ref_oid'],'RI_FKey_restrict_del',9),
                        (constraint['ref_oid'],'RI_FKey_restrict_upd',17))
            for table_oid, function, kind in variants:
                ri.append({'constraint_oid':constraint['oid'],'table_oid':table_oid,'function_name':function,
                    'function_schema':'pg_catalog','type':kind,'enabled':'O','nargs':0,'args':'','has_when':False,
                    'deferrable':False,'deferred':False})
    columns.sort(key=lambda row:(row['table_name'],row['position']))
    return {'context':context,'objects':objects,'columns':columns,'constraints':constraints,
            'versions':[{'version':8},{'version':16}] if present else [{'version':8}],
            'indexes':indexes,'functions':functions,'triggers':triggers,'ri_triggers':ri}


class Rows:
    def __init__(self, values):
        self.values = values
    def fetchall(self):
        return copy.deepcopy(self.values)


class ScriptedCatalog:
    autocommit = False
    def __init__(self, metadata=None, *, major=18, present=True, name='public'):
        self.major,self.name = major,name
        self.metadata = metadata if metadata is not None else catalog(major,present=present,name=name)
        self.initial = copy.deepcopy(self.metadata)
        self.after_install = None
        self.queries = []; self.invalid_queries = []; self.ddl = []; self.controls = []
        self.ddl_failure = False; self.driver_failure = False; self.rollback_failure = False
    def execute(self, query, params=(), *, prepare=None):
        self.queries.append((query,params,prepare))
        if self.driver_failure:
            self.driver_failure = False
            raise RuntimeError('Fictional private driver detail; never public.')
        if prepare is False:
            self.ddl.append(query)
            if self.ddl_failure:
                raise RuntimeError('Fictional DDL error; no real SQL ran.')
            self.metadata = copy.deepcopy(self.after_install if self.after_install is not None else catalog(self.major,name=self.name))
            return Rows([])
        if query.startswith('/* ss_labels:'):
            tag = query.split('/* ss_labels:',1)[1].split(' */',1)[0]
            known = {'context','objects','columns','constraints','versions','indexes','functions','triggers','ri_triggers'}
            if tag not in known:
                self.invalid_queries.append(query); raise AssertionError('Unexpected catalog query.')
            if tag=='constraints':
                expected = schema._CONSTRAINTS_SQL_18 if self.major==18 else schema._CONSTRAINTS_SQL
                if query != expected:
                    self.invalid_queries.append(query); raise AssertionError('Wrong version-specific catalog query.')
            return Rows(self.metadata[tag])
        allowed = ('SELECT pg_catalog.pg_advisory_xact_lock(81726001)',
                   'SAVEPOINT ss_intake_label_install_v9','ROLLBACK TO SAVEPOINT ss_intake_label_install_v9',
                   'RELEASE SAVEPOINT ss_intake_label_install_v9')
        if query in allowed:
            self.controls.append(query)
            if query.startswith('ROLLBACK'):
                if self.rollback_failure:
                    raise RuntimeError('Fictional disconnected transaction.')
                self.metadata = copy.deepcopy(self.initial)
            return Rows([])
        self.invalid_queries.append(query)
        raise AssertionError('Unexpected statement in scripted metadata fixture.')


def family_constraints(metadata, kind=None):
    return [row for row in metadata['constraints'] if row['table_name'] in schema.TABLES and (kind is None or row['kind']==kind)]


class IntakeLabelSchemaMetadataTests(unittest.TestCase):
    def assertDenied(self, db, code='INTAKE_LABEL_SCHEMA_INVALID'):
        with self.assertRaises(schema.LabelSchemaError) as denied:
            schema.inspect(db)
        self.assertEqual(denied.exception.code,code)
        self.assertEqual(str(denied.exception),'Shared label schema requires reviewed operator setup or recovery.')
        self.assertIsNone(denied.exception.__cause__); self.assertIsNone(denied.exception.__context__)
        self.assertEqual(db.invalid_queries,[]); self.assertEqual(db.ddl,[]); self.assertEqual(db.controls,[])
    def test_absent_family_is_read_only_and_never_reads_bundled_sql(self):
        db = ScriptedCatalog(present=False)
        with patch.object(schema.Path,'read_bytes',side_effect=AssertionError('Unexpected file IO.')):
            state = schema.inspect(db)
        self.assertEqual(state,schema.SchemaState(9,'absent',0,False))
        self.assertEqual(db.ddl,[]); self.assertEqual(db.controls,[]); self.assertEqual(db.invalid_queries,[])
    def test_exact_separate_pg15_16_17_catalog_layouts_attest_without_native_n_rows(self):
        for major in (15,16,17):
            db = ScriptedCatalog(major=major)
            self.assertEqual(len(family_constraints(db.metadata)),59)
            self.assertFalse(family_constraints(db.metadata,'n'))
            state = schema.inspect(db)
            self.assertEqual(state,schema.SchemaState(9,'present',5,False))
            self.assertEqual(db.ddl,[]); self.assertEqual(db.controls,[]); self.assertEqual(db.invalid_queries,[])
    def test_pg18_requires_exact_131_constraints_with_74_explicit_native_names(self):
        # Preserve the original direct ID; successor adds two CHECKs/two native NN rows.
        db = ScriptedCatalog()
        self.assertEqual(len(family_constraints(db.metadata)),135)
        self.assertEqual(len(family_constraints(db.metadata,'n')),76)
        self.assertEqual(len(db.metadata['ri_triggers']),56)
        self.assertEqual(schema.inspect(db),schema.SchemaState(9,'present',5,False))
        self.assertEqual(db.invalid_queries,[])
    def test_partial_and_marker_only_or_detached_families_never_become_absence(self):
        variants = []
        m = catalog(present=False); m['versions'].append({'version':16}); variants.append(m)
        m = catalog(); m['versions'] = [{'version':8}]; variants.append(m)
        m = catalog(); m['objects'] = [r for r in m['objects'] if r['name']!=schema.TABLES[0]]; variants.append(m)
        for metadata in variants:
            self.assertDenied(ScriptedCatalog(metadata),'INTAKE_LABEL_SCHEMA_PARTIAL')
    def test_unknown_alias_or_colliding_family_namespace_is_denied(self):
        for name in ('SS_INTAKE_LABEL_INPUTS','ss_intake_label_extra'):
            m = catalog(); extra = dict(m['objects'][4],name=name,oid=7000); m['objects'].append(extra)
            self.assertDenied(ScriptedCatalog(m),'INTAKE_LABEL_SCHEMA_PARTIAL')
        m = catalog(); m['functions'].append(copy.deepcopy(m['functions'][0]))
        self.assertDenied(ScriptedCatalog(m))
        for key in ('has_rules','has_inheritance'):
            m = catalog(); next(r for r in m['objects'] if r['name']==schema.TABLES[0])[key]=True
            self.assertDenied(ScriptedCatalog(m))
    def test_unsupported_schema_version_and_noncanonical_format_context_fail_closed(self):
        for key,value in (('schema_name','unreviewed'),('schema_name','ss_test_'+'A'*32),
                          ('server_version_num',140000),('server_version_num',190000),('server_version_num',True),
                          ('timezone','America/Los_Angeles'),('date_style','SQL, DMY'),('interval_style','iso_8601'),
                          ('replication_role','replica'),('replication_role','local')):
            m = catalog(); m['context'][0][key]=value
            self.assertDenied(ScriptedCatalog(m))
    def test_isolated_schema_qualified_guards_and_ref_oids_must_match(self):
        name = 'ss_test_'+'a'*32
        db = ScriptedCatalog(name=name)
        self.assertEqual(schema.inspect(db).state,'present')
        for key,value in (('function_schema','public'),('function_oid',999999)):
            m = catalog(name=name); m['triggers'][0][key]=value
            self.assertDenied(ScriptedCatalog(m,name=name))
        m = catalog(name=name); family_constraints(m,'f')[0]['ref_schema']='public'
        self.assertDenied(ScriptedCatalog(m,name=name))
    def test_base_schema_marker_and_required_key_facts_are_required(self):
        m = catalog(); m['versions']=[{'version':16}]
        self.assertDenied(ScriptedCatalog(m),'INTAKE_LABEL_SCHEMA_BASE_REQUIRED')
        m = catalog(); m['constraints']=[r for r in m['constraints'] if r['table_name']!='ss_staff']
        self.assertDenied(ScriptedCatalog(m),'INTAKE_LABEL_SCHEMA_BASE_REQUIRED')
    def test_exact_column_order_type_null_default_and_collation_are_attested(self):
        for key,value in (('position',999),('type_name','character varying'),('not_null',False),
                          ('has_default',True),('generated','s'),('identity','a'),('dropped',True),
                          ('collation','pg_catalog.C'),('deterministic_collation',False),('local',False),('inheritance_count',1),
                          ('type_schema','public'),('type_kind','d'),('type_modifier',5)):
            m = catalog(); row = next(r for r in m['columns'] if r['table_name']==schema.TABLES[0] and r['name']=='plan_hash')
            row[key]=value; self.assertDenied(ScriptedCatalog(m))
        for table in ('ss_intake_label_previews','ss_intake_label_requests'):
            for mutation in ('missing','nullable','type','position'):
                m = catalog(); row = next(r for r in m['columns'] if r['table_name']==table and r['name']=='expected_publication_revision')
                if mutation=='missing': m['columns'].remove(row)
                elif mutation=='nullable': row['not_null']=False
                elif mutation=='type': row['type_name']='integer'
                else: row['position']=1
                self.assertDenied(ScriptedCatalog(m))
    def test_missing_changed_date_check_and_literal_case_are_not_accepted(self):
        for mutation in ('missing','infinite','literal'):
            m = catalog(); rows = family_constraints(m,'c')
            if mutation=='missing':
                target = next(r for r in rows if r['name'].endswith('_dates_ck_v9')); m['constraints'].remove(target)
            elif mutation=='infinite':
                target = next(r for r in rows if r['name'].endswith('_dates_ck_v9'))
                target['check_definition']=target['check_definition'].replace('isfinite','not_isfinite',1)
            else:
                target = next(r for r in rows if r['name'].endswith('_lifecycle_ck_v9'))
                target['check_definition']=target['check_definition'].replace("'active'","'Active'",1)
            self.assertDenied(ScriptedCatalog(m))
        for table in ('ss_intake_label_previews','ss_intake_label_requests'):
            for mutation in ('missing','nonpositive'):
                m = catalog(); row = next(r for r in family_constraints(m,'c') if r['table_name']==table and r['name'].endswith('_publication_revision_ck_v9'))
                if mutation=='missing': m['constraints'].remove(row)
                else:
                    changed = row['check_definition'].replace('>= 1','>= 0')
                    self.assertNotEqual(changed,row['check_definition']); row['check_definition']=changed
                self.assertDenied(ScriptedCatalog(m))
    def test_deferrable_unvalidated_parent_or_wrong_noinherit_constraints_deny(self):
        for key,value in (('deferrable',True),('deferred',True),('validated',False),('parent_oid',7),
                          ('inheritance_count',1),('local',False),('no_inherit',False)):
            m = catalog(); family_constraints(m,'f')[0][key]=value
            self.assertDenied(ScriptedCatalog(m))
        m = catalog(); family_constraints(m,'c')[0]['no_inherit']=True
        self.assertDenied(ScriptedCatalog(m))
    def test_grouped_tenant_fk_actions_reference_and_index_binding_deny_drift(self):
        for key,value in (('columns',['receiving_id','tenant_id']),('ref_columns',['id','tenant_id']),
                          ('update_action','a'),('delete_action','c'),('match_type','f'),('ref_oid',9999),
                          ('supporting_index_valid',False)):
            m = catalog(); family_constraints(m,'f')[0][key]=value
            self.assertDenied(ScriptedCatalog(m))
        m = catalog(); family_constraints(m,'p')[0]['index_oid']=9999
        self.assertDenied(ScriptedCatalog(m))
        for key in ('columns','ref_columns'):
            m = catalog(); row = next(r for r in family_constraints(m,'f') if r['name']=='ss_intake_label_requests_preview_fk_v9')
            self.assertEqual(row[key][-1],'expected_publication_revision')
            row[key]=row[key][:-1]
            self.assertDenied(ScriptedCatalog(m))
        m = catalog(); row = next(r for r in family_constraints(m,'u') if r['name']=='ss_intake_label_previews_request_identity_uq_v9')
        self.assertEqual(row['columns'][-1],'expected_publication_revision')
        row['columns']=row['columns'][:-1]
        self.assertDenied(ScriptedCatalog(m))
    def test_pg18_native_notnull_names_columns_and_flags_deny_even_when_attnotnull_true(self):
        for mutation in ('missing','rename','column','enforced','validated','noinherit'):
            m = catalog(); nn = family_constraints(m,'n')[0]
            original_column = nn['columns'][0]
            self.assertTrue(next(r for r in m['columns'] if r['table_name']==nn['table_name'] and r['name']==original_column)['not_null'])
            if mutation=='missing': m['constraints'].remove(nn)
            elif mutation=='rename': nn['name']+='wrong'
            elif mutation=='column': nn['columns']=['id'] if original_column!='id' else ['tenant_id']
            elif mutation=='enforced': nn['enforced']=False
            elif mutation=='validated': nn['validated']=False
            else: nn['no_inherit']=True
            self.assertDenied(ScriptedCatalog(m))
        for table in ('ss_intake_label_previews','ss_intake_label_requests'):
            for mutation in ('missing','rename','column','enforced','validated','noinherit'):
                m = catalog(); nn = next(r for r in family_constraints(m,'n') if r['table_name']==table and r['columns']==['expected_publication_revision'])
                self.assertEqual(nn['name'],table+'_expected_publication_revision_nn_v9')
                self.assertTrue(next(r for r in m['columns'] if r['table_name']==table and r['name']=='expected_publication_revision')['not_null'])
                if mutation=='missing': m['constraints'].remove(nn)
                elif mutation=='rename': nn['name']+='wrong'
                elif mutation=='column': nn['columns']=['revision'] if table.endswith('previews') else ['created_at']
                elif mutation=='enforced': nn['enforced']=False
                elif mutation=='validated': nn['validated']=False
                else: nn['no_inherit']=True
                self.assertDenied(ScriptedCatalog(m))
    def test_pg18_check_fk_enforcement_and_temporal_flags_are_explicit(self):
        for kind in ('c','f','p','u'):
            for key,value in (('enforced',False),('period',True)):
                m = catalog(); family_constraints(m,kind)[0][key]=value
                self.assertDenied(ScriptedCatalog(m))
    def test_native_notnull_or_fabricated_enforcement_cannot_be_legacy_fallback(self):
        m = catalog(17); nn = copy.deepcopy(family_constraints(catalog(18),'n')[0]); m['constraints'].append(nn)
        self.assertDenied(ScriptedCatalog(m,major=17))
        m = catalog(17); family_constraints(m,'c')[0]['enforced']=True
        self.assertDenied(ScriptedCatalog(m,major=17))
        m = catalog(); m['constraints']=[r for r in m['constraints'] if r['kind']!='n']
        self.assertDenied(ScriptedCatalog(m))
    def test_missing_disabled_or_misbound_native_ri_trigger_denies_enforcement_claim(self):
        for mutation in ('missing','disabled','wrong_function','wrong_relation','deferred'):
            m = catalog(); row = m['ri_triggers'][0]
            if mutation=='missing': m['ri_triggers'].pop()
            elif mutation=='disabled': row['enabled']='D'
            elif mutation=='wrong_function': row['function_name']='RI_FKey_noaction_del'
            elif mutation=='wrong_relation': row['table_oid']=9000
            else: row['deferrable']=True
            self.assertDenied(ScriptedCatalog(m))
    def test_index_full_key_opclass_predicate_and_validity_are_checked(self):
        for key,value in (('method','hash'),('valid',False),('immediate',False),('ready',False),('live',False),
                          ('nulls_not_distinct',True),('has_predicate',True),('has_expression',True),
                          ('columns',['id']),('opclasses',['pg_catalog.text_ops']),('options',[1]),('total_count',99)):
            m = catalog(); m['indexes'][0][key]=value
            self.assertDenied(ScriptedCatalog(m))
        m = catalog(); row = next(r for r in m['indexes'] if r['name']=='ss_intake_label_previews_request_identity_uq_v9')
        self.assertEqual(row['columns'][-1],'expected_publication_revision')
        row['columns']=row['columns'][:-1]
        self.assertDenied(ScriptedCatalog(m))
    def test_guard_source_security_path_acl_and_trigger_bindings_are_checked(self):
        for key,value in (('source','BEGIN RETURN NEW; END;'),('security_definer',True),('config',None),
                          ('unexpected_acl',True),('source_bounded',False),('volatility','i'),('plain_args',False),
                          ('return_schema','public'),('return_type_name','opaque')):
            m = catalog(); m['functions'][0][key]=value
            self.assertDenied(ScriptedCatalog(m))
        for key,value in (('enabled','D'),('type',29),('nargs',1),('has_when',True),('args','ff'),('constraint_oid',7)):
            m = catalog(); m['triggers'][0][key]=value
            self.assertDenied(ScriptedCatalog(m))
        for function,needle,replacement in (
            ('ss_intake_label_publications_guard_v9','OR request_basis.expected_publication_revision IS DISTINCT FROM OLD.revision','OR FALSE'),
            ('ss_intake_label_publications_guard_v9','OR preview_basis.expected_publication_revision IS DISTINCT FROM OLD.revision','OR FALSE'),
            ('ss_intake_label_publications_guard_v9','OR preview_basis.expires_at <= pg_catalog.clock_timestamp()','OR FALSE'),
            ('ss_intake_label_publications_guard_v9','OR OLD.retention_until <= pg_catalog.clock_timestamp()','OR FALSE'),
            ('ss_intake_label_previews_guard_v9','IF NEW.expected_publication_revision IS DISTINCT FROM current_publication_revision','IF FALSE'),
            ('ss_intake_label_requests_guard_v9','OR NEW.expected_publication_revision IS DISTINCT FROM quote.expected_publication_revision','OR FALSE')):
            m = catalog(); row = next(r for r in m['functions'] if r['name']==function)
            changed = row['source'].replace(needle,replacement)
            self.assertNotEqual(changed,row['source']); row['source']=changed
            self.assertDenied(ScriptedCatalog(m))
    def test_wrong_stored_function_namespace_or_nonbuiltin_check_dependency_denies(self):
        m = catalog(); m['functions'][0]['source']=m['functions'][0]['source'].replace('"public"','"ss_test_'+'b'*32+'"')
        self.assertDenied(ScriptedCatalog(m))
        m = catalog(); family_constraints(m,'c')[0]['builtin_dependencies']=False
        self.assertDenied(ScriptedCatalog(m))
    def test_schema_spec_and_raw_orders_are_frozen_and_lifecycle_never_enabled(self):
        self.assertEqual(tuple(len(schema.RAW_FIELDS[t]) for t in schema.TABLES),(20,19,27,26,13))
        with self.assertRaises(TypeError): schema.RAW_FIELDS[schema.TABLES[0]]=()
        with self.assertRaises(FrozenInstanceError): schema.SCHEMA_SPEC[0].name='changed'
        state = schema.inspect(ScriptedCatalog())
        self.assertIs(state.operator_lifecycle_available,False)
        with self.assertRaises(FrozenInstanceError): state.operator_lifecycle_available=True
    def test_driver_failure_has_fixed_owned_error_without_cause_or_context(self):
        db = ScriptedCatalog(); db.driver_failure=True
        self.assertDenied(db)
        self.assertFalse(db.driver_failure)
        self.assertEqual(len(db.queries),1)


class IntakeLabelSchemaInstallBoundaryTests(unittest.TestCase):
    def test_exact_existing_family_is_unchanged_and_never_reads_or_executes_ddl(self):
        db = ScriptedCatalog()
        with patch.object(schema.Path,'read_bytes',side_effect=AssertionError('Unexpected source read.')):
            self.assertEqual(schema.install(db),schema.SchemaState(9,'present',5,False))
        self.assertEqual(db.ddl,[]); self.assertEqual(db.invalid_queries,[])
        self.assertEqual(db.controls,['SELECT pg_catalog.pg_advisory_xact_lock(81726001)'])
    def test_autocommit_denial_precedes_all_queries_files_or_ddl(self):
        db = ScriptedCatalog(present=False); db.autocommit=True
        with patch.object(schema.Path,'read_bytes',side_effect=AssertionError('Unexpected source read.')):
            with self.assertRaises(schema.LabelSchemaError) as denied: schema.install(db)
        self.assertEqual(denied.exception.code,'INTAKE_LABEL_SCHEMA_TRANSACTION_REQUIRED')
        self.assertEqual(db.queries,[]); self.assertEqual(db.ddl,[])
    def test_partial_or_orphan_marker_denial_never_reads_sql_or_repairs(self):
        for mutation in ('marker','partial'):
            m = catalog(present=False)
            if mutation=='marker': m['versions'].append({'version':16})
            else: m['objects'].append(copy.deepcopy(catalog()['objects'][4]))
            db = ScriptedCatalog(m,present=False)
            with patch.object(schema.Path,'read_bytes',side_effect=AssertionError('Unexpected source read.')):
                with self.assertRaises(schema.LabelSchemaError): schema.install(db)
            self.assertEqual(db.ddl,[]); self.assertEqual(db.invalid_queries,[])
            self.assertNotIn('SAVEPOINT ss_intake_label_install_v9',db.controls)
    def test_absent_install_uses_exact_bundled_bytes_token_binding_and_post_attestation(self):
        for major in (15,18):
            name = 'ss_test_'+'c'*32
            db = ScriptedCatalog(major=major,present=False,name=name)
            state = schema.install(db)
            self.assertEqual(state,schema.SchemaState(9,'present',5,False))
            raw = Path(schema.__file__).with_name('intake_label_schema.sql').read_bytes()
            self.assertEqual(db.ddl,[raw.decode('utf-8').replace('__SS_LABEL_SCHEMA__','"'+name+'"')])
            self.assertEqual(db.invalid_queries,[])
            self.assertEqual(db.controls,['SELECT pg_catalog.pg_advisory_xact_lock(81726001)',
                'SAVEPOINT ss_intake_label_install_v9','RELEASE SAVEPOINT ss_intake_label_install_v9'])
    def test_changed_bundle_fails_before_savepoint_or_ddl(self):
        db = ScriptedCatalog(present=False)
        with patch.object(schema.Path,'read_bytes',return_value=b'fictional changed source'):
            with self.assertRaises(schema.LabelSchemaError) as denied: schema.install(db)
        self.assertEqual(denied.exception.code,'INTAKE_LABEL_SCHEMA_INSTALL_SOURCE_CHANGED')
        self.assertEqual(db.ddl,[]); self.assertNotIn('SAVEPOINT ss_intake_label_install_v9',db.controls)
    def test_ddl_or_post_attestation_failure_rolls_back_only_attempt_and_never_returns_present(self):
        for mode in ('ddl','post'):
            db = ScriptedCatalog(present=False)
            if mode=='ddl': db.ddl_failure=True
            else:
                db.after_install=catalog(); db.after_install['triggers'][0]['enabled']='D'
            with self.assertRaises(schema.LabelSchemaError) as denied: schema.install(db)
            self.assertEqual(denied.exception.code,'INTAKE_LABEL_SCHEMA_INSTALL_FAILED')
            self.assertIsNone(denied.exception.__cause__); self.assertIsNone(denied.exception.__context__)
            self.assertEqual(len(db.ddl),1); self.assertEqual(db.invalid_queries,[])
            self.assertIn('ROLLBACK TO SAVEPOINT ss_intake_label_install_v9',db.controls)
            self.assertEqual(db.metadata,db.initial)


FUTURE_REAL_PG_ACCEPTANCE = (
    'Install from base8 on each supported major; compare actual59 versus18native135 catalog facts and all76 explicit NN names.',
    'Exercise ordinary insert/update/delete:sharedinput/job/request immutability,no lifecycle/operator bypass,tenant/grouped FK and exact range/reprint/pair/fence guards.',
    'Reject every required/nullable timestamp infinity/-infinity/BCE/year10000; accept valid UTC domain endpoints with actual driver-safe reads.',
    'Alter native NN name,column,enforcement/validation/inheritance and CHECK/FK NOTENFORCED while preserving other metadata; exact inspector must deny.',
    'Disable/misbind a native RI trigger or guard,alter supporting index,source,path/ACL and prove real DB writes/inspection fail closed.',
    'Prove transactional DDL/post-attestation rollback and concurrent installs under81726001; no partial repair and no inherited unit/provider acceptance.',
    'Retained accepted resume A/B keys cannot alternate for later blocked cycles before or after their original quote expiry; captured publication revision denies each old key.',
    'A valid inserted initial request delayed beyond ready-preview expiry cannot queue a still-planned publication; rollback request+queue transaction on failed consumption.',
    'An old checking preview cannot become ready after publication revision changes or cycles to the same state; reject mismatched prepared revision without rewriting it.',
    'A fresh explicit matching resume quote/request at the current publication revision succeeds once with live retention and ordered caller locks; next cycle requires another fresh approval.',
    'Published exact reprint keeps the same sealed pair/generation/current publication revision and performs no queue/render/publication mutation.',
    'Identical-key saved-result replay after original quote expiry reads only under current disclosure/source/input/policy authority and authorizes no new work or file delivery.',
)

if __name__=='__main__':
    unittest.main()

