"""Exact physical descriptors for newly reviewed catalog ownership protocol 2.

These records are comparison data, never authenticity, staff authority, or
permission to receive stock. Historical identities and hashes cannot supply
missing physical facts or establish a protocol-2 ownership record.
"""
from functools import wraps
import re

from intake_manifest import normalize_identity
from intake_physical_identity import physical_facts_v2, physical_key_v2

__all__ = (
    'CatalogOwnershipError', 'ownership_from_card', 'validate_ownership',
    'identity_for_ownership', 'require_identity',
)

_MESSAGE = 'Review the complete physical catalog ownership before receiving.'
_FIELDS = (
    'game', 'game_label', 'card_name', 'card_set', 'card_num', 'condition',
    'printing', 'language', 'item_type', 'grader', 'grade', 'grade_qualifier',
    'cert_number', 'serial_number', 'year', 'sport', 'league', 'manufacturer',
    'team', 'player',
)
_IDENTITY_FIELDS = {
    'game': 'game', 'game_label': 'game_label', 'card_name': 'name',
    'card_set': 'set', 'card_num': 'number', 'condition': 'condition',
    'printing': 'printing', 'language': 'language', 'item_type': 'kind',
    'grader': 'grading_company', 'grade': 'grade',
    'grade_qualifier': 'grade_qualifier', 'cert_number': 'certification',
    'serial_number': 'serial_number', 'year': 'year', 'sport': 'sport',
    'league': 'league', 'manufacturer': 'manufacturer', 'team': 'team',
    'player': 'player',
}


class CatalogOwnershipError(ValueError):
    """A fixed safe error that never includes rejected descriptor data."""

    __slots__ = ()

    def __init__(self):
        super().__init__(_MESSAGE)

    @property
    def args(self):
        return (_MESSAGE,)

    def __str__(self):
        return _MESSAGE

    def __repr__(self):
        return 'CatalogOwnershipError()'


def _safe(function):
    @wraps(function)
    def owned(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            pass
        raise CatalogOwnershipError() from None
    return owned


@_safe
def ownership_from_card(card):
    """Capture detached canonical facts from a current reviewed card input."""
    facts = physical_facts_v2(card)
    return {'version': 2, 'facts': facts, 'key': physical_key_v2(facts)}


@_safe
def validate_ownership(value):
    """Validate exact canonical protocol-2 bytes as independent comparison data."""
    if (type(value) is not dict or set(value) != {'version', 'facts', 'key'}
            or any(type(name) is not str for name in value)
            or type(value['version']) is not int or value['version'] != 2):
        raise ValueError()
    supplied = value['facts']
    if (type(supplied) is not dict or set(supplied) != set(_FIELDS)
            or any(type(name) is not str for name in supplied)):
        raise ValueError()
    facts = physical_facts_v2(supplied)
    if supplied != facts:
        raise ValueError()
    key = value['key']
    if (type(key) is not str or not re.fullmatch('[a-f0-9]{64}', key)
            or key != physical_key_v2(facts)):
        raise ValueError()
    return {'version': 2, 'facts': facts, 'key': key}


@_safe
def identity_for_ownership(record):
    """Project all twenty current facts to the existing manifest identity shape."""
    facts = validate_ownership(record)['facts']
    return normalize_identity({target: facts[source]
                               for source, target in _IDENTITY_FIELDS.items()})


@_safe
def require_identity(record, identity):
    """Return detached ownership only when its complete projection matches."""
    checked = validate_ownership(record)
    if identity_for_ownership(checked) != normalize_identity(identity):
        raise ValueError()
    return checked
