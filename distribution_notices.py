"""Read-only validation of the exact reviewed third-party notice payload.

The manifest is data, never executable configuration. Only its pinned bytes may
define names; all names are checked before any licence content is opened.
"""
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat

NOTICE_DIRECTORY='third_party_notices'
REVIEWED_MANIFEST_SHA256='ceb774608f656daaf592d27cb6a3f5d024ebbf6e12eb31f9978bdac84a239ae6'
REVIEWED_FILE_COUNT=142
MANIFEST_NAME='source-manifest.json'
MAX_MANIFEST_BYTES=512*1024


class NoticePayloadError(ValueError):
    pass


def _fail():
    raise NoticePayloadError('The third-party notice payload does not match the reviewed distribution. Restore the complete original notice directory.')


def _parts(value):
    if not isinstance(value,str) or not value or '\\' in value or ':' in value:_fail()
    parts=value.split('/')
    for part in parts:
        if (not part or part in ('.','..') or part[-1] in (' ','.')
            or any(ord(c)<32 or ord(c)==127 or c in '<>"|?*' for c in part)
            or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',part,re.I)):_fail()
    if PurePosixPath(value).is_absolute():_fail()
    return parts


def _info(path,*,directory=False):
    info=path.lstat()
    # OneDrive placeholders may be reparse points without redirecting names.
    # Symlinks/junctions and other name-surrogate reparse points are rejected.
    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_reparse_tag',0)&0x20000000:_fail()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):_fail()
    return info


def _read(path,limit):
    before=_info(path)
    if before.st_size>limit:_fail()
    flags=os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0)
    descriptor=os.open(path,flags)
    with os.fdopen(descriptor,'rb') as stream:
        opened=os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino):_fail()
        value=stream.read(limit+1)
        after=os.fstat(stream.fileno())
    if len(value)>limit or (after.st_size,after.st_mtime_ns)!=(opened.st_size,opened.st_mtime_ns):_fail()
    return value


def validate_notice_payload(directory):
    """Return sorted relative POSIX names mapped to absolute regular file paths.

    This function only reads the fixed manifest and reviewed licence files. It
    never creates a directory, repairs a file, extracts an archive or reads the
    contents of an unknown payload path.
    """
    try:
        root=Path(directory).absolute()
        for parent in reversed((root,*root.parents)):_info(parent,directory=True)
        manifest_path=root/MANIFEST_NAME
        manifest_bytes=_read(manifest_path,MAX_MANIFEST_BYTES)
        if hashlib.sha256(manifest_bytes).hexdigest()!=REVIEWED_MANIFEST_SHA256:_fail()
        value=json.loads(manifest_bytes.decode('utf-8'))
        if not isinstance(value,dict) or type(value.get('format_version')) is not int or value['format_version']!=1:_fail()
        rows=value.get('files')
        if not isinstance(rows,list) or len(rows)!=REVIEWED_FILE_COUNT-1:_fail()
        expected={MANIFEST_NAME:None};aliases={MANIFEST_NAME.casefold()};directories=set()
        for row in rows:
            if not isinstance(row,dict):_fail()
            name=row.get('path');parts=_parts(name)
            if name.casefold() in aliases or Path(name).suffix.casefold()=='.json':_fail()
            if (type(row.get('bytes')) is not int or not 0<=row['bytes']<=8*1024*1024
                or not isinstance(row.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',row['sha256'])):_fail()
            aliases.add(name.casefold());expected[name]=row
            directories.update('/'.join(parts[:i]) for i in range(1,len(parts)))
        if any(name in expected for name in directories):_fail()
        directory_aliases={}
        for name in sorted(directories):
            alias=name.casefold()
            if alias in aliases or alias in directory_aliases and directory_aliases[alias]!=name:_fail()
            directory_aliases[alias]=name
        found={};seen=set();pending=[root]
        while pending:
            folder=pending.pop();_info(folder,directory=True)
            for path in sorted(folder.iterdir(),key=lambda p:p.name):
                name=path.relative_to(root).as_posix();_parts(name)
                if name.casefold() in seen or name not in expected and name not in directories:_fail()
                seen.add(name.casefold())
                if name in directories:
                    _info(path,directory=True);pending.append(path)
                else:
                    _info(path);found[name]=path
        if set(found)!=set(expected) or len(found)!=REVIEWED_FILE_COUNT:_fail()
        # Unknown names/types and case aliases have all been rejected. Only now
        # open reviewed licence content, with exact size and hash verification.
        for name,row in sorted(expected.items()):
            if row is None:continue
            path=found[name]
            for parent in path.parents:
                _info(parent,directory=True)
                if parent==root:break
            data=_read(path,row['bytes'])
            if len(data)!=row['bytes'] or hashlib.sha256(data).hexdigest()!=row['sha256']:_fail()
        if _read(manifest_path,MAX_MANIFEST_BYTES)!=manifest_bytes:_fail()
        return {name:found[name] for name in sorted(found)}
    except NoticePayloadError:raise
    except (OSError,ValueError,TypeError,UnicodeError,RecursionError):
        _fail()


def validate_build_environment(directory):
    """Require the notice-reviewed interpreter and wheels before a new build.

    This inspects version metadata only and returns public component versions.
    It neither imports those third-party packages nor installs or repairs them.
    """
    files=validate_notice_payload(directory)
    raw=_read(files[MANIFEST_NAME],MAX_MANIFEST_BYTES)
    if hashlib.sha256(raw).hexdigest()!=REVIEWED_MANIFEST_SHA256:_fail()
    manifest=json.loads(raw.decode('utf-8'));expected_python=manifest.get('python');packages=manifest.get('reviewed_packages')
    message='The build environment does not match the reviewed Python and package versions. Use the pinned release environment.'
    if not isinstance(expected_python,str) or platform.python_version()!=expected_python:
        raise NoticePayloadError(message)
    if not isinstance(packages,dict) or not packages:raise NoticePayloadError(message)
    installed={}
    for name,version in sorted(packages.items()):
        if not isinstance(name,str) or not name or not isinstance(version,str) or not version:raise NoticePayloadError(message)
        try:actual=metadata.version(name)
        except metadata.PackageNotFoundError:raise NoticePayloadError(message) from None
        if actual!=version:raise NoticePayloadError(message)
        installed[name]=actual
    return {'python':expected_python,'packages':installed,'manifest_sha256':REVIEWED_MANIFEST_SHA256}
