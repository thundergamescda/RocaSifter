"""Standalone, standard-library updater for the existing Roca installation.

The small updater download also works with installations predating this module.
It never imports the merchant application or transmits local data to GitHub.
"""
import argparse
import ast
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile

REPOSITORY = 'thundergamescda/RocaSifter'
PROTECTED = {'.git', '.venv', 'station_data', 'outputs', 'backups', '__pycache__', '.env', 'roca_settings.json', 'roca_edition.json'}
REQUIRED = {'roca_to_square.py', 'station_service.py', 'station_server.py', 'station_store.py',
            'station_desktop.py', 'station_runtime.py', 'roca_update.py', 'requirements.txt',
            'station.js', 'station.css', 'roca_square_label_app.html', 'Start Roca.vbs', 'Update Roca.vbs'}
REQUIRED.update({'square_variations.py','station_pricing.py','store_pricing.py','station_edition.py','pricing_review.py',
                 'station_paths.py','station_diagnostics.py','station_cloud.py','station_shared_trade.py','scanner_adapters.py','catalog_cleanup.py','credit_tracker.py',
                 'cloud.js','cloud.css','intake.js','import.js','import.css','shared-trade.js','shared-trade.css','product_brand.py','product_brand.json','preview.js'})
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


class UpdateLock:
    def __init__(self, root):
        self.path = Path(root) / 'station_data' / 'update.lock'
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise UpdateError('An update is already running. Wait for that updater to finish.') from None
        return self

    def __exit__(self, *_):
        # Closing releases the OS lock even after a crash; no stale PID lock to remove.
        self.file.close()
        self.file = None


def update_in_progress(root):
    if not (Path(root) / 'station_data' / 'update.lock').exists():
        return False
    try:
        with UpdateLock(root):
            return False
    except UpdateError:
        return True


def literal_setting(path, name, default=None):
    for node in ast.parse(Path(path).read_text(encoding='utf-8-sig')).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    return default


def station_port(path):
    """Read the installed launcher port without importing or executing its source.

    Older releases use a literal PORT. The desktop-profile release uses this
    exact environment-backed expression; interpreting only that known shape
    preserves the launcher's port override without evaluating arbitrary code.
    """
    value = 8765
    allowed = ast.parse("int(os.environ.get('ROCA_STATION_PORT', '8765'))", mode='eval').body
    for node in ast.parse(Path(path).read_text(encoding='utf-8-sig')).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'PORT' for target in node.targets):
            if ast.dump(node.value) == ast.dump(allowed):
                try:
                    value = int(os.environ.get('ROCA_STATION_PORT', '8765'))
                except (ValueError, TypeError):
                    raise UpdateError('The installed station has an invalid local port override.') from None
            else:
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    raise UpdateError('The installed station has an unsupported local port setting.') from None
            break
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        raise UpdateError('The installed station has an invalid local port.')
    return value


def installation(root):
    root = Path(root).resolve()
    if not (root / 'roca_to_square.py').is_file():
        raise UpdateError('Choose your existing Roca application folder, containing roca_to_square.py.')
    return root


