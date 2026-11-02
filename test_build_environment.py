"""Desktop compilation cannot source native dependencies from ambient tools."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import build_desktop as build


class BuildEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fictional-build-environment-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / 'venv' / 'Scripts'
        self.runtime = self.root / 'reviewed-runtime'
        self.windows = self.root / 'Windows'
        self.powershell = self.windows / 'System32' / 'WindowsPowerShell' / 'v1.0'
        for path in (self.scripts, self.runtime, self.powershell):
            path.mkdir(parents=True)
        self.executable = self.scripts / 'python.exe'
        self.executable.write_bytes(b'Fictional path fixture, never executed')
        for target, value in (('executable', str(self.executable)), ('base_prefix', str(self.runtime))):
            patcher = patch.object(build.sys, target, value)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(build, 'windows_directory', return_value=self.windows.resolve())
        patcher.start(); self.addCleanup(patcher.stop)

    def test_only_interpreter_and_actual_windows_directories_enter_path(self):
        inherited = {'PATH': 'C:\\Fictional-Unreviewed-Native-Tools',
                     'SystemRoot': 'C:\\Fictional-Wrong-Windows',
                     'WINDIR': 'C:\\Fictional-Wrong-Windows', 'TEMP': str(self.root)}
        with patch.dict(os.environ, inherited, clear=True):
            before = dict(os.environ)
            environment = build.build_environment()
            self.assertEqual(dict(os.environ), before)
        expected = [self.scripts.resolve(), self.runtime.resolve(),
                    self.windows / 'System32', self.windows, self.powershell]
        self.assertEqual(environment['PATH'].split(os.pathsep), list(map(str, expected)))
        self.assertEqual(environment['SystemRoot'], str(self.windows))
        self.assertEqual(environment['WINDIR'], str(self.windows))
        self.assertEqual(environment['COMSPEC'], str(self.windows / 'System32' / 'cmd.exe'))
        self.assertEqual(environment['TEMP'], str(self.root))

    def test_python_and_frozen_process_overrides_are_not_inherited(self):
        inherited = {'Path': 'C:\\Fictional-Ambient', 'PYTHONPATH': 'fictional-private-modules',
                     'pythonhome': 'fictional-other-runtime', 'PYTHONWARNINGS': 'ignore',
                     '_PYI_APPLICATION_HOME_DIR': 'fictional-other-app', '_MEIPASS2': 'fictional-other-app',
                     'PYINSTALLER_CONFIG_DIR': 'fictional-other-cache', 'TMP': str(self.root)}
        with patch.dict(os.environ, inherited, clear=True):
            environment = build.build_environment()
        self.assertEqual({key: value for key, value in environment.items() if key.upper().startswith('PYTHON')},
                         {'PYTHONNOUSERSITE': '1', 'PYTHONUTF8': '1'})
        for key in inherited:
            if key != 'TMP':
                self.assertNotIn(key, environment)
        self.assertEqual(environment['TMP'], str(self.root))

    def test_missing_required_directory_refuses_instead_of_restoring_ambient_path(self):
        self.powershell.rmdir()
        with patch.dict(os.environ, {'PATH': 'C:\\Fictional-Ambient'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'required isolated'):
                build.build_environment()

    def test_duplicate_runtime_script_directory_is_not_repeated(self):
        with patch.object(build.sys, 'base_prefix', str(self.scripts)):
            environment = build.build_environment()
        paths = environment['PATH'].split(os.pathsep)
        self.assertEqual(len(paths), 4)
        self.assertEqual(len(paths), len(set(paths)))

    def test_optional_python_warning_does_not_mask_or_fake_native_failure(self):
        result = subprocess.CompletedProcess(['fictional-compiler'], 0,
                                             'WARNING: Hidden import fictional_optional not found\n')
        environment = {'PATH': 'fictional-isolated-path'}
        with patch.object(build.subprocess, 'run', return_value=result) as run, patch('builtins.print'):
            build.run_compiler(['fictional-compiler'], environment)
        self.assertIs(run.call_args.kwargs['env'], environment)
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.STDOUT)

    def test_native_missing_library_rejects_even_after_compiler_exit_zero(self):
        result = subprocess.CompletedProcess(['fictional-compiler'], 0,
                 "123 WARNING: Library not found: could not resolve 'fictional.dll', dependency of 'fictional.pyd'.\n")
        with patch.object(build.subprocess, 'run', return_value=result), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'unresolved native library'):
                build.run_compiler(['fictional-compiler'], {})

    def test_compiler_failure_is_not_accepted_when_native_warning_is_absent(self):
        result = subprocess.CompletedProcess(['fictional-compiler'], 5, 'Fictional compiler failed\n')
        with patch.object(build.subprocess, 'run', return_value=result), patch('builtins.print'):
            with self.assertRaises(subprocess.CalledProcessError):
                build.run_compiler(['fictional-compiler'], {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
