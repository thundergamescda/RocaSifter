"""RFC vectors, seed binding, recovery entropy and hosted MFA HTTP boundaries."""
import base64
import unittest
import uuid
from unittest.mock import Mock
from cryptography.fernet import Fernet
from hosted.app import create_app
from hosted.mfa import (SeedCipher, hotp, totp, matching_step, recovery_hash,
                        recovery_normalized, new_recovery_codes, secret_bytes)
from hosted.security import ServiceError


class OTPTests(unittest.TestCase):
    def test_rfc6238_appendix_b_all_algorithms(self):
        vectors = (
            (59, '94287082', '46119246', '90693936'),
            (1111111109, '07081804', '68084774', '25091201'),
            (1111111111, '14050471', '67062674', '99943326'),
            (1234567890, '89005924', '91819424', '93441116'),
            (2000000000, '69279037', '90698825', '38618901'),
            (20000000000, '65353130', '77737706', '47863826'))
        keys = (b'12345678901234567890', b'12345678901234567890123456789012',
                b'1234567890123456789012345678901234567890123456789012345678901234')
        for timestamp, *expected in vectors:
            for key, algorithm, code in zip(keys, ('sha1', 'sha256', 'sha512'), expected):
                with self.subTest(timestamp=timestamp, algorithm=algorithm):
                    self.assertEqual(totp(key, timestamp, digits=8, algorithm=algorithm), code)

    def test_rfc4226_counter_vectors(self):
        expected = ('755224', '287082', '359152', '969429', '338314',
                    '254676', '287922', '162583', '399871', '520489')
        for counter, code in enumerate(expected):
            self.assertEqual(hotp(b'12345678901234567890', counter), code)

    def test_six_digit_compatibility_and_leading_zero(self):
        self.assertEqual(totp(b'12345678901234567890', 1111111109), '081804')

    def test_validation_window_and_global_step_replay(self):
        seed = base64.b32encode(b'12345678901234567890').decode()
        for step in (99, 100, 101):
            code = hotp(secret_bytes(seed), step)
            self.assertEqual(matching_step(seed, code, 3000), step)
            self.assertIsNone(matching_step(seed, code, 3000, step))
        self.assertIsNone(matching_step(seed, hotp(secret_bytes(seed), 98), 3000))
        self.assertIsNone(matching_step(seed, hotp(secret_bytes(seed), 102), 3000))

    def test_ascii_codes_only(self):
        seed = base64.b32encode(b'12345678901234567890').decode()
        for code in (None, 123456, '１２３４５６', '12345', '1234567', ' 123456', '123456\n'):
            with self.subTest(code=code):
                self.assertIsNone(matching_step(seed, code, 3000))

    def test_invalid_counter_secret_and_parameters_fail_closed(self):
        for counter in (-1, True, 2**64, '1'):
            with self.assertRaises(ValueError): hotp(b'x' * 20, counter)
        for secret in (b'x', 'x' * 20, None):
            with self.assertRaises(ValueError): hotp(secret, 1)
        for algorithm in ('md5', 'SHA1', None):
            with self.assertRaises(ValueError): hotp(b'x' * 20, 1, algorithm=algorithm)
        for timestamp in (-1, True, float('nan'), float('inf'), '1'):
            with self.assertRaises(ValueError): totp(b'x' * 20, timestamp)


class SeedAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key().decode()
        self.seed = base64.b32encode(b'12345678901234567890').decode()
        self.tenant, self.staff = uuid.uuid4(), uuid.uuid4()
        self.cipher = SeedCipher(self.key)

    def test_bound_encryption_round_trip_hides_plain_seed(self):
        encrypted = self.cipher.encrypt(self.seed, self.tenant, self.staff, 'active', 1)
        self.assertNotIn(self.seed, encrypted)
        self.assertEqual(self.cipher.decrypt(encrypted, self.tenant, self.staff, 'active', 1), self.seed)

    def test_ciphertext_cannot_move_between_tenants_staff_purposes_or_versions(self):
        encrypted = self.cipher.encrypt(self.seed, self.tenant, self.staff, 'active', 1)
        for terms in ((uuid.uuid4(), self.staff, 'active', 1), (self.tenant, uuid.uuid4(), 'active', 1),
                      (self.tenant, self.staff, 'pending', 1), (self.tenant, self.staff, 'active', 2)):
            with self.assertRaises(ServiceError): self.cipher.decrypt(encrypted, *terms)

    def test_corruption_and_missing_key_fail_closed(self):
        encrypted = self.cipher.encrypt(self.seed, self.tenant, self.staff, 'active', 1)
        for cipher, value in ((self.cipher, encrypted[:-5] + 'xxxxx'),
                              (SeedCipher(Fernet.generate_key().decode()), encrypted),
                              (SeedCipher(''), encrypted)):
            with self.assertRaises(ServiceError) as caught:
                cipher.decrypt(value, self.tenant, self.staff, 'active', 1)
            self.assertEqual(caught.exception.status, 503)

    def test_key_rotation_accepts_old_key_and_encrypts_with_new(self):
        old = self.cipher.encrypt(self.seed, self.tenant, self.staff, 'active', 1)
        new_key = Fernet.generate_key().decode()
        rotated = SeedCipher(new_key + ',' + self.key)
        self.assertEqual(rotated.decrypt(old, self.tenant, self.staff, 'active', 1), self.seed)
        new = rotated.encrypt(self.seed, self.tenant, self.staff, 'active', 1)
        self.assertEqual(SeedCipher(new_key).decrypt(new, self.tenant, self.staff, 'active', 1), self.seed)
        with self.assertRaises(ServiceError): self.cipher.decrypt(new, self.tenant, self.staff, 'active', 1)

    def test_short_server_secrets_are_rejected(self):
        with self.assertRaises(ServiceError): SeedCipher('short-key')

    def test_recovery_codes_have_128bit_shape_and_distinct_bound_hashes(self):
        codes = new_recovery_codes()
        self.assertEqual(len(set(codes)), 10)
        for code in codes:
            self.assertRegex(code, r'^[a-f0-9]{4}(?:-[a-f0-9]{4}){7}$')
            hashed = recovery_hash(self.tenant, self.staff, code)
            self.assertEqual(len(hashed), 64)
            self.assertNotEqual(hashed, recovery_hash(uuid.uuid4(), self.staff, code))
            self.assertNotEqual(hashed, recovery_hash(self.tenant, uuid.uuid4(), code))
            self.assertEqual(hashed, recovery_hash(self.tenant, self.staff, ' ' + code.upper() + ' '))
        for code in ('123456', 'g' * 32, 'a' * 31, 'a' * 65, None, 123):
            self.assertIsNone(recovery_normalized(code))


class MFAHTTPTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'PUBLIC_BASE_URL': 'https://fixture.invalid',
                               'APP_ENCRYPTION_KEYS': Fernet.generate_key().decode()}, database=Mock())
        self.client = self.app.test_client()
        self.auth = self.app.extensions['ss_auth']
        self.actor = {'tenant_id': uuid.uuid4(), 'staff_id': uuid.uuid4(), 'role': 'owner', 'slug': 'fixture',
                      'mfa_enabled': True, 'mfa_authenticated': True}
        self.auth.resolve = Mock(return_value=self.actor)

    def post(self, path, **kwargs):
        return self.client.post(path, base_url='https://fixture.invalid', **kwargs)

    def test_password_only_challenge_has_no_access_or_csrf_and_clears_prior_cookie(self):
        self.client.set_cookie('ss_session', 'old-fixture', domain='fixture.invalid')
        self.auth.login = Mock(return_value=(None, {'mfa_required': True, 'challenge_token': 'x' * 43, 'expires_in': 300}))
        response = self.post('/api/auth/login', json={'mfa_supported': True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['mfa_required'])
        self.assertNotIn('access_token', response.json)
        self.assertNotIn('csrf_token', response.json)
        self.assertIsNone(self.client.get_cookie('ss_session', domain='fixture.invalid'))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_verified_challenge_issues_http_only_cookie_and_bearer(self):
        self.auth.mfa.verify_challenge = Mock(return_value=('verified-fixture-token', {'store': 'fixture', 'role': 'owner'}))
        response = self.post('/api/auth/mfa/challenge', json={'challenge_token': 'x' * 43, 'code': '123456'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['access_token'], 'verified-fixture-token')
        self.assertIn('csrf_token', response.json)
        self.assertTrue(self.client.get_cookie('ss_session', domain='fixture.invalid').http_only)

    def test_upgrade_required_never_sets_cookie(self):
        self.auth.login = Mock(side_effect=ServiceError('Update app for two-step sign-in.', 426))
        response = self.post('/api/auth/login', json={})
        self.assertEqual(response.status_code, 426)
        self.assertIsNone(self.client.get_cookie('ss_session', domain='fixture.invalid'))

    def test_management_writes_require_csrf_for_cookie_sessions(self):
        self.client.set_cookie('ss_session', 'fixture-token', domain='fixture.invalid')
        for route in ('enroll', 'confirm', 'recovery-codes', 'disable'):
            with self.subTest(route=route):
                response = self.post('/api/auth/mfa/' + route, json={})
                self.assertEqual(response.status_code, 403)

    def test_cross_origin_challenge_and_cancel_are_rejected(self):
        for route in ('challenge', 'cancel'):
            response = self.post('/api/auth/mfa/' + route, json={}, headers={'Origin': 'https://other.invalid'})
            self.assertEqual(response.status_code, 403)

    def test_mfa_body_cannot_supply_tenant_scope(self):
        response = self.post('/api/auth/mfa/challenge', json={'tenant_id': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 400)

    def test_bearer_management_delegates_current_actor_without_cookie_csrf(self):
        self.auth.mfa.begin = Mock(return_value={'enrollment_token': 'x' * 43, 'secret': 'fixture-only'})
        response = self.post('/api/auth/mfa/enroll', json={'password': 'fixture-password'},
                                    headers={'Authorization': 'Bearer fixture-token'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.auth.mfa.begin.call_args.args[0], self.actor)

    def test_failed_verification_never_sets_cookie(self):
        self.auth.mfa.verify_challenge = Mock(side_effect=ServiceError('Invalid verification.', 401))
        response = self.post('/api/auth/mfa/challenge', json={'challenge_token': 'x' * 43, 'code': '123456'})
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.client.get_cookie('ss_session', domain='fixture.invalid'))


if __name__ == '__main__':
    unittest.main()
