"""Build the two-file first-time updater. Contains no application or merchant data."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
output = ROOT / 'docs' / 'Roca_Automatic_Updater.zip'
with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
    for name in ('Update Roca.cmd', 'roca_update.py'):
        bundle.write(ROOT / name, name)
print('Built', output)
