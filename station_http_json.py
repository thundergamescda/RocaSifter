"""Exact bounded JSON request bytes; no HTTP, journal or application imports."""
import json


MAX_REQUEST_BYTES = 512 * 1024


class RequestBodyError(ValueError):
    def __init__(self, message, *, too_large=False):
        super().__init__(message)
        self.too_large = too_large


def _validate(value, active):
    kind = type(value)
    if value is None or kind in (str, bool, int, float):
        return
    if kind not in (dict, list):
        raise RequestBodyError('The request contains unsupported JSON values.')
    ident = id(value)
    if ident in active:
        raise RequestBodyError('The request contains a circular JSON value.')
    active.add(ident)
    try:
        if kind is dict:
            if any(type(key) is not str for key in value):
                raise RequestBodyError('JSON request field names must be text.')
            children = value.values()
        else:
            children = value
        for child in children:
            _validate(child, active)
    finally:
        active.remove(ident)


def encode_request_body(value):
    """Preserve JSON types/text/order; only whitespace and Unicode encoding vary.

    This encoder does not reinterpret or fingerprint a saved request. Its caller
    decides whether a new durable action may be created with these exact bytes.
    """
    try:
        _validate(value, set())
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except RequestBodyError:
        raise
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        raise RequestBodyError('The request cannot be represented as supported UTF-8 JSON.') from None


def preflight_request_body(value, max_bytes=MAX_REQUEST_BYTES):
    """Return exactly the bytes the production session sends, within the cap."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_REQUEST_BYTES:
        raise RequestBodyError('The JSON request byte limit is invalid.')
    encoded = encode_request_body(value)
    if len(encoded) > max_bytes:
        raise RequestBodyError('This complete request exceeds the supported cloud request size. Keep the original batch and any saved action for owner recovery; no rows were removed.', too_large=True)
    return encoded
