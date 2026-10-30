"""Square connection status must match the configured API environment."""
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import MagicMock, Mock
import uuid
from hosted.square import Square


class SquareStatusTests(unittest.TestCase):
    def setUp(self):
        self.db=MagicMock();self.cursor=self.db.transaction.return_value.__enter__.return_value
        self.actor={'tenant_id':uuid.uuid4()}
        self.row={'merchant_id':'fixture-merchant','environment':'sandbox','revoked_at':None,
                  'expires_at':datetime.now(timezone.utc)+timedelta(days=20)}

    def status(self,environment='sandbox',row=None):
        self.cursor.execute.return_value.fetchone.return_value=row
        square=Square(self.db,{'PUBLIC_BASE_URL':'https://fixture.invalid','SQUARE_ENVIRONMENT':environment,
                              'APP_ENCRYPTION_KEYS':'fictional-encryption-secret-for-tests-1234',
                              'SQUARE_APPLICATION_ID':'fixture-app','SQUARE_APPLICATION_SECRET':'fixture-secret'},transport=Mock())
        return square.status(self.actor)

    def test_only_matching_nonrevoked_environment_reports_connected(self):
        for environment in ('sandbox','production'):
            with self.subTest(environment=environment):
                result=self.status(environment,self.row|{'environment':environment})
                self.assertTrue(result['connected']);self.assertEqual(result['connection_status'],'connected')
                self.assertEqual(result['environment'],environment);self.assertEqual(result['saved_environment'],environment)
                self.assertEqual(result['merchant_id'],'fixture-merchant');self.assertFalse(result['write_checkout_enabled'])
        self.assertEqual(self.cursor.execute.call_args.args[1],(self.actor['tenant_id'],))

    def test_other_environment_never_reports_a_ready_connection_or_merchant(self):
        for configured,saved in (('sandbox','production'),('production','sandbox')):
            with self.subTest(configured=configured,saved=saved):
                result=self.status(configured,self.row|{'environment':saved})
                self.assertFalse(result['connected']);self.assertEqual(result['connection_status'],'environment_mismatch')
                self.assertEqual(result['environment'],configured);self.assertEqual(result['saved_environment'],saved)
                self.assertIsNone(result['merchant_id']);self.assertIn('different environment',result['message'])
                self.assertFalse(result['write_checkout_enabled'])

    def test_revoked_connection_is_not_connected(self):
        result=self.status(row=self.row|{'revoked_at':datetime.now(timezone.utc)})
        self.assertFalse(result['connected']);self.assertIsNone(result['merchant_id'])
        self.assertEqual(result['connection_status'],'revoked');self.assertEqual(result['saved_environment'],'sandbox')

    def test_absent_connection_is_explicit_and_does_not_expose_credential_values(self):
        result=self.status()
        self.assertFalse(result['connected']);self.assertEqual(result['connection_status'],'not_connected')
        self.assertIsNone(result['saved_environment']);self.assertIsNone(result['merchant_id'])
        self.assertNotIn('fixture-secret',str(result));self.assertNotIn('encrypted_tokens',result)

    def test_saved_connection_never_reports_requested_scopes_as_granted(self):
        result=self.status(row=self.row)
        self.assertTrue(result['connected'])
        self.assertFalse(result['grants_verified'])
        self.assertEqual(result['scopes'],[])
        self.assertEqual(result['requested_scopes'],list(Square.SCOPES))


if __name__=='__main__':unittest.main()
