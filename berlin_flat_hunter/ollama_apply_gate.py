"""OllamaApplyGate — asks Ollama whether to submit an application for a listing"""
from berlin_flat_hunter.ollama_client import OllamaClient, DEFAULT_URL, DEFAULT_MODEL

ALL_FIELDS = ("title", "address", "rooms", "size", "price", "url", "description")

_FIELD_LABELS = {
    "title": "Title",
    "address": "Address",
    "rooms": "Rooms",
    "size": "Size",
    "price": "Price",
    "url": "URL",
    "description": "Description",
}

DEFAULT_PROMPT = (
    "You are helping decide whether to apply for a Berlin apartment.\n"
    "Reply with exactly YES to apply, or NO to skip.\n\n"
    "Listing:\n{expose}"
)


class OllamaApplyGate:
    """Ask a local Ollama model whether to submit an application for an expose.

    Falls back to True (apply) if Ollama is unreachable.
    """

    DEFAULT_URL = DEFAULT_URL
    DEFAULT_MODEL = DEFAULT_MODEL
    DEFAULT_PROMPT = DEFAULT_PROMPT

    def __init__(self, config):
        cfg = config.ollama_config() if hasattr(config, "ollama_config") else {}
        apply_cfg = config.config.get("auto_apply", {}) if hasattr(config, "config") else {}

        url = cfg.get("url", DEFAULT_URL)
        model = apply_cfg.get("ollama_gate_model") or cfg.get("model", DEFAULT_MODEL)
        self.client = OllamaClient(url=url, model=model)
        self.url = url
        self.model = model
        template = apply_cfg.get("ollama_gate_prompt", DEFAULT_PROMPT)
        # Catch bad templates at config time, not on every expose.
        try:
            template.format(expose="")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"auto_apply.ollama_gate_prompt invalid ({exc}); "
                "use only {expose} as a placeholder"
            ) from exc
        self.prompt_template = template
        self.fields = apply_cfg.get("ollama_gate_fields", list(ALL_FIELDS))

    def should_apply(self, expose: dict) -> bool:
        prompt = self.prompt_template.format(expose=self._format(expose))
        return self.client.ask_yes_no(prompt, default=True, context=f"OllamaApplyGate[{expose.get('url')}]")

    def _format(self, expose: dict) -> str:
        return "\n".join(
            f"{_FIELD_LABELS.get(field, field)}: {value}"
            for field in self.fields
            if (value := expose.get(field, ""))
        )
