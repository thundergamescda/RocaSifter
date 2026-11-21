"""UNRUN: real SQLite/service/render/restart; owned fictional hosted transport.

These are separate from portable renderer40. No dependency/import substitutes,
provider endpoint, real account, Windows printer or physical scanner is used.
"""
import copy
from contextlib import closing, nullcontext
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import pypdfium2 as pdfium
import zxingcpp

import roca_to_square as core
from station_cloud import CloudError
from station_service import StationService
from station_store import StationError
from station_profile import POLICY_FILE
from station_label_publication import OrdinaryLabels, PREFIX
import station_label_publication as publication
import station_receiving_label_render as raster
import station_receiving_label_worker as worker
import test_hosted_receive as receiving_fixtures
import test_station_cloud as cloud_fixtures


def owned_pil(bitmap):
    view = bitmap.to_pil()
    try:
        return view.copy()
    finally:
        view.close()


class OrdinaryReceiptLabelPublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = receiving_fixtures.HostedReceivingTests(
            'test_plan_is_read_only_and_commit_mirrors_only_exact_proof_and_invalidates_files')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.root = self.fixture.root
        self.expected = self.fixture.session['sessionId']
        self.output = Path(core.OUTPUT_DIR)

    def review(self, receipt):
        return self.service.review_cards(receipt['id'], {'revision': receipt['revision'],
            'rowIds': [card['row_id'] for card in receipt['cards']]})

    def received(self, receipt=None):
        receipt = self.review(receipt or self.fixture.receipt())
        self.fixture.remote.target_skus.update({'Fictional A': '000123456789', 'Fictional B': ' lead>^~'})
        result = self.fixture.commit(self.fixture.prepare(self.fixture.claim(receipt)))['receipt']
        self.fixture.remote.calls.clear()
        return result

    def labels(self, receipt, **kwargs):
        return self.service.labels(receipt['id'], expected_session_id=self.expected, **kwargs)

    def current(self, receipt):
        return self.service.store.receipt(receipt['id'])

    def files(self):
        return sorted(self.output.glob(PREFIX + '*')) if self.output.exists() else []

    def publication_events(self, receipt):
        with self.service.store.db() as db:
            return list(db.execute("SELECT kind,detail FROM events WHERE receipt_id=? AND kind='ordinary_labels_published'", (receipt['id'],)))

    def immutable(self, receipt):
        return {key: copy.deepcopy(receipt.get(key)) for key in ('cards', 'hosted_receive', 'intake_claim',
            'purchase', 'inventory_status', 'inventory_intent', 'source_hash', 'original_source')}

    def assert_pair(self, receipt, *, legacy=False):
        ident = receipt['label_job_current']
        job = receipt['label_jobs'][ident]
        self.assertEqual(job['state'], 'published')
        self.assertEqual({k: receipt['files'][k] for k in ('pdf', 'zpl')}, job['files'])
        cards = {card['row_id']: card for card in self.service.approved_cards(receipt)}
        expected = []
        for row, count in sorted(job['terms']['renderCounts'].items()):
            expected.extend([core.barcode_value(cards[row])] * count)
        decoded = []
        with closing(pdfium.PdfDocument(self.output / job['files']['pdf'])) as document:
            self.assertEqual(len(document), job['terms']['copyCount'])
            for index in range(len(document)):
                with closing(document[index]) as page:
                    with closing(page.render(scale=job['terms']['profile']['dpi'] / 72)) as bitmap:
                        with closing(owned_pil(bitmap)) as image:
                            values = [value.text for value in zxingcpp.read_barcodes(image)]
                            self.assertIn(expected[index], values)
                            decoded.append(expected[index])
        self.assertEqual(decoded, expected)
        wire = (self.output / job['files']['zpl']).read_text('ascii')
        self.assertEqual(sum(map(int, re.findall(r'\^PQ(\d+)(?:,\d+)*\^XZ', wire))), len(expected))
        for row in job['terms']['renderCounts']:
            sku = core.barcode_value(cards[row])
            if legacy:
                self.assertRegex(sku, r'^\d{12}$')
                self.assertIn(sku, wire)
            else:
                self.assertIn(raster.zpl_hex(sku), wire)
        for kind in ('pdf', 'zpl'):
            content = (self.output / job['files'][kind]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), job['stage']['files'][kind]['sha256'])
            self.assertEqual(len(content), job['stage']['files'][kind]['bytes'])
        return job

    def assert_no_provider_writes(self, baseline):
        self.assertEqual(self.fixture.remote.stock_calls, baseline)
        self.assertTrue(all(method == 'GET' for method, _ in self.fixture.remote.calls))

    def test_csv_receipt_actual_pair_exact_skus_and_restart_same_job(self):
        receipt = self.received()
        before = self.immutable(receipt)
        baseline = self.fixture.remote.stock_calls
        result = self.labels(receipt)
        self.assert_pair(result)
        self.assertEqual(self.immutable(result), before)
        original = {p.name: p.read_bytes() for p in self.files()}
        self.service = self.fixture.restart()
        with patch.object(worker, 'run_pair', side_effect=AssertionError('A committed same job must not render again.')):
            retried = self.labels(self.current(receipt))
        self.assertEqual(retried['label_job_current'], result['label_job_current'])
        self.assertEqual({p.name: p.read_bytes() for p in self.files()}, original)
        self.assertEqual(len(self.publication_events(receipt)), 1)
        self.assertEqual(self.immutable(retried), before)
        self.assert_no_provider_writes(baseline)

    def test_manual_raw_and_slab_actual_range_reprint_preserves_full_marks_and_quantities(self):
        receipt = None
        for name, extra in [('Manual raw', {}), ('Manual slab', {'grader': 'PSA', 'grade': '10', 'cert_number': '000999'})]:
            data = {'requestId': str(uuid.uuid4()), 'mode': 'labels',
                    'receiptId': receipt['id'] if receipt else None, 'revision': receipt['revision'] if receipt else None,
                    'item': {'card_name': name, 'game': 'pokemon', 'condition': 'Near Mint',
                             'final_price': '13.13', 'qty': '3', **extra}}
            receipt = self.service.add_manual_item(data)
        receipt['inventory_intent'] = 'new'
        self.service.store.save_receipt(receipt)
        self.fixture.remote.target_skus.update({'Manual raw': '000123456789', 'Manual slab': 'A>5B^XZ~JA'})
        receipt = self.received(receipt)
        before = self.immutable(receipt)
        baseline = self.fixture.remote.stock_calls
        first = self.labels(receipt, copy_offset=2, copy_count=2, job_id=str(uuid.uuid4()))
        self.assert_pair(first)
        self.assertEqual(sorted(first['label_jobs'][first['label_job_current']]['terms']['renderCounts'].values()), [1, 1])
        old = {p.name: p.read_bytes() for p in self.files()}
        second = self.labels(first, copy_offset=4, copy_count=2, job_id=str(uuid.uuid4()))
        self.assert_pair(second)
        self.assertEqual(len(self.files()), 4)
        self.assertTrue(all((self.output / name).read_bytes() == content for name, content in old.items()))
        first_id = first['label_job_current']
        with patch.object(worker, 'run_pair', side_effect=AssertionError('Selecting an old pair must not render.')):
            selected = self.labels(second, copy_offset=2, copy_count=2, job_id=first_id)
        self.assertEqual(selected['files'], first['files'])
        self.assertEqual(len(self.publication_events(receipt)), 2)
        self.assertEqual(self.immutable(selected), before)
        self.assert_no_provider_writes(baseline)

    def test_legacy_actual_pair_preserves_existing_numeric_barcode_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cloud_fixtures.seed_historical_profile(root / 'station_data')
            with patch.object(core, 'SETTINGS_FILE', str(root / 'settings.json')), patch.object(core, 'OUTPUT_DIR', str(root / 'outputs')):
                service = StationService(core)
                service.store.put('preferences', service.preferences() | {'pricingEnabled': False})
                receipt = service.import_csv('Name,Game,Price,Quantity,SKU\nLegacy card,Magic,3.13,3,ABC-local\n', 'fictional.csv', 'labels')['receipt']
                receipt['inventory_intent'] = 'existing'
                service.store.save_receipt(receipt)
                receipt = service.review_cards(receipt['id'], {'revision': receipt['revision'], 'rowIds': [c['row_id'] for c in receipt['cards']]})
                before = self.immutable(receipt)
                result = service.labels(receipt['id'], copy_offset=1, copy_count=2)
                previous_service, previous_output = self.service, self.output
                try:
                    self.service, self.output = service, root / 'outputs'
                    self.assert_pair(result, legacy=True)
                    restarted = StationService(core)
                    with patch('station_label_legacy_worker.run_pair', side_effect=AssertionError('Same committed legacy pair must not render.')):
                        same = restarted.labels(receipt['id'], copy_offset=1, copy_count=2)
                    self.assertEqual(same['label_job_current'], result['label_job_current'])
                    self.assertEqual(self.immutable(same), before)
                    self.assertEqual(len(self.files()), 2)
                finally:
                    self.service, self.output = previous_service, previous_output
        # A legacy profile containing a genuine hosted receipt does not acquire
        # an offline exemption from that receipt's current canonical authority.
        legacy = receiving_fixtures.HostedReceivingTests('test_source_profile_session_and_store_changes_cannot_transfer_intent')
        legacy.setUp()
        self.addCleanup(legacy.doCleanups)
        self.fixture, self.service, self.root = legacy, legacy.service, legacy.root
        self.expected, self.output = legacy.session['sessionId'], Path(core.OUTPUT_DIR)
        self.assertEqual(self.service.profile.mode, 'legacy_compatibility')
        claimed = legacy.claim(self.review(legacy.receipt()))
        with self.assertRaises(StationError):
            self.labels(claimed)
        received = legacy.commit(legacy.prepare(claimed))['receipt']
        operation = legacy.remote.receivings[('fictional-store', received['hosted_receive']['receiving_id'])]
        operation['state'] = 'outcome_unknown'
        with self.assertRaises(StationError):
            self.labels(received)
        self.assertEqual(self.files(), [])

    def test_partial_link_failure_is_prepared_not_downloadable_and_restart_completes_pair(self):
        receipt = self.received()
        job_id = str(uuid.uuid4())
        real_link = publication.os.link
        def fail_second(source, target, **kwargs):
            if str(target).endswith('.zpl'):
                raise OSError('Fictional second-link failure')
            return real_link(source, target, **kwargs)
        with patch.object(publication.os, 'link', side_effect=fail_second), self.assertRaises(OSError):
            self.labels(receipt, job_id=job_id)
        current = self.current(receipt)
        self.assertEqual(current['label_jobs'][job_id]['state'], 'prepared')
        self.assertNotIn('pdf', current['files'])
        self.assertEqual([p.suffix for p in self.files()], ['.pdf'])
        with self.assertRaises(StationError):
            OrdinaryLabels(self.service).download(receipt['id'], job_id, 'pdf', self.expected)
        self.service = self.fixture.restart()
        with patch.object(worker, 'run_pair', side_effect=AssertionError('Recover the actual prepared pair, never rerender it.')):
            result = self.labels(current, job_id=job_id)
        self.assert_pair(result)
        self.assertEqual(len(self.publication_events(receipt)), 1)

    def test_lost_commit_ack_recovers_published_pair_and_never_cleans_ambiguous_files(self):
        receipt = self.received()
        actual = OrdinaryLabels._commit
        def lost(instance, expected, prepared, publish=None):
            result = actual(instance, expected, prepared, publish)
            if prepared['event'] == 'ordinary_labels_published':
                raise OSError('Fictional reply lost after SQLite commit')
            return result
        with patch.object(OrdinaryLabels, '_commit', new=lost):
            result = self.labels(receipt)
        self.assert_pair(result)
        self.assertEqual(len(self.publication_events(receipt)), 1)
        self.assertTrue(Path(result['label_jobs'][result['label_job_current']]['stage']['directory']).is_dir())
        self.service = self.fixture.restart()
        with patch.object(worker, 'run_pair', side_effect=AssertionError('Durable recovery cannot render again.')):
            same = self.labels(self.current(receipt))
        self.assertEqual(same['files'], result['files'])
        self.assertEqual(len(self.files()), 2)

    def _final_result_denial(self, *, lost_ack):
        for change in ('role', 'logout', 'source', 'receipt', 'profile'):
            with self.subTest(change=change, lost_ack=lost_ack):
                receipt = self.received()
                saved = self.service.cloud._saved()
                actor = self.fixture.remote.sessions[saved['token']]
                actor_before = copy.deepcopy(actor)
                operation = self.fixture.remote.receivings[('fictional-store', receipt['hosted_receive']['receiving_id'])]
                operation_before = copy.deepcopy(operation)
                policy = self.service.profile._root / POLICY_FILE
                policy_before = policy.read_bytes()
                actual_pair, actual_commit = OrdinaryLabels._pair, OrdinaryLabels._commit
                observed = {}
                baseline = self.fixture.remote.stock_calls

                def changed(instance, job, *, staged=False):
                    pair = actual_pair(instance, job, staged=staged)
                    if not staged and job['state'] == 'published' and not observed:
                        durable = self.current(receipt)
                        self.assertEqual(durable['label_jobs'][job['jobId']], job)
                        self.assertEqual(len(self.publication_events(receipt)), 1)
                        observed.update(job=copy.deepcopy(job), receipt=copy.deepcopy(durable),
                                        files={kind: bytes(raw) for kind, raw in pair.items()})
                        # The actual final pair read completed; change real owned
                        # authority/state before its caller can release a result.
                        if change == 'role':
                            actor['role'] = 'cashier'
                        elif change == 'logout':
                            self.service.cloud._clear(saved['session_id'])
                        elif change == 'source':
                            operation['state'] = 'outcome_unknown'
                        elif change == 'receipt':
                            # Same business terms, changed exact persisted body
                            # and audit: this must reach the response CAS fence.
                            self.service.store.save_receipt(durable, ('fixture_after_pair', 'Owned revision changed after final pair read.'))
                        else:
                            value = json.loads(policy.read_text())
                            value['binding'] = None
                            policy.write_text(json.dumps(value))
                    return pair

                def lost(instance, expected, prepared, publish=None):
                    result = actual_commit(instance, expected, prepared, publish)
                    if prepared['event'] == 'ordinary_labels_published':
                        raise OSError('Fictional final commit acknowledgement lost')
                    return result

                try:
                    with patch.object(OrdinaryLabels, '_pair', new=changed):
                        with (patch.object(OrdinaryLabels, '_commit', new=lost) if lost_ack else nullcontext()):
                            with self.assertRaises(StationError):
                                self.labels(receipt)
                    self.assertTrue(observed, 'The real post-commit pair read must be reached.')
                    current = self.current(receipt)
                    ident = observed['job']['jobId']
                    self.assertEqual(current['label_jobs'][ident], observed['job'])
                    self.assertEqual(current['files'], observed['receipt']['files'])
                    self.assertEqual(len(self.publication_events(receipt)), 1)
                    for kind, raw in observed['files'].items():
                        self.assertEqual((self.output / observed['job']['files'][kind]).read_bytes(), raw)
                    if lost_ack:
                        self.assertTrue(Path(observed['job']['stage']['directory']).is_dir())
                finally:
                    actor.clear()
                    actor.update(actor_before)
                    operation.clear()
                    operation.update(operation_before)
                    if change == 'logout':
                        self.service.cloud.vault.set(json.dumps(saved))
                    if change == 'profile':
                        policy.write_bytes(policy_before)
                    if change == 'receipt' and observed:
                        self.service.store.save_receipt(observed['receipt'])
                # Restored genuine authority can reopen that same committed pair
                # after service restart, without rendering or publishing again.
                self.service = self.fixture.restart()
                with patch.object(worker, 'run_pair', side_effect=AssertionError('A denied result cannot replace its already committed pair.')):
                    recovered = self.labels(self.current(receipt))
                self.assertEqual(recovered['label_job_current'], observed['job']['jobId'])
                self.assert_pair(recovered)
                self.assertEqual(len(self.publication_events(receipt)), 1)
                self.assert_no_provider_writes(baseline)

    def test_normal_final_pair_read_revalidates_result_authority_and_keeps_committed_pair(self):
        self._final_result_denial(lost_ack=False)

    def test_lost_ack_final_pair_read_revalidates_result_authority_and_keeps_committed_pair(self):
        self._final_result_denial(lost_ack=True)

    def test_active_job_is_busy_and_expired_lease_preserves_unknown_old_attempt(self):
        receipt = self.received()
        actual = OrdinaryLabels._stage
        with patch.object(OrdinaryLabels, '_stage', side_effect=KeyboardInterrupt('Fictional process termination')):
            # Also lose the failure write, exactly as a terminated process would.
            with patch.object(OrdinaryLabels, '_current', side_effect=OSError('Fictional process gone')):
                with self.assertRaises(KeyboardInterrupt):
                    self.labels(receipt)
        current = self.current(receipt)
        ident = current['label_job_current']
        job = current['label_jobs'][ident]
        old_stage = self.output / '.ss-receipt-label-stage' / job['attempt']
        foreign = old_stage / 'unknown.txt'
        foreign.write_bytes(b'preserve unknown attempt content')
        with patch.object(OrdinaryLabels, '_stage', side_effect=AssertionError('A live lease cannot render again.')):
            with self.assertRaisesRegex(StationError, 'still running'):
                self.labels(current)
        job['leaseUntil'] = 0
        self.service.store.save_receipt(current)
        with patch.object(OrdinaryLabels, '_stage', new=actual):
            result = self.labels(current)
        self.assert_pair(result)
        self.assertEqual(foreign.read_bytes(), b'preserve unknown attempt content')
        self.assertIn(job['attempt'], result['label_jobs'][ident]['priorAttempts'])

    def test_exact_receipt_cas_denies_intervening_edit_without_replacing_it(self):
        receipt = self.received()
        actual = OrdinaryLabels._stage
        def changed(instance, *args):
            owned = actual(instance, *args)
            current = self.current(receipt)
            current['notes'] = 'intervening saved operator edit'
            self.service.store.save_receipt(current)
            return owned
        with patch.object(OrdinaryLabels, '_stage', new=changed), self.assertRaisesRegex(StationError, 'changed'):
            self.labels(receipt)
        self.assertEqual(self.current(receipt)['notes'], 'intervening saved operator edit')
        self.assertEqual(self.files(), [])
        self.assertEqual(self.publication_events(receipt), [])
        # Also race AFTER the final exact-body snapshot, immediately before the
        # real SQLite UPDATE. The first subcase above remains a source/basis
        # rejection; this one proves the actual late rowcount!=1 CAS branch.
        receipt = self.received()
        actual_commit = OrdinaryLabels._commit
        observed = {}
        def persisted():
            with self.service.store.db() as db:
                row = db.execute('SELECT body,updated,deleted_at FROM receipts WHERE id=?', (receipt['id'],)).fetchone()
                summary = db.execute('SELECT receipt_updated,format_version,body,digest FROM receipt_summaries WHERE receipt_id=?', (receipt['id'],)).fetchone()
                events = db.execute('SELECT id,at,kind,detail FROM events WHERE receipt_id=? ORDER BY id', (receipt['id'],)).fetchall()
            return {'receipt': tuple(row), 'summary': tuple(summary), 'events': [tuple(event) for event in events]}
        def raced(instance, expected, prepared, publish=None):
            if prepared['event'] == 'ordinary_labels_published':
                current = self.current(receipt)
                self.assertEqual(self.service.store.receipt_compare_body(current), expected)
                current['notes'] = 'late owned writer after final publication snapshot'
                # Real persisted receipt, derived summary and audit commit. No
                # mocked rowcount, CAS implementation, guard or provider call.
                self.service.store.save_receipt(current, ('fixture_late_cas_edit', 'Intervening owned receipt edit must survive.'))
                observed.update(before=expected, after=persisted())
            return actual_commit(instance, expected, prepared, publish)
        with patch.object(OrdinaryLabels, '_commit', new=raced), self.assertRaisesRegex(StationError, 'changed before label publication'):
            self.labels(receipt)
        self.assertTrue(observed)
        self.assertNotEqual(observed['before'], observed['after']['receipt'][0])
        self.assertEqual(persisted(), observed['after'])
        self.assertEqual(self.current(receipt)['notes'], 'late owned writer after final publication snapshot')
        self.assertEqual(self.files(), [])
        self.assertEqual(self.publication_events(receipt), [])
        self.assertEqual(self.current(receipt)['label_jobs'][self.current(receipt)['label_job_current']]['state'], 'prepared')

    def test_current_receiving_source_change_during_render_denies_publication(self):
        receipt = self.received()
        actual = OrdinaryLabels._stage
        operation = self.fixture.remote.receivings[('fictional-store', receipt['hosted_receive']['receiving_id'])]
        def changed(instance, *args):
            owned = actual(instance, *args)
            operation['state'] = 'outcome_unknown'
            operation['inventory_confirmed'] = False
            return owned
        with patch.object(OrdinaryLabels, '_stage', new=changed), self.assertRaises(CloudError):
            self.labels(receipt)
        self.assertEqual(self.files(), [])
        self.assertEqual(self.publication_events(receipt), [])

    def test_actor_session_and_profile_changes_after_actual_render_deny_publication(self):
        for change in ('role', 'logout', 'profile'):
            with self.subTest(change=change):
                receipt = self.received()
                saved = self.service.cloud._saved()
                actor = self.fixture.remote.sessions[saved['token']]
                actual = OrdinaryLabels._stage
                def changed(instance, *args):
                    owned = actual(instance, *args)
                    if change == 'role':
                        actor['role'] = 'cashier'
                    elif change == 'logout':
                        self.service.cloud._clear(saved['session_id'])
                    else:
                        self.service.profile = self.fixture.restart().profile
                        # Different durable profile policy, not a synthetic role
                        # or a fabricated guard exception.
                        path = self.service.profile._root / POLICY_FILE
                        value = json.loads(path.read_text())
                        value['binding'] = None
                        path.write_text(json.dumps(value))
                    return owned
                try:
                    with patch.object(OrdinaryLabels, '_stage', new=changed), self.assertRaises(StationError):
                        self.labels(receipt)
                    self.assertFalse(any(job['state'] == 'published' for job in self.current(receipt)['label_jobs'].values()))
                finally:
                    actor['role'] = 'manager'
                    if change == 'logout':
                        self.service.cloud.vault.set(json.dumps(saved))
                    if change == 'profile':
                        # This subcase is last and the temporary profile is
                        # discarded by the fixture; no production policy change.
                        pass

    def test_managed_download_checks_actual_pair_and_fresh_receiving_authority(self):
        receipt = self.labels(self.received())
        ident = receipt['label_job_current']
        labels = OrdinaryLabels(self.service)
        downloaded = labels.download(receipt['id'], ident, 'pdf', self.expected)
        self.assertEqual(downloaded['content'], (self.output / receipt['files']['pdf']).read_bytes())
        operation = self.fixture.remote.receivings[('fictional-store', receipt['hosted_receive']['receiving_id'])]
        original = copy.deepcopy(operation)
        operation['state'] = 'outcome_unknown'
        with self.assertRaises(CloudError):
            labels.download(receipt['id'], ident, 'pdf', self.expected)
        operation.clear()
        operation.update(original)
        zpl = self.output / receipt['files']['zpl']
        before = zpl.read_bytes()
        zpl.write_bytes(before + b'corrupt')
        with self.assertRaises(StationError):
            labels.download(receipt['id'], ident, 'pdf', self.expected)
        zpl.write_bytes(before)
        saved = self.service.cloud._saved()
        self.fixture.remote.sessions[saved['token']]['role'] = 'cashier'
        with self.assertRaises(StationError):
            labels.download(receipt['id'], ident, 'pdf', self.expected)
        self.assertEqual(len(self.publication_events(receipt)), 1)

    def test_partial_private_copy_failure_cleans_only_owned_attempt_and_keeps_prior_pair(self):
        receipt = self.labels(self.received())
        prior = {p.name: p.read_bytes() for p in self.files()}
        actual = OrdinaryLabels._write_stage
        def failed(path, data, owned, kind):
            if kind == 'zpl':
                raise OSError('Fictional second private copy failure')
            return actual(path, data, owned, kind)
        with patch.object(OrdinaryLabels, '_write_stage', new=staticmethod(failed)), self.assertRaises(OSError):
            self.labels(receipt, job_id=str(uuid.uuid4()))
        self.assertEqual({p.name: p.read_bytes() for p in self.files()}, prior)
        self.assertEqual(len(self.publication_events(receipt)), 1)
        self.assertEqual(list((self.output / '.ss-receipt-label-stage').iterdir()), [])

    def test_delivery_bookend_rechecks_source_and_session_after_actual_file_read(self):
        receipt = self.labels(self.received())
        ident = receipt['label_job_current']
        saved = self.service.cloud._saved()
        operation = self.fixture.remote.receivings[('fictional-store', receipt['hosted_receive']['receiving_id'])]
        actual = OrdinaryLabels._pair
        for change in ('source', 'logout', 'receipt'):
            with self.subTest(change=change):
                original_operation = copy.deepcopy(operation)
                original_receipt = self.current(receipt)
                def changed(instance, job, **kwargs):
                    result = actual(instance, job, **kwargs)
                    if change == 'source':
                        operation['state'] = 'outcome_unknown'
                    elif change == 'logout':
                        self.service.cloud._clear(saved['session_id'])
                    else:
                        current = self.current(receipt)
                        current['notes'] = 'changed while reading label bytes'
                        self.service.store.save_receipt(current)
                    return result
                try:
                    with patch.object(OrdinaryLabels, '_pair', new=changed), self.assertRaises(StationError):
                        OrdinaryLabels(self.service).download(receipt['id'], ident, 'pdf', self.expected)
                finally:
                    operation.clear()
                    operation.update(original_operation)
                    if change == 'logout':
                        self.service.cloud.vault.set(json.dumps(saved))
                    if change == 'receipt':
                        self.service.store.save_receipt(original_receipt)

    def test_cleanup_refuses_foreign_content_replacement_identity_and_symlink(self):
        receipt = self.received()
        labels = OrdinaryLabels(self.service)
        _, stage = labels._roots()
        for kind in ('foreign', 'replacement', 'symlink'):
            with self.subTest(kind=kind):
                directory = stage / str(uuid.uuid4())
                directory.mkdir()
                target = directory / 'labels.pdf'
                target.write_bytes(b'owned')
                owned = {'directory': str(directory), 'identity': publication._identity(directory.lstat()),
                         'files': {'pdf': {'identity': publication._identity(target.lstat())}}}
                if kind == 'foreign':
                    (directory / 'unknown').write_bytes(b'foreign')
                elif kind == 'replacement':
                    replacement = directory / 'replacement'
                    replacement.write_bytes(b'foreign')
                    replacement.replace(target)
                else:
                    victim = stage / 'unrelated-victim'
                    victim.write_bytes(b'victim')
                    target.unlink()
                    target.symlink_to(victim)
                self.assertFalse(labels._cleanup(owned))
                self.assertTrue(directory.exists())
                self.assertTrue(target.exists())
                if kind == 'symlink':
                    self.assertEqual(victim.read_bytes(), b'victim')

    def test_range_and_job_identity_conflicts_never_overwrite_a_prior_output(self):
        receipt = self.labels(self.received(), copy_offset=0, copy_count=1, job_id=str(uuid.uuid4()))
        prior = {p.name: p.read_bytes() for p in self.files()}
        ident = receipt['label_job_current']
        for kwargs in ({'copy_offset': -1}, {'copy_offset': 3}, {'copy_count': 0}, {'copy_count': True},
                       {'copy_count': 1001}, {'copy_offset': 1, 'copy_count': 1, 'job_id': ident}):
            with self.subTest(kwargs=kwargs), self.assertRaises(StationError):
                self.labels(receipt, **kwargs)
        other = str(uuid.uuid4())
        collision = self.output / (PREFIX + other + '.pdf')
        collision.write_bytes(b'preexisting unrelated output')
        with self.assertRaises(StationError):
            self.labels(receipt, copy_count=1, job_id=other)
        self.assertEqual(collision.read_bytes(), b'preexisting unrelated output')
        self.assertTrue(all((self.output / name).read_bytes() == raw for name, raw in prior.items()))
        self.assertEqual(len(self.publication_events(receipt)), 1)

    def test_actual_http_routes_preserve_origin_session_and_managed_file_gates(self):
        from station_server import make_handler
        receipt = self.received()
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(core))
        server.station = self.service
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        origin = 'http://127.0.0.1:' + str(server.server_port)
        def request(method, path, body=None, *, session=None, request_origin=None):
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=140)
            headers = {'Origin': request_origin or origin, 'X-SS-Local-Session': self.expected if session is None else session}
            raw = None if body is None else json.dumps(body).encode()
            if raw is not None:
                headers['Content-Type'] = 'application/json'
            try:
                connection.request(method, path, raw, headers)
                response = connection.getresponse()
                data = response.read()
                self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                return response.status, data
            finally:
                connection.close()
        try:
            route = '/api/receipts/' + receipt['id'] + '/labels'
            status, raw = request('POST', route, {'copyCount': 1}, request_origin='https://unowned.invalid')
            self.assertEqual(status, 403)
            self.assertEqual(self.files(), [])
            status, raw = request('POST', route, {'copyCount': 1})
            self.assertEqual(status, 200, raw)
            receipt = json.loads(raw)['receipt']
            ident = receipt['label_job_current']
            download = '/api/receipt-label-publications/' + receipt['id'] + '/' + ident + '/pdf'
            self.assertEqual(request('GET', download)[0], 200)
            self.assertEqual(request('GET', download, session='')[0], 400)
            self.assertEqual(request('GET', download, request_origin='https://unowned.invalid')[0], 403)
            self.assertEqual(request('GET', '/files/' + receipt['files']['pdf'])[0], 400)
            self.assertEqual(request('GET', download + '?alternate=1')[0], 400)
            self.assertEqual(request('POST', route + '/extra', {'copyCount': 1})[0], 400)
            self.assertEqual(len(self.publication_events(receipt)), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main(verbosity=2)
