"""Tests for OllamaClient — focus on edge cases of the HTTP boundary"""
import unittest
from unittest.mock import MagicMock, patch

import requests

from berlin_flat_hunter.ollama_client import OllamaClient


def _resp(json_value, status=200):
    r = MagicMock()
    r.status_code = status
    r.raise_for_status.return_value = None
    r.json.return_value = json_value
    return r


def _bad_json_resp():
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.side_effect = ValueError("not json")
    return r


class TestOllamaClient(unittest.TestCase):

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_yes_returns_true(self, mock_post):
        mock_post.return_value = _resp({"response": "YES, apply"})
        c = OllamaClient()
        self.assertTrue(c.ask_yes_no("?", default=False))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_no_returns_false(self, mock_post):
        mock_post.return_value = _resp({"response": "NO"})
        c = OllamaClient()
        self.assertFalse(c.ask_yes_no("?", default=True))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_empty_response_returns_default(self, mock_post):
        """Empty body is unparseable → default (fail-open per caller intent)."""
        mock_post.return_value = _resp({"response": ""})
        c = OllamaClient()
        self.assertTrue(c.ask_yes_no("?", default=True))
        self.assertFalse(c.ask_yes_no("?", default=False))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_unrecognised_reply_returns_default(self, mock_post):
        """Replies like 'Maybe' or 'It depends' fall back to the default."""
        mock_post.return_value = _resp({"response": "Maybe — it depends on..."})
        c = OllamaClient()
        self.assertTrue(c.ask_yes_no("?", default=True))
        mock_post.return_value = _resp({"response": "I'd say so"})
        self.assertFalse(c.ask_yes_no("?", default=False))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_no_with_explanation_returns_false(self, mock_post):
        """`NO, this is...` still parses as NO."""
        mock_post.return_value = _resp({"response": "No, the price is too high."})
        c = OllamaClient()
        self.assertFalse(c.ask_yes_no("?", default=True))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_connection_error_returns_default(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("offline")
        c = OllamaClient()
        self.assertTrue(c.ask_yes_no("?", default=True))
        self.assertFalse(c.ask_yes_no("?", default=False))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_invalid_json_returns_default(self, mock_post):
        mock_post.return_value = _bad_json_resp()
        c = OllamaClient()
        self.assertTrue(c.ask_yes_no("?", default=True))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_missing_response_field_returns_default(self, mock_post):
        mock_post.return_value = _resp({"some_other_key": "data"})
        c = OllamaClient()
        self.assertTrue(c.ask_yes_no("?", default=True))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_non_string_response_returns_default(self, mock_post):
        mock_post.return_value = _resp({"response": None})
        c = OllamaClient()
        self.assertTrue(c.ask_yes_no("?", default=True))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_non_dict_payload_returns_default(self, mock_post):
        mock_post.return_value = _resp(["unexpected", "list"])
        c = OllamaClient()
        self.assertFalse(c.ask_yes_no("?", default=False))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_uses_configured_model_and_url(self, mock_post):
        mock_post.return_value = _resp({"response": "YES"})
        c = OllamaClient(url="http://other:11434/api/generate", model="mistral")
        c.ask_yes_no("hello")
        kwargs = mock_post.call_args[1]
        self.assertEqual(mock_post.call_args[0][0], "http://other:11434/api/generate")
        self.assertEqual(kwargs["json"]["model"], "mistral")
        self.assertEqual(kwargs["json"]["prompt"], "hello")
        self.assertFalse(kwargs["json"]["stream"])


if __name__ == "__main__":
    unittest.main()
