"""Pure v1 original-intake evidence shared by desktop and hosted claims.

This module does not discover original facts from an edited legacy receipt.
Callers capture at original intake, then retain the returned value unchanged.
All hashes use compact sorted-key UTF-8 JSON with NFC text. Text case, internal
spacing, punctuation, printing, grades and certificates are deliberately exact:
no case folding, NFKC or aliases may collapse distinct physical identities.
"""
import hashlib
import hmac
import json
import re
import unicodedata

VERSION = 1
MAX_ROWS = 1000
MAX_ROW_QUANTITY = 100000
MAX_TOTAL_QUANTITY = 1000000
MAX_SOURCE_ROW = 10000000
MAX_MANIFEST_BYTES = 512 * 1024
IDENTITY_LIMITS = {
    'name': 200, 'game': 100, 'game_label': 100, 'set': 200, 'number': 100,
    'language': 50, 'printing': 100, 'condition': 100, 'kind': 10,
    'grading_company': 100, 'grade': 30, 'grade_qualifier': 100,
    'certification': 100, 'year': 20, 'sport': 100, 'league': 100, 'team': 100,
    'player': 150, 'serial_number': 100, 'manufacturer': 100,
}
_HEX = re.compile(r'[a-f0-9]{64}')
_MANIFEST_DOMAIN = b'stock-and-sleeve:source-manifest:v1\0'
_LINE_DOMAIN = b'stock-and-sleeve:source-line:v1\0'
_SEMANTIC_DOMAIN = b'stock-and-sleeve:source-semantic:v1\0'


class ManifestError(ValueError):
    """Original intake cannot be represented or its saved evidence is invalid."""


def _keys(value, allowed, label):
    if type(value) is not dict or any(type(key) is not str or key not in allowed for key in value):
        raise ManifestError('Unsupported ' + label + ' fields.')


def _text(value, label, limit, required=False):
    if type(value) is not str or len(value) > limit * 4:
        raise ManifestError(label + ' must be supported bounded text.')
    if any(unicodedata.category(char) in ('Cc', 'Cs', 'Cf') for char in value):
        raise ManifestError(label + ' contains unsupported control or invisible characters.')
    normalized = unicodedata.normalize('NFC', value).strip()
    if len(normalized) > limit or (required and not normalized):
        raise ManifestError(label + ' is missing or too long.')
    return normalized


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ManifestError(label + ' must be a supported positive whole number.')
    return value


def normalize_identity(identity):
    """Return only canonical supported card facts; never invent required facts."""
    _keys(identity, IDENTITY_LIMITS, 'original identity')
    result = {}
    for key, limit in IDENTITY_LIMITS.items():
        required = key in ('name', 'game', 'kind')
        value = _text(identity.get(key, ''), key, limit, required)
        if value or required:
            result[key] = value
    if result['kind'] not in ('card', 'slab'):
        raise ManifestError('Original item kind must be card or slab.')
    grading = ('grading_company', 'grade', 'grade_qualifier', 'certification')
    if result['kind'] == 'slab':
        if any(not result.get(key) for key in ('grading_company', 'grade', 'certification')):
            raise ManifestError('Original slab evidence needs grading company, grade and certification.')
    elif any(result.get(key) for key in grading):
        raise ManifestError('Original grading facts require slab kind.')
    if result['game'].startswith('other:') and not result.get('game_label'):
        raise ManifestError('An additional original game needs its readable game label.')
    return result


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash(domain, value):
    return hashlib.sha256(domain + _json_bytes(value)).hexdigest()


def source_line_id(identity):
    """Identity-group key, independent of quantity, row order and local UUIDs.

    source_lines exposes this value under the wire name source_line_key. A
    corrected/split reviewed line must keep its ORIGINAL source_line_key.
    """
    return _hash(_LINE_DOMAIN, normalize_identity(identity))


