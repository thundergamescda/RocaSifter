"""Keep packaged program resources separate from persistent station data.

Resolving paths never creates directories or migrates an existing installation.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class StationPaths:
    resources: Path
    data: Path
    packaged: bool


def resolve_paths(source=None, *, frozen=None, environ=None):
    env = os.environ if environ is None else environ
    packaged = bool(getattr(sys, 'frozen', False)) if frozen is None else frozen
    resources = Path(source or __file__).resolve().parent
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
        data = Path(local) / 'Roca' / 'Station'
    else:
        data = resources
    if packaged and (data == resources or resources in data.parents):
        raise RuntimeError('Store packaged station data outside the program resource folder.')
    return StationPaths(resources, data, packaged)
