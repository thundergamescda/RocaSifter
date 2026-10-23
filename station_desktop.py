"""Windowless Windows start/stop entry point. Uses the installer's own Python."""
from contextlib import redirect_stderr, redirect_stdout
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time


def message(text, *, error=False, confirm=False):
    if os.name == 'nt':
        flags = 0x10 if error else (0x21 if confirm else 0x40)
        return ctypes.windll.user32.MessageBoxW(None, text, 'Roca Station', flags) == 1
    print(text, file=sys.__stderr__)
    return True


class LogStream:
    def __init__(self, logger):
        self.logger = logger

    def write(self, text):
        for line in text.rstrip().splitlines():
            self.logger.info('%s', line)
        return len(text)

    def flush(self):
        for handler in self.logger.handlers:
            handler.flush()


def stop_station(core):
    from station_runtime import read_runtime, running_status, runtime_request
    record = read_runtime(core)
    if not running_status(record):
        message('No background station is running from this application folder.\n\n'
                'If an older Roca command window is open, close that window to stop the older version.')
        return
    if not message('Save your card edits and close the Roca Chrome window before stopping.\n\n'
                   'Stop Roca Station and folder pickup? Any inventory operation already running will finish first.',
                   confirm=True):
        return
    runtime_request(record, '/api/shutdown', {'token': record['shutdownToken']})
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = read_runtime(core)
        if not current or current['instance'] != record['instance']:
            message('Roca Station has stopped. Saved receipts and settings are kept.')
            return
        time.sleep(.25)
    message('Stop requested. Roca is finishing its current work and will then stop.\n\n'
            'Wait before updating, moving or backing up the application folder.')


def main():
    root = Path(__file__).resolve().parent
    log_path = root / 'station_data' / 'station.log'
    logger = logging.getLogger('roca.desktop')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=2, encoding='utf-8', delay=True)
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
        with redirect_stdout(LogStream(logger)), redirect_stderr(LogStream(logger)):
            import roca_to_square as core
            if '--stop' in sys.argv[1:]:
                stop_station(core)
            else:
                core.launch_desktop_station(reuse_existing=True)
    except Exception as exc:
        logger.exception('Roca desktop launcher could not complete')
        detail = str(exc)+'\n\n' if isinstance(exc, RuntimeError) else ''
        message('Roca Station could not complete this action.\n\n'+detail+
                'Check station_data\\station.log in this application folder for details. '
                'If dependencies are missing, run Install dependencies.cmd.\n\n'
                'Keep station_data and outputs; they contain your saved work.', error=True)
        return 1
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
    return 0


if __name__ == '__main__':
    sys.exit(main())
