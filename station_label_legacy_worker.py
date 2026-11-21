"""Bounded private subprocess for the existing legacy label generators.

This does not grant hosted SKU authority. It deliberately preserves the legacy
barcode_value contract, including numeric hashing of non-12-digit local SKUs.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

MAX_INPUT = 4 * 1024 * 1024
TIMEOUT = 120
FIELDS = ('card_name', 'card_set', 'card_num', 'condition', 'printing', 'sku',
          'qty', 'final_price', 'price_missing')


def run_pair(cards, chosen, directory, identities):
    from station_label_publication import _identity, _path, _read, _fail, MAX_BYTES
    directory = _path(directory, directory=True)
    payload = {'version': 1, 'profile': chosen,
               'cards': [{k: card.get(k) for k in FIELDS} for card in cards]}
    raw = json.dumps(payload, allow_nan=False).encode('utf-8')
    if not 1 <= len(raw) <= MAX_INPUT:
        _fail('This legacy label request exceeds its bounded input size.')
    source = directory / 'input.json'
    input_identity = None
    process = None
    try:
        with source.open('xb') as stream:
            input_identity = _identity(os.fstat(stream.fileno()))
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Reserve exact file identities before a generator can fail midway.
        for kind in ('pdf', 'zpl'):
            with (directory / ('labels.' + kind)).open('xb') as stream:
                identities[kind] = {'identity': _identity(os.fstat(stream.fileno()))}
        script = _path(Path(__file__).resolve())
        executable = _path(Path(sys.executable).resolve())
        environment = {k: os.environ[k] for k in ('SYSTEMROOT', 'WINDIR') if k in os.environ}
        environment.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
                           HOME=str(directory), USERPROFILE=str(directory), TMP=str(directory),
                           TEMP=str(directory), TMPDIR=str(directory))
        process = subprocess.Popen([str(executable), '-I', '-B', str(script), '--render', str(source)],
                                   cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + TIMEOUT
        while process.poll() is None:
            if time.monotonic() >= deadline:
                _fail('The private legacy label worker exceeded its deadline. Keep this same job for retry.')
            if {p.name for p in directory.iterdir()} != {'input.json', 'labels.pdf', 'labels.zpl'}:
                _fail('Unexpected content was found in the private legacy attempt.')
            for kind in ('pdf', 'zpl'):
                path = _path(directory / ('labels.' + kind))
                if _identity(path.lstat()) != identities[kind]['identity'] or path.stat().st_size > MAX_BYTES:
                    _fail('The private legacy worker exceeded its original file bounds.')
            time.sleep(0.05)
        if process.returncode != 0:
            _fail('The private legacy label pair could not complete. Existing outputs were kept.')
        if {p.name for p in directory.iterdir()} != {'input.json', 'labels.pdf', 'labels.zpl'}:
            _fail('Unexpected content was found in the completed private legacy attempt.')
        for kind in ('pdf', 'zpl'):
            data, metadata = _read(directory / ('labels.' + kind))
            if metadata['identity'] != identities[kind]['identity']:
                _fail('The private legacy worker changed its output identity.')
            if kind == 'pdf' and not data.startswith(b'%PDF-') or kind == 'zpl' and b'^XA' not in data:
                _fail('The private legacy label pair is incomplete.')
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if input_identity is not None:
            try:
                if _identity(_path(source).lstat()) == input_identity:
                    source.unlink()
            except (OSError, ValueError):
                pass


def _render(source):
    # Executed only inside the bounded child, never by import. Project source
    # is explicit; no app/session/store initialization or credentials are needed.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from station_label_publication import _path, _identity
    from hosted_labels import profile
    source = _path(source)
    directory = _path(source.parent, directory=True)
    if source.name != 'input.json' or {p.name for p in directory.iterdir()} != {'input.json', 'labels.pdf', 'labels.zpl'}:
        raise ValueError('Invalid legacy attempt.')
    with source.open('rb') as stream:
        raw = stream.read(MAX_INPUT + 1)
    if not 1 <= len(raw) <= MAX_INPUT:
        raise ValueError('Invalid legacy input size.')
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {'version', 'profile', 'cards'} or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Invalid legacy input.')
    chosen = profile(value['profile'])
    if chosen['stock'] != '2x1':
        raise ValueError('Legacy stock is fixed at 2x1.')
    cards = value['cards']
    if type(cards) is not list or not 1 <= len(cards) <= 1000:
        raise ValueError('Invalid legacy rows.')
    copies = 0
    for card in cards:
        if type(card) is not dict or set(card) != set(FIELDS) or type(card['qty']) is not int or not 1 <= card['qty'] <= 1000:
            raise ValueError('Invalid legacy copies.')
        if card['price_missing'] is not False or type(card['final_price']) not in (int, float):
            raise ValueError('Unreviewed legacy price.')
        for field in FIELDS[:6]:
            if type(card[field]) is not str or len(card[field]) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in card[field]):
                raise ValueError('Invalid legacy print text.')
        copies += card['qty']
    if not 1 <= copies <= 1000:
        raise ValueError('Invalid legacy copy total.')
    identities = {k: _identity(_path(directory / ('labels.' + k)).lstat()) for k in ('pdf', 'zpl')}
    import roca_to_square as core
    core.generate_zebra_pdf_labels(cards, str(directory / 'labels.pdf'), layout=chosen['layout'], dpi=chosen['dpi'])
    core.generate_zebra_zpl(cards, str(directory / 'labels.zpl'), layout=chosen['layout'], dpi=chosen['dpi'])
    for kind in ('pdf', 'zpl'):
        target = _path(directory / ('labels.' + kind))
        if _identity(target.lstat()) != identities[kind]:
            raise ValueError('Legacy output was replaced.')
        with target.open('rb') as stream:
            os.fsync(stream.fileno())


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3 or sys.argv[1] != '--render':
            raise ValueError('Invalid worker invocation.')
        _render(Path(sys.argv[2]))
    except Exception:
        raise SystemExit(2) from None
