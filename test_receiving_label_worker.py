"""Private actual label children and bounded owned-process/storage failure cases."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from hosted_labels import mark
import station_receiving_label_worker as worker
import station_receiving_label_render as render


PROFILE = {'stock': '2x1', 'layout': 'side', 'dpi': 203}

def card():
    ident = str(uuid.UUID(int=1300))
    value = {'row_id': ident, 'source_line_key': 'b' * 64, 'qty': 3, 'sku': ' lead>^~',
             'card_name': 'Pokémon café fixture', 'game': 'pokemon', 'card_set': 'Fixture',
             'card_num': '001', 'condition': 'Near Mint', 'printing': 'Normal', 'final_price': 3.13,
             'include': True, 'reviewed': True, 'price_confirmed': True, 'price_missing': False}
    value['hosted_sku_resolution'] = mark(value, receiving_id='11111111-1111-4111-8111-111111111111',
        receipt_hash='a' * 64, review_line_id=ident, source_line_key='b' * 64,
        identity_hash='c' * 64, sku=value['sku'], selling_cents=313, confirmed_quantity=3)
    return value


class ReceivingLabelWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='stock-label-worker-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / 'private-stage'
        self.stage.mkdir()
        self.cards = [card()]
        self.counts = {self.cards[0]['row_id']: 1}
        self.prior = self.root / 'ss-stock-label-prior.pdf'
        self.prior.write_bytes(b'previous complete publication')

    def assert_empty_private(self):
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertEqual(self.prior.read_bytes(), b'previous complete publication')

    def fixture(self, mode):
        script = self.root / ('fixture-' + mode + '.py')
        source = f'''import hashlib,json,os,sys,time
from pathlib import Path
sys.path.insert(0,{str(Path(worker.__file__).resolve().parent)!r})
stage=Path(sys.argv[1]); mode={mode!r}
if mode=='timeout':
 (stage/'labels.pdf').write_bytes(b'%PDF-partial'); time.sleep(30)
elif mode=='partial':
 (stage/'labels.pdf').write_bytes(b'%PDF-partial'); raise SystemExit(3)
elif mode=='oversize':
 with (stage/'labels.pdf').open('xb') as stream:stream.truncate({worker.MAX_ARTIFACT_BYTES+1})
 time.sleep(30)
elif mode=='metadata-limit':
 with (stage/'result.json').open('xb') as stream:stream.truncate({worker.MAX_METADATA_BYTES+1})
elif mode=='malformed':
 (stage/'result.json').write_bytes(b'{{}}')
else:
 from station_receiving_label_render import render_pair
 raw=(stage/'input.json').read_bytes(); data=json.loads(raw)
 result=render_pair(data['cards'],stage/'labels.pdf',stage/'labels.zpl',data['profile'],data['renderCounts'])
 if mode=='bad-magic':
  with (stage/'labels.pdf').open('r+b') as stream:stream.write(b'BAD!!')
  result['pdf']['sha256']=hashlib.sha256((stage/'labels.pdf').read_bytes()).hexdigest()
 elif mode=='bad-hash':
  with (stage/'labels.pdf').open('r+b') as stream:stream.seek(-1,2);stream.write(b'X')
 elif mode=='copy-bool':result['pdf']['pages']=True
 elif mode=='basis-type':result['profile']['dpi']=203.0
 elif mode=='input-hash':pass
 elif mode=='zpl-count':
  path=stage/'labels.zpl'; value=path.read_bytes().replace(b'^PQ1^XZ',b'^PQ2^XZ');path.write_bytes(value)
  result['zpl']['sha256']=hashlib.sha256(value).hexdigest()
 body={{'version':1,'inputSha256':hashlib.sha256(raw).hexdigest(),'mode':'render','render':result}}
 if mode=='input-hash':body['inputSha256']='0'*64
 (stage/'result.json').write_text(json.dumps(body),encoding='utf-8')
'''
        script.write_text(source, encoding='utf-8')
        return lambda directory, child_mode: [sys.executable, '-X', 'utf8', str(script), str(directory)]

    def test_actual_pair_private_full_quantities_unchanged_and_no_parent_raster(self):
        self.cards[0].update(customer='PRIVATE-NOT-PRINTED', notes='PRIVATE-NOTES', intake_value=999,
                             purchase={'amount': 400}, storage_location='Edited')
        before = copy.deepcopy(self.cards)
        with (patch.object(render, 'preflight', side_effect=AssertionError('No parent raster')),
              patch.object(render, 'render_pair', side_effect=AssertionError('No parent renderer'))):
            result = worker.run_pair(self.cards, PROFILE, self.counts, self.stage)
        self.assertEqual(result['directory'].parent, self.stage)
        self.assertEqual(result['pdf'].read_bytes()[:5], b'%PDF-')
        self.assertIn(b'^PQ1^XZ', result['zpl'].read_bytes())
        self.assertEqual(result['metadata']['expectedPdfPages'], 1)
        self.assertEqual(result['metadata']['expectedZplCopies'], 1)
        self.assertFalse(result['metadata']['hardwareAcceptanceComplete'])
        raw = (result['directory'] / 'input.json').read_bytes()
        self.assertNotIn(b'PRIVATE-', raw)
        projected = json.loads(raw)['cards'][0]
        self.assertNotIn('purchase', projected)
        self.assertEqual((projected['qty'], projected['hosted_sku_resolution']['confirmed_quantity']), (3, 3))
        self.assertEqual(self.cards, before)
        with (result['pdf']).open('rb') as stream:
            self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), result['metadata']['pdf']['sha256'])
        worker.cleanup_attempt(result)
        self.assert_empty_private()

    def test_actual_preflight_only_matches_pair_basis_and_removes_attempt(self):
        with patch.object(render, 'preflight', side_effect=AssertionError('No parent raster')):
            basis = worker.preflight_pair(self.cards, PROFILE, self.counts, self.stage)
        self.assert_empty_private()
        result = worker.run_pair(self.cards, PROFILE, self.counts, self.stage)
        self.assertEqual(basis, {key: value for key, value in result['metadata'].items() if key not in ('pdf', 'zpl')})
        worker.cleanup_attempt(result)
        self.assert_empty_private()

    def test_two_actual_attempts_are_unique_and_cleanup_cannot_remove_other_pair(self):
        first = worker.run_pair(self.cards, PROFILE, self.counts, self.stage)
        second = worker.run_pair(self.cards, PROFILE, self.counts, self.stage)
        self.assertNotEqual(first['attemptId'], second['attemptId'])
        self.assertNotEqual(first['directory'], second['directory'])
        worker.cleanup_attempt(first)
        self.assertTrue(second['pdf'].is_file())
        self.assertTrue(second['zpl'].is_file())
        worker.cleanup_attempt(second)
        self.assert_empty_private()

    def test_actual_unsupported_font_text_fails_child_without_half_pair(self):
        self.cards[0]['card_name'] = '日本語'
        with self.assertRaises(worker.LabelWorkerError):
            worker.run_pair(self.cards, PROFILE, self.counts, self.stage)
        self.assert_empty_private()

    def test_invalid_full_mark_or_copy_range_creates_no_attempt_or_child(self):
        for mutate in ('quantity-bool', 'mark', 'range'):
            cards = copy.deepcopy(self.cards)
            counts = dict(self.counts)
            if mutate == 'quantity-bool':cards[0]['qty'] = True
            elif mutate == 'mark':cards[0]['hosted_sku_resolution']['confirmed_quantity'] = 1
            else:counts[cards[0]['row_id']] = True
            with self.subTest(mutate=mutate), patch.object(worker.subprocess, 'Popen') as spawn, self.assertRaises(worker.LabelWorkerError):
                worker.run_pair(cards, PROFILE, counts, self.stage)
            spawn.assert_not_called()
            self.assert_empty_private()

    def test_bad_deadline_or_input_byte_limit_creates_no_attempt(self):
        for value in (True, 0, -1, float('nan'), float('inf'), 121, '1'):
            with self.subTest(timeout=value), patch.object(worker.subprocess, 'Popen') as spawn, self.assertRaises(worker.LabelWorkerError):
                worker.run_pair(self.cards, PROFILE, self.counts, self.stage, timeout=value)
            spawn.assert_not_called()
        with patch.object(worker, 'MAX_INPUT_BYTES', 1), self.assertRaises(worker.LabelWorkerError):
            worker.run_pair(self.cards, PROFILE, self.counts, self.stage)
        self.assert_empty_private()

    def test_relative_traversal_file_and_simulated_reparse_roots_reject(self):
        for path in (Path('relative'), self.stage / '..' / 'private-stage', self.prior):
            with self.subTest(path=path), self.assertRaises(worker.LabelWorkerError):
                worker.run_pair(self.cards, PROFILE, self.counts, path)
        original = Path.lstat
        def reparse(path):
            info = original(path)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == self.stage else info
        with patch.object(Path, 'lstat', reparse), self.assertRaises(worker.LabelWorkerError):
            worker.run_pair(self.cards, PROFILE, self.counts, self.stage)
        self.assert_empty_private()

    def test_timeout_stops_only_exact_owned_child_and_cleans_partial(self):
        owned = []
        real_spawn = subprocess.Popen
        def spawn(*args, **kwargs):
            process = real_spawn(*args, **kwargs)
            owned.append(process)
            return process
        with patch.object(worker, '_command', self.fixture('timeout')), patch.object(worker.subprocess, 'Popen', spawn), self.assertRaises(worker.LabelWorkerError) as caught:
            worker.run_pair(self.cards, PROFILE, self.counts, self.stage, timeout=.3)
        self.assertEqual(caught.exception.code, 'ORIGINAL_STOCK_LABEL_WORKER_TIMEOUT')
        self.assertEqual(len(owned), 1)
        self.assertIsNotNone(owned[0].poll())
        self.assert_empty_private()

    def test_partial_exit_oversize_and_metadata_failures_leave_no_half_pair(self):
        for mode in ('partial', 'oversize', 'metadata-limit', 'malformed'):
            with self.subTest(mode=mode), patch.object(worker, '_command', self.fixture(mode)), self.assertRaises(worker.LabelWorkerError):
                worker.run_pair(self.cards, PROFILE, self.counts, self.stage, timeout=5)
            self.assert_empty_private()

    def test_cancellation_propagates_after_stopping_owned_child_and_cleaning_attempt(self):
        processes = []
        real_spawn = subprocess.Popen
        def spawn(*args, **kwargs):
            process = real_spawn(*args, **kwargs)
            processes.append(process)
            original_wait = process.wait
            first = True
            def wait(*args, **kwargs):
                nonlocal first
                if first:
                    first = False
                    raise KeyboardInterrupt('Fictional cancellation')
                return original_wait(*args, **kwargs)
            process.wait = wait
            return process
        with patch.object(worker, '_command', self.fixture('timeout')), patch.object(worker.subprocess, 'Popen', spawn), self.assertRaises(KeyboardInterrupt):
            worker.run_pair(self.cards, PROFILE, self.counts, self.stage)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assert_empty_private()

    def test_preset_operator_event_denies_preflight_and_render_before_files_or_spawn(self):
        event=threading.Event();event.set()
        for operation in (worker.preflight_pair,worker.run_pair):
            with self.subTest(operation=operation.__name__),patch.object(worker.subprocess,'Popen') as spawn,self.assertRaises(worker.LabelWorkerError) as caught:
                operation(self.cards,PROFILE,self.counts,self.stage,cancel_event=event)
            self.assertEqual(caught.exception.code,'ORIGINAL_STOCK_LABEL_WORKER_CANCELLED')
            spawn.assert_not_called();self.assert_empty_private()

    def test_active_operator_event_stops_exact_child_for_both_modes(self):
        real_spawn=subprocess.Popen
        for operation in (worker.preflight_pair,worker.run_pair):
            event=threading.Event();processes=[]
            def spawn(*args,**kwargs):
                process=real_spawn(*args,**kwargs);processes.append(process);return process
            timer=threading.Timer(.3,event.set);timer.start()
            try:
                with self.subTest(operation=operation.__name__),patch.object(worker,'_command',self.fixture('timeout')), \
                     patch.object(worker.subprocess,'Popen',spawn),self.assertRaises(worker.LabelWorkerError) as caught:
                    operation(self.cards,PROFILE,self.counts,self.stage,cancel_event=event)
                self.assertEqual(caught.exception.code,'ORIGINAL_STOCK_LABEL_WORKER_CANCELLED')
                self.assertEqual(len(processes),1);self.assertIsNotNone(processes[0].poll())
                self.assert_empty_private()
            finally:timer.cancel();timer.join(2)

    def test_magic_hash_copy_type_basis_type_input_hash_and_zpl_count_are_verified(self):
        for mode in ('bad-magic', 'bad-hash', 'copy-bool', 'basis-type', 'input-hash', 'zpl-count'):
            with self.subTest(mode=mode), patch.object(worker, '_command', self.fixture(mode)), self.assertRaises(worker.LabelWorkerError):
                worker.run_pair(self.cards, PROFILE, self.counts, self.stage)
            self.assert_empty_private()

    def test_cleanup_requires_owned_identity_and_never_recurses_into_unknown_content(self):
        with self.assertRaises(worker.LabelWorkerError):
            worker.cleanup_attempt({'attemptId': uuid.uuid4().hex, 'directory': self.stage})
        attempt = worker._new_attempt(self.stage)
        unknown = attempt['directory'] / 'unexpected'
        unknown.mkdir()
        sentinel = unknown / 'keep.txt'
        sentinel.write_text('retain unknown data')
        with self.assertRaises(worker.LabelWorkerError):
            worker.cleanup_attempt(attempt)
        self.assertEqual(sentinel.read_text(), 'retain unknown data')
        sentinel.unlink()
        unknown.rmdir()
        worker.cleanup_attempt(attempt)
        self.assert_empty_private()

    def test_cleanup_refuses_simulated_reparse_and_retains_existing_private_file(self):
        attempt = worker._new_attempt(self.stage)
        path = attempt['directory'] / 'input.json'
        path.write_text('{}')
        original = Path.lstat
        def reparse(item):
            info = original(item)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if item == path else info
        with patch.object(Path, 'lstat', reparse), self.assertRaises(worker.LabelWorkerError):
            worker.cleanup_attempt(attempt)
        self.assertEqual(path.read_text(), '{}')
        worker.cleanup_attempt(attempt)
        self.assert_empty_private()

    def test_source_frozen_cli_shape_and_minimal_environment_without_runtime_execution(self):
        directory = self.stage / uuid.uuid4().hex
        source = worker._command(directory, 'render')
        self.assertEqual(source[:4], [str(Path(sys.executable).resolve()), '-X', 'utf8', str(Path(worker.__file__).resolve())])
        with patch.object(worker.sys, 'frozen', True, create=True):
            frozen = worker._command(directory, 'preflight')
        self.assertEqual(frozen[:2], [str(Path(sys.executable).resolve()), '--stock-label-worker'])
        self.assertIn('--mode', frozen)
        import os
        with patch.dict(os.environ, {'DATABASE_URL': 'fictional-secret', 'HTTP_PROXY': 'fictional-proxy', 'PYTHONPATH': 'untrusted-path', 'STORE_TOKEN': 'fictional-token'}):
            env = worker._environment()
        self.assertFalse({'DATABASE_URL', 'HTTP_PROXY', 'PYTHONPATH', 'STORE_TOKEN'} & env.keys())
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
        self.assert_empty_private()

    def test_child_cli_rejects_mismatched_input_and_duplicate_json_without_initializing_app(self):
        attempt = worker._new_attempt(self.stage)
        path = attempt['directory'] / 'input.json'
        path.write_text('{"version":1,"version":1}')
        self.assertEqual(worker.main(['--mode', 'preflight', '--input', str(path), '--stage', str(attempt['directory'])]), 2)
        self.assertEqual({p.name for p in attempt['directory'].iterdir()}, {'input.json'})
        other = self.root / 'other.json'
        other.write_text('{}')
        self.assertEqual(worker.main(['--mode', 'preflight', '--input', str(other), '--stage', str(attempt['directory'])]), 2)
        self.assertEqual(other.read_text(), '{}')
        worker.cleanup_attempt(attempt)
        self.assert_empty_private()
