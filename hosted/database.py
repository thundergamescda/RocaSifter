"""PostgreSQL transactions, tenant scope and versioned schema installation."""
from contextlib import contextmanager
from pathlib import Path
import os
import re
import threading
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


def _tls_mode(url):
    """Preserve explicit URL > environment TLS policy; never weaken verification.

    A service can override environment defaults, so an opaque service requires
    an explicit URL mode. Nested database conninfo is unsupported, not expanded.
    Otherwise require encryption for the private pilot; system trust retains
    libpq's verify-full default. Require alone does not authenticate a hostname.
    Trust paths and all other connection parameters remain libpq-owned.
    """
    if not isinstance(url,str):raise ValueError('Invalid database connection configuration.')
    try:parameters=conninfo_to_dict(url)
    except (ProgrammingError,TypeError,ValueError):
        raise ValueError('Invalid database connection configuration.') from None
    dbname=parameters.get('dbname') or os.environ.get('PGDATABASE','')
    if '=' in dbname or dbname.startswith(('postgres://','postgresql://')):
        raise ValueError('Nested database connection configuration is not supported.')
    mode=parameters.get('sslmode')
    if mode is None:
        if parameters.get('service') or os.environ.get('PGSERVICE'):
            raise ValueError('Database services require an explicit TLS mode in the connection string.')
        mode=os.environ.get('PGSSLMODE')
        if mode is None:
            root=parameters.get('sslrootcert') or os.environ.get('PGSSLROOTCERT')
            mode='verify-full' if root=='system' else 'require'
    if mode not in ('require','verify-ca','verify-full'):
        raise ValueError('Database TLS mode must be require, verify-ca or verify-full.')
    return mode


class Database:
    def __init__(self,url,*,max_size=5,schema=None):
        kwargs={'row_factory':dict_row,'connect_timeout':10,'sslmode':_tls_mode(url)}
        if schema is not None:
            if not re.fullmatch(r'ss_test_[a-f0-9]{32}',schema):raise ValueError('Invalid isolated test schema.')
            kwargs['options']='-c search_path='+schema+',pg_catalog'
        # Create worker threads in the serving process, not during WSGI import.
        # An eagerly opened pool can be inherited across a prefork server start
        # with no functioning maintenance threads in the child.
        self._url,self._kwargs,self._max_size=url,kwargs,max_size
        self._pool=None
        self._pid=os.getpid()
        self._pool_lock=threading.Lock()

    @property
    def pool(self):
        pid=os.getpid()
        if self._pid!=pid:
            self._pid=pid;self._pool=None;self._pool_lock=threading.Lock()
        with self._pool_lock:
            if self._pool is None:
                pool=ConnectionPool(self._url,min_size=1,max_size=self._max_size,
                                    open=False,timeout=10,kwargs=self._kwargs)
                try:pool.open(wait=True,timeout=10)
                except Exception:
                    pool.close();raise
                self._pool=pool
            return self._pool

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
            return db.execute('SELECT version FROM ss_schema_versions WHERE version=15').fetchone() is not None

    def close(self):
        if self._pool is not None and self._pid==os.getpid():self._pool.close()


def audit(db,tenant,staff,action,entity,details=None):
    db.execute('INSERT INTO ss_audit(tenant_id,staff_id,action,entity_id,details) VALUES (%s,%s,%s,%s,%s)',
               (tenant,staff,action,str(entity),Jsonb(details or {})))
