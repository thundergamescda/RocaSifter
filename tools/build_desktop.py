"""Create a clean, standalone Windows preview; never include merchant files."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
BUILD_ENVIRONMENT_POLICY = 'isolated-python-and-windows-path-v1'
ASSETS = ('roca_square_label_app.html', 'station.js', 'station.css', 'preview.js', 'cloud.js', 'cloud.css', 'intake.js', 'import.js', 'import.css', 'shared-trade.js', 'shared-trade.css', 'intake-claim.js', 'intake-claim.css', 'receiving.js', 'receiving.css', 'roca_edition.json', 'product_brand.json',
          'assets/stock-and-sleeve.png', 'assets/stock-and-sleeve.ico')


def windows_directory():
    """Read the actual OS directory rather than trust an inherited PATH root."""
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise RuntimeError('Cannot identify the Windows directory for the desktop build.')
    return Path(buffer.value).resolve(strict=True)


def build_environment():
    """Keep unrelated native tools and Python overrides out of the compiler.

    PyInstaller still performs its dependency analysis normally; an unresolved
    dependency must be reviewed rather than found through a developer's PATH.
    The public manifest names this policy, without publishing local directories.
    """
    system = windows_directory()
    directories = [Path(sys.executable).resolve(strict=True).parent,
                   Path(sys.base_prefix).resolve(strict=True),
                   system / 'System32', system,
                   system / 'System32' / 'WindowsPowerShell' / 'v1.0']
    if any(not directory.is_dir() for directory in directories):
        raise RuntimeError('A required isolated desktop build directory is missing.')
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(('PYTHON', 'PYINSTALLER_', '_PYI_'))
                   and key.upper() not in ('PATH', '_MEIPASS2', 'SYSTEMROOT', 'WINDIR', 'COMSPEC')}
    environment.update(PATH=os.pathsep.join(dict.fromkeys(map(str, directories))),
                       SystemRoot=str(system), WINDIR=str(system),
                       COMSPEC=str(system / 'System32' / 'cmd.exe'),
                       PYTHONNOUSERSITE='1', PYTHONUTF8='1')
    return environment


def run_compiler(command, environment):
    result = subprocess.run(command, cwd=ROOT, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding='utf-8', errors='replace')
    print(result.stdout, end='', flush=True)
    result.check_returncode()
    # PyInstaller can exit successfully after reporting an unresolved native
    # dependency. Optional Python-module warnings have different semantics.
    if 'Library not found: could not resolve' in result.stdout:
        raise RuntimeError('The desktop compiler reported an unresolved native library. '
                           'Do not deliver this output; review the build log.')


def build_command(output_directory=None,build_manifest=None):
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
               '--onedir', '--windowed', '--name', 'StockAndSleeve',
               '--icon', str(ROOT / 'assets' / 'stock-and-sleeve.ico'),
               '--distpath', str(output_directory or ROOT / 'dist' / 'preview'), '--workpath', str(ROOT / 'build' / ('desktop-' + uuid.uuid4().hex[:8])),
               '--specpath', str(ROOT / 'build'), '--paths', str(ROOT)]
    for asset in ASSETS:
        command += ['--add-data', f'{ROOT / asset}:{Path(asset).parent.as_posix()}']
    command += ['--add-data',f'{ROOT / "third_party_notices"}:third_party_notices']
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
    from distribution_notices import validate_build_environment
    if json.loads((ROOT / 'roca_edition.json').read_text()).get('edition') not in ('standard','credit-tracker'):
        parser.error('Select a supported edition.')
    environment=validate_build_environment(ROOT/'third_party_notices')
    compiler_environment=build_environment()
    inputs=build_input_hashes(ROOT)
    build_id=hashlib.sha256(json.dumps(inputs,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    metadata=ROOT/'build'/('payload-'+uuid.uuid4().hex)
    metadata.mkdir(parents=True)
    manifest=metadata/'desktop_build.json'
    manifest.write_text(json.dumps({'format_version':1,'channel':'private-pilot',
                                    'build_id':build_id,'input_sha256':inputs,
                                    'reviewed_build_environment':environment,
                                    'build_environment_policy':BUILD_ENVIRONMENT_POLICY,
                                    'native_warning_policy':'unresolved-native-libraries-rejected-v1'},indent=2)+'\n',encoding='utf-8')
    print('Build environment policy:', BUILD_ENVIRONMENT_POLICY, flush=True)
    run_compiler(build_command(args.output_directory,manifest), compiler_environment)
    if build_input_hashes(ROOT)!=inputs:
        raise RuntimeError('Program inputs changed during the build. Do not deliver this output; rebuild from stable sources.')
    print('Build identity:',build_id)
    print('Preview built:', (args.output_directory or ROOT/'dist'/'preview')/'StockAndSleeve'/'StockAndSleeve.exe',
          '(unsigned; workstation validation required)')


if __name__ == '__main__':
    main()
