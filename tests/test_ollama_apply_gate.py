"""Tests for OllamaApplyGate"""
import unittest
from unittest.mock import MagicMock, patch

import requests

from berlin_flat_hunter.ollama_apply_gate import OllamaApplyGate, ALL_FIELDS

EXPOSE = {
    "id": 1,
    "url": "https://www.gewobag.de/listing/1/",
    "title": "Schöne 2-Zimmer-Wohnung in Mitte",
    "address": "Beispielstraße 1, 10115 Berlin",
    "rooms": "2 Zi.",
    "size": "65.50 m²",
    "price": "900.00 €",
    "description": "Ruhige Lage, Balkon, Einbauküche.",
    "crawler": "Gewobag",
}


class FakeConfig:
    def __init__(self, apply_cfg=None, ollama_cfg=None):
        self.config = {"auto_apply": apply_cfg or {}, "ollama": ollama_cfg or {}}

    def ollama_config(self):
        return self.config.get("ollama", {})


class TestOllamaApplyGate(unittest.TestCase):

    def _gate(self, apply_cfg=None, ollama_cfg=None):
        return OllamaApplyGate(FakeConfig(apply_cfg, ollama_cfg))

    def _mock_resp(self, text):
        resp = MagicMock()
        resp.json.return_value = {"response": text}
        resp.raise_for_status.return_value = None
        return resp

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_yes_returns_true(self, mock_post):
        mock_post.return_value = self._mock_resp("YES")
        self.assertTrue(self._gate().should_apply(EXPOSE))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_no_returns_false(self, mock_post):
        mock_post.return_value = self._mock_resp("NO")
        self.assertFalse(self._gate().should_apply(EXPOSE))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_yes_case_insensitive(self, mock_post):
        mock_post.return_value = self._mock_resp("yes, apply")
        self.assertTrue(self._gate().should_apply(EXPOSE))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_connection_error_defaults_to_true(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("offline")
        self.assertTrue(self._gate().should_apply(EXPOSE))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_timeout_defaults_to_true(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout()
        self.assertTrue(self._gate().should_apply(EXPOSE))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_empty_response_returns_default(self, mock_post):
        """Empty/unparseable reply → fail-open (apply). OllamaApplyGate passes
        default=True so a confused model never blocks an otherwise-valid listing."""
        mock_post.return_value = self._mock_resp("")
        self.assertTrue(self._gate().should_apply(EXPOSE))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_uses_gate_model_over_ollama_model(self, mock_post):
        mock_post.return_value = self._mock_resp("YES")
        gate = self._gate(
            apply_cfg={"ollama_gate_model": "mistral"},
            ollama_cfg={"model": "llama3"},
        )
        gate.should_apply(EXPOSE)
        self.assertEqual(mock_post.call_args[1]["json"]["model"], "mistral")

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_falls_back_to_ollama_model(self, mock_post):
        mock_post.return_value = self._mock_resp("YES")
        gate = self._gate(ollama_cfg={"model": "llama3"})
        gate.should_apply(EXPOSE)
        self.assertEqual(mock_post.call_args[1]["json"]["model"], "llama3")

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_custom_prompt_used(self, mock_post):
        mock_post.return_value = self._mock_resp("YES")
        gate = self._gate(apply_cfg={"ollama_gate_prompt": "Apply? {expose}"})
        gate.should_apply(EXPOSE)
        prompt = mock_post.call_args[1]["json"]["prompt"]
        self.assertTrue(prompt.startswith("Apply?"))

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_selected_fields_only_in_prompt(self, mock_post):
        mock_post.return_value = self._mock_resp("YES")
        gate = self._gate(apply_cfg={"ollama_gate_fields": ["title", "price"]})
        gate.should_apply(EXPOSE)
        prompt = mock_post.call_args[1]["json"]["prompt"]
        self.assertIn("Schöne 2-Zimmer-Wohnung", prompt)
        self.assertIn("900.00", prompt)
        self.assertNotIn("Beispielstraße", prompt)  # address excluded

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_empty_fields_omitted_from_prompt(self, mock_post):
        mock_post.return_value = self._mock_resp("YES")
        expose = dict(EXPOSE, description="")
        gate = self._gate()
        gate.should_apply(expose)
        prompt = mock_post.call_args[1]["json"]["prompt"]
        self.assertNotIn("Description:", prompt)

    @patch("berlin_flat_hunter.ollama_client.requests.post")
    def test_all_default_fields_included(self, mock_post):
        mock_post.return_value = self._mock_resp("YES")
        gate = self._gate()
        gate.should_apply(EXPOSE)
        prompt = mock_post.call_args[1]["json"]["prompt"]
        self.assertIn("Title:", prompt)
        self.assertIn("Address:", prompt)
        self.assertIn("Price:", prompt)

    def test_default_url_used(self):
        gate = self._gate()
        self.assertEqual(gate.url, OllamaApplyGate.DEFAULT_URL)

    def test_invalid_prompt_template_raises(self):
        """A bad placeholder must fail fast at construct time, not per-expose."""
        with self.assertRaises(ValueError):
            self._gate(apply_cfg={"ollama_gate_prompt": "Apply? {bogus} {expose}"})

    def test_custom_url_used(self):
        gate = self._gate(ollama_cfg={"url": "http://other:11434/api/generate"})
        self.assertEqual(gate.url, "http://other:11434/api/generate")


class TestAutoApplicatorWithGate(unittest.TestCase):
    """Integration: AutoApplicator respects gate decision."""

    def _make_config(self, gate_enabled=True):
        cfg = MagicMock()
        cfg.config = {"auto_apply": {"ollama_gate": gate_enabled}}
        cfg.applicant_config.return_value = {}
        cfg.ollama_config.return_value = {}
        return cfg

    def test_gate_disabled_skips_ollama(self):
        from berlin_flat_hunter.applicator import AutoApplicator
        proc = AutoApplicator(self._make_config(gate_enabled=False))
        self.assertIsNone(proc.gate)

    def test_gate_enabled_creates_gate(self):
        from berlin_flat_hunter.applicator import AutoApplicator
        proc = AutoApplicator(self._make_config(gate_enabled=True))
        self.assertIsNotNone(proc.gate)

    def test_gate_no_blocks_application(self):
        from berlin_flat_hunter.applicator import AutoApplicator
        proc = AutoApplicator(self._make_config(gate_enabled=True))
        proc.gate = MagicMock(should_apply=MagicMock(return_value=False))
        for app in proc.applicators:
            app.apply = MagicMock(return_value=False)
        expose = {"url": "https://www.gewobag.de/listing/1/", "title": "Test"}
        result = proc.process_expose(expose)
        for app in proc.applicators:
            app.apply.assert_not_called()
        self.assertNotIn("applied", result)

    def test_gate_yes_allows_application(self):
        from berlin_flat_hunter.applicator import AutoApplicator
        proc = AutoApplicator(self._make_config(gate_enabled=True))
        proc.gate = MagicMock(should_apply=MagicMock(return_value=True))
        proc.applicators[0].apply = MagicMock(return_value=True)
        proc.applicators[1].apply = MagicMock(return_value=False)
        expose = {"url": "https://www.gewobag.de/listing/1/", "title": "Test"}
        result = proc.process_expose(expose)
        self.assertTrue(result.get("applied"))


if __name__ == "__main__":
    unittest.main()
