import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi.testclient import TestClient
import app
import subtitle_translation as translation


class TranslationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"TRANSLATION_PROVIDER": "gemini"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_qwen_translation_uses_selected_provider_without_gemini(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value.json.return_value = {"choices": [{"message": {"content": '{"translations":[{"id":0,"translation":"你好"}]}'}}]}
        with patch.dict(os.environ, {"TRANSLATION_PROVIDER": "qwen", "ALIYUN_REGION": "beijing"}), patch.object(app.aliyun, "api_keys", return_value=["private-key"]), patch.object(app.requests, "Session", return_value=session), patch.object(app, "gemini_client", None):
            result = TestClient(app.app).post("/api/translate_subtitles", json={"sentences": [{"id": 0, "text": "Hello"}]}).json()
        self.assertEqual(result["provider"], "qwen")
        self.assertEqual(result["translations"][0]["translation"], "你好")
        self.assertFalse(session.trust_env)
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "qwen-flash")
        self.assertFalse(payload["enable_thinking"])
        self.assertIn("Hello", payload["messages"][0]["content"])

    def test_translation_preserves_ids_and_source_without_truncation(self):
        sentences = [{"id": 4, "text": "It's raining cats and dogs."}, {"id": 9, "text": "Then we stayed inside."}]
        client = Mock()
        client.aio.models.generate_content = AsyncMock(return_value=Mock(text=json.dumps({"translations": [
            {"id": 9, "translation": "于是我们待在室内。"}, {"id": 4, "translation": "雨下得很大。"}]})))
        with patch.object(app, "gemini_client", client):
            response = TestClient(app.app).post("/api/translate_subtitles", json={"sentences": sentences})
        self.assertEqual(response.json()["translations"], [{"id": 4, "translation": "雨下得很大。"}, {"id": 9, "translation": "于是我们待在室内。"}])
        prompt = client.aio.models.generate_content.call_args.kwargs["contents"]
        for sentence in sentences:
            self.assertIn(sentence["text"], prompt)
        self.assertEqual(client.aio.models.generate_content.call_args.kwargs["config"], {"response_mime_type": "application/json"})

    def test_invalid_batches_never_call_the_model(self):
        client = Mock()
        client.aio.models.generate_content = AsyncMock()
        for items in [[], [{"id": 0, "text": " "}], [{"id": True, "text": "Text"}],
                      [{"id": 0, "text": "One"}, {"id": 0, "text": "Two"}],
                      [{"id": i, "text": "Text"} for i in range(21)]]:
            with patch.object(app, "gemini_client", client):
                self.assertEqual(TestClient(app.app).post("/api/translate_subtitles", json={"sentences": items}).status_code, 400)
        client.aio.models.generate_content.assert_not_called()

    def test_missing_configuration_is_a_recoverable_error(self):
        with patch.object(app, "gemini_client", None):
            result = TestClient(app.app).post("/api/translate_subtitles", json={"sentences": [{"id": 0, "text": "Hello"}]}).json()
        self.assertEqual(result["status"], "error")
        self.assertIn("设置", result["message"])

    def test_bad_model_results_cannot_misalign_subtitles(self):
        sentences = [{"id": 0, "text": "One"}, {"id": 1, "text": "Two"}]
        for result in ["not json", '{"translations":[]}', json.dumps({"translations": [
            {"id": 0, "translation": "一"}, {"id": 0, "translation": "二"}]}), json.dumps({"translations": [
            {"id": 0, "translation": "一"}, {"id": 2, "translation": "二"}]})]:
            with self.assertRaises(ValueError):
                translation.translations_from(result, sentences)

    def test_network_error_does_not_expose_credentials(self):
        client = Mock()
        client.aio.models.generate_content = AsyncMock(side_effect=RuntimeError("secret-key in upstream error"))
        with patch.object(app, "gemini_client", client):
            result = TestClient(app.app).post("/api/translate_subtitles", json={"sentences": [{"id": 0, "text": "Hello"}]}).json()
        self.assertEqual(result["status"], "error")
        self.assertNotIn("secret-key", result["message"])


if __name__ == "__main__":
    unittest.main()
