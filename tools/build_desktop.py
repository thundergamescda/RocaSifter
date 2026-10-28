"""Create a clean, standalone Windows preview; never include merchant files."""
import argparse
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ('roca_square_label_app.html', 'station.js', 'station.css', 'preview.js', 'cloud.js', 'cloud.css', 'intake.js', 'import.js', 'import.css', 'shared-trade.js', 'shared-trade.css', 'roca_edition.json', 'product_brand.json',
          'assets/stock-and-sleeve.png', 'assets/stock-and-sleeve.ico')


def build_command(output_directory=None,build_manifest=None):
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
               '--onedir', '--windowed', '--name', 'StockAndSleeve',
               '--icon', str(ROOT / 'assets' / 'stock-and-sleeve.ico'),
               '--distpath', str(output_directory or ROOT / 'dist' / 'preview'), '--workpath', str(ROOT / 'build' / ('desktop-' + uuid.uuid4().hex[:8])),
               '--specpath', str(ROOT / 'build'), '--paths', str(ROOT)]
    for asset in ASSETS:
        command += ['--add-data', f'{ROOT / asset}:{Path(asset).parent.as_posix()}']
    if build_manifest:
        command += ['--add-data',f'{build_manifest}:.']
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
    output=(args.output_directory or ROOT/'dist'/'preview').resolve()
    if (output/'StockAndSleeve').exists():
        parser.error('Choose a fresh --output-directory. Existing builds are preserved; do not replace a running or locked program folder.')
    import json
    import hashlib
    sys.path.insert(0,str(ROOT))
    from station_runtime import build_input_hashes
    if json.loads((ROOT / 'roca_edition.json').read_text()).get('edition') not in ('standard','credit-tracker'):
        parser.error('Select a supported edition.')
    inputs=build_input_hashes(ROOT)
    build_id=hashlib.sha256(json.dumps(inputs,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    metadata=ROOT/'build'/('payload-'+uuid.uuid4().hex)
    metadata.mkdir(parents=True)
    manifest=metadata/'desktop_build.json'
    manifest.write_text(json.dumps({'format_version':1,'channel':'private-pilot',
                                    'build_id':build_id,'input_sha256':inputs},indent=2)+'\n',encoding='utf-8')
    subprocess.run(build_command(args.output_directory,manifest), cwd=ROOT, check=True)
    if build_input_hashes(ROOT)!=inputs:
        raise RuntimeError('Program inputs changed during the build. Do not deliver this output; rebuild from stable sources.')
    print('Build identity:',build_id)
    print('Preview built:', (args.output_directory or ROOT/'dist'/'preview')/'StockAndSleeve'/'StockAndSleeve.exe',
          '(unsigned; workstation validation required)')


if __name__ == '__main__':
    main()
