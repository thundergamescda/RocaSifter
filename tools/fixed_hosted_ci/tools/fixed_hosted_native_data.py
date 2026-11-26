"""Exact generic ELF version/base-data functions copied from reviewed ordinary CI source."""
from pathlib import Path
import hashlib
import re
import stat
import struct

def regular(path, limit):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("REGULAR_FILE_BOUND")
    raw = path.read_bytes()
    if len(raw) != info.st_size:
        raise ValueError("FILE_CHANGED")
    return raw


def elf_versions(raw):
    """Read ELF64/amd64 exported version names without loading the library."""
    if len(raw) < 64 or raw[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', raw, 18)[0] != 62:
        raise ValueError('ELF_ARCHITECTURE')
    offset = struct.unpack_from('<Q', raw, 40)[0]
    width, count = struct.unpack_from('<HH', raw, 58)
    if width != 64 or count > 4096 or offset + width * count > len(raw):
        raise ValueError('ELF_SECTION_BOUND')

    def string(start):
        end = raw.find(b'\0', start, start + 4096)
        if not 0 <= start < len(raw) or end < 0:
            raise ValueError('ELF_STRING_BOUND')
        return raw[start:end].decode('ascii')

    exported = set()
    for index in range(count):
        section = offset + index * width
        if struct.unpack_from('<I', raw, section + 4)[0] != 0x6ffffffd:
            continue
        start, size = struct.unpack_from('<QQ', raw, section + 24)
        link = struct.unpack_from('<I', raw, section + 40)[0]
        if start + size > len(raw) or link >= count:
            raise ValueError('ELF_VERSION_BOUND')
        strings = struct.unpack_from('<Q', raw, offset + link * width + 24)[0]
        cursor = start
        for _ in range(4096):
            if cursor + 20 > start + size:
                raise ValueError('ELF_VERSION_ENTRY')
            auxiliary, following = struct.unpack_from('<II', raw, cursor + 12)
            if cursor + auxiliary + 8 > start + size:
                raise ValueError('ELF_VERSION_AUXILIARY')
            name = struct.unpack_from('<I', raw, cursor + auxiliary)[0]
            exported.add(string(strings + name))
            if following == 0:
                break
            if following < 20:
                raise ValueError('ELF_VERSION_PROGRESS')
            cursor += following
        else:
            raise ValueError('ELF_VERSION_LIMIT')
    return exported


def base_native(admission):
    result = []
    for requirement in admission['base_symbol_requirements']:
        soname = requirement['soname']
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+', soname):
            raise ValueError('SONAME')
        candidates = []
        for directory in ('/lib/x86_64-linux-gnu', '/usr/lib/x86_64-linux-gnu', '/lib64'):
            path = Path(directory) / soname
            if path.exists():
                resolved = path.resolve(strict=True)
                if not (resolved.is_relative_to('/usr/lib') or resolved.is_relative_to('/lib')):
                    raise ValueError('BASE_LIBRARY_PATH')
                candidates.append(resolved)
        candidates = sorted(set(candidates))
        if len(candidates) != 1:
            raise ValueError('BASE_LIBRARY_MISSING_OR_AMBIGUOUS')
        raw = regular(candidates[0], 128 * 1024 * 1024)
        exported = elf_versions(raw)
        if not set(requirement['required_versions']) <= exported:
            raise ValueError('BASE_SYMBOL_VERSION_MISSING')
        result.append({'soname': soname, 'path': str(candidates[0]), 'bytes': len(raw),
                       'sha256': hashlib.sha256(raw).hexdigest(),
                       'required_versions': requirement['required_versions'],
                       'exported_version_count': len(exported)})
    return result


