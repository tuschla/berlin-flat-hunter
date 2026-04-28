"""OllamaFilter — filter exposes via local Ollama LLM"""
from typing import Iterator

from flathunter.abstract_processor import Processor

from berlin_flat_hunter.ollama_client import OllamaClient, DEFAULT_URL, DEFAULT_MODEL

DEFAULT_PROMPT = (
    "You are a Berlin flat-hunting assistant. "
    "Given this listing, reply with exactly YES if it looks worth applying for, "
    "or NO if not. Listing:\n\n{expose}"
)


class OllamaFilter(Processor):
    """Send each expose to Ollama; drop it if model says NO.

    On Ollama failure, keeps the expose (fail-open).
    """

    DEFAULT_URL = DEFAULT_URL
    DEFAULT_MODEL = DEFAULT_MODEL
    DEFAULT_PROMPT = DEFAULT_PROMPT

    def __init__(self, config):
        cfg = config.ollama_config() if hasattr(config, "ollama_config") else {}
        self.url = cfg.get("url", DEFAULT_URL)
        self.model = cfg.get("model", DEFAULT_MODEL)
        self.client = OllamaClient(url=self.url, model=self.model)
        template = cfg.get("prompt", DEFAULT_PROMPT)
        # Validate at startup — a missing/bad placeholder would otherwise raise
        # KeyError once per expose during a hunt.
        try:
            template.format(expose="")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"ollama.prompt template invalid ({exc}); "
                "use only {expose} as a placeholder"
            ) from exc
        self.prompt_template = template

    def process_exposes(self, exposes) -> Iterator[dict]:  # type: ignore[override]
        for expose in exposes:
            if self._keep(expose):
                yield expose

    def _keep(self, expose: dict) -> bool:
        text = (
            f"Title: {expose.get('title', '')}\n"
            f"Address: {expose.get('address', '')}\n"
            f"Rooms: {expose.get('rooms', '')}\n"
            f"Size: {expose.get('size', '')}\n"
            f"Price: {expose.get('price', '')}\n"
            f"URL: {expose.get('url', '')}"
        )
        prompt = self.prompt_template.format(expose=text)
        return self.client.ask_yes_no(prompt, default=True, context=f"OllamaFilter[{expose.get('url')}]")
