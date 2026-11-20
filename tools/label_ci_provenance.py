"""Stdlib-only wheel/installed RECORD ownership, before foreign imports.

An identical file reachable from a shared site-packages root is not evidence
that the declared distribution owns it. Both independent records must agree.
"""
import base64
import csv
from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
import io
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
import zipfile

MAX_RECORD = 4 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_ENTRIES = 20000


class ProvenanceError(ValueError):
    pass


def canonical(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*', name):
        raise ProvenanceError('DISTRIBUTION_NAME')
    return re.sub(r'[-_.]+', '-', name).lower()


def safe_path(name):
    if (not isinstance(name, str) or len(name) > 1024 or not name or '\\' in name or ':' in name
        or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ProvenanceError('UNSAFE_ASSET_PATH')
    value = PurePosixPath(name)
    if value.is_absolute() or '..' in value.parts or str(value) != name:
        raise ProvenanceError('UNSAFE_ASSET_PATH')
    return value


def raw_file(path, limit=MAX_FILE):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ProvenanceError('REGULAR_FILE_BOUND')
    raw = path.read_bytes()
    if len(raw) != info.st_size:
        raise ProvenanceError('FILE_CHANGED')
    return raw


def record_rows(raw, wheel=False):
    if len(raw) > MAX_RECORD:
        raise ProvenanceError('RECORD_BOUND')
    rows = {}
    try:
        for row in csv.reader(io.StringIO(raw.decode('utf-8')), strict=True):
            if len(row) != 3 or not row[0] or len(row[0]) > 1024 or len(rows) >= MAX_ENTRIES:
                raise ProvenanceError('RECORD_ROW')
            name, digest, size = row
            if name in rows:
                raise ProvenanceError('DUPLICATE_RECORD')
            if wheel:
                safe_path(name)
            if digest and not re.fullmatch(r'sha256=[A-Za-z0-9_-]{43}', digest):
                raise ProvenanceError('RECORD_DIGEST')
            if size and (not size.isascii() or not size.isdecimal() or str(int(size)) != size):
                raise ProvenanceError('RECORD_SIZE')
            rows[name] = (digest, int(size) if size else None)
    except (UnicodeError, csv.Error) as error:
        raise ProvenanceError('RECORD_ENCODING') from error
    return MappingProxyType(rows)


def record_hash(raw):
    return 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode('ascii').rstrip('=')


@dataclass(frozen=True)
class VerifiedWheel:
    name: str
    version: str
    record_path: str
    record_sha256: str
    rows: object


def read_wheel(path, expected):
    raw = raw_file(Path(path))
    if len(raw) != expected['bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
        raise ProvenanceError('WHEEL_ARCHIVE_PIN')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries if not entry.is_dir()]
        if (len(entries) > MAX_ENTRIES or len(set(names)) != len(names)
            or sum(entry.file_size for entry in entries) > MAX_EXPANDED):
            raise ProvenanceError('WHEEL_ENTRY_BOUND')
        for entry in entries:
            if entry.is_dir():
                continue
            safe_path(entry.filename)
            if entry.file_size > MAX_FILE or stat.S_ISLNK(entry.external_attr >> 16) or entry.filename.endswith('.pth'):
                raise ProvenanceError('WHEEL_ENTRY_TYPE')
        record_path = expected['record_path']
        metadata_path = expected['metadata_path']
        if (len([name for name in names if re.fullmatch(r'[^/]+\.dist-info/RECORD', name)]) != 1
            or len([name for name in names if re.fullmatch(r'[^/]+\.dist-info/METADATA', name)]) != 1
            or record_path not in names or metadata_path not in names
            or PurePosixPath(record_path).parent != PurePosixPath(metadata_path).parent):
            raise ProvenanceError('WHEEL_DISTRIBUTION_METADATA')
        if archive.getinfo(record_path).file_size > MAX_RECORD or archive.getinfo(metadata_path).file_size > MAX_RECORD:
            raise ProvenanceError('WHEEL_METADATA_BOUND')
        record = archive.read(record_path)
        if hashlib.sha256(record).hexdigest() != expected['record_sha256']:
            raise ProvenanceError('WHEEL_RECORD_PIN')
        rows = record_rows(record, wheel=True)
        if set(rows) != set(names) or rows[record_path] != ('', None):
            raise ProvenanceError('WHEEL_RECORD_MEMBERSHIP')
        for name in names:
            if name == record_path:
                continue
            data = archive.read(name)
            if rows[name] != (record_hash(data), len(data)):
                raise ProvenanceError('WHEEL_RECORD_CONTENT')
        fields = BytesParser().parsebytes(archive.read(metadata_path), headersonly=True)
        if (canonical(fields['Name']) != canonical(expected['name']) or fields['Version'] != expected['version']):
            raise ProvenanceError('WHEEL_DISTRIBUTION_IDENTITY')
    return VerifiedWheel(canonical(expected['name']), expected['version'], record_path,
                         expected['record_sha256'], rows)


@dataclass(frozen=True)
class VerifiedInstall:
    name: str
    version: str
    root: Path
    distribution: object
    rows: object
    files: object
    record_sha256: str


def installed_path(root, name):
    relative = safe_path(name)
    path = root.joinpath(*relative.parts)
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_relative_to(root):
        raise ProvenanceError('UNSAFE_RESOLVED_PATH')
    return path


def read_install(distribution, root, wheel):
    root = Path(root).resolve(strict=True)
    if canonical(distribution.metadata['Name']) != wheel.name or distribution.version != wheel.version:
        raise ProvenanceError('INSTALLED_DISTRIBUTION_IDENTITY')
    files = distribution.files
    if files is None or len(files) > MAX_ENTRIES:
        raise ProvenanceError('INSTALLED_FILES_BOUND')
    by_name = {str(item): item for item in files}
    if len(by_name) != len(files) or wheel.record_path not in by_name:
        raise ProvenanceError('INSTALLED_RECORD_MEMBERSHIP')
    path = installed_path(root, wheel.record_path)
    if Path(distribution.locate_file(wheel.record_path)).resolve(strict=True) != path:
        raise ProvenanceError('INSTALLED_RECORD_LOCATION')
    raw = raw_file(path, MAX_RECORD)
    text = distribution.read_text('RECORD')
    # PathDistribution.read_text applies universal-newline translation; pip's
    # CSV writer may produce CRLF. Keep the raw-file hash as evidence while
    # comparing the same text source with that documented translation only.
    normalized = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    if text is None or text != normalized:
        raise ProvenanceError('INSTALLED_RECORD_SOURCE')
    rows = record_rows(raw)
    if set(rows) != set(by_name):
        raise ProvenanceError('INSTALLED_FILES_RECORD_DISAGREEMENT')
    return VerifiedInstall(wheel.name, wheel.version, root, distribution, rows,
                           MappingProxyType(by_name), hashlib.sha256(raw).hexdigest())


def verify_asset(pin, wheel, installed):
    safe_path(pin['path'])
    if (type(pin['bytes']) is not int or not 0 <= pin['bytes'] <= MAX_FILE
        or not isinstance(pin['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', pin['sha256'])):
        raise ProvenanceError('ASSET_PIN_TYPE')
    if (canonical(pin['distribution']) != wheel.name or installed.name != wheel.name
        or pin['version'] != wheel.version or installed.version != wheel.version):
        raise ProvenanceError('ASSET_OWNER_IDENTITY')
    name = pin['path']
    if (name == wheel.record_path or name not in wheel.rows or name not in installed.rows
        or name not in installed.files):
        raise ProvenanceError('ASSET_RECORD_MEMBERSHIP')
    expected_hash = 'sha256=' + base64.urlsafe_b64encode(bytes.fromhex(pin['sha256'])).decode('ascii').rstrip('=')
    expected = (expected_hash, pin['bytes'])
    item = installed.files[name]
    if (wheel.record_sha256 != pin['wheel_record_sha256'] or wheel.rows[name] != expected
        or installed.rows[name] != expected or item.hash is None or item.hash.mode != 'sha256'
        or item.hash.value != expected_hash.removeprefix('sha256=') or item.size != pin['bytes']):
        raise ProvenanceError('ASSET_RECORD_HASH_OR_SIZE')
    path = installed_path(installed.root, name)
    if Path(installed.distribution.locate_file(item)).resolve(strict=True) != path:
        raise ProvenanceError('ASSET_INSTALLED_LOCATION')
    raw = raw_file(path)
    if len(raw) != pin['bytes'] or hashlib.sha256(raw).hexdigest() != pin['sha256']:
        raise ProvenanceError('ASSET_CONTENT')
    return {'distribution': wheel.name, 'version': wheel.version, 'path': name,
            'bytes': len(raw), 'sha256': pin['sha256'], 'wheel_record_sha256': wheel.record_sha256,
            'installed_record_sha256': installed.record_sha256, 'record_ownership_verified': True}
