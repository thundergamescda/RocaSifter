"""DRAFT UNRUN: real Flask startup/health with no configured database.

No database, imported dependency or service is mocked. This case must remain
independent of PostgreSQL fixtures so missing DATABASE_URL cannot skip it.
"""
import unittest

from hosted.app import create_app


class UnconfiguredStaffRecoveryStartup(unittest.TestCase):
    def test_unconfigured_app_keeps_public_pages_health_and_explicit_not_ready_apis(self):
        origin = 'https://unconfigured-recovery.fixture.invalid'
        app = create_app({'TESTING': True, 'PUBLIC_BASE_URL': origin,
            'DATABASE_URL': '', 'APP_ENCRYPTION_KEYS': '', 'BOOTSTRAP_TOKEN': ''})
        self.assertIsNone(app.extensions['ss_database'])
        self.assertIsNone(app.extensions['ss_auth'])
        client = app.test_client()
        for path in ('/', '/staff-password-recovery', '/healthz'):
            response = client.get(path, base_url=origin)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(client.get('/healthz', base_url=origin).get_json()['status'], 'running')
        response = client.get('/readyz', base_url=origin)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {'database_ready': False, 'service_ready': False,
                                             'mode': 'pilot', 'commercial_release': False})
        for path in ('/api/staff/00000000-0000-0000-0000-000000000001/password-recovery',
                     '/api/staff/password-recovery/redeem'):
            response = client.post(path, base_url=origin, headers={'Origin': origin}, json={})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json(), {'error': 'Database configuration is required.'})
            self.assertNotIn('Set-Cookie', response.headers)
