"""SOURCE-ONLY shared strict TAR DATA core, not an archive execution authority.

Raw header/error/type/counter logic is byte-exact from the independently
source-reviewed fixed Debian reader. The termination block differs only by
fixed indentation inside its wrapper, with a complete inverse in provenance.
No links/modes/filesystem member paths or payloads are applied/executed.
"""
from pathlib import PurePosixPath
import tarfile
import time

CHUNK = 65536
MAX_MEMBER = 384 * 1024 * 1024
STARTED = time.monotonic()


class DataFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise DataFailure(code)


def deadline():
    require(time.monotonic() - STARTED <= 180, 'DATA_DEADLINE')


class BoundedTarInfo(tarfile.TarInfo):
    header_count = 0
    metadata_count = 0
    terminal_seen = False

    @classmethod
    def frombuf(cls, buf, encoding, errors):
        try:
            member = super().frombuf(buf, encoding, errors)
        except tarfile.EOFHeaderError:
            # Only one complete all-zero block may terminate iteration. The
            # second block and all trailer bytes are checked through the tar
            # stream below, including its private read-ahead buffer.
            require(len(buf) == tarfile.BLOCKSIZE and buf == b'\x00' * tarfile.BLOCKSIZE
                    and not cls.terminal_seen, 'TAR_TERMINATOR_FORM')
            cls.terminal_seen = True
            raise
        except tarfile.HeaderError as error:
            # TarFile.next normally treats several later-header errors as EOF.
            # A distinct exception must escape that suppression.
            raise DataFailure('TAR_MALFORMED_TRUNCATED_OR_MISSING_HEADER') from error
        cls.header_count += 1
        require(cls.header_count <= 20000 and member.size <= MAX_MEMBER
                and member.type != tarfile.GNUTYPE_SPARSE, 'TAR_RAW_HEADER_BOUND')
        require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.LNKTYPE,
                                tarfile.SYMTYPE, tarfile.DIRTYPE, tarfile.XHDTYPE,
                                tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME,
                                tarfile.GNUTYPE_LONGLINK), 'TAR_UNSUPPORTED_RAW_TYPE')
        # Solaris PAX b'X', sparse and unknown raw types fail above before
        # stdlib metadata processing. Supported extensions retain exact bounds.
        if member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE,
                           tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            cls.metadata_count += 1
            require(member.size <= 65536 and cls.metadata_count <= 128, 'TAR_EXTENDED_METADATA_BOUND')
        return member


def canonical_member(name, is_directory):
    require(type(name) is str and 0 < len(name) <= 2048
            and '\x00' not in name and '\\' not in name and ':' not in name
            and not name.startswith('/'), 'TAR_PATH_FORM')
    while name.startswith('./'):
        name = name[2:]
    if name in ('', '.'):
        require(is_directory, 'TAR_ROOT_KIND')
        return '.'
    parts = PurePosixPath(name).parts
    require(all(part not in ('', '.', '..') for part in parts), 'TAR_PATH_COMPONENT')
    normalized = '/'.join(parts)
    normalized.encode('utf-8', errors='strict')
    return normalized


def begin_tar_dataset():
    """Reset only end-of-TAR state; aggregate header/metadata caps stay monotonic."""
    BoundedTarInfo.terminal_seen = False


def validate_tar_ending(archive, decoded):
    require(BoundedTarInfo.terminal_seen, 'TAR_MISSING_TERMINATOR')
    first_zero_end = archive.fileobj.tell()
    require(first_zero_end >= tarfile.BLOCKSIZE
            and first_zero_end % tarfile.BLOCKSIZE == 0, 'TAR_TERMINATOR_ALIGNMENT')
    trailer_bytes = 0
    # Drain this stream before closing TarFile. Reading the lower
    # buffered reader would omit bytes already held by _Stream.
    while chunk := archive.fileobj.read(CHUNK):
        deadline()
        require(not any(chunk), 'TAR_NONZERO_TRAILER')
        trailer_bytes += len(chunk)
    require(trailer_bytes >= tarfile.BLOCKSIZE
            and trailer_bytes % tarfile.BLOCKSIZE == 0, 'TAR_SECOND_ZERO_BLOCK_REQUIRED')
    require(decoded.finished and decoded.count == first_zero_end + trailer_bytes
            and decoded.count % tarfile.BLOCKSIZE == 0, 'TAR_TRAILER_COMPLETE_XZ_EOF')
    return {'first_zero_header_end': first_zero_end,
            'additional_zero_bytes': trailer_bytes,
            'second_zero_block_present': True,
            'complete_aligned_zero_trailer': True,
            'complete_compressed_eof': True}
