"""Create a clean, standalone Windows preview; never include merchant files."""
import argparse
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ('roca_square_label_app.html', 'station.js', 'station.css', 'preview.js', 'cloud.js', 'cloud.css', 'intake.js', 'roca_edition.json', 'product_brand.json',
          'assets/stock-and-sleeve.png', 'assets/stock-and-sleeve.ico')


def build_command(output_directory=None):
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
               '--onedir', '--windowed', '--name', 'StockAndSleeve',
               '--icon', str(ROOT / 'assets' / 'stock-and-sleeve.ico'),
               '--distpath', str(output_directory or ROOT / 'dist' / 'preview'), '--workpath', str(ROOT / 'build' / ('desktop-' + uuid.uuid4().hex[:8])),
               '--specpath', str(ROOT / 'build'), '--paths', str(ROOT)]
    for asset in ASSETS:
        command += ['--add-data', f'{ROOT / asset}:{Path(asset).parent.as_posix()}']
    # Edition tracker is imported dynamically by the service.
    command += ['--collect-all', 'reportlab', '--collect-all', 'certifi',
                '--hidden-import', 'credit_tracker', str(ROOT / 'station_desktop.py')]
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--print-command', action='store_true')
    parser.add_argument('--output-directory', type=Path, help='Separate output folder for a preview build')
    args = parser.parse_args()
    if args.print_command:
        print(subprocess.list2cmdline(build_command(args.output_directory)))
        return
    if sys.platform != 'win32':
        parser.error('Build the Windows preview on Windows.')
    import json
    if json.loads((ROOT / 'roca_edition.json').read_text()).get('edition') not in ('standard','credit-tracker'):
        parser.error('Select a supported edition.')
    subprocess.run(build_command(args.output_directory), cwd=ROOT, check=True)
    print('Preview built:', (args.output_directory or ROOT/'dist'/'preview')/'StockAndSleeve'/'StockAndSleeve.exe',
          '(unsigned; workstation validation required)')


if __name__ == '__main__':
    main()
