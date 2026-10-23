"""Local persistence, credential vault, and admin authentication for Roca Station."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from cryptography.fernet import Fernet


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
                id TEXT PRIMARY KEY, source_hash TEXT, updated TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS receipts_source ON receipts(source_hash);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT, at TEXT, kind TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS operations (
                receipt_id TEXT, key TEXT, request TEXT, response TEXT,
                PRIMARY KEY(receipt_id,key));
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, updated TEXT, body TEXT);
            ''')
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
        receipt['updated'] = now()
        receipt['revision'] = receipt.get('revision', 0) + 1
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?)',
                (receipt['id'], receipt.get('source_hash', ''), receipt['updated'], json.dumps(receipt)))
            if event:
                db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                    (receipt['id'], now(), event[0], event[1]))
        return receipt

    def find_source(self, digest):
        with self.db() as db:
            row = db.execute('SELECT id FROM receipts WHERE source_hash=? ORDER BY updated DESC LIMIT 1', (digest,)).fetchone()
        return row[0] if row else None

    def summaries(self):
        with self.db() as db:
            rows = db.execute('SELECT body FROM receipts ORDER BY updated DESC LIMIT 150').fetchall()
        summaries = []
        for row in rows:
            receipt = json.loads(row[0])
            cards = [c for c in receipt['cards'] if c.get('include', True)]
            receipt['rows'] = len(cards)
            receipt['units'] = sum(c['qty'] for c in cards)
            receipt['retail'] = round(sum(c['qty'] * c['final_price'] for c in cards), 2)
            receipt.pop('cards')
            summaries.append(receipt)
        return summaries

    def activity(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 150')]


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
