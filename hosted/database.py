"""PostgreSQL transactions, tenant scope and versioned schema installation."""
from contextlib import contextmanager
from pathlib import Path
import re
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self,url,*,max_size=5,schema=None):
        kwargs={'row_factory':dict_row,'connect_timeout':10,'sslmode':'require'}
        if schema is not None:
            if not re.fullmatch(r'ss_test_[a-f0-9]{32}',schema):raise ValueError('Invalid isolated test schema.')
            kwargs['options']='-c search_path='+schema+',pg_catalog'
        self.pool=ConnectionPool(url,min_size=0,max_size=max_size,open=True,
                                timeout=10,kwargs=kwargs)

    @contextmanager
    def transaction(self):
        with self.pool.connection() as db:
            with db.transaction():yield db

    def migrate(self):
        with self.transaction() as db:
            db.execute('SELECT pg_advisory_xact_lock(81726001)')
            db.execute((Path(__file__).parent/'schema.sql').read_text(),prepare=False)

    def ready(self):
        with self.transaction() as db:
            return db.execute('SELECT version FROM ss_schema_versions WHERE version=1').fetchone() is not None

    def close(self):self.pool.close()


def audit(db,tenant,staff,action,entity,details=None):
    db.execute('INSERT INTO ss_audit(tenant_id,staff_id,action,entity_id,details) VALUES (%s,%s,%s,%s,%s)',
               (tenant,staff,action,str(entity),Jsonb(details or {})))
