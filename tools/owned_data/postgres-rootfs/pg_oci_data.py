"""UNRUN owned-Linux DATA inspection, never install/execute image payloads.

False admission, complete source/input COPY, immutable bootstrap image and host
ownership/cleanup are independent gates. No local Python invocation authorized.
The shared strict raw TAR core must independently receive source review first.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import sys
import time
import zlib

CODE = Path('/ci-code')
INPUT = Path('/ci-input')
OUTPUT = Path('/ci-evidence')
CHUNK = 65536
MAX_MEMBER = 256 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024
MAX_OUTPUT = 768 * 1024 * 1024
MAX_MEMBERS = 250000
STARTED = time.monotonic()
CREATED = []
OUTPUT_BYTES = 0
EXPANDED_BYTES = 0
CORE = None


class DataFailure(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise DataFailure(code)


def deadline():
    require(time.monotonic() - STARTED <= 900, 'DATA_DEADLINE')


def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'JSON_DUPLICATE')
        value[key] = item
    return value


def readonly_file(path, size, expected):
    for ancestor in (path, *path.parents):
        require(not ancestor.is_symlink(), 'READONLY_SYMLINK')
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and before.st_size == size and before.st_mode & 0o222 == 0, 'READONLY_FILE_FORM')
    hashed, count = hashlib.sha256(), 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), 'READONLY_OPEN_IDENTITY')
        while chunk := stream.read(CHUNK):
            deadline()
            count += len(chunk)
            require(count <= size, 'READONLY_READ_BOUND')
            hashed.update(chunk)
    after = path.lstat()
    require(count == size and hashed.hexdigest() == expected
            and (after.st_dev, after.st_ino, after.st_size) == (before.st_dev, before.st_ino, before.st_size),
            'READONLY_PREIMAGE_CHANGED')
    return {'bytes': count, 'sha256': expected, 'device': before.st_dev, 'inode': before.st_ino}


def json_file(path, limit=4 * 1024 * 1024):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_size <= limit, 'JSON_BOUND')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(DataFailure('JSON_NONFINITE')))


def bootstrap_receipt():
    """Admitted immutable base only; observe providers before any archive decode."""
    import base64  # Deliberate trustedbase-provider observation beforedecode/export.
    import gzip
    modules, providers = {}, {}
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, '__file__', None)
        if not isinstance(filename, str) or filename.startswith('<'):
            continue
        path = Path(filename).resolve(strict=True)
        if path.is_relative_to(CODE):
            continue  # Separately exact-bound reviewed source, never image members.
        require(path.is_relative_to('/usr/local') or path.is_relative_to('/usr/lib')
                or path.is_relative_to('/lib'), 'UNEXPECTED_BOOTSTRAP_MODULE_PATH')
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 128 * 1024 * 1024, 'BOOTSTRAP_MODULE_BOUND')
        modules[name] = {'path': str(path), 'bytes': info.st_size,
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    for line in Path('/proc/self/maps').read_text().splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6 or not fields[5].startswith('/'):
            continue
        filename = fields[5]
        require(not filename.endswith(' (deleted)'), 'DELETED_BOOTSTRAP_MAPPING')
        path = Path(filename).resolve(strict=True)
        require(path.is_relative_to('/usr/local') or path.is_relative_to('/usr/lib')
                or path.is_relative_to('/lib'), 'UNEXPECTED_BOOTSTRAP_PROVIDER_PATH')
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 128 * 1024 * 1024, 'BOOTSTRAP_PROVIDER_BOUND')
        if str(path) not in providers:
            providers[str(path)] = {'bytes': info.st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    require(any(name in modules for name in ('zlib',)) and providers, 'BOOTSTRAP_PROVIDER_OBSERVATION')
    return {'profile_id': 'postgres-rootfs', 'successful': True,
            'scope': 'Root-admitted immutable Python base stdlib only; no image/vendor ELF import.',
            'python': sys.version, 'executable': str(Path(sys.executable).resolve()),
            'zlib_compiled': zlib.ZLIB_VERSION, 'zlib_runtime': zlib.ZLIB_RUNTIME_VERSION,
            'stdlib_modules': modules, 'actual_mapped_providers': providers,
            'before_archive_decode': True, 'vendor_image_code_loaded': False}


def output_file(name):
    require(re.fullmatch(r'(?:[a-f0-9]{64}\.data|[a-z0-9-]+\.json)', name) is not None, 'OUTPUT_OPAQUE_NAME')
    path = OUTPUT / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    info = os.fstat(descriptor)
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'OUTPUT_FILE_FORM')
    CREATED.append({'path': name, 'device': info.st_dev, 'inode': info.st_ino})
    return os.fdopen(descriptor, 'wb')


def save(name, value):
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False) + '\n').encode()
    require(len(raw) <= 32 * 1024 * 1024, 'REPORT_BOUND')
    with output_file(name) as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


class Slice:
    def __init__(self, stream, size):
        self.stream, self.remaining = stream, size

    def read(self):
        take = min(CHUNK, self.remaining)
        if not take:
            return b''
        raw = self.stream.read(take)
        require(len(raw) == take, 'COMPRESSED_SOURCE_TRUNCATED')
        self.remaining -= len(raw)
        return raw


class BoundedGzip(io.RawIOBase):
    """Explicit zlib EOF/tail/remaining proof; do not infer from file position."""
    def __init__(self, source, limit):
        super().__init__()
        self.source, self.limit = source, limit
        self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self.pending = b''
        self.count, self.hashed, self.finished = 0, hashlib.sha256(), False

    def readable(self):
        return True

    def readinto(self, output):
        deadline()
        if not output:
            return 0
        while not self.decoder.eof:
            incoming = self.pending or self.source.read()
            require(bool(incoming), 'GZIP_TRUNCATED_OR_UNFINISHED')
            data = self.decoder.decompress(incoming, max_length=min(len(output), CHUNK))
            self.pending = self.decoder.unconsumed_tail
            if data:
                self.count += len(data)
                require(self.count <= self.limit, 'GZIP_EXPANSION_BOUND')
                self.hashed.update(data)
                output[:len(data)] = data
                return len(data)
        require(not self.pending and not self.decoder.unused_data and not self.decoder.unconsumed_tail
                and self.source.remaining == 0, 'GZIP_CONCATENATED_OR_UNUSED_OR_UNCONSUMED_DATA')
        self.finished = True
        return 0


def selected_payload(name, prefix):
    return bool(prefix.startswith(b'\x7fELF') or name in ('etc/os-release', 'usr/lib/os-release',
                'var/lib/dpkg/status', 'usr/local/bin/docker-entrypoint.sh')
                or re.fullmatch(r'var/lib/dpkg/info/[^/]+\.(?:list|md5sums)', name)
                or re.fullmatch(r'usr/share/doc/.+/(?:copyright|LICENSE[^/]*|COPYING[^/]*|NOTICE[^/]*)', name)
                or re.fullmatch(r'etc/apt/(?:sources\.list|sources\.list\.d/[^/]+|trusted\.gpg\.d/[^/]+)', name)
                or name in ('etc/ld.so.cache', 'etc/ld.so.conf')
                or re.fullmatch(r'etc/ld\.so\.conf\.d/[^/]+', name))


def read_regular(archive, member, index, name):
    global OUTPUT_BYTES
    hashed, count, prefix, stream, data_path = hashlib.sha256(), 0, b'', None, None
    source = archive.extractfile(member)  # Read bytes only; never extract/apply filesystem paths.
    require(source is not None, 'REGULAR_MEMBER_STREAM')
    try:
        with source:
            while chunk := source.read(CHUNK):
                deadline()
                count += len(chunk)
                require(count <= member.size, 'REGULAR_MEMBER_SIZE_BOUND')
                if not prefix:
                    prefix = chunk[:16]
                    if selected_payload(name, prefix):
                        data_path = hashlib.sha256((str(index) + '\0' + name).encode()).hexdigest() + '.data'
                        stream = output_file(data_path)
                if stream is not None:
                    OUTPUT_BYTES += len(chunk)
                    require(OUTPUT_BYTES <= MAX_OUTPUT, 'TOTAL_OUTPUT_BOUND')
                    stream.write(chunk)
                hashed.update(chunk)
        if count == 0 and selected_payload(name, b''):
            data_path = hashlib.sha256((str(index) + '\0' + name).encode()).hexdigest() + '.data'
            stream = output_file(data_path)
        require(count == member.size, 'REGULAR_MEMBER_TRUNCATED')
        if stream is not None:
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if stream is not None:
            stream.close()
    identity = None
    if data_path is not None:
        info, created = (OUTPUT / data_path).lstat(), CREATED[-1]
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == count
                and (info.st_dev, info.st_ino) == (created['device'], created['inode']), 'PAYLOAD_OUTPUT_IDENTITY')
        identity = {'bytes': count, 'sha256': hashed.hexdigest(), 'device': info.st_dev, 'inode': info.st_ino}
    return {'sha256': hashed.hexdigest(), 'data_path': data_path, 'elf': prefix.startswith(b'\x7fELF'),
            'prefix_hex': prefix[:8].hex(), 'payload_identity': identity}


def inspect_layer(path, pin, expected_diff_id, index, TarInfo):
    """The strict raw class is imported unchanged; no suppressed HeaderError."""
    global EXPANDED_BYTES
    rows, seen = [], set()
    header_start, metadata_start = TarInfo.header_count, TarInfo.metadata_count
    CORE.begin_tar_dataset()  # End-marker reset only; preserve aggregate header/metadata counters.
    with path.open('rb') as stream:
        decoded = BoundedGzip(Slice(stream, pin['bytes']), MAX_EXPANDED - EXPANDED_BYTES)
        buffered = io.BufferedReader(decoded, buffer_size=CHUNK)
        try:
            with CORE.tarfile.open(fileobj=buffered, mode='r|', bufsize=CHUNK, tarinfo=TarInfo) as archive:
                for member in archive:
                    deadline()
                    require(len(rows) < 20000 and 0 <= member.size <= MAX_MEMBER and not member.sparse,
                            'MEMBER_COUNT_SIZE_OR_SPARSE')
                    name = CORE.canonical_member(member.name, member.isdir(), allow_posix_colon=True)
                    require(name not in seen, 'LAYER_DUPLICATE_CANONICAL_PATH')
                    seen.add(name)
                    require(member.isfile() or member.isdir() or member.issym() or member.islnk(),
                            'UNSUPPORTED_SPECIAL_FILE_NO_APPLY')
                    require(member.isfile() or member.size == 0, 'NONREGULAR_PAYLOAD_UNSUPPORTED')
                    require(len(json.dumps(member.pax_headers)) <= 65536, 'PAX_METADATA_BOUND')
                    row = {'path': name, 'source_name': member.name, 'bytes': member.size,
                           'mode_recorded_not_applied': member.mode, 'uid_recorded_not_applied': member.uid,
                           'gid_recorded_not_applied': member.gid, 'pax_headers': member.pax_headers,
                           'layer_index': index, 'type': 'regular' if member.isfile() else
                           'directory' if member.isdir() else 'symlink' if member.issym() else 'hardlink'}
                    if member.issym() or member.islnk():
                        require(member.size == 0 and len(member.linkname) <= 2048 and '\x00' not in member.linkname
                                and '\\' not in member.linkname, 'LINK_DATA_FORM')
                        member.linkname.encode('utf-8', errors='strict')
                        row['link_target_recorded_not_created'] = member.linkname
                    if member.isfile():
                        row.update(read_regular(archive, member, index, name))
                    rows.append(row)
                global_pax = dict(archive.pax_headers)
                require(len(json.dumps(global_pax)) <= 65536, 'GLOBAL_PAX_METADATA_BOUND')
                ending = CORE.validate_tar_ending(archive, decoded)
                first_zero_end = ending['first_zero_header_end']
                trailer_bytes = ending['additional_zero_bytes']
            require(decoded.finished and decoded.hashed.hexdigest() == expected_diff_id.removeprefix('sha256:'),
                    'EXACT_ORDERED_UNCOMPRESSED_DIFF_ID')
            EXPANDED_BYTES += decoded.count
            return {'layer_index': index, 'compressed_sha256': pin['digest'], 'compressed_bytes': pin['bytes'],
                    'expanded_bytes': decoded.count, 'diff_id': expected_diff_id, 'members': rows,
                    'global_pax_headers': global_pax, 'raw_header_count': TarInfo.header_count - header_start,
                    'metadata_header_count': TarInfo.metadata_count - metadata_start,
                    'dataset_raw_header_count': TarInfo.header_count,
                    'dataset_metadata_header_count': TarInfo.metadata_count, 'first_zero_header_end': first_zero_end,
                    'additional_zero_bytes': trailer_bytes, 'complete_gzip_consumption': True,
                    'two_zero_blocks_and_aligned_zero_trailer': True}
        finally:
            buffered.close()


def remove_prefix(current, name, lower_than=None, children_only=False):
    affected = [path for path, row in current.items()
                if (path.startswith(name + '/') or (not children_only and path == name))
                and (lower_than is None or row['layer_index'] < lower_than)]
    for path in affected:
        del current[path]
    return affected


def link_target(name, value, hard=False):
    require(type(value) is str and 0 < len(value) <= 2048 and '\x00' not in value
            and '\\' not in value, 'VIRTUAL_LINK_FORM')
    parts = [] if hard or value.startswith('/') else name.split('/')[:-1]
    for part in value.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            require(bool(parts), 'VIRTUAL_LINK_ROOT_ESCAPE')
            parts.pop()
        else:
            parts.append(part)
    return '/'.join(parts) or '.'


def virtual_rootfs(layers):
    """Metadata operations only. Never create links, directories, modes or image paths."""
    current, objects, changes = {}, {}, []
    for layer in layers:
        index, removed = layer['layer_index'], []
        # OCI whiteouts affect lower layers only, regardless of TAR ordering.
        for row in layer['members']:
            base = row['path'].rsplit('/', 1)[-1]
            if not base.startswith('.wh.'):
                continue
            require(row['type'] == 'regular' and row['bytes'] == 0, 'EMPTY_REGULAR_WHITEOUT_REQUIRED')
            parent = row['path'].rsplit('/', 1)[0] if '/' in row['path'] else '.'
            if base == '.wh..wh..opq':
                if parent == '.':
                    affected = [path for path, old in current.items() if path != '.' and old['layer_index'] < index]
                    for path in affected:
                        del current[path]
                else:
                    affected = remove_prefix(current, parent, lower_than=index, children_only=True)
            else:
                component = base[4:]
                require(component not in ('', '.', '..'), 'INVALID_WHITEOUT_TARGET_COMPONENT')
                target = (parent + '/' if parent != '.' else '') + component
                affected = remove_prefix(current, target, lower_than=index)
            removed.extend(affected)
        for row in layer['members']:
            path = row['path']
            if path.rsplit('/', 1)[-1].startswith('.wh.'):
                continue
            parts = path.split('/')
            for size in range(1, len(parts)):
                ancestor = '/'.join(parts[:size])
                require(ancestor not in current or current[ancestor]['type'] == 'directory',
                        'UNSUPPORTED_NON_DIRECTORY_ANCESTOR')
            require(row['type'] == 'directory' or path in current
                    or not any(name.startswith(path + '/') for name in current),
                    'IMPLICIT_DIRECTORY_DESCENDANT_CONFLICT')
            if path in current and not (row['type'] == current[path]['type'] == 'directory'):
                remove_prefix(current, path)
            value = dict(row)
            if row['type'] == 'regular':
                object_id = str(index) + ':' + path
                objects[object_id] = dict(row)
                value['object_id'] = object_id
            elif row['type'] == 'hardlink':
                target = link_target(path, row['link_target_recorded_not_created'], hard=True)
                require(target in current and current[target]['type'] in ('regular', 'hardlink'),
                        'UNRESOLVED_OR_UNSUPPORTED_HARDLINK')
                value['object_id'] = current[target]['object_id']
                original = objects[value['object_id']]
                require(all(row[key] == original[key] for key in ('mode_recorded_not_applied',
                        'uid_recorded_not_applied', 'gid_recorded_not_applied')), 'HARDLINK_ATTRIBUTE_AMBIGUITY')
            current[path] = value
            require(len(current) <= MAX_MEMBERS, 'FINAL_ROOTFS_MEMBER_BOUND')
        names = sorted(set(removed))
        changes.append({'layer_index': index, 'lower_paths_removed': len(names),
                        'removed_names_sha256': hashlib.sha256(json.dumps(names, separators=(',', ':')).encode()).hexdigest()})
    rows = []
    for path, value in sorted(current.items()):
        row = dict(value)
        if row['type'] in ('regular', 'hardlink'):
            original = objects[row['object_id']]
            row['effective_content'] = {key: original[key] for key in ('bytes', 'sha256', 'data_path', 'elf', 'prefix_hex', 'payload_identity')}
        elif row['type'] == 'symlink':
            row['virtual_target'] = link_target(path, row['link_target_recorded_not_created'])
        rows.append(row)
    return {'format': 'stock-and-sleeve:postgres-oci-virtual-rootfs:v1', 'members': rows,
            'member_count': len(rows), 'whiteout_changes': changes,
            'no_filesystem_apply_or_links_or_modes_or_execution': True,
            'unresolved_runtime_symlinks': 'Recorded only; later native loader/rootfs evidence must independently resolve applicable paths.'}


def domain_corpus():
    """Bounded constructed codec/virtual vectors; no image payload or filesystem apply.

    Shared TAR/ELF parser corpus is independently owned; these vectors exercise
    this PG adapter without resetting shared header/metadata/deadline counters.
    """
    cases = []

    def case(name, operation, expected_error=None):
        try:
            result = operation()
        except DataFailure as error:
            require(expected_error is not None and str(error) == expected_error, 'CORPUS_UNEXPECTED_REJECTION')
            cases.append({'id': name, 'successful': True, 'expected_rejection': expected_error})
        else:
            require(expected_error is None and result is True, 'CORPUS_EXPECTED_REJECTION_OR_FACT_MISSING')
            cases.append({'id': name, 'successful': True, 'expected_rejection': None})

    def packed(raw):
        compressor = zlib.compressobj(level=6, wbits=16 + zlib.MAX_WBITS)
        return compressor.compress(raw) + compressor.flush()

    def consume(raw, expected, limit=4096):
        decoded = BoundedGzip(Slice(io.BytesIO(raw), len(raw)), limit)
        with io.BufferedReader(decoded, buffer_size=CHUNK) as stream:
            result = stream.read(limit + 1)
        return decoded.finished is True and decoded.count == len(expected) and result == expected

    raw = b'bounded gzip corpus\n' * 8
    valid = packed(raw)
    case('PGD01_gzip_complete_payload', lambda: consume(valid, raw))
    case('PGD02_gzip_complete_empty', lambda: consume(packed(b''), b''))
    case('PGD03_gzip_truncated_trailer', lambda: consume(valid[:-4], raw), 'GZIP_TRUNCATED_OR_UNFINISHED')
    case('PGD04_gzip_unused_suffix', lambda: consume(valid + b'x', raw), 'GZIP_CONCATENATED_OR_UNUSED_OR_UNCONSUMED_DATA')
    case('PGD05_gzip_concatenated_member', lambda: consume(valid + packed(b'x'), raw), 'GZIP_CONCATENATED_OR_UNUSED_OR_UNCONSUMED_DATA')
    case('PGD06_gzip_expansion_limit', lambda: consume(valid, raw, limit=16), 'GZIP_EXPANSION_BOUND')

    def row(name, index, kind='regular', text='one', target=None, mode=0o644):
        value = {'path': name, 'source_name': name, 'bytes': len(text) if kind == 'regular' else 0,
                 'mode_recorded_not_applied': mode, 'uid_recorded_not_applied': 0,
                 'gid_recorded_not_applied': 0, 'pax_headers': {}, 'layer_index': index, 'type': kind}
        if kind == 'regular':
            value.update({'sha256': hashlib.sha256(text.encode()).hexdigest(), 'data_path': None,
                          'elf': False, 'prefix_hex': text.encode()[:8].hex(), 'payload_identity': None})
        if target is not None:
            value['link_target_recorded_not_created'] = target
        return value

    def virtual(*layers):
        value = virtual_rootfs([{'layer_index': index, 'members': rows} for index, rows in enumerate(layers)])
        require(value['no_filesystem_apply_or_links_or_modes_or_execution'] is True, 'CORPUS_VIRTUAL_ONLY')
        return {item['path']: item for item in value['members']}

    def exact_whiteout():
        result = virtual([row('old', 0), row('keep', 0)],
                         [row('new', 1), row('.wh.old', 1, text='')])
        return set(result) == {'keep', 'new'}

    def opaque_preserves_new():
        result = virtual([row('d', 0, 'directory'), row('d/old', 0)],
                         [row('d/new', 1), row('d/.wh..wh..opq', 1, text='')])
        return set(result) == {'d', 'd/new'}

    def root_opaque():
        result = virtual([row('.', 0, 'directory'), row('old', 0)],
                         [row('new', 1), row('.wh..wh..opq', 1, text='')])
        return set(result) == {'.', 'new'}

    def directory_replacement():
        result = virtual([row('d', 0, 'directory'), row('d/old', 0)], [row('d', 1, text='replacement')])
        return set(result) == {'d'} and result['d']['type'] == 'regular'

    def directory_metadata():
        result = virtual([row('d', 0, 'directory'), row('d/old', 0)], [row('d', 1, 'directory', mode=0o755)])
        return set(result) == {'d', 'd/old'} and result['d']['mode_recorded_not_applied'] == 0o755

    def hardlink_snapshot():
        result = virtual([row('a', 0), row('b', 0, 'hardlink', target='a')], [row('a', 1, text='two')])
        return (result['b']['effective_content']['sha256'] == hashlib.sha256(b'one').hexdigest()
                and result['a']['effective_content']['sha256'] == hashlib.sha256(b'two').hexdigest())

    case('PGD07_exact_lower_whiteout', exact_whiteout)
    case('PGD08_opaque_preserves_same_layer_new', opaque_preserves_new)
    case('PGD09_root_opaque_retains_root_and_new', root_opaque)
    case('PGD10_file_replaces_directory_tree', directory_replacement)
    case('PGD11_directory_metadata_retains_children', directory_metadata)
    case('PGD12_hardlink_keeps_original_content', hardlink_snapshot)
    case('PGD13_forward_hardlink_denied', lambda: virtual([row('b', 0, 'hardlink', target='a'), row('a', 0)]),
         'UNRESOLVED_OR_UNSUPPORTED_HARDLINK')
    case('PGD14_hardlink_attribute_ambiguity_denied', lambda: virtual([row('a', 0), row('b', 0, 'hardlink', target='a', mode=0o600)]),
         'HARDLINK_ATTRIBUTE_AMBIGUITY')
    case('PGD15_symlink_ancestor_not_followed', lambda: virtual([row('d', 0, 'symlink', target='elsewhere'), row('d/x', 0)]),
         'UNSUPPORTED_NON_DIRECTORY_ANCESTOR')
    case('PGD16_symlink_root_escape_denied', lambda: virtual([row('a', 0, 'symlink', target='../outside')]),
         'VIRTUAL_LINK_ROOT_ESCAPE')
    case('PGD17_nonempty_whiteout_denied', lambda: virtual([row('old', 0)], [row('.wh.old', 1, text='notempty')]),
         'EMPTY_REGULAR_WHITEOUT_REQUIRED')
    case('PGD18_implicit_directory_same_layer_regular_denied', lambda: virtual([row('a/b', 0), row('a', 0)]),
         'IMPLICIT_DIRECTORY_DESCENDANT_CONFLICT')
    case('PGD19_implicit_directory_later_layer_regular_denied', lambda: virtual([row('a/b', 0)], [row('a', 1)]),
         'IMPLICIT_DIRECTORY_DESCENDANT_CONFLICT')
    case('PGD20_implicit_directory_same_layer_symlink_denied', lambda: virtual([row('a/b', 0), row('a', 0, 'symlink', target='other')]),
         'IMPLICIT_DIRECTORY_DESCENDANT_CONFLICT')
    case('PGD21_implicit_directory_later_layer_symlink_denied', lambda: virtual([row('a/b', 0)], [row('a', 1, 'symlink', target='other')]),
         'IMPLICIT_DIRECTORY_DESCENDANT_CONFLICT')
    case('PGD22_dot_whiteout_target_denied', lambda: virtual([row('keep', 0)], [row('.wh..', 1, text='')]),
         'INVALID_WHITEOUT_TARGET_COMPONENT')
    case('PGD23_dotdot_whiteout_target_denied', lambda: virtual([row('keep', 0)], [row('d/.wh...', 1, text='')]),
         'INVALID_WHITEOUT_TARGET_COMPONENT')
    # Additive POSIX-colon DATA vectors after the unchanged original23 cases.
    constructed_expanded_start, constructed_output_start = EXPANDED_BYTES, OUTPUT_BYTES
    def canonical(name, is_directory=False, **options):
        try:
            return CORE.canonical_member(name, is_directory, **options)
        except CORE.DataFailure as error:
            raise DataFailure(str(error)) from None  # Fixed owned core code only.

    posix_name = 'var/lib/dpkg/info/fictional:amd64.list'
    case('PGD24_default_colon_metadata_denied', lambda: canonical(posix_name), 'TAR_PATH_FORM')
    case('PGD25_explicit_false_colon_metadata_denied',
         lambda: canonical(posix_name, allow_posix_colon=False), 'TAR_PATH_FORM')
    case('PGD26_explicit_posix_colon_metadata_preserved',
         lambda: canonical(posix_name, allow_posix_colon=True) == posix_name)
    case('PGD27_posix_absolute_path_still_denied',
         lambda: canonical('/owned:pkg', allow_posix_colon=True), 'TAR_PATH_FORM')
    case('PGD28_posix_backslash_still_denied',
         lambda: canonical('owned\\pkg:name', allow_posix_colon=True), 'TAR_PATH_FORM')
    case('PGD29_posix_nul_still_denied',
         lambda: canonical('owned\x00pkg:name', allow_posix_colon=True), 'TAR_PATH_FORM')
    case('PGD30_posix_parent_component_still_denied',
         lambda: canonical('owned:pkg/../escape', allow_posix_colon=True), 'TAR_PATH_COMPONENT')
    case('PGD31_posix_regular_root_still_denied',
         lambda: canonical('.', allow_posix_colon=True), 'TAR_ROOT_KIND')
    case('PGD32_posix_directory_root_still_valid',
         lambda: canonical('./', True, allow_posix_colon=True) == '.')

    def exact_boolean_opt_in():
        for value in (0, 1, None, 'true', [], {}):
            try:
                CORE.canonical_member(posix_name, False, allow_posix_colon=value)
            except CORE.DataFailure as error:
                require(str(error) == 'TAR_PATH_COLON_POLICY', 'CORPUS_COLON_POLICY_WRONG_REJECTION')
            else:
                return False
        return True
    case('PGD33_nonboolean_posix_opt_in_denied', exact_boolean_opt_in)

    def strict_utf8_unchanged():
        try:
            CORE.canonical_member('owned:\ud800', False, allow_posix_colon=True)
        except UnicodeEncodeError:
            return True
        return False
    case('PGD34_posix_strict_utf8_guard_retained', strict_utf8_unchanged)

    def full_layer(member_name, *, is_directory=False):
        # Inert gzip/TAR input with a generated hash-only private basename.
        # No archive member name is ever used as a host filesystem path.
        global OUTPUT_BYTES, EXPANDED_BYTES
        payload = b'' if is_directory else b'owned fictional package declaration\n'
        member = CORE.tarfile.TarInfo(member_name)
        member.type = CORE.tarfile.DIRTYPE if is_directory else CORE.tarfile.REGTYPE
        member.size, member.mode, member.uid, member.gid, member.mtime = len(payload), 0o600, 10001, 10001, 0
        tar_bytes = member.tobuf(format=CORE.tarfile.USTAR_FORMAT) + payload + bytes((-len(payload)) % 512) + bytes(1024)
        gzip_bytes = packed(tar_bytes)
        require(len(gzip_bytes) <= 8192, 'CORPUS_PRIVATE_FIXTURE_BOUND')
        expanded_before = EXPANDED_BYTES
        require(expanded_before + len(tar_bytes) <= MAX_EXPANDED, 'CORPUS_EXPANSION_BUDGET')
        digest = hashlib.sha256(gzip_bytes).hexdigest()
        input_name = digest + '.data'
        data_name = hashlib.sha256(('4096\0' + member_name).encode('utf-8')).hexdigest() + '.data'
        allowed = {input_name: (len(gzip_bytes), digest), data_name: (len(payload), hashlib.sha256(payload).hexdigest())}
        prior_created = tuple(dict(item) for item in CREATED)
        path = OUTPUT / input_name
        try:
            OUTPUT_BYTES += len(gzip_bytes)
            require(OUTPUT_BYTES <= MAX_OUTPUT, 'CORPUS_PRIVATE_INPUT_BOUND')
            with output_file(input_name) as stream:
                stream.write(gzip_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            input_identity = dict(CREATED[-1], bytes=len(gzip_bytes), sha256=digest)
            require(private_payload({'data_path': input_name, 'bytes': len(gzip_bytes),
                    'payload_identity': input_identity}, 8192) == gzip_bytes, 'CORPUS_INPUT_READBACK')
            try:
                result = inspect_layer(path, {'bytes': len(gzip_bytes), 'digest': 'sha256:' + digest},
                    'sha256:' + hashlib.sha256(tar_bytes).hexdigest(), 4096, CORE.BoundedTarInfo)
            except CORE.DataFailure as error:
                raise DataFailure(str(error)) from None
            require(result['complete_gzip_consumption'] is True and result['two_zero_blocks_and_aligned_zero_trailer'] is True
                    and result['expanded_bytes'] == len(tar_bytes) and len(result['members']) == 1,
                    'CORPUS_FULL_LAYER_COMPLETION')
            actual = result['members'][0]
            require(actual['path'] == ('.' if is_directory and member_name in ('.', './') else member_name)
                    and actual['source_name'] == member_name
                    and actual['type'] == ('directory' if is_directory else 'regular')
                    and actual['mode_recorded_not_applied'] == 0o600
                    and actual['uid_recorded_not_applied'] == actual['gid_recorded_not_applied'] == 10001,
                    'CORPUS_FULL_LAYER_EXACT_METADATA')
            if not is_directory:
                require(actual['sha256'] == hashlib.sha256(payload).hexdigest()
                        and actual['data_path'] == data_name and private_payload(actual, 8192) == payload,
                        'CORPUS_FULL_LAYER_EXACT_PRIVATE_PAYLOAD')
            return True
        finally:
            # inspect_layer charges its successful stream. For a deliberately
            # rejected synthetic layer, conservatively charge its full known
            # plaintext bound rather than refunding or losing partial work.
            require(EXPANDED_BYTES in (expanded_before, expanded_before + len(tar_bytes)),
                    'CORPUS_EXPANSION_CHARGE_BASIS')
            if EXPANDED_BYTES == expanded_before:
                EXPANDED_BYTES += len(tar_bytes)
            # Only files registered after this fixture began may be removed,
            # after exact generated-name/device/inode/size/content validation.
            additions = tuple(CREATED[len(prior_created):])
            for created in additions:
                require(created['path'] in allowed, 'CORPUS_UNEXPECTED_PRIVATE_FILE')
                owned = OUTPUT / created['path']
                info = owned.lstat()
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                        and (info.st_dev, info.st_ino) == (created['device'], created['inode'])
                        and info.st_size == allowed[created['path']][0],
                        'CORPUS_PRIVATE_FILE_CHANGED')
                identity = dict(created, bytes=allowed[created['path']][0], sha256=allowed[created['path']][1])
                private_payload({'data_path': created['path'], 'bytes': identity['bytes'], 'payload_identity': identity}, 8192)
                named = owned.lstat()
                require(stat.S_ISREG(named.st_mode) and named.st_nlink == 1
                        and (named.st_dev, named.st_ino, named.st_size) == (identity['device'], identity['inode'], identity['bytes']),
                        'CORPUS_CLEANUP_NAMED_IDENTITY')
                owned.unlink()
            del CREATED[len(prior_created):]
            require(tuple(CREATED) == prior_created, 'CORPUS_PRIOR_OUTPUT_REGISTRY_CHANGED')
            # Shared header/metadata counters, both deadlines, OUTPUT_BYTES and
            # successful EXPANDED_BYTES remain monotonic; never reset budgets.

    case('PGD35_full_layer_posix_package_metadata_positive',
         lambda: full_layer(posix_name))
    case('PGD36_full_layer_absolute_path_denied',
         lambda: full_layer('/owned:pkg'), 'TAR_PATH_FORM')
    case('PGD37_full_layer_parent_component_denied',
         lambda: full_layer('owned:pkg/../escape'), 'TAR_PATH_COMPONENT')
    case('PGD38_full_layer_regular_root_denied',
         lambda: full_layer('.'), 'TAR_ROOT_KIND')
    case('PGD39_full_layer_directory_root_positive',
         lambda: full_layer('.', is_directory=True))

    require(len(cases) == 39 and len({item['id'] for item in cases}) == 39, 'EXACT_DOMAIN_CORPUS_IDS')
    return {'profile_id': 'postgres-rootfs', 'successful': True, 'cases': cases,
            'case_count': 39, 'native_or_image_execution': False, 'filesystem_member_application': False,
            'constructed_layer_expanded_budget_bytes': EXPANDED_BYTES - constructed_expanded_start,
            'constructed_private_output_budget_bytes': OUTPUT_BYTES - constructed_output_start,
            'private_fixture_files_removed': True,
            'shared_tar_counters_reset': False, 'constructed_payload_bytes': len(raw)}


def private_payload(content, limit):
    require(content['data_path'] is not None and content['bytes'] <= limit, 'RETAINED_PRIVATE_PAYLOAD_BOUND')
    path = OUTPUT / content['data_path']
    identity = content['payload_identity']
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == identity['bytes']
                and (info.st_dev, info.st_ino) == (identity['device'], identity['inode']), 'PRIVATE_PAYLOAD_IDENTITY')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = -1
            raw = stream.read(limit + 1)
        require(len(raw) == identity['bytes'] and hashlib.sha256(raw).hexdigest() == identity['sha256'],
                'PRIVATE_PAYLOAD_PREIMAGE')
        return raw
    finally:
        if descriptor != -1:
            os.close(descriptor)


def version_labels(path, identity):
    """Bounded corroborating labels only; never infer fixes/support from strings."""
    patterns = (re.compile(rb'OpenSSL [0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}[A-Za-z0-9 .-]{0,64}'),
                re.compile(rb'PostgreSQL [0-9]{1,4}\.[0-9]{1,4}(?:\.[0-9]{1,4})?'),
                re.compile(rb'go1\.[0-9]{1,4}(?:\.[0-9]{1,4})?(?:[a-z]{1,8}[0-9]{1,4})?'))
    rows, seen, offset, tail = [], set(), 0, b''
    hashed = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require((info.st_dev, info.st_ino, info.st_size) == (identity['device'], identity['inode'], identity['bytes'])
                and stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'VERSION_LABEL_INPUT_IDENTITY')
        while chunk := stream.read(CHUNK):
            deadline()
            hashed.update(chunk)
            window = tail + chunk
            base = offset - len(tail)
            final_chunk = offset + len(chunk) == identity['bytes']
            complete_end = len(window) if final_chunk else max(0, len(window) - 256)
            for pattern in patterns:
                for match in pattern.finditer(window):
                    if match.end() > complete_end:
                        continue  # Revisit in512byte overlap; do not report partial labels.
                    key = (base + match.start(), match.group().decode('ascii'))
                    if key not in seen:
                        require(len(rows) < 256, 'VERSION_LABEL_VECTOR_BOUND_NO_TRUNCATION')
                        seen.add(key)
                        rows.append({'file_offset': key[0], 'label': key[1]})
            offset += len(chunk)
            tail = window[-512:]
    require(offset == identity['bytes'] and hashed.hexdigest() == identity['sha256'], 'VERSION_LABEL_INPUT_CHANGED')
    return {'labels': sorted(rows, key=lambda row: (row['file_offset'], row['label'])),
            'complete_bounded_scan': True, 'authority': 'Corroborating embedded labels; not build/advisory/runtime approval.'}


def native_data(rootfs, elf_reader):
    payloads = {}
    for row in rootfs['members']:
        content = row.get('effective_content')
        if content and content['elf']:
            require(content['data_path'] is not None, 'EVERY_FINAL_ELF_RETAINED')
            value = payloads.setdefault(content['data_path'], {'content': content, 'rootfs_paths': []})
            value['rootfs_paths'].append(row['path'])
    groups = {}
    for name, value in sorted(payloads.items()):
        identity = value['content']['payload_identity']
        group = groups.setdefault(identity['sha256'], {'bytes': identity['bytes'], 'inputs': []})
        require(group['bytes'] == identity['bytes'], 'ELF_HASH_SIZE_ALIAS_CONFLICT')
        group['inputs'].append({'private_payload_name': name, 'identity': identity,
                                'final_rootfs_paths': sorted(value['rootfs_paths'])})
    result = []
    for content_sha256, group in sorted(groups.items()):
        deadline()
        representative = group['inputs'][0]
        name, identity = representative['private_payload_name'], representative['identity']
        facts = elf_reader.inspect_elf(OUTPUT / name, identity)
        go = go_buildinfo(OUTPUT / name, identity, facts)
        labels = version_labels(OUTPUT / name, identity)
        report_name = 'elf-' + content_sha256 + '.json'
        save(report_name, {'format': 'stock-and-sleeve:one-elf-complete-data:v1',
             'content_sha256': content_sha256, 'inspection_source': representative,
             'byte_equal_sources': group['inputs'], 'elf_data': facts, 'go_buildinfo': go,
             'embedded_version_labels': labels, 'native_execution': False})
        paths = sorted({path for source in group['inputs'] for path in source['final_rootfs_paths']})
        result.append({'content_sha256': content_sha256, 'identity': identity,
                       'final_rootfs_paths': paths, 'derived_report': report_name,
                       'go_buildinfo': go, 'embedded_version_labels': labels,
                       'byte_equal_sources': group['inputs']})
    return {'format': 'stock-and-sleeve:postgres-final-elf-data:v1', 'elf_payload_count': len(payloads),
            'unique_elf_contents': len(result), 'complete_vectors': result, 'vectors_in_registered_reports': True,
            'native_loaded': False,
            'closure_limit': 'Native facts only; exact loader/cache/provider/OS/advisory analysis remains independent.'}


def bounded_region(path, identity, offset, count):
    require(type(offset) is int and type(count) is int and 0 <= offset <= identity['bytes']
            and 0 <= count <= 2 * 1024 * 1024 and offset + count <= identity['bytes'], 'PRIVATE_REGION_BOUND')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and (before.st_dev, before.st_ino, before.st_size) ==
                    (identity['device'], identity['inode'], identity['bytes']), 'PRIVATE_REGION_IDENTITY')
        stream.seek(offset)
        raw = stream.read(count)
        after = os.fstat(stream.fileno())
        require(len(raw) == count and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                'PRIVATE_REGION_CHANGED')
    return raw


def go_buildinfo(path, identity, facts):
    """Bounded Go>=1.18 inline buildinfo facts, not Go execution/build replay.

    Format reference: https://go.dev/src/debug/buildinfo/buildinfo.go
    The shared ELF reader owns section/header parsing; no second ELF parser.
    """
    matches = [section for section in facts['sections'] if section['name'] == '.go.buildinfo']
    if not matches:
        return {'present': False, 'section_scope_only': True}
    require(len(matches) == 1, 'GO_BUILDINFO_SECTION_AMBIGUITY')
    section = matches[0]
    require(section['type'] != 8 and 32 <= section['bytes'] <= 2 * 1024 * 1024,
            'GO_BUILDINFO_SECTION_BOUND')
    raw = bounded_region(path, identity, section['offset'], section['bytes'])
    positions = [offset for offset in range(0, len(raw) - 31, 16)
                 if raw[offset:offset + 14] == b'\xff Go buildinf:']
    require(len(positions) == 1, 'GO_BUILDINFO_MAGIC_ALIGNMENT')
    start = positions[0]
    pointer_size, flags = raw[start + 14], raw[start + 15]
    require(pointer_size == 8 and flags & ~3 == 0 and flags & 1 == 0 and flags & 2 == 2,
            'GO_PRE118_POINTER_OR_UNKNOWN_FORMAT_UNSUPPORTED')

    def string_at(offset):
        value, shift, count = 0, 0, 0
        while True:
            require(offset + count < len(raw) and count < 10, 'GO_STRING_VARINT_BOUND')
            byte = raw[offset + count]
            require(not (count == 9 and byte > 1), 'GO_STRING_VARINT_OVERFLOW')
            value |= (byte & 127) << shift
            count += 1
            if byte < 128:
                require(count == 1 or byte != 0, 'GO_STRING_NONCANONICAL_VARINT')
                break
            shift += 7
        begin = offset + count
        require(value <= 1024 * 1024 and begin + value <= len(raw), 'GO_STRING_DATA_BOUND')
        return raw[begin:begin + value], begin + value

    version, position = string_at(start + 32)
    modules, position = string_at(position)
    version = version.decode('ascii', errors='strict')
    require(re.fullmatch(r'go[0-9]{1,4}\.[0-9]{1,4}(?:\.[0-9]{1,4})?(?:[a-z]{1,8}[0-9]{1,4})?', version),
            'GO_BUILD_VERSION_FORM')
    if modules:
        require(len(modules) >= 33 and modules[-17] == 10, 'GO_MODULE_FRAMING')
        text = modules[16:-16].decode('utf-8', errors='strict')
        records = text.splitlines()
        require(len(records) <= 4096 and all(len(line) <= 65536 for line in records), 'GO_MODULE_RECORD_BOUND')
    else:
        records = []
    return {'present': True, 'format': 'Go>=1.18-inline', 'go_toolchain_version': version,
            'section_offset': section['offset'], 'section_bytes': section['bytes'],
            'section_sha256': hashlib.sha256(raw).hexdigest(), 'header_relative_offset': start,
            'module_info_raw_sha256': hashlib.sha256(modules).hexdigest(), 'module_record_lines': records,
            'record_parsing': 'Exact text lines; no eval/Go-quoted-string execution or module resolution.',
            'advisory_or_provenance_clearance': False}


def gosu_facts(native, config):
    candidates = [row for row in native['complete_vectors'] if 'usr/local/bin/gosu' in row['final_rootfs_paths']]
    require(len(candidates) == 1 and candidates[0]['go_buildinfo']['present'], 'EXACT_GOSU_GO_BUILDINFO_REQUIRED')
    environment = [value.partition('=')[2] for value in config['config']['Env'] if value.startswith('GOSU_VERSION=')]
    require(len(environment) == 1 and re.fullmatch('[A-Za-z0-9.+-]{1,64}', environment[0]), 'CONFIG_GOSU_VERSION')
    row = candidates[0]
    return {'format': 'stock-and-sleeve:gosu-go-build-facts:v1', 'gosu_config_version': environment[0],
            'exact_binary_content': {key: row['identity'][key] for key in ('bytes', 'sha256')},
            'verified_byte_equal_sources': row['byte_equal_sources'], 'final_rootfs_paths': row['final_rootfs_paths'],
            'go_buildinfo': row['go_buildinfo'], 'embedded_label_corroboration': row['embedded_version_labels'],
            'execution': False, 'advisory_support_or_signature_clearance': False,
            'official_format_reference': 'https://go.dev/src/debug/buildinfo/buildinfo.go'}


def output_inventory(layers):
    expected = {row['data_path']: row['payload_identity'] for layer in layers for row in layer['members']
                if row.get('data_path') is not None}
    inventory = []
    for created in tuple(CREATED):
        path = OUTPUT / created['path']
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        hashed, count = hashlib.sha256(), 0
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                    and (before.st_dev, before.st_ino) == (created['device'], created['inode']),
                    'OUTPUT_INVENTORY_IDENTITY')
            while chunk := stream.read(CHUNK):
                deadline()
                count += len(chunk)
                require(count <= MAX_MEMBER if path.name.endswith('.data') else count <= 32 * 1024 * 1024,
                        'OUTPUT_INVENTORY_BOUND')
                hashed.update(chunk)
            after = os.fstat(stream.fileno())
        require(count == before.st_size and
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                'OUTPUT_INVENTORY_CHANGED')
        actual = {'path': path.name, 'bytes': count, 'sha256': hashed.hexdigest(),
                  'device': before.st_dev, 'inode': before.st_ino, 'private_opaque_payload': path.name.endswith('.data')}
        if path.name.endswith('.data'):
            require(path.name in expected and all(actual[key] == expected[path.name][key]
                    for key in ('bytes', 'sha256', 'device', 'inode')), 'OUTPUT_PAYLOAD_INPUT_MISMATCH')
        inventory.append(actual)
    require({row['path'] for row in inventory if row['private_opaque_payload']} == set(expected),
            'ALL_PRIVATE_PAYLOADS_REHASHED')
    return {'format': 'stock-and-sleeve:private-output-hash-inventory:v1', 'files': inventory,
            'inventory_self_excluded': True, 'raw_payload_publication_authorized': False,
            'final_transport_inventory': 'Rootcommonexporter independentlyrehashes all allowlistedderivedreceipts after entry returns.'}


def package_and_notice_facts(rootfs):
    by_name = {row['path']: row for row in rootfs['members']}
    status = by_name.get('var/lib/dpkg/status')
    require(status is not None and status.get('effective_content'), 'FINAL_DPKG_STATUS_REQUIRED')
    content = status['effective_content']
    text = private_payload(content, 16 * 1024 * 1024).decode('utf-8', errors='strict')
    packages = []
    for paragraph in text.split('\n\n'):
        if not paragraph.strip():
            continue
        fields, previous = {}, None
        for line in paragraph.splitlines():
            if line.startswith((' ', '\t')):
                require(previous is not None, 'DPKG_CONTINUATION')
                fields[previous] += '\n' + line
            else:
                key, separator, value = line.partition(':')
                require(separator and re.fullmatch('[A-Za-z0-9-]+', key) and key not in fields, 'DPKG_FIELD_FORM')
                fields[key], previous = value.lstrip(' '), key
        require('Package' in fields and 'Version' in fields and len(packages) < 20000, 'DPKG_IDENTITY_COUNT')
        packages.append({key: fields.get(key) for key in ('Package', 'Version', 'Architecture', 'Source', 'Status')})
    ownership, notices, configs = [], [], []
    for name, row in sorted(by_name.items()):
        data = row.get('effective_content')
        if not data or data['data_path'] is None:
            continue
        if re.fullmatch(r'var/lib/dpkg/info/[^/]+\.(?:list|md5sums)', name):
            raw = private_payload(data, 16 * 1024 * 1024)
            ownership.append({'path': name, 'bytes': len(raw), 'sha256': data['sha256'],
                              'lines': raw.decode('utf-8', errors='strict').splitlines(),
                              'scope': 'Exact raw dpkg declaration; no symlink/path ownership inference.'})
        elif re.fullmatch(r'usr/share/doc/.+/(?:copyright|LICENSE[^/]*|COPYING[^/]*|NOTICE[^/]*)', name):
            raw = private_payload(data, 4 * 1024 * 1024)
            notices.append({'path': name, 'bytes': len(raw), 'sha256': data['sha256'],
                            'text': raw.decode('utf-8', errors='strict'), 'legal_approval': False})
        elif name in ('etc/os-release', 'usr/lib/os-release', 'etc/ld.so.conf') or name.startswith('etc/ld.so.conf.d/'):
            raw = private_payload(data, 4 * 1024 * 1024)
            configs.append({'path': name, 'bytes': len(raw), 'sha256': data['sha256'],
                            'text': raw.decode('utf-8', errors='strict')})
    return {'format': 'stock-and-sleeve:postgres-os-package-notice-data:v1', 'packages': packages,
            'dpkg_status_sha256': content['sha256'], 'dpkg_declared_paths_and_md5sums': ownership,
            'copyright_notice_data': notices, 'os_and_loader_configuration_data': configs,
            'advisory_or_legal_clearance': False, 'raw_payloads_publication_authorized': False}


def export_derived(profile_id, reports, rehash_all, manifest_fields=None):
    """SOURCE-ONLY common DATA export; embed byte-exact after independent review.

    No .data/archive/native payloads, arbitrary output roots, user URLs or new
    authority. The fixed profile owns the explicit report selection and its
    source/input/private-output rehash callback. All checks precede first frame.
    """
    import base64
    import gzip
    import hashlib
    import json
    import os
    from pathlib import Path
    import re
    import stat
    import sys

    RAW_FILE = 32 * 1024 * 1024
    RAW_TOTAL = 128 * 1024 * 1024
    GZIP_TOTAL = 16 * 1024 * 1024
    FILES = 1024
    FACT_NODES = 256
    FACT_KEYS_PER_GROUP = 64
    FACT_STRING_CHARACTERS = 512
    FACT_ENCODED_BYTES = 16 * 1024

    def check(value, code):
        if not value:
            raise ValueError('DATA_EXPORT_' + code)

    def duplicate_free(pairs):
        value = {}
        for key, item in pairs:
            check(key not in value, 'DUPLICATE_JSON_KEY')
            value[key] = item
        return value

    def finite(_):
        raise ValueError('DATA_EXPORT_NONFINITE_JSON')

    def snapshot(path, remaining=RAW_FILE):
        check(type(path) is Path or isinstance(path, Path), 'PATH_TYPE')
        check(path.is_absolute() and path.parent in (Path('/ci-evidence'), Path('/ci-evidence/reports')),
              'FIXED_REPORT_PARENT')
        for ancestor in (path, *path.parents):
            check(not ancestor.is_symlink(), 'SYMLINK')
        before = path.lstat()
        check(type(remaining) is int and 0 <= remaining <= RAW_TOTAL, 'REMAINING_RAW_BUDGET')
        check(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == os.getuid()
              and 0 < before.st_size <= min(RAW_FILE, remaining), 'REGULAR_OWNED_BOUNDED_FILE')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            check((opened.st_dev, opened.st_ino, opened.st_size) ==
                  (before.st_dev, before.st_ino, before.st_size), 'OPEN_IDENTITY')
            raw = stream.read(RAW_FILE + 1)
            after = os.fstat(stream.fileno())
        check(len(raw) == before.st_size and
              (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
              (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'FILE_CHANGED')
        current = path.lstat()
        check((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) ==
              (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'PATH_REPLACED')
        try:
            parsed = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=duplicate_free,
                                parse_constant=finite)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError('DATA_EXPORT_INVALID_UTF8_JSON') from error
        check(type(parsed) is dict, 'JSON_ROOT_OBJECT')
        return raw, {'decoded_bytes': len(raw), 'decoded_sha256': hashlib.sha256(raw).hexdigest(),
                     'device': after.st_dev, 'inode': after.st_ino}

    check(profile_id in ('debian-archives', 'postgres-rootfs'), 'FIXED_PROFILE')
    check(type(reports) is dict and 2 <= len(reports) < FILES and callable(rehash_all), 'REPORT_MAP')
    check({'bootstrap-receipt.json', 'data-inventory.json'} <= set(reports)
          and 'output-manifest.json' not in reports, 'COMMON_NAMES_AND_SELF_EXCLUSION')
    check(all(type(name) is str and re.fullmatch(r'[a-z][a-z0-9-]{0,95}\.json', name)
              for name in reports), 'FLAT_REVIEWED_NAMES')
    check(all(isinstance(path, Path) and path.name == name for name, path in reports.items()), 'NAME_PATH_BINDING')
    parents = {path.parent for path in reports.values()}
    check(len(parents) == 1 and next(iter(parents)) in (Path('/ci-evidence'), Path('/ci-evidence/reports')),
          'ONE_FIXED_REPORT_DIRECTORY')
    parent = next(iter(parents))
    fields = {} if manifest_fields is None else manifest_fields
    check(type(fields) is dict and len(fields) <= 2 and set(fields) <= {'inventory', 'limits'}
          and all(type(value) is dict for value in fields.values()), 'PROFILE_FACTS_ONLY')
    # The only caller facts are two bounded dictionaries of scalar values. No
    # arbitrary nested containers, recursive traversal, floats or unbounded ints.
    # Charge a conservative ensure_ascii=True JSON size before serialization.
    fact_nodes, fact_encoded_bound = 1, 2
    for group_name, group in fields.items():
        check(len(group) <= FACT_KEYS_PER_GROUP, 'FACT_GROUP_KEY_BOUND')
        fact_nodes += 1
        fact_encoded_bound += len(group_name) + 7
        for key, value in group.items():
            check(type(key) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key), 'FACT_KEY_FORM')
            fact_nodes += 1
            fact_encoded_bound += len(key) + 4
            if type(value) is str:
                check(len(value) <= FACT_STRING_CHARACTERS, 'FACT_STRING_BOUND')
                try:
                    value.encode('utf-8', errors='strict')
                except UnicodeError as error:
                    raise ValueError('DATA_EXPORT_FACT_STRING_UTF8') from error
                fact_encoded_bound += 12 * len(value) + 2
            elif type(value) is int:
                check(-(2 ** 63) <= value <= 2 ** 63 - 1, 'FACT_INTEGER_BOUND')
                fact_encoded_bound += 20
            elif value is None or type(value) is bool:
                fact_encoded_bound += 5
            else:
                raise ValueError('DATA_EXPORT_FACT_SCALAR_TYPE_AND_FIXED_DEPTH')
            check(fact_nodes <= FACT_NODES and fact_encoded_bound <= FACT_ENCODED_BYTES,
                  'FACT_NODE_OR_ENCODED_BOUND')

    raw_by_name, observed, total = {}, {}, 0
    for name, path in sorted(reports.items()):
        # Reserve against the aggregate before this report is read/allocated.
        raw, row = snapshot(path, RAW_TOTAL - total)
        total += len(raw)
        check(total <= RAW_TOTAL, 'TOTAL_RAW_BOUND')
        raw_by_name[name], observed[name] = raw, row
    manifest = {'format': 'stock-and-sleeve:owned-data-derived-export-manifest:v1',
                'profile_id': profile_id, 'successful': True,
                'derived_exports': [{'name': name, 'decoded_bytes': observed[name]['decoded_bytes'],
                                     'decoded_sha256': observed[name]['decoded_sha256']} for name in sorted(observed)],
                'self_excluded': True, 'raw_opaque_payload_exported': False,
                'profile_inventory': fields.get('inventory', {}), 'profile_limits': fields.get('limits', {}),
                'transport_limits': {'decoded_bytes_per_file': RAW_FILE, 'decoded_bytes_total': RAW_TOTAL,
                                     'gzip_bytes_total': GZIP_TOTAL, 'files_including_manifest': FILES},
                'profile_fact_limits': {'fixed_container_depth': 2, 'nodes': FACT_NODES,
                                        'keys_per_group': FACT_KEYS_PER_GROUP,
                                        'string_characters': FACT_STRING_CHARACTERS,
                                        'conservative_encoded_bytes': FACT_ENCODED_BYTES}}
    try:
        manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=True, allow_nan=False) + '\n').encode()
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError('DATA_EXPORT_MANIFEST_FACTS_JSON') from error
    check(0 < len(manifest_raw) <= RAW_FILE and total + len(manifest_raw) <= RAW_TOTAL, 'MANIFEST_SIZE')
    manifest_path = parent / 'output-manifest.json'
    descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(), 'MANIFEST_IDENTITY')
        stream.write(manifest_raw)
        stream.flush()
        os.fsync(stream.fileno())
    reread, row = snapshot(manifest_path)
    check(reread == manifest_raw, 'MANIFEST_READBACK')
    raw_by_name['output-manifest.json'], observed['output-manifest.json'] = reread, row
    check(len(raw_by_name) <= FILES, 'FINAL_REPORT_COUNT')

    prepared, gzip_bytes, payload_bytes = [], 0, 0
    for name, raw in sorted(raw_by_name.items()):
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        gzip_bytes += len(compressed)
        check(gzip_bytes <= GZIP_TOTAL, 'TOTAL_GZIP_BOUND')
        payload = base64.encodebytes(compressed)
        lines = payload.splitlines()
        check(payload.endswith(b'\n') and lines and all(len(line) == 76 for line in lines[:-1])
              and 1 <= len(lines[-1]) <= 76 and base64.decodebytes(payload) == compressed,
              'CANONICAL_76_LF_BASE64')
        payload_bytes += len(payload)
        header = {'name': name, 'encoding': 'gzip+base64',
                  'decoded_bytes': len(raw), 'decoded_sha256': hashlib.sha256(raw).hexdigest(),
                  'compressed_bytes': len(compressed), 'compressed_sha256': hashlib.sha256(compressed).hexdigest(),
                  'payload_bytes': len(payload), 'payload_sha256': hashlib.sha256(payload).hexdigest()}
        prepared.append((header, payload))
    # The source profile independently checks ALL source/input/opaque output and
    # derived files, including this one exact new manifest. It must return True.
    check(rehash_all() is True, 'PROFILE_FINAL_REHASH')
    for name, prior in observed.items():
        path = manifest_path if name == 'output-manifest.json' else reports[name]
        raw, current = snapshot(path)
        check(current == prior and raw == raw_by_name[name], 'REPORT_FINAL_BOOKEND')

    output = sys.stdout.buffer

    def write_all(raw):
        view = memoryview(raw)
        while view:
            count = output.write(view)
            check(type(count) is int and 0 < count <= len(view), 'STDOUT_SHORT_WRITE')
            view = view[count:]

    for header, payload in prepared:
        write_all(b'SS_DATA_JSON_BEGIN ' + json.dumps(header, sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n')
        write_all(payload)
        write_all(b'SS_DATA_JSON_END ' + json.dumps({'name': header['name']}, separators=(',', ':')).encode('ascii') + b'\n')
    write_all(b'SS_DATA_JSON_COMPLETE ' + json.dumps({'profile_id': profile_id,
              'reports': len(prepared), 'successful': True}, separators=(',', ':')).encode('ascii') + b'\n')
    output.flush()
    return {'profile_id': profile_id, 'reports': len(prepared), 'decoded_bytes': total + len(manifest_raw),
            'gzip_bytes': gzip_bytes, 'payload_bytes': payload_bytes,
            'manifest_decoded_sha256': observed['output-manifest.json']['decoded_sha256'],
            'all_source_input_private_output_rehash_callback': True,
            'all_reports_bookended': True, 'raw_opaque_payload_exported': False}


def main():
    global CORE
    require(sys.argv[1:] == [], 'FIXED_SCANNER_CLI_NO_ARGUMENTS')
    require(os.environ.get('SS_DATA_PROFILE_ID') == 'postgres-rootfs', 'EXACT_COMMON_PROFILE_ID')
    policy = json_file(CODE / 'admission.json')
    require(policy.get('execution_admission') == 'REVIEWED_DATA_ONLY_ROOTFS_JOB', 'SOURCE_ONLY_NO_DATA_EXECUTION_ADMISSION')
    require(sys.platform == 'linux' and platform.machine() == 'x86_64' and os.getuid() == 10001
            and sys.version_info[:3] == (3, 12, 16) and sys.flags.isolated and sys.flags.no_site
            and sys.flags.dont_write_bytecode and sys.flags.ignore_environment, 'ISOLATED_DATA_RUNTIME')
    interfaces = {line.split(':', 1)[0].strip() for line in Path('/proc/net/dev').read_text().splitlines() if ':' in line}
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    require(interfaces == {'lo'} and int(status['CapEff'].strip(), 16) == 0
            and status['NoNewPrivs'].strip() == '1', 'DATA_NETWORK_PRIVILEGE_BOUNDARY')
    require(not any(key in os.environ for key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME')),
            'AMBIENT_LOADER_OVERRIDE')
    manifest_path = CODE / 'source-manifest.json'
    manifest_info = manifest_path.lstat()
    require(stat.S_ISREG(manifest_info.st_mode) and not manifest_path.is_symlink()
            and manifest_info.st_size <= 4 * 1024 * 1024, 'SOURCE_MANIFEST_BOUND')
    manifest_raw = manifest_path.read_bytes()
    require(hashlib.sha256(manifest_raw).hexdigest() == os.environ.get('SS_DATA_SOURCE_MANIFEST_SHA256'),
            'EXTERNAL_SOURCE_MANIFEST_PIN')
    manifest = json.loads(manifest_raw, object_pairs_hook=unique)
    expected_names = {'pg_oci_data.py', 'strict_debian_tar_data.py', 'ci_elf_data.py', 'admission.json', 'input-spec.json'}
    require(type(manifest) is dict and set(manifest) == {'format', 'profile_id', 'files'}
            and manifest['format'] == 'stock-and-sleeve:owned-data-source-manifest:v1'
            and manifest['profile_id'] == 'postgres-rootfs'
            and type(manifest['files']) is list and len(manifest['files']) == 5
            and all(type(row) is dict and set(row) == {'name', 'bytes', 'sha256'} for row in manifest['files'])
            and {row['name'] for row in manifest['files']} == expected_names
            and all(type(row['bytes']) is int and 0 <= row['bytes'] <= 4 * 1024 * 1024
                    and re.fullmatch('[a-f0-9]{64}', row['sha256']) for row in manifest['files']),
            'SOURCE_MANIFEST_EXACT_TYPED_SELECTION')
    before = {row['name']: readonly_file(CODE / row['name'], row['bytes'], row['sha256']) for row in manifest['files']}
    before['source-manifest.json'] = readonly_file(manifest_path, len(manifest_raw), hashlib.sha256(manifest_raw).hexdigest())
    require(before['input-spec.json']['sha256'] == '9a9ad0ac3eb3ce7a75cc61b650bf21bd04a352dd88d57b0b9be5b07855bcb2b0'
            == os.environ.get('SS_DATA_INPUT_SPEC_SHA256') == policy['sealed_inputs']['input_spec_sha256'],
            'EXACT_NORMALIZED14_SPEC_AND_ENVIRONMENT')
    require({path.name for path in CODE.iterdir()} == set(before), 'EXACT_CONTROL_FILES')
    spec = json_file(CODE / 'input-spec.json')
    require(type(spec) is dict and set(spec) == {'format', 'profile_id', 'total_bytes', 'inputs'}
            and spec['format'] == 'stock-and-sleeve:postgres-rootfs-normalized-inputs:v1'
            and spec['profile_id'] == 'postgres-rootfs' and type(spec['total_bytes']) is int
            and spec['total_bytes'] == 168558897 and type(spec['inputs']) is list and len(spec['inputs']) == 14
            and all(type(row['bytes']) is int and 0 < row['bytes'] <= 128 * 1024 * 1024
                    and re.fullmatch('[a-f0-9]{64}', row['sha256'])
                    and row['digest'] == 'sha256:' + row['sha256']
                    and row['name'] == row['sha256'] + '.blob-data' for row in spec['inputs'])
            and sum(row['bytes'] for row in spec['inputs']) == 168558897, 'EXACT14_TYPED_INPUTS')
    names = {row['name'] for row in spec['inputs']}
    require(len(names) == 14 and {path.name for path in INPUT.iterdir()} == names, 'EXACT_INPUT_NAMES')
    input_before = {row['digest']: readonly_file(INPUT / row['name'],
                      row['bytes'], row['sha256']) for row in spec['inputs']}
    core = policy['strict_tar_core']
    readonly_file(CODE / 'strict_debian_tar_data.py', core['bytes'], core['sha256'])
    readonly_file(CODE / 'ci_elf_data.py', policy['elf_core']['bytes'], policy['elf_core']['sha256'])
    sys.path.insert(0, str(CODE))
    import strict_debian_tar_data as shared_core
    import ci_elf_data as elf_reader
    CORE = shared_core
    require(core['sha256'] == '837ba3df9fc30b6f34a21c4aa2e3491c8874a9d7219d2ec2add36fc2f498c821',
            'EXACT_SHARED_STRICT_TAR_CORE')
    bootstrap = bootstrap_receipt()
    save('bootstrap-receipt.json', bootstrap)
    source_sha = os.environ.get('SS_DATA_SOURCE_SHA')
    require(type(source_sha) is str and re.fullmatch('[a-f0-9]{40}', source_sha), 'EXACT_HOST_SOURCE_SHA')
    # First stdout record: detects lost/rolled prefixes before any archive decoding.
    print('SS_DATA_JSON_STREAM ' + json.dumps({'profile_id': 'postgres-rootfs', 'source_sha': source_sha},
          sort_keys=True, separators=(',', ':'), ensure_ascii=True), flush=True)
    corpus = domain_corpus()
    save('data-corpus.json', corpus)
    layers = [row for row in spec['inputs'] if row['kind'] == 'proposed_layer']
    config_pin = next(row for row in spec['inputs'] if row['kind'] == 'proposed_config')
    historical_pin = next(row for row in spec['inputs'] if row['kind'] == 'historical_config_only')
    config = json_file(INPUT / config_pin['name'])
    historical = json_file(INPUT / historical_pin['name'])
    require(config['architecture'] == historical['architecture'] == 'amd64'
            and config['os'] == historical['os'] == 'linux'
            and config['rootfs']['type'] == historical['rootfs']['type'] == 'layers'
            and len(layers) == len(config['rootfs']['diff_ids']) == 12
            and len(historical['rootfs']['diff_ids']) == 13,
            'CONFIG_PLATFORM_ORDERED_LAYER_COUNT')
    save('config-facts.json', {'historical_digest': historical_pin['digest'], 'proposed_digest': config_pin['digest'],
         'proposed_created': config.get('created'), 'proposed_diff_ids': config['rootfs']['diff_ids'],
         'historical_diff_ids': historical['rootfs']['diff_ids'],
         'proposed_public_pg_environment': [value for value in config['config']['Env']
             if re.match(r'^(?:PG_MAJOR|PG_VERSION|PGDATA|GOSU_VERSION)=', value)]})
    scanned = []
    for index, (pin, diff_id) in enumerate(zip(layers, config['rootfs']['diff_ids'], strict=True)):
        require(pin['layer_index'] == index and pin['media_type'] == 'application/vnd.oci.image.layer.v1.tar+gzip',
                'EXACT_ORDERED_GZIP_LAYER')
        save('layer-' + str(index) + '-started.json', {'layer_index': index, 'started_utc': datetime.now(timezone.utc).isoformat(),
             'pid': os.getpid(), 'compressed_digest': pin['digest'], 'expected_diff_id': diff_id})
        started_utc, started_wall, started_cpu = datetime.now(timezone.utc).isoformat(), time.monotonic(), time.process_time()
        layer = inspect_layer(INPUT / pin['name'], pin, diff_id,
                              index, shared_core.BoundedTarInfo)
        layer['timing'] = {'started_utc': started_utc, 'finished_utc': datetime.now(timezone.utc).isoformat(),
                           'elapsed_monotonic': time.monotonic() - started_wall,
                           'elapsed_process_cpu': time.process_time() - started_cpu}
        save('layer-' + str(index) + '-complete.json', layer)
        scanned.append(layer)
    rootfs = virtual_rootfs(scanned)
    save('final-virtual-rootfs.json', rootfs)
    native = native_data(rootfs, elf_reader)
    save('native-elf-data.json', native)
    save('gosu-go-build-facts.json', gosu_facts(native, config))
    save('os-package-notice-data.json', package_and_notice_facts(rootfs))
    save('layer-verification-summary.json', {'layer_count': 12, 'layers': [
        {key: layer[key] for key in ('layer_index', 'compressed_sha256', 'compressed_bytes', 'expanded_bytes',
            'diff_id', 'raw_header_count', 'metadata_header_count', 'dataset_raw_header_count',
            'dataset_metadata_header_count', 'first_zero_header_end', 'additional_zero_bytes',
            'complete_gzip_consumption', 'two_zero_blocks_and_aligned_zero_trailer', 'timing')}
        for layer in scanned]})
    after = {row['name']: readonly_file(CODE / row['name'], row['bytes'], row['sha256']) for row in manifest['files']}
    after['source-manifest.json'] = readonly_file(manifest_path, len(manifest_raw), hashlib.sha256(manifest_raw).hexdigest())
    input_after = {row['digest']: readonly_file(INPUT / row['name'],
                     row['bytes'], row['sha256']) for row in spec['inputs']}
    require(before == after and input_before == input_after, 'SOURCE_OR_INPUT_CHANGED')
    retained = {row['data_path']: row['payload_identity'] for layer in scanned for row in layer['members']
                if row.get('data_path') is not None}
    registered = {item['path']: item for item in CREATED}
    require(all(name in registered and all(registered[name][key] == identity[key] for key in ('device', 'inode'))
                for name, identity in retained.items()), 'RETAINED_REAL_LAYER_REGISTRATION')
    save('data-inventory.json', {'profile_id': 'postgres-rootfs', 'successful': True,
         'status': 'EXACT12_LAYER_DATA_INVENTORY_ONLY_NATIVE_OS_CRYPTO_ADVISORY_UNPROVEN',
         'passed': True, 'pid': os.getpid(), 'python': sys.version, 'zlib_compiled': zlib.ZLIB_VERSION,
         'zlib_runtime': zlib.ZLIB_RUNTIME_VERSION, 'interfaces': sorted(interfaces), 'uid': os.getuid(),
         'source_before': before, 'source_after': after, 'input_before': input_before, 'input_after': input_after,
         'layer_count': 12, 'expanded_bytes': sum(layer['expanded_bytes'] for layer in scanned),
         'retained_payload_bytes': sum(identity['bytes'] for identity in retained.values()),
         'expanded_budget_bytes': EXPANDED_BYTES, 'private_output_budget_bytes': OUTPUT_BYTES,
         'constructed_layer_expanded_budget_bytes': corpus['constructed_layer_expanded_budget_bytes'],
         'constructed_private_output_budget_bytes': corpus['constructed_private_output_budget_bytes'],
         'final_member_count': rootfs['member_count'], 'created_files': CREATED,
         'no_image_or_native_payload_or_provider_execution': True})
    inventory = output_inventory(scanned)
    save('output-inventory.json', inventory)

    def rehash_all():
        current_source = {row['name']: readonly_file(CODE / row['name'], row['bytes'], row['sha256'])
                          for row in manifest['files']}
        current_source['source-manifest.json'] = readonly_file(manifest_path, len(manifest_raw),
                                                              hashlib.sha256(manifest_raw).hexdigest())
        current_inputs = {row['digest']: readonly_file(INPUT / row['name'],
                          row['bytes'], row['sha256']) for row in spec['inputs']}
        require(current_source == before and current_inputs == input_before, 'EXPORT_SOURCE_INPUT_BOOKEND')
        require(bootstrap_receipt() == bootstrap, 'EXPORT_BOOTSTRAP_MODULE_PROVIDER_BOOKEND')
        fresh = output_inventory(scanned)
        expected = {row['path']: row for row in inventory['files']}
        actual = {row['path']: row for row in fresh['files']}
        require(set(actual) == set(expected) | {'output-inventory.json'}
                and all(actual[name] == value for name, value in expected.items()), 'EXPORT_PRIVATE_DERIVED_BOOKEND')
        output_names = {path.name for path in OUTPUT.iterdir()}
        require(output_names == set(actual) | {'output-manifest.json'}, 'EXPORT_EXACT_OUTPUT_NAMESPACE')
        export_manifest = OUTPUT / 'output-manifest.json'
        export_info = export_manifest.lstat()
        require(stat.S_ISREG(export_info.st_mode) and export_info.st_nlink == 1
                and export_info.st_uid == os.getuid() and 0 < export_info.st_size <= 32 * 1024 * 1024,
                'EXPORT_MANIFEST_REGULAR_OWNED')
        # The common exporter also bookends the manifest against its owned hash.
        raw_export_manifest = export_manifest.read_bytes()
        require(len(raw_export_manifest) == export_info.st_size, 'EXPORT_MANIFEST_FULL_REHASH')
        hashlib.sha256(raw_export_manifest).hexdigest()
        return True

    fixed_reports = {'bootstrap-receipt.json', 'data-inventory.json', 'config-facts.json',
                     'layer-verification-summary.json', 'final-virtual-rootfs.json', 'native-elf-data.json',
                     'os-package-notice-data.json', 'gosu-go-build-facts.json', 'output-inventory.json',
                     'data-corpus.json'}
    elf_reports = {row['derived_report'] for row in native['complete_vectors']}
    require(all(re.fullmatch(r'elf-[a-f0-9]{64}\.json', name) for name in elf_reports), 'EXACT_ELF_REPORT_NAMES')
    selected_reports = {name: OUTPUT / name for name in sorted(fixed_reports | elf_reports)}
    export_derived('postgres-rootfs', selected_reports, rehash_all,
                   {'inventory': {'inputs': 14, 'layers': 12, 'source_files': 6,
                                  'rootfs_members': rootfs['member_count'],
                                  'unique_native_contents': native['unique_elf_contents'],
                                  'raw_private_payloads_not_exported': True},
                    'limits': {'archive_expansion_bytes': MAX_EXPANDED,
                               'retained_private_output_bytes': MAX_OUTPUT}})
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # Preserve data/partial outputs. Never print raw path, blob, config or URI errors.
        accepted_types = (DataFailure,) if CORE is None else (DataFailure, CORE.DataFailure)
        code = str(error) if type(error) in accepted_types and re.fullmatch('[A-Z0-9_]{1,100}', str(error)) else 'DATA_FAILURE_REDACTED'
        failure = {'profile_id': 'postgres-rootfs', 'successful': False,
                   'error_type': type(error).__name__, 'error_code': code,
                   'raw_payload_or_exception_detail_printed': False}
        sys.stderr.write('SS_DATA_PROFILE_FAILURE ' +
            json.dumps(failure, sort_keys=True, separators=(',', ':')) + '\n')
        sys.stderr.flush()
        try:
            save('data-failure.json', {'passed': False, 'error_type': type(error).__name__, 'error_code': code,
                                     'created_files': CREATED, 'payload_execution': False, 'automatic_retry': False})
        except Exception:
            pass  # Typed failure above survives even if private storage is full.
        raise SystemExit(1)
