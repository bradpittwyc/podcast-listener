import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import app
from aliyun import api_keys


class SettingsTests(unittest.TestCase):
    def test_save_keeps_other_settings_and_does_not_return_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('PORT=8557\nDASHSCOPE_API_KEY_1=old-key\n', encoding='utf-8')
            with patch.object(app, 'BASE_DIR', directory), patch.object(app, 'SETTINGS_PATH', str(path)), patch.dict(os.environ, {'DASHSCOPE_API_KEY_1': 'old-key', 'DASHSCOPE_API_KEY_2': ''}):
                client = TestClient(app.app)
                response = client.post('/api/settings', json={'aliyun_key_1': 'first-secret', 'aliyun_key_2': 'second-secret'})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('secret', response.text)
                self.assertEqual(api_keys(), ['first-secret', 'second-secret'])
                self.assertIn('PORT=8557', path.read_text())
                self.assertIn('second-secret', path.read_text())
                client.post('/api/settings', json={'aliyun_key_1': ''})
                self.assertEqual(api_keys()[0], 'first-secret')
                self.assertNotIn('secret', client.get('/api/settings').text)

    def test_reject_cross_origin_and_invalid_values(self):
        client = TestClient(app.app)
        self.assertEqual(client.post('/api/settings', json={}, headers={'Origin': 'https://example.com'}).status_code, 403)
        self.assertEqual(client.post('/api/settings', data='aliyun_key_1=test').status_code, 415)
        self.assertEqual(client.post('/api/settings', json={'aliyun_key_1': 'key\nPORT=9999'}).status_code, 400)
        self.assertEqual(client.post('/api/settings', json=[]).status_code, 400)
        self.assertEqual(client.post('/api/settings', json={'dictionary_provider': 'invalid'}).status_code, 400)

    def test_dictionary_choice_is_persisted_without_changing_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('DASHSCOPE_API_KEY_1=old-key\n', encoding='utf-8')
            with patch.object(app, 'BASE_DIR', directory), patch.object(app, 'SETTINGS_PATH', str(path)), patch.dict(os.environ, {}):
                client = TestClient(app.app)
                for provider in ('gemini', 'qwen', 'auto'):
                    response = client.post('/api/settings', json={'dictionary_provider': provider})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(client.get('/api/settings').json()['options']['dictionary_provider'], provider)
                    self.assertIn('DICTIONARY_PROVIDER', path.read_text())
                    self.assertIn('old-key', path.read_text())

    def test_qwen_lookup_does_not_use_gemini(self):
        from unittest.mock import MagicMock
        session = MagicMock()
        session.__enter__.return_value = session
        response = session.post.return_value
        response.ok = True
        response.json.return_value = {'choices': [{'message': {'content': '{"word":"steady","translation_cn":"稳定的"}'}}]}
        with patch.dict(os.environ, {'DICTIONARY_PROVIDER': 'qwen', 'ALIYUN_REGION': 'beijing'}), patch.object(app.aliyun, 'api_keys', return_value=['private-key']), patch.object(app.requests, 'Session', return_value=session), patch.object(app, 'gemini_client', None):
            result = TestClient(app.app).get('/api/define?word=steady&context=The+market+is+steady.').json()
            self.assertEqual(result['data']['word'], 'steady')
            self.assertFalse(session.trust_env)
            request = session.post.call_args
            self.assertIn('dashscope.aliyuncs.com', request.args[0])
            self.assertEqual(request.kwargs['json']['model'], 'qwen-flash')
            self.assertIn('The market is steady.', request.kwargs['json']['messages'][0]['content'])

    def test_gemini_lookup_does_not_use_qwen(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.models.generate_content.return_value.text = '{"word":"steady"}'
        with patch.dict(os.environ, {'DICTIONARY_PROVIDER': 'gemini'}), patch.object(app, 'dictionary_proxy', return_value=None), patch.object(app, 'gemini_client', client), patch.object(app.requests, 'Session') as qwen:
            result = TestClient(app.app).get('/api/define?word=steady').json()
            self.assertEqual(result['data']['word'], 'steady')
            client.models.generate_content.assert_called_once()
            qwen.assert_not_called()

    def test_auto_routes_change_with_proxy_on_each_lookup(self):
        from unittest.mock import MagicMock
        session = MagicMock()
        session.__enter__.return_value = session
        response = session.post.return_value
        response.ok = True
        client = TestClient(app.app)
        with patch.dict(os.environ, {'DICTIONARY_PROVIDER': 'auto'}), patch.object(app, 'gemini_client', MagicMock()), patch.object(app.aliyun, 'api_keys', return_value=['private-key']), patch.object(app.requests, 'Session', return_value=session), patch.object(app, 'dictionary_proxy') as proxy:
            for active in (False, True, False):
                proxy.return_value = 'http://127.0.0.1:1234' if active else None
                response.json.return_value = ({'candidates': [{'content': {'parts': [{'text': '{"word":"steady"}'}]}}]}
                    if active else {'choices': [{'message': {'content': '{"word":"steady"}'}}]})
                result = client.get('/api/define?word=steady').json()
                self.assertEqual(result['provider'], 'gemini' if active else 'qwen')
                self.assertIn('googleapis.com' if active else 'aliyuncs.com', session.post.call_args.args[0])
                if active:self.assertEqual(session.post.call_args.kwargs['proxies']['https'], proxy.return_value)

    def test_proxy_detection_uses_configuration_not_open_port_guessing(self):
        with patch.object(app, 'get_local_proxy', return_value=None), patch.object(app.urllib.request, 'getproxies', return_value={}):
            self.assertIsNone(app.dictionary_proxy())
        with patch.object(app, 'get_local_proxy', return_value=None), patch.object(app.urllib.request, 'getproxies', return_value={'https': 'http://system-proxy:8080'}):
            self.assertEqual(app.dictionary_proxy(), 'http://system-proxy:8080')


if __name__ == '__main__':
    unittest.main()
