"""Read-only readiness checks. Never initialize the store or contact Square."""
import importlib.util
import os
from pathlib import Path
import platform
from station_paths import resolve_paths


def readiness(paths=None):
    paths = paths or resolve_paths()
    checks = []
    for name in ('roca_square_label_app.html', 'station.js', 'station.css', 'preview.js', 'cloud.js', 'cloud.css', 'intake.js', 'roca_edition.json', 'product_brand.json'):
        checks.append({'name': name, 'ready': (paths.resources / name).is_file()})
    for module in ('requests', 'reportlab', 'sqlite3'):
        checks.append({'name': module, 'ready': importlib.util.find_spec(module) is not None})
    ancestor = paths.data
    while not ancestor.exists() and ancestor.parent != ancestor:
        ancestor = ancestor.parent
    checks.append({'name': 'data_folder_access', 'ready': ancestor.is_dir() and os.access(ancestor, os.W_OK)})
    # Do not expose settings, account identifiers, filesystem usernames or logs.
    return {'ready': all(c['ready'] for c in checks), 'packaged': paths.packaged,
            'platform': platform.system(), 'python': platform.python_version(),
            'checks': checks, 'square_contacted': False,
            'limitations': ['Chrome launch, printer and scanner require a workstation test.',
                            'Folder access is a preliminary check, not a write probe.',
                            'Online synchronization and OAuth are not connected in this milestone.']}
