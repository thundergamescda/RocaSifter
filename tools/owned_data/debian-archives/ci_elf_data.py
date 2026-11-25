"""UNRUN bounded ELF64/le/amd64 facts reader. Never load or execute ELF code.

Public API: inspect_elf(path, identity). Identity has exactly bytes, sha256,
device and inode; numeric fields must be actual nonnegative ints, not bools.
The caller supplies a freshly owned opaque regular file. Writable mode is
allowed. This reader opens read-only/no-follow, checks identity and streamed
SHA before and after bounded seeks, and returns facts rather than ABI approval.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time


__all__ = ['ElfDataError', 'inspect_elf']
MAX_ELF_BYTES = 384 * 1024 * 1024
MAX_SECTION_READ = 32 * 1024 * 1024
MAX_SYMBOLS = 262144
MAX_NAME_BYTES = 16 * 1024 * 1024
MAX_CACHED_STRING_BYTES = 64 * 1024 * 1024
MAX_METADATA_ENTRIES = 131072
MAX_STRING_ENTRIES = 1048576
MAX_RESULT_BYTES = 32 * 1024 * 1024
CHUNK = 65536


class ElfDataError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ElfDataError(code)


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_nlink,
            info.st_mtime_ns, info.st_ctime_ns)


class _View:
    def __init__(self, path, identity):
        require(sys.platform == 'linux', 'OWNED_LINUX_DATA_CONTEXT_REQUIRED')
        require(type(identity) is dict and set(identity) == {'bytes', 'sha256', 'device', 'inode'},
                'EXACT_ELF_IDENTITY_SCHEMA')
        require(all(type(identity[key]) is int and identity[key] >= 0
                    for key in ('bytes', 'device', 'inode'))
                and 64 <= identity['bytes'] <= MAX_ELF_BYTES
                and type(identity['sha256']) is str
                and re.fullmatch(r'[a-f0-9]{64}', identity['sha256']), 'ELF_IDENTITY_FIELDS')
        self.path, self.identity = Path(path), dict(identity)
        require(self.path.is_absolute(), 'ABSOLUTE_OWNED_DATA_PATH_REQUIRED')
        require(all(not parent.is_symlink() for parent in (self.path, *self.path.parents)),
                'ELF_DATA_ANCESTOR_SYMLINK')
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self.started = time.monotonic()
            self.name_bytes = 0
            self.string_entries = 0
            self.cached_string_bytes = 0
            self.metadata_entries = 0
            self.string_cache = {}
            self.initial = os.fstat(self.fd)
            require(stat.S_ISREG(self.initial.st_mode) and self.initial.st_nlink == 1
                    and (self.initial.st_dev, self.initial.st_ino, self.initial.st_size) ==
                        (identity['device'], identity['inode'], identity['bytes']), 'ELF_OPEN_IDENTITY')
        except BaseException:
            os.close(self.fd)
            raise

    def deadline(self):
        require(time.monotonic() - self.started <= 60, 'ELF_DATA_DEADLINE')

    def read(self, offset, size):
        self.deadline()
        require(type(offset) is int and type(size) is int and 0 <= size <= MAX_SECTION_READ
                and 0 <= offset <= self.identity['bytes']
                and size <= self.identity['bytes'] - offset, 'ELF_BOUNDED_RANGE')
        result = bytearray()
        while len(result) < size:
            part = os.pread(self.fd, min(CHUNK, size - len(result)), offset + len(result))
            require(bool(part), 'ELF_RANGE_TRUNCATED')
            result.extend(part)
            self.deadline()
        return bytes(result)

    def strings(self, offset, size):
        key = (offset, size)
        if key not in self.string_cache:
            require(type(size) is int and 0 <= size <= MAX_SECTION_READ
                    and self.cached_string_bytes + size <= MAX_CACHED_STRING_BYTES,
                    'ELF_AGGREGATE_STRING_CACHE_BOUND')
            self.cached_string_bytes += size
            self.string_cache[key] = self.read(offset, size)
        return self.string_cache[key]

    def charge_metadata(self, count):
        require(type(count) is int and count >= 0
                and self.metadata_entries + count <= MAX_METADATA_ENTRIES,
                'ELF_METADATA_ENTRY_AGGREGATE_BOUND')
        self.metadata_entries += count

    def hash_and_identity(self):
        hashed, offset = hashlib.sha256(), 0
        while offset < self.identity['bytes']:
            self.deadline()
            part = os.pread(self.fd, min(CHUNK, self.identity['bytes'] - offset), offset)
            require(bool(part), 'ELF_HASH_TRUNCATED')
            hashed.update(part)
            offset += len(part)
        after, named = os.fstat(self.fd), self.path.lstat()
        require(_stamp(after) == _stamp(self.initial) == _stamp(named)
                and stat.S_ISREG(named.st_mode) and not self.path.is_symlink()
                and hashed.hexdigest() == self.identity['sha256'], 'ELF_BYTES_OR_IDENTITY_CHANGED')
        return hashed.hexdigest()

    def text(self, raw, offset):
        self.deadline()
        require(type(offset) is int and 0 <= offset < len(raw), 'ELF_STRING_OFFSET')
        end = raw.find(b'\x00', offset, min(len(raw), offset + 4096))
        require(end >= offset, 'ELF_STRING_TERMINATOR_BOUND')
        # Count entries and the terminator even for an empty name, before
        # materializing or appending a string.
        require(self.string_entries + 1 <= MAX_STRING_ENTRIES
                and self.name_bytes + (end - offset) + 1 <= MAX_NAME_BYTES,
                'ELF_STRING_AGGREGATE_BOUND')
        self.string_entries += 1
        self.name_bytes += (end - offset) + 1
        return raw[offset:end].decode('utf-8', errors='surrogateescape')

    def close(self):
        os.close(self.fd)


def _sections(view, header):
    offset = struct.unpack_from('<Q', header, 40)[0]
    width, count, name_index = struct.unpack_from('<HHH', header, 58)
    require(width == 64 and 0 < count <= 4096 and 0 < name_index < count,
            'ELF_SECTION_HEADER_LAYOUT')
    table = view.read(offset, width * count)
    rows = []
    for index in range(count):
        name_at, kind, flags, address, start, size, link, info, alignment, entry = \
            struct.unpack_from('<IIQQQQIIQQ', table, index * width)
        require(kind == 8 or start <= view.identity['bytes'] and size <= view.identity['bytes'] - start,
                'ELF_SECTION_FILE_RANGE')
        rows.append({'index': index, 'name_offset': name_at, 'type': kind, 'flags': flags,
                     'address': address, 'offset': start, 'bytes': size, 'link': link,
                     'info': info, 'alignment': alignment, 'entry_bytes': entry})
    names = rows[name_index]
    require(names['type'] == 3, 'ELF_SECTION_NAMES_TYPE')
    raw_names = view.strings(names['offset'], names['bytes'])
    for row in rows:
        row['name'] = view.text(raw_names, row.pop('name_offset'))
    return rows


def _programs(view, header):
    offset = struct.unpack_from('<Q', header, 32)[0]
    width, count = struct.unpack_from('<HH', header, 54)
    require(count <= 4096 and (count == 0 or width == 56), 'ELF_PROGRAM_HEADER_LAYOUT')
    table = view.read(offset, width * count) if count else b''
    rows, interpreters = [], []
    for index in range(count):
        kind, flags, start, virtual, physical, file_size, memory_size, alignment = \
            struct.unpack_from('<IIQQQQQQ', table, index * width)
        require(start <= view.identity['bytes'] and file_size <= view.identity['bytes'] - start
                and (kind != 1 or file_size <= memory_size), 'ELF_PROGRAM_FILE_RANGE')
        rows.append({'index': index, 'type': kind, 'flags': flags, 'offset': start,
                     'virtual_address': virtual, 'physical_address': physical,
                     'file_bytes': file_size, 'memory_bytes': memory_size, 'alignment': alignment})
        if kind == 3:
            require(0 < file_size <= 4096, 'ELF_INTERPRETER_BOUND')
            raw = view.read(start, file_size)
            require(raw[-1:] == b'\x00' and b'\x00' not in raw[:-1], 'ELF_INTERPRETER_FORM')
            interpreters.append(raw[:-1].decode('utf-8', errors='surrogateescape'))
    require(len(interpreters) <= 1, 'ELF_INTERPRETER_UNIQUENESS')
    return rows, interpreters[0] if interpreters else None


def _inspect(view):
    header = view.read(0, 64)
    require(header[:7] == b'\x7fELF\x02\x01\x01' and struct.unpack_from('<H', header, 18)[0] == 62
            and struct.unpack_from('<H', header, 52)[0] == 64, 'ELF64_LE_AMD64_HEADER')
    elf_type = struct.unpack_from('<H', header, 16)[0]
    require(elf_type in (1, 2, 3), 'ELF_TYPE_UNSUPPORTED')
    sections = _sections(view, header)
    programs, interpreter = _programs(view, header)
    def linked_strings(row):
        require(0 <= row['link'] < len(sections) and sections[row['link']]['type'] == 3,
                'ELF_LINKED_STRING_TABLE')
        linked = sections[row['link']]
        # Aliased section indices share one range-keyed cache allocation.
        return view.strings(linked['offset'], linked['bytes'])

    needed, sonames, rpath, runpath = [], [], [], []
    version_need, version_def = [], []
    required_by_index, defined_by_index = {}, {}
    dynamic_sections = [row for row in sections if row['type'] == 6]
    require(len(dynamic_sections) <= 1, 'ELF_DYNAMIC_UNIQUENESS')
    for row in dynamic_sections:
        require(row['bytes'] % 16 == 0, 'ELF_DYNAMIC_ENTRY_WIDTH')
        view.charge_metadata(row['bytes'] // 16)
        raw, strings = view.read(row['offset'], row['bytes']), linked_strings(row)
        require(len(raw) % 16 == 0, 'ELF_DYNAMIC_ENTRY_WIDTH')
        terminated = False
        for at in range(0, len(raw), 16):
            tag, value = struct.unpack_from('<qQ', raw, at)
            if tag == 0:
                terminated = True
                break
            if tag == 1:
                needed.append(view.text(strings, value))
            elif tag == 14:
                sonames.append(view.text(strings, value))
            elif tag == 15:
                rpath.append(view.text(strings, value))
            elif tag == 29:
                runpath.append(view.text(strings, value))
        require(terminated, 'ELF_DYNAMIC_TERMINATOR')
    require(len(needed) == len(set(needed)) and len(sonames) <= 1
            and len(rpath) <= 1 and len(runpath) <= 1, 'ELF_DYNAMIC_STRING_UNIQUENESS')

    for row in sections:
        if row['type'] not in (0x6ffffffe, 0x6ffffffd):
            continue
        raw, strings = view.read(row['offset'], row['bytes']), linked_strings(row)
        require(bool(raw), 'ELF_VERSION_SECTION_EMPTY')
        at, visited, records = 0, set(), 0
        while True:
            view.deadline()
            require(at not in visited and records < 4096, 'ELF_VERSION_CHAIN_BOUND')
            visited.add(at)
            records += 1
            view.charge_metadata(1)
            if row['type'] == 0x6ffffffe:
                require(at + 16 <= len(raw), 'ELF_VERNEED_HEADER_BOUND')
                revision, count, file_at, aux_at, next_at = struct.unpack_from('<HHIII', raw, at)
                require(revision == 1 and 0 < count <= 4096 and aux_at >= 16, 'ELF_VERNEED_HEADER')
                provider = view.text(strings, file_at)
                view.charge_metadata(count)
                aux = at + aux_at
                auxiliary_seen = set()
                for index in range(count):
                    require(aux not in auxiliary_seen and aux + 16 <= len(raw), 'ELF_VERNEED_AUX_BOUND')
                    auxiliary_seen.add(aux)
                    hashed, flags, other, name_at, onward = struct.unpack_from('<IHHII', raw, aux)
                    version = view.text(strings, name_at)
                    record = {'provider': provider, 'version': version, 'flags': flags,
                              'hash': hashed, 'index_raw': other, 'index': other & 0x7fff}
                    require(record['index'] >= 2 and record['index'] not in required_by_index,
                            'ELF_REQUIRED_VERSION_INDEX_UNIQUENESS')
                    required_by_index[record['index']] = record
                    version_need.append(record)
                    require((index + 1 == count and onward == 0) or
                            (index + 1 < count and onward >= 16), 'ELF_VERNEED_AUX_CHAIN')
                    aux += onward
                minimum = aux + 16 - at
            else:
                require(at + 20 <= len(raw), 'ELF_VERDEF_HEADER_BOUND')
                revision, flags, version_index, count, hashed, aux_at, next_at = \
                    struct.unpack_from('<HHHHIII', raw, at)
                require(revision == 1 and 0 < count <= 4096 and aux_at >= 20, 'ELF_VERDEF_HEADER')
                view.charge_metadata(count)
                aux, names, auxiliary_seen = at + aux_at, [], set()
                for index in range(count):
                    require(aux not in auxiliary_seen and aux + 8 <= len(raw), 'ELF_VERDEF_AUX_BOUND')
                    auxiliary_seen.add(aux)
                    name_at, onward = struct.unpack_from('<II', raw, aux)
                    names.append(view.text(strings, name_at))
                    require((index + 1 == count and onward == 0) or
                            (index + 1 < count and onward >= 8), 'ELF_VERDEF_AUX_CHAIN')
                    aux += onward
                record = {'index_raw': version_index, 'index': version_index & 0x7fff,
                          'flags': flags, 'hash': hashed, 'name': names[0], 'parents': names[1:]}
                require(record['index'] >= 1 and record['index'] not in defined_by_index,
                        'ELF_DEFINED_VERSION_INDEX_UNIQUENESS')
                defined_by_index[record['index']] = record
                version_def.append(record)
                minimum = aux + 8 - at
            if next_at == 0:
                break
            require(next_at >= minimum and at + next_at < len(raw), 'ELF_VERSION_RECORD_PROGRESS')
            at += next_at
        require(row['info'] in (0, records), 'ELF_VERSION_SECTION_COUNT')

    undefined, defined, null_symbols, tables = [], [], [], []
    total_symbols = 0
    for row in sections:
        if row['type'] != 11:
            continue
        require(row['entry_bytes'] == 24 and row['bytes'] % 24 == 0, 'ELF_DYNSYM_WIDTH')
        count = row['bytes'] // 24
        total_symbols += count
        require(total_symbols <= MAX_SYMBOLS, 'ELF_DYNAMIC_SYMBOL_COUNT_BOUND')
        raw, strings = view.read(row['offset'], row['bytes']), linked_strings(row)
        versions = [item for item in sections if item['type'] == 0x6fffffff and item['link'] == row['index']]
        require(len(versions) <= 1, 'ELF_VERSYM_UNIQUENESS')
        version_raw = None
        if versions:
            require(versions[0]['bytes'] == count * 2, 'ELF_VERSYM_SIZE')
            version_raw = view.read(versions[0]['offset'], versions[0]['bytes'])
        tables.append({'section_index': row['index'], 'entries': count,
                       'versym_section_index': versions[0]['index'] if versions else None})
        for index in range(count):
            if index % 1024 == 0:
                view.deadline()
            name_at, info, other, section_index, value, size = struct.unpack_from('<IBBHQQ', raw, index * 24)
            require(section_index != 0xffff, 'ELF_EXTENDED_SYMBOL_INDEX_UNSUPPORTED')
            name = view.text(strings, name_at)
            raw_index = struct.unpack_from('<H', version_raw, index * 2)[0] if version_raw is not None else None
            version_index = raw_index & 0x7fff if raw_index is not None else None
            is_defined = section_index != 0
            definition = defined_by_index.get(version_index) if version_index is not None and version_index >= 2 else None
            requirement = required_by_index.get(version_index) if version_index is not None and version_index >= 2 else None
            require(version_index is None or version_index < 2 or
                    (definition is not None or requirement is not None), 'ELF_SYMBOL_VERSION_INDEX_MISSING')
            # Executable COPY-relocation definitions can retain a needed
            # version. Preserve that fact instead of inventing a verdef.
            reference = definition if is_defined and definition is not None else requirement
            version_source = 'definition' if reference is definition and definition is not None else \
                             'requirement' if reference is requirement and requirement is not None else None
            symbol = {'table_section_index': row['index'], 'index': index, 'name': name,
                'binding': info >> 4, 'symbol_type': info & 15, 'visibility': other & 3,
                'other_raw': other, 'section_index': section_index, 'defined': is_defined,
                'weak': info >> 4 == 2, 'value': value, 'bytes': size,
                'version_index': version_index, 'version_index_raw': raw_index,
                'version_hidden': bool(raw_index & 0x8000) if raw_index is not None else False,
                'unversioned': version_index is None or version_index < 2,
                'version_source': version_source,
                'provider': reference['provider'] if version_source == 'requirement' else None,
                'version': reference['name' if version_source == 'definition' else 'version'] if reference is not None else None,
                'binding_visibility_candidate': info >> 4 in (1, 2, 10) and other & 3 in (0, 3)}
            if name == '' and index == 0:
                null_symbols.append(symbol)
            else:
                (defined if is_defined else undefined).append(symbol)
    require(total_symbols == len(undefined) + len(defined) + len(null_symbols), 'ELF_ALL_SYMBOLS_ACCOUNTED')
    return {'format': 'stock-and-sleeve:bounded-elf-data-facts:v1', 'architecture': 'ELF64/le/amd64',
            'elf_type': elf_type, 'entry_address': struct.unpack_from('<Q', header, 24)[0],
            'identity': dict(view.identity), 'interpreter': interpreter,
            'sections': sections, 'program_headers': programs, 'needed': needed,
            'soname': sonames[0] if sonames else None, 'rpath': rpath, 'runpath': runpath,
            'required_symbol_versions': version_need, 'defined_version_records': version_def,
            'exported_versions': sorted({row['name'] for row in version_def}),
            'dynamic_symbol_tables': tables, 'dynamic_symbol_entry_count': total_symbols,
            'undefined_symbols': undefined, 'defined_dynamic_symbols': defined,
            'null_dynamic_symbols': null_symbols, 'native_execution': False,
            'data_resource_usage': {'cached_string_bytes': view.cached_string_bytes,
                'metadata_entries': view.metadata_entries, 'string_entries': view.string_entries,
                'name_bytes_including_terminators': view.name_bytes},
            'limit': 'Section-derived byte facts only; PT_DYNAMIC/loadable-segment consistency is not proven. No loader use, ABI/provider/advisory/license or execution approval.'}


def inspect_elf(path, identity):
    """Return full bounded JSON-safe facts from the exact owned opaque file."""
    view = None
    try:
        view = _View(path, identity)
        view.hash_and_identity()
        result = _inspect(view)
        view.hash_and_identity()
        encoded_bytes = 0
        for index, part in enumerate(json.JSONEncoder(ensure_ascii=True, sort_keys=True,
                separators=(',', ':'), allow_nan=False).iterencode(result)):
            if index % 4096 == 0:
                view.deadline()
            encoded_bytes += len(part.encode('ascii'))
            require(encoded_bytes <= MAX_RESULT_BYTES - 128, 'ELF_RESULT_JSON_BOUND')
        result['encoded_fact_bytes_before_size_field'] = encoded_bytes
        return result
    finally:
        if view is not None:
            view.close()
