"""Bounded private label subprocesses; no app/profile/provider initialization."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
import uuid


MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_TIMEOUT = 120
_FILES = {'input.json', 'result.json', 'labels.pdf', 'labels.zpl'}
_CARD_FIELDS = ('row_id', 'source_line_key', 'qty', 'sku', 'include', 'reviewed',
                'price_confirmed', 'price_missing', 'final_price', 'card_name', 'game',
                'card_set', 'card_num', 'condition', 'printing', 'hosted_sku_resolution')
_BASE_KEYS = {'version', 'rendererVersion', 'profile', 'cardsHash', 'receivingId',
              'terminalReceiptHash', 'copyCount', 'rowCount', 'renderCountsHash', 'fonts',
              'fontBundleHash', 'renderBasisHash', 'expectedPdfPages', 'expectedZplCopies',
              'estimatedZplBytes', 'overflows', 'hardwareAcceptanceComplete'}
_OWNED = {}
_LOCK = threading.RLock()


class LabelWorkerError(ValueError):
    def __init__(self, message='The private stock-label worker could not complete safely.', *, code='ORIGINAL_STOCK_LABEL_WORKER_FAILED'):
        super().__init__(message)
        self.code = code


def _fail(message='The private stock-label worker could not complete safely.', code='ORIGINAL_STOCK_LABEL_WORKER_FAILED'):
    raise LabelWorkerError(message, code=code)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _load(raw):
    def invalid(_):
        _fail()
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        _fail()


def _is_reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _safe_path(value, *, directory=False):
    try:
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts:
            _fail()
        for ancestor in reversed((path, *path.parents)):
            info = ancestor.lstat()
            if _is_reparse(info):
                _fail('Private label staging cannot use symlinks or reparse points.')
        resolved = path.resolve(strict=True)
        info = resolved.lstat()
        if resolved != path or directory and not stat.S_ISDIR(info.st_mode):
            _fail()
        if not directory and not stat.S_ISREG(info.st_mode):
            _fail()
        return resolved
    except LabelWorkerError:
        raise
    except (OSError, TypeError, ValueError):
        _fail('The private label staging path is unavailable or invalid.')


def _attempt(path):
    path = _safe_path(path, directory=True)
    if not re.fullmatch('[a-f0-9]{32}', path.name):
        _fail()
    return path


def _identity(path):
    info = path.lstat()
    return info.st_dev, info.st_ino


def _scan(path, *, limits=True):
    path = _attempt(path)
    for item in path.iterdir():
        if item.name not in _FILES:
            _fail('Unexpected content was found in private label staging.')
        safe = _safe_path(item)
        limit = MAX_INPUT_BYTES if item.name == 'input.json' else MAX_METADATA_BYTES if item.name == 'result.json' else MAX_ARTIFACT_BYTES
        if limits and safe.stat().st_size > limit:
            _fail('The private label worker exceeded its file-size limit.', 'ORIGINAL_STOCK_LABEL_BYTES_EXCEEDED')


def cleanup_attempt(result):
    """Remove only a currently owned, exact flat nonreparse UUID attempt."""
    ident = result.get('attemptId') if type(result) is dict else None
    with _LOCK:
        record = _OWNED.get(ident)
    if record is None or result.get('directory') != record['directory']:
        _fail('This private label attempt is not owned by the current worker.')
    directory = _attempt(record['directory'])
    if directory.parent != record['root'] or _identity(directory) != record['identity']:
        _fail()
    # Validate every child before removing any. No recursive delete, following
    # reparse points, arbitrary caller root, or unknown files is permitted.
    _scan(directory, limits=False)
    files = list(directory.iterdir())
    for path in files:
        if _attempt(directory) != directory or _identity(directory) != record['identity']:
            _fail()
        _safe_path(path).unlink()
    directory.rmdir()
    with _LOCK:
        _OWNED.pop(ident, None)


def _new_attempt(stage_root):
    root = _safe_path(stage_root, directory=True)
    ident = uuid.uuid4().hex
    directory = root / ident
    directory.mkdir(mode=0o700)
    if _attempt(directory).parent != root:
        _fail()
    record = {'attemptId': ident, 'directory': directory, 'root': root, 'identity': _identity(directory)}
    with _LOCK:
        _OWNED[ident] = record
    return {'attemptId': ident, 'directory': directory}


def _project(cards, chosen, counts):
    # This validator is pure/bounded: no font loading, rasterization, app, vault,
    # SQLite or network. All expensive text/raster work belongs in the child.
    from station_receiving_label_plan import LabelPlanError, validate_render_counts
    try:
        facts, chosen, counts = validate_render_counts(cards, chosen, counts)
        projected = [{key: card.get(key, '') for key in _CARD_FIELDS} for card in cards]
        projected = _load(_json(projected))
        projected_facts, _, _ = validate_render_counts(projected, chosen, counts)
        if _json(facts) != _json(projected_facts):
            _fail()
        return projected, chosen, counts, facts
    except LabelWorkerError:
        raise
    except (LabelPlanError, ValueError, TypeError, KeyError, OverflowError):
        _fail('The original full-card label evidence or copy range is invalid.', 'ORIGINAL_STOCK_LABEL_WORKER_INPUT_INVALID')


def _environment():
    result = {name: os.environ[name] for name in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP') if name in os.environ}
    result.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    if getattr(sys, 'frozen', False):
        # Preserve only the ordinary active PyInstaller bootstrap, not user
        # Python paths, credentials, proxy settings or application variables.
        for name in ('_PYI_APPLICATION_HOME_DIR', '_PYI_ARCHIVE_FILE', '_PYI_PARENT_PROCESS_LEVEL'):
            if name in os.environ:
                result[name] = os.environ[name]
    return result


def _command(directory, mode):
    executable = Path(sys.executable).resolve(strict=True)
    prefix = [str(executable), '--stock-label-worker'] if getattr(sys, 'frozen', False) else [str(executable), '-X', 'utf8', str(Path(__file__).resolve(strict=True))]
    return prefix + ['--mode', mode, '--input', str(directory / 'input.json'), '--stage', str(directory)]


def _stop(process):
    # Only this exact Popen handle. The renderer launches no other processes.
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _file(path, metadata, extra, expected):
    if type(metadata) is not dict or set(metadata) != {'sha256', 'bytes', extra}:
        _fail()
    if type(metadata['bytes']) is not int or not 1 <= metadata['bytes'] <= MAX_ARTIFACT_BYTES:
        _fail()
    if type(metadata[extra]) is not int or metadata[extra] != expected:
        _fail()
    if type(metadata['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', metadata['sha256']):
        _fail()
    path = _safe_path(path)
    if path.stat().st_size != metadata['bytes']:
        _fail()
    with path.open('rb') as stream:
        if path.suffix == '.pdf' and stream.read(5) != b'%PDF-':
            _fail('The private PDF label artifact is invalid.')
        stream.seek(0)
        if hashlib.file_digest(stream, 'sha256').hexdigest() != metadata['sha256']:
            _fail('The private label artifact changed before publication.')


def _verify(metadata, facts, chosen, counts, directory, mode):
    from hosted_labels import fingerprint
    from station_receiving_label_render import FONT_HASHES, RENDERER_VERSION
    keys = _BASE_KEYS | ({'pdf', 'zpl'} if mode == 'render' else set())
    if type(metadata) is not dict or set(metadata) != keys:
        _fail()
    copies = sum(counts.values())
    required = {'version': 1, 'rendererVersion': RENDERER_VERSION, 'profile': chosen,
                'cardsHash': facts['cardsHash'], 'receivingId': facts['receivingId'],
                'terminalReceiptHash': facts['terminalReceiptHash'], 'copyCount': copies,
                'rowCount': len(counts), 'renderCountsHash': fingerprint(counts), 'fonts': FONT_HASHES,
                'fontBundleHash': fingerprint(FONT_HASHES), 'expectedPdfPages': copies,
                'expectedZplCopies': copies, 'hardwareAcceptanceComplete': False}
    if any(_json(metadata[key]) != _json(value) for key, value in required.items()):
        _fail('The private label worker returned mismatched input facts.')
    if type(metadata['renderBasisHash']) is not str or not re.fullmatch('[a-f0-9]{64}', metadata['renderBasisHash']):
        _fail()
    if type(metadata['estimatedZplBytes']) is not int or not 1 <= metadata['estimatedZplBytes'] <= MAX_ARTIFACT_BYTES:
        _fail()
    overflows = metadata['overflows']
    if type(overflows) is not list or len(overflows) > 4 * len(counts):
        _fail()
    for row in overflows:
        if (type(row) is not dict or set(row) != {'reviewLineId', 'field', 'shownText', 'originalLength'}
                or row['reviewLineId'] not in counts or type(row['field']) is not str
                or type(row['shownText']) is not str or len(row['shownText']) > 1000
                or type(row['originalLength']) is not int or not 0 <= row['originalLength'] <= 1000):
            _fail()
    if mode == 'render':
        _file(directory / 'labels.pdf', metadata['pdf'], 'pages', copies)
        _file(directory / 'labels.zpl', metadata['zpl'], 'expectedCopies', copies)
        if metadata['estimatedZplBytes'] != metadata['zpl']['bytes']:
            _fail()
        quantities = []
        with (directory / 'labels.zpl').open('rb') as stream:
            for line in stream:
                line.decode('ascii')
                if line.startswith(b'^PQ'):
                    match = re.fullmatch(rb'\^PQ([1-9][0-9]*)\^XZ\n', line)
                    if not match:
                        _fail()
                    quantities.append(int(match[1]))
        if len(quantities) != len(counts) or sum(quantities) != copies:
            _fail('The private ZPL copy count does not match its selected range.')
    return metadata


def _cancel(cancel_event):
    if cancel_event is None:
        return
    probe = getattr(cancel_event, 'is_set', None)
    if not callable(probe):
        _fail('The label-worker cancellation signal is invalid.')
    value = probe()
    if type(value) is not bool:
        _fail('The label-worker cancellation signal is invalid.')
    if value:
        _fail('The private label worker was cancelled.', 'ORIGINAL_STOCK_LABEL_WORKER_CANCELLED')


def _run(cards, chosen, render_counts, stage_root, timeout, mode, cancel_event):
    _cancel(cancel_event)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
        _fail('Choose a finite bounded label-worker deadline.')
    deadline = time.monotonic() + timeout
    cards, chosen, counts, facts = _project(cards, chosen, render_counts)
    raw = _json({'version': 1, 'cards': cards, 'profile': chosen, 'renderCounts': counts})
    if len(raw) > MAX_INPUT_BYTES:
        _fail('The private label-worker input exceeds its byte limit.', 'ORIGINAL_STOCK_LABEL_BYTES_EXCEEDED')
    _cancel(cancel_event)
    result = _new_attempt(stage_root)
    directory = result['directory']
    process = None
    try:
        with (directory / 'input.json').open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _cancel(cancel_event)
        process = subprocess.Popen(_command(directory, mode), cwd=directory, env=_environment(), shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        while process.poll() is None:
            _cancel(cancel_event)
            _scan(directory)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail('The private label worker exceeded its deadline.', 'ORIGINAL_STOCK_LABEL_WORKER_TIMEOUT')
            try:
                process.wait(timeout=min(.05, remaining))
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0:
            _fail()
        _cancel(cancel_event)
        _scan(directory)
        path = _safe_path(directory / 'result.json')
        with path.open('rb') as stream:
            encoded = stream.read(MAX_METADATA_BYTES + 1)
        if not 1 <= len(encoded) <= MAX_METADATA_BYTES:
            _fail()
        envelope = _load(encoded)
        if (type(envelope) is not dict or set(envelope) != {'version', 'inputSha256', 'mode', 'render'}
                or type(envelope['version']) is not int or envelope['version'] != 1
                or envelope['inputSha256'] != hashlib.sha256(raw).hexdigest() or envelope['mode'] != mode):
            _fail()
        metadata = _verify(envelope['render'], facts, chosen, counts, directory, mode)
        _cancel(cancel_event)
        if mode == 'preflight':
            if {p.name for p in directory.iterdir()} != {'input.json', 'result.json'}:
                _fail()
            cleanup_attempt(result)
            return metadata
        result.update(pdf=directory / 'labels.pdf', zpl=directory / 'labels.zpl', metadata=metadata)
        return result
    except BaseException:
        if process is not None:
            _stop(process)
        # A suspicious/reparse/unknown attempt is retained instead of following
        # or deleting it. Cleanup never broadens to a caller-supplied parent.
        try:
            cleanup_attempt(result)
        except (LabelWorkerError, OSError):
            pass
        raise


def run_pair(cards, chosen, render_counts, stage_root, *, timeout=MAX_TIMEOUT, cancel_event=None):
    return _run(cards, chosen, render_counts, stage_root, timeout, 'render', cancel_event)


def preflight_pair(cards, chosen, render_counts, stage_root, *, timeout=MAX_TIMEOUT, cancel_event=None):
    return _run(cards, chosen, render_counts, stage_root, timeout, 'preflight', cancel_event)


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--mode', choices=('preflight', 'render'), required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--stage', required=True)
    try:
        args = parser.parse_args(argv)
        directory = _attempt(args.stage)
        source = _safe_path(args.input)
        if source != directory / 'input.json' or {p.name for p in directory.iterdir()} != {'input.json'}:
            _fail()
        with source.open('rb') as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if not 1 <= len(raw) <= MAX_INPUT_BYTES:
            _fail()
        data = _load(raw)
        if type(data) is not dict or set(data) != {'version', 'cards', 'profile', 'renderCounts'} or type(data['version']) is not int or data['version'] != 1:
            _fail()
        cards, chosen, counts, _ = _project(data['cards'], data['profile'], data['renderCounts'])
        if _json(cards) != _json(data['cards']):
            _fail()
        from station_receiving_label_render import preflight, render_pair
        basis = preflight(cards, chosen, counts)
        rendered = basis if args.mode == 'preflight' else render_pair(cards, directory / 'labels.pdf', directory / 'labels.zpl', chosen, counts)
        if _json(basis) != _json({key: value for key, value in rendered.items() if key not in ('pdf', 'zpl')}):
            _fail()
        encoded = _json({'version': 1, 'inputSha256': hashlib.sha256(raw).hexdigest(), 'mode': args.mode, 'render': rendered})
        if len(encoded) > MAX_METADATA_BYTES:
            _fail()
        with (directory / 'result.json').open('xb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return 0
    except (Exception, KeyboardInterrupt):
        # No customer/card payload, filenames, input JSON or traceback is logged.
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
