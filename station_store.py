"""Local persistence, credential vault, and admin authentication for Roca Station."""
import hashlib
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
    SUMMARY_VERSION = 2
    SUMMARY_FIELDS = frozenset(('id', 'title', 'source_name', 'mode', 'created', 'updated', 'revision',
        'inventory_status', 'inventory_intent', 'prepared', 'pickup_pending', 'pricing_game', 'mixed_games',
        'requires_purchase', 'cash_percent', 'credit_percent', 'channel', 'storage_strategy', 'storage_location', 'error'))
    PURCHASE_FIELDS = frozenset(('type', 'amount', 'at', 'intake_total', 'scope'))
    SHARED_FIELDS = frozenset(('state', 'trade_id', 'message'))

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

    def find_source(self, digest):
        with self.db() as db:
            row = db.execute('SELECT id FROM receipts WHERE source_hash=? ORDER BY updated DESC LIMIT 1', (digest,)).fetchone()
        return row[0] if row else None

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
            allowed = cls.SUMMARY_FIELDS | {'rows', 'units', 'retail', 'all_reviewed', 'purchase', 'files', 'shared_trade'}
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

    def set(self, token):
        with self.lock:
            raw = token.strip().encode()
            if not raw:
                raise StationError('Enter a token to save, or leave the field empty to keep the current token.')
            data = b'DP1' + self.dpapi(raw, True) if os.name == 'nt' else b'FN1' + self.fernet().encrypt(raw)
            private_write(self.path, data)
            # Verify it before a caller removes any legacy plaintext settings.
            if self.read() != token.strip():
                raise StationError('Credential verification failed.')

    def read(self):
        if not self.path.exists():
            return ''
        data = self.path.read_bytes()
        if data[:3] == b'DP1' and os.name == 'nt':
            return self.dpapi(data[3:], False).decode()
        if data[:3] == b'FN1':
            try:
                return self.fernet().decrypt(data[3:]).decode()
            except Exception:
                raise StationError('The saved token could not be decrypted. Save a replacement in Admin.') from None
        raise StationError('This credential belongs to another operating system. Save a token in Admin.')

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
