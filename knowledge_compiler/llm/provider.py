"""LLM providers (ADR-008): one thin interface — complete(prompt, schema) -> dict.

The Anthropic provider is the built-in reference implementation; extractor code
never imports a vendor SDK (ADR-008 invariant). Credentials resolve from the
environment via the SDK (ANTHROPIC_API_KEY/OPENAI_API_KEY/OPENAI_AZURE_API_KEY/
CF_ACCOUNT_ID+CF_API_TOKEN / auth profile) — never hardcoded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL = "claude-opus-4-8"


class LLMProviderError(Exception):
    """Provider unavailable/failed — triggers degraded compile (pipeline.md §6.1)."""


class AnthropicProvider:
    """Reference LLMProvider: structured outputs via output_config.format."""

    def __init__(self, model_id: str = DEFAULT_MODEL) -> None:
        try:
            import anthropic
        except ImportError as exc:  # dependency is optional: pip install knowledge-compiler[llm]
            raise LLMProviderError(
                "anthropic SDK not installed — pip install 'knowledge-compiler[llm]'") from exc
        self.model_id = model_id
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        import json

        try:
            response = self._client.messages.create(
                model=self.model_id,
                max_tokens=8192,
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}],
            )
        except self._anthropic.APIError as exc:
            raise LLMProviderError(f"anthropic API error: {exc}") from exc
        if response.stop_reason == "refusal":
            raise LLMProviderError("model refused the extraction request")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMProviderError(f"no text block in response (stop: {response.stop_reason})")
        return json.loads(text)


class OpenAIProvider:
    """Alternative LLMProvider: OpenAI structured outputs (strict json_schema).

    Credentials: OPENAI_API_KEY from the environment (SDK default resolution).
    Requires `pip install 'knowledge-compiler[llm-openai]'`.
    """

    DEFAULT_MODEL = "gpt-4o"

    def __init__(self, model_id: str = DEFAULT_MODEL) -> None:
        try:
            import openai
        except ImportError as exc:
            raise LLMProviderError(
                "openai SDK not installed — pip install 'knowledge-compiler[llm-openai]'") from exc
        self.model_id = model_id
        self._openai = openai
        try:
            self._client = openai.OpenAI()  # raises at construction when OPENAI_API_KEY absent
        except openai.OpenAIError as exc:
            raise LLMProviderError(f"openai client init failed: {exc}") from exc

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        import json

        try:
            response = self._client.chat.completions.create(
                model=self.model_id,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "extraction", "strict": True,
                                                 "schema": schema}},
            )
        except self._openai.OpenAIError as exc:
            raise LLMProviderError(f"openai API error: {exc}") from exc
        choice = response.choices[0]
        if choice.finish_reason == "content_filter" or choice.message.refusal:
            raise LLMProviderError("model refused the extraction request")
        if not choice.message.content:
            raise LLMProviderError(f"empty response (finish: {choice.finish_reason})")
        return json.loads(choice.message.content)


class AzureOpenAIProvider(OpenAIProvider):
    """OpenAI via Azure deployments. Env (never config files):
    OPENAI_AZURE_ENDPOINT / OPENAI_AZURE_API_KEY / OPENAI_AZURE_DEPLOYMENT
    (AZURE_OPENAI_* accepted as fallbacks); KC_AZURE_OPENAI_API_VERSION optional.
    The deployment name plays the model-id role (it also keys the LLM cache)."""

    def __init__(self, model_id: str | None = None) -> None:
        import os

        try:
            import openai
        except ImportError as exc:
            raise LLMProviderError(
                "openai SDK not installed — pip install 'knowledge-compiler[llm-openai]'") from exc

        def env(*names: str) -> str | None:
            for n in names:
                if os.environ.get(n):
                    return os.environ[n]
            return None

        endpoint = env("OPENAI_AZURE_ENDPOINT", "AZURE_OPENAI_ENDPOINT")
        api_key = env("OPENAI_AZURE_API_KEY", "AZURE_OPENAI_API_KEY")
        deployment = model_id or env("OPENAI_AZURE_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT")
        if not (endpoint and api_key and deployment):
            raise LLMProviderError(
                "azure-openai needs OPENAI_AZURE_ENDPOINT, OPENAI_AZURE_API_KEY and "
                "OPENAI_AZURE_DEPLOYMENT in the environment")

        self.model_id = deployment
        self._openai = openai
        try:
            self._client = openai.AzureOpenAI(
                azure_endpoint=endpoint, api_key=api_key,
                api_version=os.environ.get("KC_AZURE_OPENAI_API_VERSION", "2024-10-21"))
        except openai.OpenAIError as exc:
            raise LLMProviderError(f"azure-openai client init failed: {exc}") from exc


class CloudflareProvider(OpenAIProvider):
    """Workers AI via its OpenAI-compatible endpoint. response_format json_schema
    is not documented as supported there, so the schema is enforced via a
    forced function/tool call instead — the confirmed-supported mechanism.

    Env (never config files): CF_ACCOUNT_ID, CF_API_TOKEN.
    Requires `pip install 'knowledge-compiler[llm-openai]'` (same openai SDK,
    pointed at Cloudflare's base URL)."""

    DEFAULT_MODEL = "@cf/google/gemma-4-26b-a4b-it"
    _TOOL_NAME = "extraction"

    def __init__(self, model_id: str | None = None) -> None:
        import os

        try:
            import openai
        except ImportError as exc:
            raise LLMProviderError(
                "openai SDK not installed — pip install 'knowledge-compiler[llm-openai]'") from exc

        account_id = os.environ.get("CF_ACCOUNT_ID")
        api_token = os.environ.get("CF_API_TOKEN")
        if not (account_id and api_token):
            raise LLMProviderError(
                "cloudflare needs CF_ACCOUNT_ID and CF_API_TOKEN in the environment")

        self.model_id = model_id or self.DEFAULT_MODEL
        self._openai = openai
        try:
            self._client = openai.OpenAI(
                base_url=f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
                api_key=api_token,
            )
        except openai.OpenAIError as exc:
            raise LLMProviderError(f"cloudflare client init failed: {exc}") from exc

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        import json

        tool = {"type": "function",
                "function": {"name": self._TOOL_NAME, "description": "Report the extraction.",
                             "parameters": schema}}
        try:
            response = self._client.chat.completions.create(
                model=self.model_id,
                messages=[{"role": "user", "content": prompt}],
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": self._TOOL_NAME}},
            )
        except self._openai.OpenAIError as exc:
            raise LLMProviderError(f"cloudflare API error: {exc}") from exc
        choice = response.choices[0]
        if choice.finish_reason == "content_filter":
            raise LLMProviderError("model refused the extraction request")
        calls = choice.message.tool_calls or []
        if not calls:
            raise LLMProviderError(f"no tool call in response (finish: {choice.finish_reason})")
        return json.loads(calls[0].function.arguments)


def build_provider(llm_config: dict[str, Any]):
    """Provider factory (ADR-007/008): selection is explicit kc.toml configuration.

    [llm] provider = "anthropic" (default) | "openai" | "azure-openai" | "cloudflare";
    model overrides per provider."""
    name = llm_config.get("provider", "anthropic")
    model = llm_config.get("model")
    if name == "anthropic":
        return AnthropicProvider(model_id=model or DEFAULT_MODEL)
    if name == "openai":
        return OpenAIProvider(model_id=model or OpenAIProvider.DEFAULT_MODEL)
    if name == "azure-openai":
        return AzureOpenAIProvider(model_id=model)
    if name == "cloudflare":
        return CloudflareProvider(model_id=model)
    raise LLMProviderError(
        f"unknown llm provider '{name}' (supported: anthropic, openai, azure-openai, cloudflare)")


@dataclass
class FakeLLMProvider:
    """Deterministic provider for tests and offline development.

    `responses` maps a substring of the prompt (e.g. a file path) to the output
    dict; unmatched prompts return an empty extraction."""

    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    model_id: str = "fake-llm"
    calls: int = 0

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        for needle, output in sorted(self.responses.items()):
            if needle in prompt:
                return output
        return {"business_rules": [], "features": [], "risks": []}
