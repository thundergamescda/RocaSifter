"""Capture orchestration only; fake handles do not prove DOM/CSP/pixel behavior."""
import unittest
from types import SimpleNamespace

from tools.hosted_browser_ci import CaseContext


class _PrivacyHandle:
    def __init__(self, page):
        self.page = page

    def evaluate(self, expression):
        self.page.events.append('restore')
        if self.page.restore_raises:
            raise RuntimeError('FIXTURE_RESTORE_FAILED')
        return self.page.restore_ok

    def dispose(self):
        self.page.events.append('dispose')
        if self.page.dispose_raises:
            raise RuntimeError('FIXTURE_DISPOSE_FAILED')


class _CapturePage:
    url = 'https://fixture.invalid/'
    viewport_size = {'width': 390, 'height': 844}

    def __init__(self, *, fail=False, prepare_raises=False, hidden=True,
                 restore_ok=True, restore_raises=False, current=True, hidden_after=True,
                 current_raises=False, dispose_raises=False):
        self.calls, self.events, self.prepared_selectors = [], [], None
        self.fail, self.prepare_raises, self.hidden = fail, prepare_raises, hidden
        self.restore_ok, self.restore_raises = restore_ok, restore_raises
        self.current, self.current_raises = current, current_raises
        self.hidden_after = hidden_after
        self.dispose_raises = dispose_raises
        self.handle = _PrivacyHandle(self)
        self.evaluations = 0

    def locator(self, selector):
        return selector

    def evaluate_handle(self, expression, selectors):
        self.events.append('prepare')
        self.prepared_selectors = selectors
        if self.prepare_raises:
            raise RuntimeError('FIXTURE_PREPARE_BOUNDS_OR_CONTEXT')
        return self.handle

    def evaluate(self, expression, handle):
        if handle is not self.handle:
            raise RuntimeError('FIXTURE_HANDLE_CHANGED')
        self.evaluations += 1
        if expression == 'state=>state.check(true)':
            self.events.append('hidden_check' if self.evaluations == 1 else 'captured_hidden_check')
            return self.hidden if self.evaluations == 1 else self.hidden_after
        self.events.append('current_restore_check')
        if self.current_raises:
            raise RuntimeError('FIXTURE_DOCUMENT_CHANGED')
        return self.current

    def screenshot(self, **options):
        self.events.append('capture')
        self.calls.append(options)
        if self.fail:
            raise RuntimeError('FIXTURE_CAPTURE_FAILED')
        return b'\x89PNG\r\n\x1a\nfictional-capture'


class HostedBrowserCapturePrivacyTests(unittest.TestCase):
    def _context(self):
        fixture = SimpleNamespace(origin='https://fixture.invalid', accounts={})
        context = CaseContext(fixture, None)
        context.case_id = 'HUI04'
        return context

    def _unpublished(self, context):
        self.assertEqual(context.screenshots, [])
        self.assertEqual(context.screenshot_bytes, 0)

    def test_exact_sensitive_subtrees_hidden_only_during_existing_masked_capture(self):
        context, page = self._context(), _CapturePage()
        context.screenshot(page, 'hui04-mobile390.png',
                           mask_selectors=('#staffList', '#storeName'))
        self.assertEqual(page.events, ['prepare', 'hidden_check', 'capture', 'captured_hidden_check',
                                       'restore', 'current_restore_check', 'dispose'])
        expected = ('input', 'textarea', '#mfaRecoveryCodes', '#mfaSetupKey',
                    '#staffInvitationResult', '#staffList', '#storeName')
        self.assertEqual(page.prepared_selectors, list(expected))
        self.assertEqual(len(page.calls), 1)
        options = page.calls[0]
        self.assertEqual(set(options), {'type', 'full_page', 'animations', 'mask',
                                        'mask_color', 'timeout'})
        self.assertEqual(options['mask'], list(expected + ('#staffList', '#storeName')))
        self.assertEqual(options['type'], 'png')
        self.assertIs(options['full_page'], True)
        self.assertEqual(options['animations'], 'disabled')
        self.assertEqual(options['mask_color'], '#27272A')
        self.assertEqual(options['timeout'], 15000)
        self.assertEqual(len(context.screenshots), 1)
        self.assertEqual(context.screenshots[0]['name'], 'hui04-mobile390.png')
        self.assertEqual(context.screenshot_bytes, context.screenshots[0]['bytes'])

    def test_unknown_or_css_injecting_extra_selector_refused_before_capture(self):
        invalid = [('body',), ('*',), ('#accountSecurity',), ('#storeName,button',),
                   ('input{display:none}',), (None,), ('#storeName',) * 8, 'input']
        for selectors in invalid:
            with self.subTest(selectors=selectors):
                context, page = self._context(), _CapturePage()
                with self.assertRaisesRegex(AssertionError, '^SCREENSHOT_MASK_SELECTOR$'):
                    context.screenshot(page, 'hui04-mobile390.png', mask_selectors=selectors)
                self.assertEqual(page.events, [])
                self.assertEqual(page.calls, [])
                self._unpublished(context)

    def test_capture_error_propagates_without_retry_or_publishing_image(self):
        context, page = self._context(), _CapturePage(fail=True)
        with self.assertRaises(RuntimeError):
            context.screenshot(page, 'hui04-mobile390.png')
        self.assertEqual(page.events, ['prepare', 'hidden_check', 'capture', 'restore',
                                       'current_restore_check', 'dispose'])
        self.assertEqual(len(page.calls), 1)
        self.assertNotIn('style', page.calls[0])
        self._unpublished(context)

    def test_prepare_bounds_or_context_failure_is_fixed_and_never_captures(self):
        context, page = self._context(), _CapturePage(prepare_raises=True)
        with self.assertRaisesRegex(AssertionError, '^SCREENSHOT_PRIVACY_PREPARE$'):
            context.screenshot(page, 'hui04-mobile390.png')
        self.assertEqual(page.events, ['prepare'])
        self.assertEqual(page.calls, [])
        self._unpublished(context)

    def test_failed_hidden_check_restores_before_refusing_capture(self):
        for options, captures in (({'hidden': False}, 0), ({'hidden_after': False}, 1)):
            with self.subTest(phase=next(iter(options))):
                context, page = self._context(), _CapturePage(**options)
                with self.assertRaisesRegex(AssertionError, '^SCREENSHOT_PRIVACY_HIDDEN$'):
                    context.screenshot(page, 'hui04-mobile390.png')
                self.assertIn('restore', page.events)
                self.assertEqual(page.events[-1], 'dispose')
                self.assertEqual(len(page.calls), captures)
                self._unpublished(context)

    def test_restore_document_or_dispose_failure_discards_captured_image(self):
        for options in ({'restore_ok': False}, {'restore_raises': True},
                        {'current': False}, {'current_raises': True}, {'dispose_raises': True}):
            with self.subTest(failure=next(iter(options))):
                context, page = self._context(), _CapturePage(**options)
                with self.assertRaisesRegex(AssertionError, '^SCREENSHOT_PRIVACY_RESTORE$'):
                    context.screenshot(page, 'hui04-mobile390.png')
                self.assertEqual(len(page.calls), 1)
                self.assertEqual(page.events[-1], 'dispose')
                self._unpublished(context)


if __name__ == '__main__':
    unittest.main()
