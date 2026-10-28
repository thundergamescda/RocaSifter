"""Keep packaged program resources separate from persistent station data.

Resolving paths never creates directories or migrates an existing installation.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys


@dataclass(frozen=True)
class StationPaths:
    resources: Path
    data: Path
    packaged: bool


def pilot_build_id(resources):
    """A frozen pilot must identify its channel before choosing any data folder."""
    try:
        record = json.loads((Path(resources) / 'desktop_build.json').read_text(encoding='utf-8'))
        if (type(record.get('format_version')) is not int or record['format_version'] != 1
                or record.get('channel') != 'private-pilot'
                or not isinstance(record.get('build_id'), str)
                or not re.fullmatch('[a-f0-9]{64}', record['build_id'])):
            raise ValueError()
        return record['build_id']
    except (OSError, ValueError, TypeError, AttributeError):
        raise RuntimeError('The Stock & Sleeve pilot build record is missing or invalid. '
                           'Extract the complete pilot download again and use its installed Pilot shortcut.') from None


def resolve_paths(source=None, *, frozen=None, environ=None):
    env = os.environ if environ is None else environ
    packaged = bool(getattr(sys, 'frozen', False)) if frozen is None else frozen
    resources = Path(source or __file__).resolve().parent
    build_id = pilot_build_id(resources) if packaged else None
    override = env.get('ROCA_DATA_HOME', '').strip()
    if override:
        data = Path(override)
        if not data.is_absolute():
            raise RuntimeError('ROCA_DATA_HOME must be an absolute folder path.')
        data = data.resolve()
    elif packaged:
        local = env.get('LOCALAPPDATA', '')
        if not local or not Path(local).is_absolute():
            raise RuntimeError('A packaged station requires a valid LOCALAPPDATA folder.')
        data = Path(local) / 'StockAndSleeve' / 'PilotDirect' / build_id
    else:
        data = resources
    if packaged and (data == resources or resources in data.parents):
        raise RuntimeError('Store packaged station data outside the program resource folder.')
    return StationPaths(resources, data, packaged)
