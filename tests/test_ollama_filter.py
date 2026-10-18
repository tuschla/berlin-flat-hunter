"""Tests for OllamaFilter processor"""
import unittest
from unittest.mock import MagicMock, patch

import requests

from berlin_flat_hunter.config import BerlinConfig
from berlin_flat_hunter.ollama_filter import OllamaFilter

OLLAMA_URL = "http://localhost:11434/api/generate"

EXPOSE = {
    "id": 1,
    "url": "https://www.gewobag.de/listing/1/",
    "title": "Schöne 2-Zimmer-Wohnung",
    "address": "Beispielstraße 1, 10115 Berlin",
    "rooms": "2 Zi.",
    "size": "65.50 m²",
    "price": "900.00 €",
    "crawler": "Gewobag",
}


def _make_config(ollama_cfg):
    return BerlinConfig({"ollama": ollama_cfg})


class TestOllamaFilter(unittest.TestCase):

    def setUp(self):
        self.fltr = OllamaFilter(_make_config({"url": OLLAMA_URL, "model": "llama3"}))

    def _mock_response(self, text):
        resp = MagicMock()
        resp.json.return_value = {"response": text}
        resp.raise_for_status.return_value = None
        return resp

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_yes_keeps_expose(self, mock_post):
        mock_post.return_value = self._mock_response("YES")
        results = list(self.fltr.process_exposes([EXPOSE]))
        self.assertEqual(len(results), 1)

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_no_drops_expose(self, mock_post):
        mock_post.return_value = self._mock_response("NO")
        results = list(self.fltr.process_exposes([EXPOSE]))
        self.assertEqual(len(results), 0)

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_yes_case_insensitive(self, mock_post):
        mock_post.return_value = self._mock_response("yes, looks good")
        results = list(self.fltr.process_exposes([EXPOSE]))
        self.assertEqual(len(results), 1)

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_connection_error_keeps_expose(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("offline")
        results = list(self.fltr.process_exposes([EXPOSE]))
        self.assertEqual(len(results), 1)

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_timeout_keeps_expose(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout()
        results = list(self.fltr.process_exposes([EXPOSE]))
        self.assertEqual(len(results), 1)

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_multiple_exposes_filtered_correctly(self, mock_post):
        mock_post.side_effect = [
            self._mock_response("YES"),
            self._mock_response("NO"),
            self._mock_response("YES"),
        ]
        exposes = [dict(EXPOSE, id=i, url=f"https://example.com/{i}/") for i in range(3)]
        results = list(self.fltr.process_exposes(exposes))
        self.assertEqual(len(results), 2)

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_post_called_with_model(self, mock_post):
        mock_post.return_value = self._mock_response("YES")
        list(self.fltr.process_exposes([EXPOSE]))
        call_kwargs = mock_post.call_args[1]
        self.assertEqual(call_kwargs["json"]["model"], "llama3")

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_post_called_with_expose_content(self, mock_post):
        mock_post.return_value = self._mock_response("YES")
        list(self.fltr.process_exposes([EXPOSE]))
        prompt = mock_post.call_args[1]["json"]["prompt"]
        self.assertIn("Schöne 2-Zimmer-Wohnung", prompt)

    def test_default_url_used_without_config(self):
        fltr = OllamaFilter(_make_config({}))
        self.assertEqual(fltr.url, OllamaFilter.DEFAULT_URL)

    def test_custom_prompt_template(self):
        fltr = OllamaFilter(_make_config({"prompt": "Is this good? {expose}"}))
        self.assertIn("{expose}", fltr.prompt_template)

    def test_invalid_prompt_template_raises(self):
        """A bad placeholder (e.g. {title}) must fail fast at construct time."""
        with self.assertRaises(ValueError):
            OllamaFilter(_make_config({"prompt": "Apply? {title} {expose}"}))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_empty_response_keeps_expose_fail_open(self, mock_post):
        """Empty/unparseable replies must keep the expose — OllamaFilter passes
        default=True so a flaky model never silently drops valid listings."""
        mock_post.return_value = self._mock_response("")
        results = list(self.fltr.process_exposes([EXPOSE]))
        self.assertEqual(len(results), 1)

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_unrecognised_reply_keeps_expose(self, mock_post):
        mock_post.return_value = self._mock_response("Maybe — depends on...")
        results = list(self.fltr.process_exposes([EXPOSE]))
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
