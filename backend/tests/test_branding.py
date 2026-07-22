import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openlaunch.routers.auths import AdminConfig, update_admin_config
from openlaunch.utils.branding import (
    APP_NAME_MAX_LENGTH,
    get_logo_fallback_filename,
    normalize_app_name,
)
from pydantic import ValidationError


def admin_config(**overrides) -> AdminConfig:
    values = {
        'SHOW_ADMIN_DETAILS': False,
        'ADMIN_EMAIL': None,
        'APP_NAME': 'OpenLaunch',
        'OPENLAUNCH_URL': 'http://localhost:3000',
        'ENABLE_SIGNUP': True,
        'ENABLE_API_KEYS': True,
        'ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS': False,
        'API_KEYS_ALLOWED_ENDPOINTS': '',
        'DEFAULT_USER_ROLE': 'pending',
        'DEFAULT_GROUP_ID': '',
        'JWT_EXPIRES_IN': '-1',
        'ENABLE_COMMUNITY_SHARING': True,
        'ENABLE_MESSAGE_RATING': True,
        'ENABLE_FOLDERS': True,
        'FOLDER_MAX_FILE_COUNT': None,
        'AUTOMATION_MAX_COUNT': None,
        'AUTOMATION_MIN_INTERVAL': None,
        'ENABLE_AUTOMATIONS': True,
        'ENABLE_CHANNELS': True,
        'ENABLE_CALENDAR': True,
        'ENABLE_MEMORIES': True,
        'ENABLE_MEMORY_SYSTEM_CONTEXT': True,
        'ENABLE_NOTES': True,
        'ENABLE_USER_WEBHOOKS': True,
        'ENABLE_USER_STATUS': True,
        'PENDING_USER_OVERLAY_TITLE': None,
        'PENDING_USER_OVERLAY_CONTENT': None,
        'RESPONSE_WATERMARK': None,
    }
    values.update(overrides)
    return AdminConfig.model_validate(values)


class AppNameValidationTests(unittest.TestCase):
    def test_normalizes_surrounding_whitespace(self):
        self.assertEqual(normalize_app_name('  Acme AI  '), 'Acme AI')
        self.assertEqual(admin_config(APP_NAME='  Acme AI  ').APP_NAME, 'Acme AI')

    def test_rejects_empty_control_character_and_overlong_names(self):
        for name in ('   ', 'Acme\nAI', 'x' * (APP_NAME_MAX_LENGTH + 1)):
            with self.subTest(name=repr(name)), self.assertRaises((ValueError, ValidationError)):
                admin_config(APP_NAME=name)


class LogoFallbackTests(unittest.TestCase):
    def test_known_variants_preserve_the_bundled_theme_defaults(self):
        self.assertEqual(get_logo_fallback_filename('splash'), 'splash.png')
        self.assertEqual(get_logo_fallback_filename('splash-dark'), 'splash-dark.png')
        self.assertEqual(get_logo_fallback_filename('apple-touch'), 'apple-touch-icon.png')

    def test_unknown_variants_cannot_select_arbitrary_files(self):
        self.assertEqual(get_logo_fallback_filename('../../config.py'), 'favicon.png')
        self.assertEqual(get_logo_fallback_filename(None), 'favicon.png')


class AppNameUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_update_persists_and_applies_name_immediately(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(OPENLAUNCH_NAME='OpenLaunch')))
        form = admin_config(APP_NAME='Acme AI')

        with (
            patch('openlaunch.routers.auths.Config.upsert', new_callable=AsyncMock) as upsert,
            patch(
                'openlaunch.routers.auths.get_config_values',
                new_callable=AsyncMock,
                return_value={'APP_NAME': 'Acme AI'},
            ),
        ):
            response = await update_admin_config(request, form, user=object())

        self.assertEqual(request.app.state.OPENLAUNCH_NAME, 'Acme AI')
        self.assertEqual(response['APP_NAME'], 'Acme AI')
        self.assertEqual(upsert.await_args.args[0]['ui.name'], 'Acme AI')


if __name__ == '__main__':
    unittest.main()
