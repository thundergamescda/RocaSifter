"""Release evidence must reject missing, duplicate and misleading case coverage."""
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from tools import test_release as release
from tools import test_hosted_postgres as postgres


class EvidenceGuards(unittest.TestCase):
    def evidence(self):
        return {'expected_test_ids': ['suite.case_a', 'suite.case_b'],
                'started_test_ids': ['suite.case_a', 'suite.case_b'],
                'successful_test_ids': ['suite.case_a', 'suite.case_b'],
                'tests_run': 2, 'expected_count': 2}

    def test_complete_exact_nonempty_coverage(self):
        self.assertTrue(release.complete_coverage(self.evidence()))
        self.assertFalse(release.complete_coverage({}))
        self.assertFalse(release.complete_coverage({key: [] for key in self.evidence()}))

    def test_missing_extra_duplicate_or_false_count_cannot_pass(self):
        for key, value in (
                ('started_test_ids', ['suite.case_a']),
                ('successful_test_ids', ['suite.case_a', 'suite.case_a']),
                ('started_test_ids', ['suite.case_a', 'suite.case_b', 'suite.case_c']),
                ('expected_test_ids', ['suite.case_a', 'suite.case_a']),
                ('tests_run', True), ('expected_count', True),
                ('tests_run', 3), ('expected_count', 1),
                ('successful_test_ids', 'suite.case_a'),
                ('successful_test_ids', ['suite.case_a', 2])):
            with self.subTest(key=key, value=value):
                self.assertFalse(release.complete_coverage(self.evidence() | {key: value}))

    def test_new_unit_module_is_included_and_postgres_remains_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('test_new_claim.py', 'test_two_pc_postgres.py', 'ordinary.py'):
                (root / name).write_bytes(b'# fictional discovery input only\n')
            with patch.object(release, 'root', root):
                modules = release.selected_modules()
            self.assertIn('test_new_claim', modules)
            self.assertNotIn('test_two_pc_postgres', modules)
            self.assertNotIn('ordinary', modules)
            self.assertEqual(len(modules), len(set(modules)))

    def test_postgres_selection_contains_every_named_pg_module(self):
        expected = {'hosted.integration'} | {p.stem for p in postgres.ROOT.glob('test_*_postgres.py')}
        self.assertEqual(set(postgres.ALLOWED), expected)
        self.assertIn('test_hosted_physical_intake_postgres', postgres.ALLOWED)
        self.assertEqual(len(postgres.ALLOWED), len(expected))

    def test_delivered_log_hash_bytes_have_no_windows_translation(self):
        # Prior frozen evidence documented normalized text hashes. New evidence
        # uses bytes so a recipient can hash exactly the delivered log instead.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fictional.log'
            payload = 'PASS fictional case\nPASS Unicode Pok\u00e9mon\n'.encode('utf-8')
            evidence = postgres.write_log(path, payload.decode('utf-8'))
            self.assertEqual(path.read_bytes(), payload)
            self.assertNotIn(b'\r\n', path.read_bytes())
            self.assertEqual(evidence['log_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main(verbosity=2)
