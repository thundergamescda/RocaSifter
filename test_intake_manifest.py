"""Pinned pure original-intake contract; no UI, DB, provider or file data."""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from intake_manifest import (
    ManifestError, MAX_MANIFEST_BYTES, capture_manifest, manifest_digest,
    normalize_identity, semantic_fingerprint, source_line_id, source_lines,
    validate_manifest,
)


def identity(**changes):
    return {'name': 'Fictional é card', 'game': 'pokemon', 'kind': 'card', 'set': 'Test',
            'number': '001', 'printing': 'Normal', 'condition': 'Near Mint', 'language': 'English'} | changes


def row(quantity=2, source_row=2, **changes):
    return {'identity': identity(**changes), 'quantity': quantity, 'source_row': source_row}


def csv(rows=None, raw='a' * 64):
    return capture_manifest('csv', [row()] if rows is None else rows, raw_csv_sha256=raw)


class IntakeManifestTests(unittest.TestCase):
    def test_pinned_utf8_vectors_and_exact_canonical_json(self):
        value = csv()
        self.assertEqual(value['sha256'], '5505ac73eef229153394ab540ac68846276ff376b82f9d895ed383e80cb20e20')
        self.assertEqual(source_line_id(identity()), '0a8eab9f3a60c3181d7dcbede6b5f4e614e73e818e6f2bfa43b8a4bdf8fbbf06')
        self.assertEqual(semantic_fingerprint(value), '7b6f0c42621f70508a47c1e7cb796de140da93431329938eca762654487abed0')
        literal = ('{"kind":"csv","raw_csv_sha256":"' + 'a' * 64 + '","rows":[{"identity":'
                   '{"condition":"Near Mint","game":"pokemon","kind":"card","language":"English",'
                   '"name":"Fictional é card","number":"001","printing":"Normal","set":"Test"},'
                   '"quantity":2,"source_row":2}],"version":1}')
        self.assertEqual(hashlib.sha256(b'stock-and-sleeve:source-manifest:v1\0' + literal.encode('utf-8')).hexdigest(), value['sha256'])
        self.assertEqual(manifest_digest(value), value['sha256'])

    def test_dictionary_key_order_is_irrelevant_but_source_row_order_is_evidence(self):
        first, second = row(1, 2), row(3, 4, name='Other fictional card')
        forward, reverse = csv([first, second]), csv([second, first])
        self.assertNotEqual(manifest_digest(forward), manifest_digest(reverse))
        self.assertEqual(semantic_fingerprint(forward), semantic_fingerprint(reverse))
        reversed_keys = {key: forward[key] for key in reversed(forward)}
        self.assertEqual(validate_manifest(reversed_keys), forward)

    def test_duplicate_raw_card_groups_and_split_quantities_have_same_semantics(self):
        separate = csv([row(1, 3), row(2, 2)])
        combined = csv([row(3, 91)])
        self.assertNotEqual(manifest_digest(separate), manifest_digest(combined))
        self.assertEqual(semantic_fingerprint(separate), semantic_fingerprint(combined))
        groups = source_lines(separate)
        self.assertEqual(groups, [{'source_line_key': source_line_id(identity()), 'identity': identity(),
                                   'quantity': 3, 'source_rows': [2, 3]}])
        altered = csv([row(4, 91)])
        self.assertNotEqual(semantic_fingerprint(combined), semantic_fingerprint(altered))

    def test_raw_file_hash_source_positions_and_kind_do_not_create_new_semantic_identity(self):
        first = csv()
        other_file = csv([row(2, 99)], raw='b' * 64)
        manual = capture_manifest('manual', [{'identity': identity(), 'quantity': 2}])
        self.assertEqual(len({manifest_digest(x) for x in (first, other_file, manual)}), 3)
        self.assertEqual(len({semantic_fingerprint(x) for x in (first, other_file, manual)}), 1)
        self.assertEqual(source_lines(manual)[0]['source_rows'], [])

    def test_nfc_is_stable_but_case_spacing_punctuation_and_aliases_are_not_collapsed(self):
        decomposed = row(name='  Fictional e\u0301 card  ')
        self.assertEqual(csv([decomposed]), csv())
        for change in ({'name': 'fictional é card'}, {'name': 'Fictional  é card'},
                       {'name': 'Fictional é-card'}, {'printing': 'Non-Foil'},
                       {'condition': 'NM'}, {'number': '００１'}, {'game': 'Pokémon'}):
            with self.subTest(change=change):
                self.assertNotEqual(source_line_id(identity(**change)), source_line_id(identity()))

    def test_distinct_slab_grades_certificates_and_sports_facts_stay_distinct(self):
        slab = identity(kind='slab', grading_company='Fictional Grader', grade='10', certification='Cert-A')
        base = capture_manifest('manual', [{'identity': slab, 'quantity': 1}])
        for change in ({'grade': '１０'}, {'grade': '10 Black Label'}, {'certification': 'cert-a'},
                       {'grade_qualifier': 'Pristine'}, {'serial_number': '01/10'}, {'team': 'Fixture team'}):
            with self.subTest(change=change):
                value = capture_manifest('manual', [{'identity': slab | change, 'quantity': 1}])
                self.assertNotEqual(semantic_fingerprint(value), semantic_fingerprint(base))

    def test_capture_and_validate_return_independent_copies(self):
        original = [row()]
        captured = capture_manifest('csv', original, raw_csv_sha256='a' * 64)
        original[0]['identity']['name'] = 'Edited after intake'
        original[0]['quantity'] = 9
        self.assertEqual(captured, csv())
        validated = validate_manifest(captured)
        validated['rows'][0]['identity']['name'] = 'Mutated validation result'
        self.assertEqual(captured, csv())
        lines = source_lines(captured)
        lines[0]['identity']['name'] = 'Mutated grouping result'
        self.assertEqual(captured, csv())

    def test_tampered_rows_hash_or_version_fail_without_rehashing_as_original(self):
        for key, value in (('sha256', '0' * 64), ('version', 2), ('version', True)):
            bad = csv();bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ManifestError):validate_manifest(bad)
        for edit in (lambda v: v['rows'][0].update(quantity=3),
                     lambda v: v['rows'][0]['identity'].update(name='Edited card'),
                     lambda v: v.update(raw_csv_sha256='b' * 64)):
            bad = csv();edit(bad)
            with self.assertRaises(ManifestError):validate_manifest(bad)

    def test_noncanonical_wire_forms_are_rejected_even_with_normalized_hash(self):
        for edit in (lambda v: v['rows'][0]['identity'].update(name=' Fictional é card '),
                     lambda v: v['rows'][0]['identity'].update(name='Fictional e\u0301 card'),
                     lambda v: v['rows'][0]['identity'].update(player='')):
            bad = csv();edit(bad)
            with self.assertRaises(ManifestError):validate_manifest(bad)
        manual = capture_manifest('manual', [row()]);manual['raw_csv_sha256'] = None
        with self.assertRaises(ManifestError):validate_manifest(manual)

    def test_unknown_fields_including_price_review_local_ids_and_pii_are_rejected(self):
        for field in ('final_price', 'unit_intake_cents', 'reviewed', 'included', 'local_uuid', 'dob', 'government_id'):
            bad = row();bad[field] = 'forbidden'
            with self.subTest(field=field), self.assertRaises(ManifestError):csv([bad])
            bad = row();bad['identity'][field] = 'forbidden'
            with self.subTest(identity_field=field), self.assertRaises(ManifestError):csv([bad])
        for field in ('url', 'request_key', 'tenant_id', 'source_line_key'):
            bad = csv();bad[field] = 'forbidden'
            with self.subTest(field=field), self.assertRaises(ManifestError):validate_manifest(bad)

    def test_required_original_facts_are_never_invented(self):
        for field in ('name', 'game', 'kind'):
            bad = row();bad['identity'].pop(field)
            with self.subTest(field=field), self.assertRaises(ManifestError):csv([bad])
        for bad in (None, [], 'edited legacy receipt', {'identity': None, 'quantity': 1}):
            with self.subTest(value=bad), self.assertRaises(ManifestError):csv([bad])
        bad = row(game='other:fixture')
        with self.assertRaises(ManifestError):csv([bad])
        bad['identity']['game_label'] = 'Fictional additional game'
        self.assertEqual(csv([bad])['rows'][0]['identity']['game_label'], 'Fictional additional game')

    def test_control_invisible_surrogate_wrong_type_and_length_fields_fail(self):
        for name in ('bad\x00name', 'bad\nname', 'bad\u202ename', 'bad\u200dname', 'bad\ud800name', 'x' * 201, None, 3):
            with self.subTest(name=repr(name)), self.assertRaises(ManifestError):csv([row(name=name)])
        with self.assertRaises(ManifestError):csv([row(kind='sealed')])

    def test_positive_integer_and_source_position_boundaries(self):
        for quantity in (True, False, 0, -1, 1.0, '2', 100001, 10**100):
            with self.subTest(quantity=quantity), self.assertRaises(ManifestError):csv([row(quantity)])
        for position in (True, 0, -1, 1.0, '2', 10000001):
            with self.subTest(position=position), self.assertRaises(ManifestError):csv([row(1, position)])
        with self.assertRaises(ManifestError):csv([row(1, 2), row(1, 2)])
        self.assertEqual(csv([row(100000, 10000000)])['rows'][0]['quantity'], 100000)

    def test_whole_source_row_total_and_byte_limits_never_truncate(self):
        with self.assertRaises(ManifestError):csv([])
        with self.assertRaises(ManifestError):csv([row(1, n) for n in range(1, 1002)])
        at_limit = csv([row(100000, n) for n in range(1, 11)])
        self.assertEqual(source_lines(at_limit)[0]['quantity'], 1000000)
        with self.assertRaises(ManifestError):csv([row(100000, n) for n in range(1, 12)])
        huge = identity(name='界' * 200, game='界' * 100, set='界' * 200,
                        player='界' * 150, team='界' * 100, manufacturer='界' * 100)
        with self.assertRaisesRegex(ManifestError, 'byte limit'):
            capture_manifest('manual', [{'identity': huge, 'quantity': 1, 'source_row': n} for n in range(1, 1001)])
        healthy = csv([row(1, n) for n in range(1, 1001)])
        self.assertEqual(len(healthy['rows']), 1000)
        self.assertLessEqual(len(json.dumps(healthy, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()), MAX_MANIFEST_BYTES)

    def test_slabs_require_original_complete_unique_certificate_and_unit_quantity(self):
        slab = identity(kind='slab', grading_company='Fixture Grader', grade='9.5', certification='FIXTURE-001')
        for missing in ('grading_company', 'grade', 'certification'):
            bad = dict(slab);bad.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ManifestError):capture_manifest('manual', [{'identity': bad, 'quantity': 1}])
        with self.assertRaises(ManifestError):capture_manifest('manual', [{'identity': slab, 'quantity': 2}])
        with self.assertRaises(ManifestError):capture_manifest('manual', [{'identity': slab, 'quantity': 1}, {'identity': slab | {'name': 'Other label'}, 'quantity': 1}])
        with self.assertRaises(ManifestError):csv([row(grade='9.5')])

    def test_source_hash_format_kind_and_manual_provenance_cannot_be_fabricated(self):
        for digest in (None, '', 'a' * 63, 'A' * 64, 1, True, 'a' * 64 + '\n'):
            with self.subTest(digest=digest), self.assertRaises(ManifestError):csv(raw=digest)
        with self.assertRaises(ManifestError):capture_manifest('manual', [row()], raw_csv_sha256='a' * 64)
        for kind in (None, 'legacy', 'CSV', 1, True):
            with self.subTest(kind=kind), self.assertRaises(ManifestError):capture_manifest(kind, [row()])

    def test_pure_helper_imports_only_standard_library_and_contains_no_legacy_capture_path(self):
        source = Path(__file__).with_name('intake_manifest.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        imports = {node.names[0].name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import)}
        self.assertEqual(imports, {'hashlib', 'hmac', 'json', 're', 'unicodedata'})
        self.assertFalse(any(isinstance(node, ast.ImportFrom) for node in ast.walk(tree)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
