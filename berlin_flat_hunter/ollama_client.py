"""Shared Ollama HTTP client used by OllamaFilter and OllamaApplyGate"""
import requests

from flathunter.logging import logger

DEFAULT_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "llama3"


class OllamaClient:
    """Minimal client for the local Ollama /api/generate endpoint"""

    def __init__(self, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL, timeout: int = 60):
        self.url = url
        self.model = model
        self.timeout = timeout

    def ask_yes_no(self, prompt: str, default: bool = True, context: str = "ollama") -> bool:
        """Ask Ollama a question; return True if the answer parses as YES,
        False if it parses as NO, ``default`` for unrecognised replies.

        On network, HTTP, or JSON-parse error also falls back to ``default``.
        Treating unrecognised replies (``"Maybe"``, ``"It depends..."``) as
        the configured default keeps the fail-open semantics callers rely on.
        """
        try:
            resp = requests.post(
                self.url,
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.RequestException as exc:
            logger.warning("%s unreachable (%s); falling back to %s", context, exc, default)
            return default
        except ValueError as exc:  # JSON decode error not always wrapped as RequestException
            logger.warning("%s returned invalid JSON (%s); falling back to %s", context, exc, default)
            return default

        answer = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(answer, str):
            logger.warning("%s response missing 'response' string; falling back to %s",
                           context, default)
            return default
        normalised = answer.strip().upper()
        logger.debug("%s verdict: %s", context, normalised)
        if normalised.startswith("YES"):
            return True
        if normalised.startswith("NO"):
            return False
        logger.warning("%s reply %r is neither YES nor NO; falling back to %s",
                       context, answer.strip()[:80], default)
        return default
