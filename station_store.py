"""Local persistence, credential vault, and admin authentication for Roca Station."""
import hashlib
import copy
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager


class StationError(ValueError):
    pass


def now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def private_write(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class Store:
    SUMMARY_VERSION = 3
    SUMMARY_FIELDS = frozenset(('id', 'title', 'source_name', 'mode', 'created', 'updated', 'revision',
        'inventory_status', 'inventory_intent', 'prepared', 'pickup_pending', 'pricing_game', 'mixed_games',
        'requires_purchase', 'cash_percent', 'credit_percent', 'channel', 'storage_strategy', 'storage_location', 'error'))
    PURCHASE_FIELDS = frozenset(('type', 'amount', 'at', 'intake_total', 'scope'))
    SHARED_FIELDS = frozenset(('state', 'trade_id', 'message'))
    RECEIVING_FIELDS = frozenset(('state','receiving_id','message'))

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self.path = self.root / 'station.sqlite3'
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY, source_hash TEXT, updated TEXT NOT NULL, body TEXT NOT NULL, deleted_at TEXT,
                pickup_pending INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS receipts_source ON receipts(source_hash);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT, at TEXT, kind TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS operations (
                receipt_id TEXT, key TEXT, request TEXT, response TEXT,
                PRIMARY KEY(receipt_id,key));
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, updated TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS receipt_summaries (
                receipt_id TEXT PRIMARY KEY, receipt_updated TEXT NOT NULL,
                format_version INTEGER NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS receipts_summary_insert AFTER INSERT ON receipts
                BEGIN DELETE FROM receipt_summaries WHERE receipt_id=NEW.id; END;
            CREATE TRIGGER IF NOT EXISTS receipts_summary_update AFTER UPDATE ON receipts
                BEGIN DELETE FROM receipt_summaries WHERE receipt_id=OLD.id OR receipt_id=NEW.id; END;
            CREATE TRIGGER IF NOT EXISTS receipts_summary_delete AFTER DELETE ON receipts
                BEGIN DELETE FROM receipt_summaries WHERE receipt_id=OLD.id; END;
            ''')
            if 'deleted_at' not in {row['name'] for row in db.execute('PRAGMA table_info(receipts)')}:
                db.execute('ALTER TABLE receipts ADD COLUMN deleted_at TEXT')
            if 'pickup_pending' not in {row['name'] for row in db.execute('PRAGMA table_info(receipts)')}:
                db.execute('ALTER TABLE receipts ADD COLUMN pickup_pending INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE INDEX IF NOT EXISTS receipts_pickup ON receipts(pickup_pending,deleted_at)')
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def db(self):
        with self.lock:
            connection = sqlite3.connect(self.path, timeout=15)
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))

    def receipt(self, receipt_id):
        with self.db() as db:
            row = db.execute('SELECT body FROM receipts WHERE id=?', (receipt_id,)).fetchone()
        if not row:
            raise StationError('Receipt not found.')
        return json.loads(row[0])

    def save_receipt(self, receipt, event=None):
        with self.db() as db:
            receipt['updated'] = now()
            receipt['revision'] = receipt.get('revision', 0) + 1
            body = json.dumps(receipt)
            # Derive from the exact serialized snapshot, never a mutable second view.
            summary = self._make_summary(json.loads(body))
            db.execute('INSERT OR REPLACE INTO receipts (id,source_hash,updated,body,deleted_at,pickup_pending) VALUES (?,?,?,?,?,?)',
                (receipt['id'], receipt.get('source_hash', ''), receipt['updated'], body, receipt.get('deleted_at'),
                 int(bool(receipt.get('pickup_pending')))))
            self._save_summary(db, receipt['id'], receipt['updated'], summary)
            if event:
                db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                    (receipt['id'], now(), event[0], event[1]))
        return receipt

    def receipt_compare_body(self, receipt):
        """Capture exact active bytes before detached preflight work."""
        try:
            if type(receipt) is not dict or not isinstance(receipt.get('id'),str):raise ValueError()
            with self.db() as db:
                row=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(receipt['id'],)).fetchone()
            if (not row or row['deleted_at'] is not None
                or json.dumps(json.loads(row['body']),sort_keys=True,separators=(',',':'),allow_nan=False)
                    !=json.dumps(receipt,sort_keys=True,separators=(',',':'),allow_nan=False)):
                raise ValueError()
            return row['body']
        except (ValueError,TypeError,KeyError,OverflowError,RecursionError):
            raise StationError('This batch changed. Reload it before creating its original intake request.') from None

    def save_receipt_if_current(self, receipt, expected_body, event=None):
        """CAS one prepared update and summary/audit, preserving intervening edits.

        Intended for a new intake intent after full outgoing-wire preflight.
        The caller invokes this outside OS guards. Serialization and summary
        hashing finish before the short SQLite write transaction begins.
        """
        try:
            if type(receipt) is not dict or not isinstance(expected_body,str):raise ValueError()
            previous=json.loads(expected_body)
            revision=previous.get('revision')
            if (type(previous) is not dict or type(revision) is not int or not 0<=revision<2**63-1
                or receipt.get('id')!=previous.get('id') or not isinstance(receipt.get('id'),str)
                or type(receipt.get('revision')) is not int or receipt['revision']!=revision
                or previous.get('deleted_at') is not None or receipt.get('deleted_at') is not None):raise ValueError()
            if event is not None and (not isinstance(event,(tuple,list)) or len(event)!=2 or any(not isinstance(item,str) for item in event)):
                raise ValueError()
            current=copy.deepcopy(receipt);current['updated']=now();current['revision']=revision+1
            body=json.dumps(current,allow_nan=False)
            summary=json.dumps(self._make_summary(json.loads(body)),separators=(',',':'),allow_nan=False)
            summary_digest=hashlib.sha256(summary.encode('utf-8')).hexdigest()
            source_hash=current.get('source_hash','');pickup=int(bool(current.get('pickup_pending')))
            prepared_event=tuple(event) if event is not None else None
        except (ValueError,TypeError,KeyError,OverflowError,RecursionError,AttributeError):
            raise StationError('This batch could not be prepared safely. Reload its original intake before retrying.') from None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(current['id'],)).fetchone()
            if not row or row['deleted_at'] is not None or row['body']!=expected_body:
                raise StationError('This batch changed during request preflight. Reload it; no original intake request was replaced.')
            changed=db.execute('''UPDATE receipts SET source_hash=?,updated=?,body=?,pickup_pending=?
                WHERE id=? AND body=? AND deleted_at IS NULL''',
                (source_hash,current['updated'],body,pickup,current['id'],expected_body))
            if changed.rowcount!=1:raise StationError('This batch changed before its intake intent could be saved. Reload it.')
            db.execute('INSERT OR REPLACE INTO receipt_summaries(receipt_id,receipt_updated,format_version,body,digest) VALUES (?,?,?,?,?)',
                (current['id'],current['updated'],self.SUMMARY_VERSION,summary,summary_digest))
            if prepared_event:
                db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                    (current['id'],current['updated'],prepared_event[0],prepared_event[1]))
        receipt.clear();receipt.update(current)
        return receipt

    def find_source(self, digest):
        with self.db() as db:
            row = db.execute('SELECT id FROM receipts WHERE source_hash=? ORDER BY updated DESC LIMIT 1', (digest,)).fetchone()
        return row[0] if row else None

    def record_publication_snapshot(self, receipt):
        """Read exact persisted bytes outside the short publication transaction."""
        with self.db() as db:
            row=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(receipt['id'],)).fetchone()
        same=row and json.dumps(json.loads(row['body']),sort_keys=True,separators=(',',':'),allow_nan=False)==json.dumps(receipt,sort_keys=True,separators=(',',':'),allow_nan=False)
        if not row or row['deleted_at'] is not None or not same:
            raise StationError('The original record changed while preparing its PDF. Reopen it before printing.')
        return row['body']

    def prepare_record_publication(self, receipt, filename, kind):
        """Serialize the complete merge and derived summary before OS/DB guards."""
        current=copy.deepcopy(receipt)
        current.setdefault('files',{})['buy']=filename
        current['updated']=now();current['revision']=receipt.get('revision',0)+1
        body=json.dumps(current)
        summary=json.dumps(self._make_summary(current),separators=(',',':'))
        detail='Prepared the original accepted '+kind+' record. No new acceptance, credit, payment or stock operation. '+filename
        return {'receipt':current,'body':body,'summary':summary,'summary_digest':hashlib.sha256(summary.encode()).hexdigest(),
                'filename':filename,'detail':detail}

    def commit_record_publication(self, expected_body, prepared, publish_file):
        """CAS the verified row; callback only publishes a complete unique file.

        SQLite and the filesystem are separate resources. A crash can leave an
        unlinked complete document; a previous document is never overwritten.
        """
        current=prepared['receipt']
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(current['id'],)).fetchone()
            if not row or row['deleted_at'] is not None or row['body']!=expected_body:
                raise StationError('The original record changed before its PDF could be published. Reopen it before printing.')
            changed=db.execute('UPDATE receipts SET updated=?,body=? WHERE id=? AND body=? AND deleted_at IS NULL',
                (current['updated'],prepared['body'],current['id'],expected_body))
            if changed.rowcount!=1:raise StationError('The original record changed before publication. Reopen it before printing.')
            db.execute('INSERT OR REPLACE INTO receipt_summaries(receipt_id,receipt_updated,format_version,body,digest) VALUES (?,?,?,?,?)',
                (current['id'],current['updated'],self.SUMMARY_VERSION,prepared['summary'],prepared['summary_digest']))
            db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                (current['id'],current['updated'],'hosted_agreement_printed',prepared['detail']))
            publish_file()
        return current

    def record_publication_referenced(self, receipt_id, filename, detail):
        """A failed commit is not assumed rolled back until its exact link is read."""
        with self.db() as db:
            row=db.execute("SELECT json_extract(body,'$.files.buy') AS filename FROM receipts WHERE id=?",(receipt_id,)).fetchone()
            event=db.execute("SELECT 1 FROM events WHERE receipt_id=? AND kind='hosted_agreement_printed' AND detail=? LIMIT 1",(receipt_id,detail)).fetchone()
        return bool(row and row['filename']==filename or event)

    @staticmethod
    def _original_trade_rows(db, trade_id):
        rows = db.execute("""SELECT id,body,deleted_at FROM receipts
            WHERE json_extract(body,'$.purchase.scope')='hosted'
              AND json_extract(body,'$.purchase.hosted_trade_id')=? ORDER BY id""", (trade_id,)).fetchall()
        result = []
        for row in rows:
            value = json.loads(row['body'])
            if value.get('id') != row['id'] or value.get('deleted_at') != row['deleted_at']:
                raise StationError('The saved original-trade index needs owner recovery. No replacement record was created.')
            result.append(value)
        return result

    def original_trade_receipts(self, trade_id):
        """Read candidates only; caller validates store authority and exact proof."""
        with self.db() as db:
            return self._original_trade_rows(db, trade_id)

    def commit_original_trade(self, receipt, *, service_origin, tenant_id, trade_id,
                              expected_existing_id, expected_deleted_at,
                              restore_existing, validate_existing, validate_linked=None):
        """Atomic local projection/restore, with no financial or provider operation.

        The caller holds its short session-file guard and supplies a pure existing-
        record verifier. BEGIN IMMEDIATE serializes two processes using the same
        profile. Existing ordinary mirrors participate in the lookup as well.
        """
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # The linked CSV's revision, original manifest and workflow locks
            # must be checked after this write reservation. Another process may
            # have saved it since the preview or the caller's preflight check.
            # This callback is a local, transaction-bound read; never network.
            if validate_linked is not None:
                validate_linked(db)
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS receipts_original_trade_recovery_v1
                ON receipts(json_extract(body,'$.original_trade_recovery.serviceOrigin'),
                            json_extract(body,'$.original_trade_recovery.tenantId'),
                            json_extract(body,'$.original_trade_recovery.tradeId'))
                WHERE json_extract(body,'$.original_trade_recovery.version')=1""")
            existing = self._original_trade_rows(db, trade_id)
            if len(existing) > 1:
                raise StationError('Several local records reference this original trade. An owner must resolve them before recovery; no record was overwritten.')
            if existing:
                current = existing[0]
                validate_existing(current)
                if expected_existing_id and current['id'] != expected_existing_id:
                    raise StationError('The local original-trade record changed. Preview its current record before recovery.')
                if not current.get('deleted_at'):
                    return {'receipt': current, 'reused': True, 'restored': False}
                if (restore_existing is not True or expected_existing_id != current['id']
                    or not expected_deleted_at or current['deleted_at'] != expected_deleted_at):
                    raise StationError('The original local record is deleted. Preview and explicitly confirm restoration of that same record; no second identity was created.')
                current = copy.deepcopy(current)
                current.pop('deleted_at', None)
                current['updated'] = now(); current['revision'] = current.get('revision', 0) + 1
                body = json.dumps(current)
                db.execute('UPDATE receipts SET body=?,updated=?,deleted_at=NULL WHERE id=?',
                           (body, current['updated'], current['id']))
                self._save_summary(db, current['id'], current['updated'], self._make_summary(json.loads(body)))
                db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                           (current['id'], now(), 'original_trade_restored',
                            'Explicitly restored the same local original-trade record. No agreement, credit, payment or inventory action.'))
                return {'receipt': current, 'reused': True, 'restored': True}
            if expected_existing_id or restore_existing:
                raise StationError('The selected local original-trade record is no longer available. Preview again; no replacement identity was created.')
            value = copy.deepcopy(receipt)
            marker = value.get('original_trade_recovery') or {}
            if (marker.get('version') != 1 or marker.get('serviceOrigin') != service_origin
                or marker.get('tenantId') != tenant_id or marker.get('tradeId') != trade_id):
                raise StationError('Original-trade recovery authority is inconsistent. No local record was created.')
            value['updated'] = now(); value['revision'] = 1
            body = json.dumps(value)
            db.execute('INSERT INTO receipts(id,source_hash,updated,body,deleted_at,pickup_pending) VALUES (?,?,?,?,NULL,0)',
                       (value['id'], value['source_hash'], value['updated'], body))
            self._save_summary(db, value['id'], value['updated'], self._make_summary(json.loads(body)))
            db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                       (value['id'], now(), 'original_trade_recovered',
                        'Recovered the verified original accepted agreement as a local record. No new agreement, credit, payment or inventory action.'))
            return {'receipt': value, 'reused': False, 'restored': False}

    def summaries(self):
        return self._summary_rows()

    @staticmethod
    def _scalar(value):
        return value is None or isinstance(value, (str, int, float, bool))

    @classmethod
    def _make_summary(cls, receipt):
        cards = [card for card in receipt['cards'] if card.get('include', True)]
        result = {key: receipt[key] for key in cls.SUMMARY_FIELDS if key in receipt and cls._scalar(receipt[key])}
        result.update(rows=len(cards), units=sum(card['qty'] for card in cards),
                      retail=round(sum(card['qty'] * card['final_price'] for card in cards), 2),
                      all_reviewed=bool(cards) and all(card.get('reviewed') and not card.get('price_missing') for card in cards))
        purchase = receipt.get('purchase')
        result['purchase'] = ({key: purchase[key] for key in cls.PURCHASE_FIELDS if key in purchase and cls._scalar(purchase[key])}
                              if isinstance(purchase, dict) else None)
        files = receipt.get('files', {})
        if isinstance(files, dict):
            result['files'] = {key: value for key, value in list(files.items())[:20]
                               if isinstance(key, str) and len(key) <= 64 and isinstance(value, str) and len(value) <= 512}
        shared = receipt.get('shared_trade')
        if isinstance(shared, dict):
            result['shared_trade'] = {key: shared[key][:1000] for key in cls.SHARED_FIELDS
                                     if isinstance(shared.get(key), str)}
        receiving=receipt.get('hosted_receive')
        if isinstance(receiving,dict):
            result['hosted_receive']={key:receiving[key][:1000] for key in cls.RECEIVING_FIELDS if isinstance(receiving.get(key),str)}
        return result

    @classmethod
    def _save_summary(cls, db, receipt_id, updated, summary):
        body = json.dumps(summary, separators=(',', ':'))
        hashed = hashlib.sha256(body.encode()).hexdigest()
        db.execute('INSERT OR REPLACE INTO receipt_summaries(receipt_id,receipt_updated,format_version,body,digest) VALUES (?,?,?,?,?)',
                   (receipt_id, updated, cls.SUMMARY_VERSION, body, hashed))

    @classmethod
    def _cached_summary(cls, row):
        try:
            body = row['summary_body']
            if (row['format_version'] != cls.SUMMARY_VERSION or row['receipt_updated'] != row['updated']
                    or not isinstance(body, str) or hashlib.sha256(body.encode()).hexdigest() != row['digest']):
                return None
            result = json.loads(body)
            allowed = cls.SUMMARY_FIELDS | {'rows', 'units', 'retail', 'all_reviewed', 'purchase', 'files', 'shared_trade','hosted_receive'}
            if not isinstance(result, dict) or not set(result) <= allowed or result.get('id') != row['id']:
                return None
            if type(result.get('rows')) is not int or result['rows'] < 0:
                return None
            if type(result.get('all_reviewed')) is not bool:
                return None
            for key in ('units', 'retail'):
                value = result.get(key)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    return None
            if any(not cls._scalar(result[key]) for key in cls.SUMMARY_FIELDS if key in result):
                return None
            purchase = result.get('purchase')
            if purchase is not None and (not isinstance(purchase, dict) or not set(purchase) <= cls.PURCHASE_FIELDS
                                         or any(not cls._scalar(value) for value in purchase.values())):
                return None
            files = result.get('files', {})
            if not isinstance(files, dict) or len(files) > 20 or any(not isinstance(key, str) or len(key) > 64
                or not isinstance(value, str) or len(value) > 512 for key, value in files.items()):
                return None
            shared = result.get('shared_trade', {})
            if not isinstance(shared, dict) or not set(shared) <= cls.SHARED_FIELDS or any(
                    not isinstance(value, str) or len(value) > 1000 for value in shared.values()):
                return None
            receiving=result.get('hosted_receive',{})
            if not isinstance(receiving,dict) or not set(receiving)<=cls.RECEIVING_FIELDS or any(not isinstance(value,str) or len(value)>1000 for value in receiving.values()):return None
            return result
        except (ValueError, TypeError, KeyError):
            return None

    def _summary_rows(self, pickup=False):
        where = 'r.pickup_pending=1 AND r.deleted_at IS NULL' if pickup else 'r.deleted_at IS NULL'
        order = 'r.updated,r.id' if pickup else 'r.updated DESC'
        result = []
        with self.db() as db:
            query = f'''SELECT r.id,r.updated,s.receipt_updated,s.format_version,
                                  s.body AS summary_body,s.digest FROM receipts r
                                  LEFT JOIN receipt_summaries s ON s.receipt_id=r.id
                                  WHERE {where} ORDER BY {order} LIMIT 150'''
            rows = db.execute(query).fetchall()
            cached = [self._cached_summary(row) for row in rows]
            if all(summary is not None for summary in cached):
                return cached
            # Warm polls need no writer lock. Repair re-reads under an immediate
            # transaction so another process cannot pair a new body with an old
            # stamp after the initial read. Only derived rows are written.
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute(query).fetchall()
            for row in rows:
                summary = self._cached_summary(row)
                if summary is None:
                    # Only one original payload is materialized at a time. The
                    # repair never updates original receipts, events or journals.
                    original = db.execute('SELECT body FROM receipts WHERE id=?', (row['id'],)).fetchone()
                    summary = self._make_summary(json.loads(original['body']))
                    self._save_summary(db, row['id'], row['updated'], summary)
                result.append(summary)
        return result

    def activity(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 150')]

    def pickup_queue(self):
        return [{key: receipt.get(key, '') for key in ('id', 'title', 'source_name', 'mode')}
                for receipt in self._summary_rows(pickup=True)]


class Vault:
    """Windows DPAPI on Windows; local Fernet key + restricted files elsewhere."""
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'square.secret'
        self.lock = threading.Lock()

    @staticmethod
    def dpapi(data, encrypt):
        import ctypes
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        result = Blob()
        fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR if encrypt else ctypes.POINTER(wintypes.LPWSTR),
                       ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        fn.restype = wintypes.BOOL
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
        # CRYPTPROTECT_UI_FORBIDDEN: never prompt from the local HTTP service.
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise StationError('Windows could not open the saved credential. Use the same Windows user or save a new token.')
        try:
            return ctypes.string_at(result.data, result.size)
        finally:
            ctypes.windll.kernel32.LocalFree(result.data)

    def fernet(self):
        # Windows uses DPAPI and must not depend on a compiled crypto wheel.
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            raise StationError('This saved credential needs the non-Windows encryption package. '
                               'Install requirements.txt, or save a new token in Admin on Windows.') from None
        keypath = self.root / 'vault.key'
        if not keypath.exists():
            private_write(keypath, Fernet.generate_key())
        return Fernet(keypath.read_bytes())

    def prepare_bytes(self, token):
        """Encrypt a detached value without reading or replacing this vault file."""
        raw = token.strip().encode()
        if not raw:
            raise StationError('Enter a token to save, or leave the field empty to keep the current token.')
        return b'DP1' + self.dpapi(raw, True) if os.name == 'nt' else b'FN1' + self.fernet().encrypt(raw)

    def decode_bytes(self, data):
        """Decode already captured bytes without rereading the vault file."""
        if data[:3] == b'DP1' and os.name == 'nt':
            return self.dpapi(data[3:], False).decode()
        if data[:3] == b'FN1':
            try:
                return self.fernet().decrypt(data[3:]).decode()
            except Exception:
                raise StationError('The saved token could not be decrypted. Save a replacement in Admin.') from None
        raise StationError('This credential belongs to another operating system. Save a token in Admin.')

    def set(self, token):
        with self.lock:
            data = self.prepare_bytes(token)
            private_write(self.path, data)
            # Verify it before a caller removes any legacy plaintext settings.
            if self.read() != token.strip():
                raise StationError('Credential verification failed.')

    def read(self):
        if not self.path.exists():
            return ''
        return self.decode_bytes(self.path.read_bytes())

    def exists(self):
        return self.path.exists()


class AdminAuth:
    def __init__(self, store):
        self.store = store
        self.sessions = {}
        self.failures = {}
        self.lock = threading.Lock()

    def configured(self):
        return bool(self.store.get('admin_password'))

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000).hex()

    def set_password(self, password):
        if len(password) < 12 or len(password) > 256:
            raise StationError('Use an admin password between 12 and 256 characters.')
        salt = secrets.token_bytes(24)
        self.store.put('admin_password', {'salt': salt.hex(), 'hash': self.password_hash(password, salt)})
        self.sessions.clear()

    def login(self, password, address):
        with self.lock:
            attempts = [t for t in self.failures.get(address, []) if t > time.monotonic() - 300]
            if len(attempts) >= 5:
                raise StationError('Too many attempts. Try again in five minutes.')
            record = self.store.get('admin_password')
            valid = record and hmac.compare_digest(self.password_hash(password, bytes.fromhex(record['salt'])), record['hash'])
            if not valid:
                self.failures[address] = attempts + [time.monotonic()]
                raise StationError('Incorrect admin password.')
            self.failures.pop(address, None)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = time.monotonic() + 1800
            return token

    def authorized(self, token):
        with self.lock:
            valid = bool(token) and self.sessions.get(token, 0) > time.monotonic()
            if valid:
                self.sessions[token] = time.monotonic() + 1800
            return valid

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)
