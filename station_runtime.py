"""Identify this station instance and stop it without terminating other Python apps."""
import hmac
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request

from station_store import private_write


def build_input_hashes(root):
    """Hash program inputs only, never settings, receipts or credentials."""
    root=Path(root)
    files=[path for path in root.iterdir() if path.is_file() and
           (path.suffix in ('.js','.css','.html') or
            path.suffix=='.py' and not path.name.startswith('test_'))]
    files.extend(root/name for name in ('product_brand.json','roca_edition.json',
                                      'assets/stock-and-sleeve.png','assets/stock-and-sleeve.ico'))
    return {path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(files)) if path.is_file()}


def current_build(core):
    """Instance compatibility marker; this is not a signature or trust check."""
    root=Path(core.APP_DIR)
    if getattr(sys,'frozen',False):
        try:
            identifier=json.loads((root/'desktop_build.json').read_text(encoding='utf-8'))['build_id']
            if not isinstance(identifier,str) or len(identifier)!=64 or any(c not in '0123456789abcdef' for c in identifier):
                raise ValueError()
            return 'package:'+identifier
        except (OSError,ValueError,KeyError,TypeError) as error:
            raise RuntimeError('The desktop build record is missing or invalid. Extract the complete Stock & Sleeve package again.') from error
    content=json.dumps(build_input_hashes(root),sort_keys=True,separators=(',',':')).encode()
    return 'source:'+hashlib.sha256(content).hexdigest()


def drain_station_work(station):
    """Wait for accepted local and cloud work without nesting their locks.

    Admission must already be closed by the runtime's stopping event. Cloud
    requests use their own lock so a slow provider does not block local saves.
    Drain separately to avoid a cloud operation needing the local lock while
    teardown waits on its cloud lock.
    """
    with station.lock:
        pass
    cloud_lock=getattr(getattr(station,'cloud',None),'lock',None)
    if cloud_lock is not None:
        with cloud_lock:
            pass


class RuntimeControl:
    def __init__(self, server, core):
        self.server = server
        self.path = Path(core.SETTINGS_FILE).parent / 'station_data' / 'runtime.json'
        self.stopping = threading.Event()
        self.record = {'instance': secrets.token_hex(24), 'shutdownToken': secrets.token_hex(32),
                       'port': server.server_port, 'version': core.APP_VERSION, 'pid': os.getpid(),
                       'build':current_build(core)}
        private_write(self.path, json.dumps(self.record).encode())

    def status(self):
        return {key: self.record[key] for key in ('instance', 'version', 'build')} | {'stopping': self.stopping.is_set()}

    def authorized(self, token):
        return isinstance(token, str) and hmac.compare_digest(token.encode(), self.record['shutdownToken'].encode())

    def request_stop(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.server.station.stop_event.set()

        def finish_work():
            drain_station_work(self.server.station)
            self.server.shutdown()

        threading.Thread(target=finish_work, name='roca-shutdown', daemon=True).start()

    def close(self):
        # Windows readers normally do not grant FILE_SHARE_DELETE. The Stop
        # shortcut polls this record, so unlink may briefly fail while that
        # reader is open. Do not abandon cleanup and leave a false live record.
        deadline=time.monotonic()+2
        while True:
            try:
                if json.loads(self.path.read_text(encoding='utf-8')).get('instance')!=self.record['instance']:
                    return
                self.path.unlink()
                return
            except OSError as error:
                if getattr(error,'winerror',None) not in (32,33) or time.monotonic()>=deadline:
                    return
                # Re-read ownership on retry; never delete a replacement
                # instance's record after a transient sharing violation.
                time.sleep(.02)
            except ValueError:
                return


def read_runtime(core):
    path = Path(core.SETTINGS_FILE).parent / 'station_data' / 'runtime.json'
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if (isinstance(record, dict) and isinstance(record.get('port'), int)
                and 0 < record['port'] < 65536
                and all(isinstance(record.get(key), str) and record[key]
                        for key in ('instance', 'shutdownToken', 'version'))):
            return record
    except (OSError, ValueError):
        pass
    return None


def runtime_request(record, path, data=None):
    """Never use proxy environment variables for local process control."""
    request = urllib.request.Request(f"http://127.0.0.1:{record['port']}{path}",
                                    data=None if data is None else json.dumps(data).encode(),
                                    headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
        return json.load(response)


def running_status(record):
    if not record:
        return None
    try:
        status = runtime_request(record, '/api/runtime')
        if isinstance(status, dict) and status.get('instance') == record['instance']:
            return status
    except (OSError, ValueError):
        pass
    return None