def _payload(kind, rows, raw_csv_sha256):
    if kind not in ('csv', 'manual') or type(kind) is not str:
        raise ManifestError('Original source kind must be csv or manual.')
    if type(rows) is not list or not 1 <= len(rows) <= MAX_ROWS:
        raise ManifestError('Hosted original intake supports between 1 and 1,000 rows; no partial capture is allowed.')
    if kind == 'csv':
        if type(raw_csv_sha256) is not str or not _HEX.fullmatch(raw_csv_sha256):
            raise ManifestError('CSV original intake needs its exact lowercase SHA-256 file reference.')
    elif raw_csv_sha256 is not None:
        raise ManifestError('Manual original intake cannot invent a CSV file hash.')
    normalized = []
    total = 0
    ordinals = set()
    certificates = set()
    for row in rows:
        _keys(row, ('identity', 'quantity', 'source_row'), 'original source row')
        identity = normalize_identity(row.get('identity'))
        quantity = _integer(row.get('quantity'), 'Original quantity', 1, MAX_ROW_QUANTITY)
        total += quantity
        if total > MAX_TOTAL_QUANTITY:
            raise ManifestError('Original intake exceeds the supported total quantity.')
        if identity['kind'] == 'slab':
            if quantity != 1:
                raise ManifestError('An original certified slab must have quantity one.')
            certificate = (identity['grading_company'], identity['certification'])
            if certificate in certificates:
                raise ManifestError('The same original certified slab cannot appear twice.')
            certificates.add(certificate)
        item = {'identity': identity, 'quantity': quantity}
        if 'source_row' in row:
            ordinal = _integer(row['source_row'], 'Original source row', 1, MAX_SOURCE_ROW)
            if ordinal in ordinals:
                raise ManifestError('Original source row positions must not be duplicated.')
            ordinals.add(ordinal)
            item['source_row'] = ordinal
        normalized.append(item)
    result = {'version': VERSION, 'kind': kind, 'rows': normalized}
    if kind == 'csv':
        result['raw_csv_sha256'] = raw_csv_sha256
    return result


def capture_manifest(kind, rows, *, raw_csv_sha256=None):
    """Capture complete original rows in their original order, before any edit.

    Caller must catch ManifestError as a hosted-intake limitation and preserve
    the complete local intake. Never truncate or fabricate a replacement source.
    This digest is integrity/provenance, not proof a physical stack exists.
    """
    payload = _payload(kind, rows, raw_csv_sha256)
    result = payload | {'sha256': _hash(_MANIFEST_DOMAIN, payload)}
    if len(_json_bytes(result)) > MAX_MANIFEST_BYTES:
        raise ManifestError('Original intake exceeds the supported manifest byte limit; no partial capture is allowed.')
    return result


def validate_manifest(value):
    """Validate strict schema/canonical form/hash and return an independent copy."""
    _keys(value, ('version', 'kind', 'raw_csv_sha256', 'rows', 'sha256'), 'original manifest')
    if type(value.get('version')) is not int or value['version'] != VERSION:
        raise ManifestError('Original intake manifest version is unsupported.')
    expected = capture_manifest(value.get('kind'), value.get('rows'), raw_csv_sha256=value.get('raw_csv_sha256'))
    supplied = value.get('sha256')
    if type(supplied) is not str or not _HEX.fullmatch(supplied) or not hmac.compare_digest(expected['sha256'], supplied):
        raise ManifestError('Original intake manifest hash does not match its saved rows.')
    # No omitted required keys, manual raw_csv_sha256:null, ignored empty keys,
    # padded/decomposed text or other noncanonical alternatives on the wire.
    if expected != value:
        raise ManifestError('Original intake manifest must use its exact canonical saved form.')
    return expected


def manifest_digest(value):
    """Validated ordered-manifest hash, including kind/raw hash/row positions."""
    return validate_manifest(value)['sha256']


def source_lines(manifest):
    """Return deterministic aggregated source groups with original row positions.

    Row positions are provenance only. They MUST NOT be zipped against a later
    reordered/re-exported manifest. Map each original identity to its line key.
    """
    value = validate_manifest(manifest)
    grouped = {}
    for row in value['rows']:
        key = source_line_id(row['identity'])
        if key not in grouped:
            grouped[key] = {'source_line_key': key, 'identity': dict(row['identity']), 'quantity': 0, 'source_rows': []}
        group = grouped[key]
        if group['identity'] != row['identity']:
            raise ManifestError('Original source identity hash collision requires review.')
        group['quantity'] += row['quantity']
        if 'source_row' in row:
            group['source_rows'].append(row['source_row'])
    for group in grouped.values():
        group['source_rows'].sort()
    return [grouped[key] for key in sorted(grouped)]


def semantic_fingerprint(manifest):
    """Order-equivalent identity/quantity hash; excludes kind/raw hash/positions.

    Two kinds may describe the same known stack; kind is not a dedup bypass.
    Additional physical occurrences require a separate central authorization.
    """
    groups = [{'identity': row['identity'], 'quantity': row['quantity']} for row in source_lines(manifest)]
    return _hash(_SEMANTIC_DOMAIN, {'version': VERSION, 'lines': groups})