def fetch(url, target=None, headers=None):
    request = urllib.request.Request(url, headers={'User-Agent': 'Roca-Station-Updater', 'Cache-Control': 'no-cache', **(headers or {})})
    with urllib.request.urlopen(request, timeout=30) as response:
        chunks = []
        size = 0
        while True:
            chunk = response.read(128 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise UpdateError('The update exceeds the supported download size. No application files were changed.')
            chunks.append(chunk)
    content = b''.join(chunks)
    if target is not None:
        Path(target).write_bytes(content)
    return content


def download_release(stage):
    # Git's public ref advertisement needs neither an installed Git client nor an API token.
    refs = fetch(f'https://github.com/{REPOSITORY}.git/info/refs?service=git-upload-pack',
                 headers={'Git-Protocol': 'version=0'})
    matches = set(re.findall(rb'([0-9a-f]{40}) refs/heads/main(?:\x00|\n)', refs))
    if len(matches) != 1:
        raise UpdateError('GitHub did not return a valid update revision.')
    commit = matches.pop().decode('ascii')
    archive = Path(stage) / 'release.zip'
    # Pin the archive to the exact revision returned above, even if main changes meanwhile.
    fetch(f'https://codeload.github.com/{REPOSITORY}/zip/{commit}', archive)
    return archive, commit


def bundled_release(manifest_path):
    """Validate a supplied release before stopping the station or changing files."""
    manifest_path = Path(manifest_path).resolve()
    try:
        if manifest_path.stat().st_size > 16 * 1024:
            raise ValueError()
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not isinstance(manifest, dict) or manifest.get('format') != 1:
            raise ValueError()
        filename = manifest['archive']
        if filename != 'application.zip':
            raise ValueError()
        if manifest.get('edition') not in ('standard', 'credit-tracker'):
            raise ValueError()
        if not re.fullmatch(r'\d+\.\d+\.\d+', str(manifest.get('version', ''))):
            raise ValueError()
        expected = manifest['sha256']
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError()
        archive = manifest_path.parent / filename
        if archive.is_symlink() or archive.stat().st_size > MAX_DOWNLOAD:
            raise ValueError()
        digest = hashlib.sha256()
        with archive.open('rb') as source:
            for chunk in iter(lambda: source.read(128 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise UpdateError('The included update is missing or damaged. Extract the complete updater ZIP again. No application files were changed.') from None
    return archive, 'bundle:' + expected, manifest


def unpack_release(archive, destination, edition=None):
    files = []
    seen = set()
    total = 0
    top = None
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            # Windows ZipInfo normalizes backslashes in filename; validate the
            # original archive spelling before that normalization can hide it.
            name = member.orig_filename
            parts = PurePosixPath(name).parts
            if ('\\' in name or not parts or name.startswith('/')
                    or any(p in ('.', '..') or p != p.rstrip(' .') or re.search(r'[<>:"|?*]', p)
                           or re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',p,re.I) for p in parts)):
                raise UpdateError('The update contains an invalid path.')
            if top is None:
                top = parts[0]
            if parts[0] != top:
                raise UpdateError('The update contains more than one application folder.')
            if stat.S_ISLNK(member.external_attr >> 16):
                raise UpdateError('The update contains an unsupported file link.')
            if len(parts) < 2 or member.is_dir():
                continue
            relative = Path(*parts[1:])
            edition_file = edition is not None and relative.as_posix() == 'roca_edition.json'
            if (not edition_file and any(p.casefold() in PROTECTED for p in relative.parts)
                    or len(relative.parts) == 1 and relative.suffix.casefold() in ('.csv', '.pdf', '.zpl')):
                continue
            key = relative.as_posix().casefold()
            if key in seen:
                raise UpdateError('The update contains duplicate filenames.')
            seen.add(key)
            total += member.file_size
            if total > MAX_EXPANDED or len(seen) > 5000:
                raise UpdateError('The expanded update is too large.')
            target = Path(destination) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
            files.append(relative)
    if not REQUIRED.issubset({str(p) for p in files}):
        raise UpdateError('The download is missing required application files. Nothing was installed.')
    if edition is not None:
        try:
            selected = json.loads((Path(destination) / 'roca_edition.json').read_text(encoding='utf-8'))['edition']
        except (OSError, ValueError, KeyError, TypeError):
            selected = None
        if selected != edition or (edition == 'credit-tracker' and Path('credit_tracker.py') not in files):
            raise UpdateError('The included update does not match its selected edition. Nothing was installed.')
    version = literal_setting(Path(destination) / 'roca_to_square.py', 'APP_VERSION')
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise UpdateError('The update does not contain a valid version number.')
    # Detect broken Python before stopping the old station or changing dependencies.
    for relative in files:
        if relative.suffix == '.py':
            compile((Path(destination) / relative).read_bytes(), str(relative), 'exec')
    return files, version


def local_json(port, path, data=None):
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
        data=None if data is None else json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
        return json.load(response)


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.5):
            return True
    except OSError:
        return False


def stop_existing_station(root, port, report, timeout=300):
    runtime = root / 'station_data' / 'runtime.json'
    try:
        record = json.loads(runtime.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        record = {}
    if not port_open(port):
        return
    try:
        if record.get('port') != port:
            raise ValueError()
        status = local_json(port, '/api/runtime')
        if not record.get('shutdownToken') or status.get('instance') != record.get('instance'):
            raise ValueError()
        local_json(port, '/api/shutdown', {'token': record['shutdownToken']})
        report(32, 'Waiting for Roca to finish its active work and stop…')
    except (OSError, ValueError, AttributeError):
        report(32, 'Close the older Roca command window to continue. The update will resume automatically.')
    deadline = time.monotonic() + timeout
    while port_open(port):
        if time.monotonic() >= deadline:
            raise UpdateError('Roca is still running. Finish its work and close it, then run the updater again. No application files were changed.')
        time.sleep(.25)


@contextmanager
def reserve_port(port):
    with socket.socket() as reservation:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            reservation.bind(('127.0.0.1', port))
            reservation.listen(1)
        except OSError:
            raise UpdateError('Roca restarted while preparing the update. Close it and run the updater again.') from None
        yield


def backup_files(root):
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if name.casefold() not in {'.venv', '.git', 'backups', '__pycache__'}]
        for name in folders + files:
            if (Path(directory) / name).is_symlink():
                raise UpdateError('Automatic backup needs regular files and folders, not linked folders. Ask your manager to check the application folder.')
        for name in files:
            relative = (Path(directory) / name).relative_to(root)
            # Windows byte-range locks also prevent a second file handle reading this byte.
            if relative.as_posix().casefold() != 'station_data/update.lock':
                yield relative


def make_backup(root, files, payload):
    originals = list(backup_files(root))
    size = sum((root / p).stat().st_size for p in originals) + sum((payload / p).stat().st_size for p in files)
    if shutil.disk_usage(root).free < size + 100 * 1024 * 1024:
        raise UpdateError('There is not enough free disk space for the backup and update.')
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    backup = root / 'backups' / (stamp + '_' + uuid.uuid4().hex[:6])
    backup.mkdir(parents=True, mode=0o700)
    existed = []
    for relative in originals:
        target = backup / 'files' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
        existed.append(relative.as_posix())
    manifest = {'created': datetime.now().isoformat(), 'originalFiles': existed,
                'updateFiles': [p.as_posix() for p in files], 'status': 'backed_up'}
    (backup / 'update.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return backup


def replace_file(source, target):
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise UpdateError('A file link prevents safely installing this update.')
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.roca-update-', dir=target.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def restore_application(root, backup, files):
    # Roll back only managed application files, never replace business data or labels.
    for relative in files:
        old = backup / 'files' / relative
        target = root / relative
        if old.is_file():
            replace_file(old, target)
        elif target.is_file():
            target.unlink()


def python_executable(root):
    return root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def run_command(command, root, log, timeout=600):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with log.open('ab') as output:
        result = subprocess.run([str(arg) for arg in command], cwd=root, stdout=output, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags, timeout=timeout)
    if result.returncode:
        raise UpdateError('A setup check failed. See the update log: ' + str(log))


def install_dependencies(root, payload, log):
    python = python_executable(root)
    if not python.is_file():
        run_command([sys.executable, '-E', '-s', '-m', 'venv', root / '.venv'], root, log)
    run_command([python, '-E', '-s', '-m', 'pip', 'install', '--disable-pip-version-check', '--only-binary=:all:',
                 '-r', payload / 'requirements.txt'], root, log)


def validate_application(root, payload, log):
    run_command([python_executable(root), '-E', '-s', '-c',
                 'import roca_to_square as app; assert app.requests and app.canvas; print("Roca import check passed")'], payload, log, timeout=60)


def start_updated_station(root, version, port):
    python = python_executable(root)
    if os.name == 'nt':
        python = python.with_name('pythonw.exe')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = subprocess.Popen([str(python), str(root / 'station_desktop.py')], cwd=root,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=flags)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if local_json(port, '/api/version').get('version') == version:
                return True
        except (OSError, ValueError):
            pass
        if process.poll() is not None:
            break
        time.sleep(.25)
    return False


def perform_update(folder, report=lambda *_: None, restart=True, bundle=None):
    root = installation(folder)
    backup = None
    with UpdateLock(root), tempfile.TemporaryDirectory(prefix='Roca-update-') as temporary:
        stage = Path(temporary)
        release = None
        if bundle is not None:
            report(8, 'Checking the included Roca update…')
            archive, commit, release = bundled_release(bundle)
        else:
            report(8, 'Downloading the latest Roca version…')
            archive, commit = download_release(stage)
        payload = stage / 'application'
        report(22, 'Checking the application files…')
        files, version = unpack_release(archive, payload, edition=release['edition'] if release else None)
        if release and version != release['version']:
            raise UpdateError('The included update has inconsistent version information. Nothing was installed.')
        current = literal_setting(root / 'roca_to_square.py', 'APP_VERSION', '0.0.0')
        if re.fullmatch(r'\d+\.\d+\.\d+', str(current)):
            if tuple(map(int, current.split('.'))) > tuple(map(int, version.split('.'))):
                raise UpdateError('This installation is newer than the published update. It has been left unchanged.')
        marker = root / 'station_data' / 'installed_release.json'
        try:
            installed = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            installed = {}
        current_release = installed.get('commit') == commit and current == version
        if release:
            try:
                edition = json.loads((root / 'roca_edition.json').read_text(encoding='utf-8'))['edition']
            except FileNotFoundError:
                edition = 'standard'
            except (OSError, ValueError, KeyError, TypeError):
                edition = None
            current_release = current_release and edition == release['edition']
        port = station_port(root / 'roca_to_square.py')
        if not current_release:
            report(30, 'Finishing the current Roca session…')
            stop_existing_station(root, port, report)
            with reserve_port(port):
                report(40, 'Backing up your application, receipts, labels and settings…')
                backup = make_backup(root, files, payload)
                log = backup / 'setup.log'
                report(55, 'Installing the required packages automatically…')
                install_dependencies(root, payload, log)
                validate_application(root, payload, log)
                report(78, 'Installing the new application files…')
                try:
                    for relative in files:
                        replace_file(payload / relative, root / relative)
                    validate_application(root, root, log)
                    manifest_path = backup / 'update.json'
                    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                    manifest.update(status='installed', version=version, commit=commit)
                    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
                    marker_source = stage / 'installed_release.json'
                    marker_source.write_text(json.dumps({'commit': commit, 'version': version}), encoding='utf-8')
                    replace_file(marker_source, marker)
                except Exception as exc:
                    try:
                        restore_application(root, backup, files)
                    except Exception:
                        raise UpdateError('The update could not finish and needs manual recovery. Your backup is at: ' + str(backup)) from exc
                    raise UpdateError('The update could not finish. Previous application files were restored; your receipts and settings were kept. Backup: ' + str(backup)) from exc
    # Release the update lock and reserved port before the normal launcher starts.
    report(92, 'Reopening Roca in Chrome…' if restart else 'Update installed.')
    reopened = start_updated_station(root, version, port) if restart else False
    text = 'Roca ' + version + ' is ready.' if reopened else 'Open Start Roca to continue.'
    report(100, ('Already up to date. ' if current_release else 'Update installed. ') + text)
    return {'version': version, 'current': current_release, 'backup': str(backup) if backup else None, 'reopened': reopened}


def desktop(folder=None, bundle=None):
    import queue
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    release = bundled_release(bundle)[2] if bundle is not None else None
    app = tk.Tk()
    app.title('Update Roca Station')
    app.geometry('650x570')
    app.minsize(590, 540)
    app.configure(bg='#f5f7fa')
    app.option_add('*Font', ('Segoe UI', 10))
    panel = tk.Frame(app, bg='#ffffff', padx=32, pady=26)
    panel.pack(fill='both', expand=True, padx=18, pady=18)
    def label(text, size=10, color='#737e90', **kwargs):
        item = tk.Label(panel, text=text, bg='white', fg=color, font=('Segoe UI', size), justify='left', anchor='w', **kwargs)
        item.pack(fill='x', pady=(0, 12))
        return item
    label('ROCA STATION  /  SOFTWARE UPDATE', 9, '#315dea')
    label('Install Roca ' + release['version'] + '.' if release else 'Keep your station current.', 22, '#192438')
    label(('Included edition: ' + ('Standard' if release['edition'] == 'standard' else 'Credit tracker') + '. Backups and setup are handled for you.\n' if release else 'Downloads, backups and setup are handled for you.\n') + 'Your receipts, labels and saved settings stay in place.', wraplength=510)
    candidate = Path(folder) if folder else Path(__file__).resolve().parent
    path = tk.StringVar(value=str(candidate) if (candidate / 'roca_to_square.py').is_file() else '')
    label('Existing Roca application folder', 10, '#192438')
    row = tk.Frame(panel, bg='white');row.pack(fill='x', pady=(0, 16))
    entry = ttk.Entry(row, textvariable=path);entry.pack(side='left', fill='x', expand=True)
    def browse():
        selected = filedialog.askdirectory(title='Choose your existing Roca application folder', parent=app)
        if selected:
            path.set(selected)
    browse_button = ttk.Button(row, text='Choose folder', command=browse);browse_button.pack(side='right', padx=(8, 0))
    label('Save your work and close the Roca browser window before starting.', 9)
    status = label('Ready when you are.', 10, '#192438', wraplength=510)
    progress = ttk.Progressbar(panel, maximum=100);progress.pack(fill='x', pady=(0, 12))
    detail = label('', 9, wraplength=510)
    events = queue.Queue()
    running = False
    def finish_close():
        if running:
            messagebox.showinfo('Update in progress', 'Please leave this updater open until it finishes. Your current backup and update are being completed.', parent=app)
        else:
            app.destroy()
    app.protocol('WM_DELETE_WINDOW', finish_close)
    def worker(root):
        try:
            events.put(('done', perform_update(root, lambda value, text: events.put(('progress', value, text)), bundle=bundle)))
        except Exception as exc:
            events.put(('error', str(exc)))
    def start():
        nonlocal running
        try:
            root = installation(path.get())
        except Exception as exc:
            messagebox.showerror('Choose your Roca folder', str(exc), parent=app)
            return
        running = True
        button.configure(state='disabled');browse_button.configure(state='disabled');entry.configure(state='disabled')
        threading.Thread(target=worker, args=(root,), daemon=False).start()
    button = tk.Button(panel, text='Update & reopen Roca', command=start, bg='#315dea', fg='white',
                       activebackground='#244bd0', activeforeground='white', relief='flat', padx=18, pady=10)
    button.pack(anchor='e', pady=(6, 0))
    def poll():
        nonlocal running
        try:
            while True:
                event = events.get_nowait()
                if event[0] == 'progress':
                    progress['value'] = event[1];status.configure(text=event[2])
                elif event[0] == 'done':
                    running = False
                    result = event[1]
                    detail.configure(text=('Backup saved: '+result['backup']) if result['backup'] else 'No update was needed.')
                    button.configure(text='Close', state='normal', command=finish_close)
                else:
                    running = False
                    status.configure(text='The update did not finish.', fg='#a55751')
                    detail.configure(text=event[1])
                    button.configure(text='Close', state='normal', command=finish_close)
        except queue.Empty:
            pass
        app.after(100, poll)
    poll();app.mainloop()


def main():
    parser = argparse.ArgumentParser(description='Update the existing Roca Station installation.')
    parser.add_argument('--folder', type=Path)
    parser.add_argument('--bundle', type=Path, help='Install the release supplied beside this updater, without a GitHub download.')
    args = parser.parse_args()
    try:
        if sys.version_info < (3,10):
            raise UpdateError('Python 3.10 or newer is required.')
        desktop(args.folder, bundle=args.bundle)
    except Exception as exc:
        if os.name == 'nt':
            import ctypes
            message = str(exc) if isinstance(exc, UpdateError) else 'The updater could not open. Install Python 3.10 or newer with Tcl/Tk support.\n\n' + str(exc)
            ctypes.windll.user32.MessageBoxW(None, message, 'Roca Station updater', 0x10)
        else:
            raise


if __name__ == '__main__':
    main()
