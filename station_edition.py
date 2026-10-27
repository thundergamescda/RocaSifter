"""Edition selection is installation data, preserved by the automatic updater."""
import json
from pathlib import Path
from station_store import StationError

def read(root):
    path=Path(root)/'roca_edition.json'
    if not path.exists():return 'standard'
    try:value=json.loads(path.read_text(encoding='utf-8'))['edition']
    except (OSError,ValueError,KeyError,TypeError):raise StationError('The edition setting is invalid. Reinstall the chosen edition without removing station_data.') from None
    if value not in ('standard','credit-tracker'):raise StationError('Choose the standard or credit-tracker edition.')
    return value
