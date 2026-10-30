import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from station_paths import resolve_paths, StationPaths
from station_diagnostics import readiness


class DesktopFoundationTests(unittest.TestCase):
    @staticmethod
    def build_record(resources, identity='a' * 64):
        resources.mkdir(parents=True, exist_ok=True)
        (resources / 'desktop_build.json').write_text(json.dumps({'format_version': 1, 'channel': 'private-pilot', 'build_id': identity}))

    def test_source_keeps_existing_data_and_resolves_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'uncreated'
            paths = resolve_paths(root / 'app.py', frozen=False, environ={})
            self.assertEqual(paths.data, root)
            self.assertFalse(root.exists())

    def test_packaged_resources_and_persistent_data_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.build_record(root / 'program')
            paths = resolve_paths(root / 'program' / 'app.py', frozen=True,
                                  environ={'LOCALAPPDATA': str(root / 'profile')})
            self.assertEqual(paths.data, root / 'profile' / 'StockAndSleeve' / 'PilotDirect' / ('a' * 64))
            self.assertNotEqual(paths.resources, paths.data)
            self.assertFalse(paths.data.exists())
            override = resolve_paths(root / 'program' / 'app.py', frozen=True,
                                     environ={'ROCA_DATA_HOME': str(root / 'isolated')})
            self.assertEqual(override.data, root / 'isolated')

    def test_invalid_data_paths_fail_instead_of_using_current_directory(self):
        with self.assertRaisesRegex(RuntimeError, 'absolute'):
            resolve_paths(frozen=False, environ={'ROCA_DATA_HOME': 'relative'})
        with tempfile.TemporaryDirectory() as folder:
            self.build_record(Path(folder))
            with self.assertRaisesRegex(RuntimeError, 'LOCALAPPDATA'):
                resolve_paths(Path(folder) / 'app.py', frozen=True, environ={})
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                resolve_paths(Path(folder) / 'app.py', frozen=True,
                              environ={'ROCA_DATA_HOME': str(Path(folder) / 'data')})

    def test_direct_pilot_builds_never_select_or_write_legacy_store_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / 'profile/Roca/Station'
            legacy.mkdir(parents=True)
            sentinel = legacy / 'saved-receipt.txt'
            sentinel.write_text('fictional old store; must remain untouched')
            resource = root / 'program'
            self.build_record(resource)
            first = resolve_paths(resource / 'app.py', frozen=True, environ={'LOCALAPPDATA': str(root / 'profile')})
            self.build_record(resource, 'b' * 64)
            second = resolve_paths(resource / 'app.py', frozen=True, environ={'LOCALAPPDATA': str(root / 'profile')})
            self.assertNotEqual(first.data, second.data)
            self.assertNotEqual(first.data, legacy)
            self.assertFalse(first.data.exists())
            self.assertFalse(second.data.exists())
            self.assertEqual(sentinel.read_text(), 'fictional old store; must remain untouched')

    def test_missing_invalid_or_other_channel_manifest_fails_before_data_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / 'desktop_build.json'
            valid = {'format_version': 1, 'channel': 'private-pilot', 'build_id': 'a' * 64}
            records = [None, {}, [], dict(valid, format_version=True), dict(valid, channel='production'),
                       dict(valid, build_id='../outside'), dict(valid, build_id='A' * 64)]
            for record in records:
                with self.subTest(record=record):
                    if record is not None:
                        manifest.write_text(json.dumps(record))
                    with self.assertRaisesRegex(RuntimeError, 'pilot build record'):
                        resolve_paths(root / 'app.py', frozen=True, environ={'ROCA_DATA_HOME': str(root / 'uncreated')})
                    self.assertFalse((root / 'uncreated').exists())

    def test_frozen_launcher_default_port_preserves_explicit_override_and_source_default(self):
        import station_desktop
        with tempfile.TemporaryDirectory() as folder:
            paths = StationPaths(Path(folder), Path(folder) / 'isolated', True)
            for frozen, supplied, expected in ((True, None, '8877'), (True, '42003', '42003'), (False, None, None)):
                with self.subTest(frozen=frozen, supplied=supplied):
                    core = SimpleNamespace(launch_desktop_station=Mock())
                    values = {} if supplied is None else {'ROCA_STATION_PORT': supplied}
                    with patch.dict(os.environ, values, clear=True), patch.object(sys, 'frozen', frozen, create=True), \
                         patch.object(sys, 'argv', ['station_desktop.py']), patch('station_paths.resolve_paths', return_value=paths), \
                         patch.dict(sys.modules, {'roca_to_square': core}):
                        self.assertEqual(station_desktop.main(), 0)
                        self.assertEqual(os.environ.get('ROCA_STATION_PORT'), expected)
                        core.launch_desktop_station.assert_called_once_with(reuse_existing=True)

    def test_manifest_failure_shows_branded_error_without_opening_profile(self):
        import station_desktop
        core = SimpleNamespace(launch_desktop_station=Mock())
        with patch.object(sys, 'argv', ['station_desktop.py']), \
             patch('station_paths.resolve_paths', side_effect=RuntimeError('The Stock & Sleeve pilot build record is missing or invalid.')), \
             patch.object(station_desktop, 'message') as notice, patch.dict(sys.modules, {'roca_to_square': core}), \
             self.assertLogs('roca.desktop', level='ERROR'):
            self.assertEqual(station_desktop.main(), 1)
        core.launch_desktop_station.assert_not_called()
        notice.assert_called_once()
        self.assertTrue(notice.call_args.kwargs['error'])
        self.assertIn('No store profile was opened', notice.call_args.args[0])

    def test_readiness_does_not_create_store_or_read_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('station.js', 'station.css', 'preview.js', 'cloud.js', 'cloud.css', 'intake.js', 'import.js', 'import.css', 'shared-trade.js', 'shared-trade.css', 'intake-claim.js', 'intake-claim.css', 'roca_square_label_app.html', 'roca_edition.json', 'product_brand.json'):
                (root / name).write_text('fixture')
            data = root / 'uncreated'
            report = readiness(StationPaths(root, data, False))
            self.assertTrue(report['ready'])
            self.assertFalse(report['square_contacted'])
            self.assertFalse(data.exists())
            for name in ('station.css','intake-claim.js','intake-claim.css'):
                with self.subTest(missing_asset=name):
                    (root/name).unlink()
                    self.assertFalse(readiness(StationPaths(root, data, False))['ready'])
                    self.assertFalse(data.exists())
                    (root/name).write_text('fixture')

    def test_self_check_exits_without_initializing_a_station(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            report = Path(folder) / 'report.json'
            result = subprocess.run([sys.executable, 'station_desktop.py', '--self-check', '--report', str(report)],
                                    env=dict(os.environ, ROCA_DATA_HOME=str(data)),
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(report.read_text())['ready'])
            self.assertFalse(data.exists())


if __name__ == '__main__':
    unittest.main()
