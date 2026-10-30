"""Product branding is independent of scanner adapters and legacy data paths."""
import json
from pathlib import Path


def display_name():
    path = Path(__file__).resolve().parent / 'product_brand.json'
    value = json.loads(path.read_text(encoding='utf-8'))['display_name']
    if (not isinstance(value, str) or not value.strip() or len(value) > 60
            or any(c in value for c in '<>:"/\\|?*\r\n')
            or value != value.strip() or value.endswith('.')):
        raise RuntimeError('The product display name is invalid.')
    return value
