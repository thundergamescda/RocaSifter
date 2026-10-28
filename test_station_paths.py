import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from station_paths import resolve_paths, StationPaths
from station_diagnostics import readiness


class DesktopFoundationTests(unittest.TestCase):
    def test_source_keeps_existing_data_and_resolves_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'uncreated'
            paths = resolve_paths(root / 'app.py', frozen=False, environ={})
            self.assertEqual(paths.data, root)
            self.assertFalse(root.exists())

    def test_packaged_resources_and_persistent_data_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = resolve_paths(root / 'program' / 'app.py', frozen=True,
                                  environ={'LOCALAPPDATA': str(root / 'profile')})
            self.assertEqual(paths.data, root / 'profile' / 'Roca' / 'Station')
            self.assertNotEqual(paths.resources, paths.data)
            self.assertFalse(paths.data.exists())
            override = resolve_paths(root / 'program' / 'app.py', frozen=True,
                                     environ={'ROCA_DATA_HOME': str(root / 'isolated')})
            self.assertEqual(override.data, root / 'isolated')

    def test_invalid_data_paths_fail_instead_of_using_current_directory(self):
        with self.assertRaisesRegex(RuntimeError, 'absolute'):
            resolve_paths(frozen=False, environ={'ROCA_DATA_HOME': 'relative'})
        with self.assertRaisesRegex(RuntimeError, 'LOCALAPPDATA'):
            resolve_paths(frozen=True, environ={})
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                resolve_paths(Path(folder) / 'app.py', frozen=True,
                              environ={'ROCA_DATA_HOME': str(Path(folder) / 'data')})

    def test_readiness_does_not_create_store_or_read_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('station.js', 'station.css', 'preview.js', 'cloud.js', 'cloud.css', 'intake.js', 'roca_square_label_app.html', 'roca_edition.json', 'product_brand.json'):
                (root / name).write_text('fixture')
            data = root / 'uncreated'
            report = readiness(StationPaths(root, data, False))
            self.assertTrue(report['ready'])
            self.assertFalse(report['square_contacted'])
            self.assertFalse(data.exists())
            (root / 'station.css').unlink()
            self.assertFalse(readiness(StationPaths(root, data, False))['ready'])

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
