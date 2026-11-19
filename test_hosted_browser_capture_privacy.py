"""Capture-call contract only; no browser, pixels or Playwright cleanup acceptance."""
import unittest
from types import SimpleNamespace

from tools.hosted_browser_ci import CaseContext


class _CapturePage:
    url = 'https://fixture.invalid/'
    viewport_size = {'width': 390, 'height': 844}

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def locator(self, selector):
        return selector

    def screenshot(self, **options):
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

    def test_exact_sensitive_subtrees_hidden_only_during_existing_masked_capture(self):
        context, page = self._context(), _CapturePage()
        context.screenshot(page, 'hui04-mobile390.png',
                           mask_selectors=('#staffList', '#storeName'))
        self.assertEqual(len(page.calls), 1)
        options = page.calls[0]
        self.assertEqual(set(options), {'type', 'full_page', 'animations', 'mask',
                                        'mask_color', 'timeout', 'style'})
        expected = ('input', 'textarea', '#mfaRecoveryCodes', '#mfaSetupKey',
                    '#staffInvitationResult', '#staffList', '#storeName')
        self.assertEqual(options['mask'], list(expected + ('#staffList', '#storeName')))
        selectors, separator, declaration = options['style'].partition('{')
        self.assertEqual(separator, '{')
        self.assertEqual(declaration, 'visibility:hidden!important}')
        self.assertEqual(set(selectors.split(',')),
                         set(expected) | {selector + ' *' for selector in expected})
        self.assertEqual(options['type'], 'png')
        self.assertIs(options['full_page'], True)
        self.assertEqual(options['animations'], 'disabled')
        self.assertEqual(options['mask_color'], '#27272A')
        self.assertEqual(options['timeout'], 15000)
        self.assertEqual(len(context.screenshots), 1)
        self.assertEqual(context.screenshots[0]['name'], 'hui04-mobile390.png')
        self.assertEqual(context.screenshot_bytes, context.screenshots[0]['bytes'])

    def test_unknown_or_css_injecting_extra_selector_refused_before_capture(self):
        for selector in ('body', '*', '#accountSecurity', '#storeName,button',
                         'input{display:none}', None):
            with self.subTest(selector=selector):
                context, page = self._context(), _CapturePage()
                with self.assertRaisesRegex(AssertionError, '^SCREENSHOT_MASK_SELECTOR$'):
                    context.screenshot(page, 'hui04-mobile390.png',
                                       mask_selectors=(selector,))
                self.assertEqual(page.calls, [])
                self.assertEqual(context.screenshots, [])
                self.assertEqual(context.screenshot_bytes, 0)

    def test_capture_error_propagates_without_retry_or_publishing_image(self):
        context, page = self._context(), _CapturePage(fail=True)
        with self.assertRaises(RuntimeError):
            context.screenshot(page, 'hui04-mobile390.png')
        self.assertEqual(len(page.calls), 1)
        self.assertIn('style', page.calls[0])
        self.assertEqual(context.screenshots, [])
        self.assertEqual(context.screenshot_bytes, 0)


if __name__ == '__main__':
    unittest.main()
