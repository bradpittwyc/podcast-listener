import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import app
from transcription import groq_api_keys


class SettingsTests(unittest.TestCase):
    def test_save_keeps_other_settings_and_does_not_return_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('PORT=8557\nGROQ_API_KEY_1=old-key\n', encoding='utf-8')
            with patch.object(app, 'BASE_DIR', directory), patch.object(app, 'SETTINGS_PATH', str(path)), patch.dict(os.environ, {'GROQ_API_KEY_1': 'old-key', 'GROQ_API_KEY_2': ''}):
                client = TestClient(app.app)
                response = client.post('/api/settings', json={'groq_key_1': 'first-secret', 'groq_key_2': 'second-secret'})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('secret', response.text)
                self.assertEqual(groq_api_keys(), ['first-secret', 'second-secret'])
                self.assertIn('PORT=8557', path.read_text())
                self.assertIn('second-secret', path.read_text())
                client.post('/api/settings', json={'groq_key_1': ''})
                self.assertEqual(groq_api_keys()[0], 'first-secret')
                self.assertNotIn('secret', client.get('/api/settings').text)

    def test_reject_cross_origin_and_invalid_values(self):
        client = TestClient(app.app)
        self.assertEqual(client.post('/api/settings', json={}, headers={'Origin': 'https://example.com'}).status_code, 403)
        self.assertEqual(client.post('/api/settings', data='groq_key_1=test').status_code, 415)
        self.assertEqual(client.post('/api/settings', json={'groq_key_1': 'key\nPORT=9999'}).status_code, 400)
        self.assertEqual(client.post('/api/settings', json=[]).status_code, 400)


if __name__ == '__main__':
    unittest.main()
