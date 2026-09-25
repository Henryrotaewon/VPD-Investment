import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from magi1.archive import Drive, oauth_failure


class OAuthFailureTests(unittest.TestCase):
    def test_known_errors_have_fixed_actions_without_response_details(self):
        for code, action in [('invalid_grant', 'reauthorize_same_client_and_account'),
                             ('invalid_client', 'check_client_id_and_secret')]:
            with self.subTest(code=code):
                response = MagicMock(status=400)
                response.json = AsyncMock(return_value={
                    'error': code, 'error_description': 'private-token',
                    'access_token': 'private-token'})
                error = asyncio.run(oauth_failure(response))
                self.assertEqual(str(error),
                                 f'Drive OAuth refresh HTTP 400 code={code} action={action}')
                self.assertNotIn('private-token', str(error))

    def test_untrusted_and_malformed_errors_do_not_leak(self):
        for payload in [{'error': 'private-token'}, {'error': ['private-token']},
                        ['private-token'], None]:
            with self.subTest(payload=payload):
                response = MagicMock(status=400)
                response.json = AsyncMock(return_value=payload)
                error = asyncio.run(oauth_failure(response))
                self.assertIn('code=unknown', str(error))
                self.assertNotIn('private-token', str(error))
        response.json = AsyncMock(side_effect=ValueError('private-token'))
        self.assertIn('code=unknown', str(asyncio.run(oauth_failure(response))))

    def test_failed_refresh_stops_before_drive_access(self):
        response = MagicMock(status=400)
        response.json = AsyncMock(return_value={'error': 'invalid_grant'})
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.post.return_value = context
        drive = Drive(session)
        credentials = {f'MAGI1_GOOGLE_{key}': 'private-value'
                       for key in ('CLIENT_ID', 'CLIENT_SECRET', 'REFRESH_TOKEN')}
        with patch.dict('os.environ', credentials):
            with self.assertRaisesRegex(RuntimeError, 'code=invalid_grant'):
                asyncio.run(drive.folder())
        session.post.assert_called_once()
        session.request.assert_not_called()
        self.assertIsNone(drive.token)
