"""Identify this station instance and stop it without terminating other Python apps."""
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import urllib.error
import urllib.request

from station_store import private_write


class RuntimeControl:
    def __init__(self, server, core):
        self.server = server
        self.path = Path(core.SETTINGS_FILE).parent / 'station_data' / 'runtime.json'
        self.stopping = threading.Event()
        self.record = {'instance': secrets.token_hex(24), 'shutdownToken': secrets.token_hex(32),
                       'port': server.server_port, 'version': core.APP_VERSION, 'pid': os.getpid()}
        private_write(self.path, json.dumps(self.record).encode())

    def status(self):
        return {key: self.record[key] for key in ('instance', 'version')} | {'stopping': self.stopping.is_set()}

    def authorized(self, token):
        return isinstance(token, str) and hmac.compare_digest(token.encode(), self.record['shutdownToken'].encode())

    def request_stop(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.server.station.stop_event.set()

        def finish_work():
            # All HTTP writes and watched-folder imports use this same lock.
            with self.server.station.lock:
                pass
            self.server.shutdown()

        threading.Thread(target=finish_work, name='roca-shutdown', daemon=True).start()

    def close(self):
        try:
            if json.loads(self.path.read_text(encoding='utf-8')).get('instance') == self.record['instance']:
                self.path.unlink()
        except (OSError, ValueError):
            pass


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
