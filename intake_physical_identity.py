"""Full physical comparison data, independent of legacy SKU addressing.

These facts and hashes do not establish completeness, genuine grading, staff
authority, acceptance or eligibility. No missing physical fact is invented.
"""

from functools import wraps
import hashlib
import unicodedata

__all__ = (
    'PHYSICAL_IDENTITY_VERSION', 'PhysicalIdentityError',
    'physical_facts_v2', 'physical_key_v2',
)

PHYSICAL_IDENTITY_VERSION = 2
_FIELDS = (
    'game', 'game_label', 'card_name', 'card_set', 'card_num', 'condition',
    'printing', 'language', 'item_type', 'grader', 'grade', 'grade_qualifier',
    'cert_number', 'serial_number', 'year', 'sport', 'league', 'manufacturer',
    'team', 'player',
)
_GRADING_FIELDS = ('grader', 'grade', 'grade_qualifier', 'cert_number')
_DOMAIN = b'stock-and-sleeve:physical-card:v2'
_MESSAGE = 'Card identity requires a fresh review.'
_ERROR_ARGS = (_MESSAGE,)


class PhysicalIdentityError(ValueError):
    """Fixed safe payload for owned invalid physical comparison data."""

    __slots__ = ()

    def __init__(self):
        super().__init__(_MESSAGE)

    @property
    def code(self):
        return 'PHYSICAL_IDENTITY_INVALID'

    @property
    def args(self):
        return _ERROR_ARGS

    def __str__(self):
        return _MESSAGE

    def __repr__(self):
        return 'PhysicalIdentityError()'


def _safe(function):
    @wraps(function)
    def owned(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            pass
        # Raise after the owned handler; caller chains/locals are not erased.
        raise PhysicalIdentityError() from None

    return owned


def _require(condition):
    if not condition:
        raise ValueError('Invalid physical comparison data.')


def _text(value):
    if value is None:
        return ''
    _require(type(value) is str and len(value) <= 2000)
    normalized = unicodedata.normalize('NFC', value).strip()
    _require(len(normalized) <= 500 and '\x00' not in normalized)
    normalized.encode('utf-8', 'strict')
    return normalized


@_safe
def physical_facts_v2(card):
    """Return a new ordered twenty-field representation, not a permit."""
    _require(type(card) is dict)
    facts = {name: _text(card.get(name)) for name in _FIELDS}
    if facts['item_type']:
        _require(facts['item_type'] in ('card', 'slab'))
    else:
        facts['item_type'] = 'slab' if any(facts[name] for name in _GRADING_FIELDS) else 'card'
    return facts


def _frame(value):
    return str(len(value)).encode('ascii') + b':' + value


@_safe
def physical_key_v2(card):
    """Recompute all facts and return the full domain-separated SHA256."""
    facts = physical_facts_v2(card)
    digest = hashlib.sha256(_DOMAIN + b'\x00')
    for name in _FIELDS:
        digest.update(_frame(name.encode('ascii')))
        digest.update(_frame(facts[name].encode('utf-8', 'strict')))
    return digest.hexdigest()
