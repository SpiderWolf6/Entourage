"""llm client — routes calls to the Azure OpenAI provider.

Credentials are passed explicitly per-call rather than read from os.environ.
This eliminates the env-patching race condition when multiple users run
pipelines in parallel.

The provider is instantiated per-call with the given credentials — no singleton.
For the admin path (use_env_creds), credentials are read from os.environ directly.
"""

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.credentials import Credentials

from llm.base_provider import LLMResponse
from llm.azure_openai import AzureOpenAIProvider


def _make_provider(creds: "Credentials | None") -> AzureOpenAIProvider:
    """Create a provider from explicit credentials or fall back to env (admin path)."""
    if creds is None:
        # admin path — read from os.environ
        return AzureOpenAIProvider()
    return AzureOpenAIProvider(
        api_key    = creds.azure_openai_api_key,
        endpoint   = creds.azure_openai_endpoint,
        deployment = creds.azure_openai_deployment_full,
        api_version= creds.azure_openai_api_version,
    )


# ── async interface ───────────────────────────────────────────────────────────

async def async_call_llm(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4096,
    model: str = "full",
    creds: "Credentials | None" = None,
) -> str:
    """async llm call — returns text only."""
    provider = _make_provider(creds)
    resp = await provider.call(system_prompt, user_prompt, max_tokens, model=model)
    return resp.text


async def async_call_llm_tracked(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4096,
    model: str = "full",
    creds: "Credentials | None" = None,
) -> LLMResponse:
    """async llm call — returns full LLMResponse with token counts."""
    provider = _make_provider(creds)
    return await provider.call(system_prompt, user_prompt, max_tokens, model=model)


async def async_stream_llm(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4096,
    model: str = "full",
    creds: "Credentials | None" = None,
):
    """async streaming call — yields text chunks."""
    provider = _make_provider(creds)
    async for chunk in provider.stream(system_prompt, user_prompt, max_tokens, model=model):
        yield chunk


# ── sync interface (kept for scripts / one-off calls) ─────────────────────────

def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 4096,
             model: str = "full", creds: "Credentials | None" = None) -> str:
    """synchronous wrapper — spins up a new event loop. don't use inside async code."""
    return asyncio.run(async_call_llm(system_prompt, user_prompt, max_tokens, model=model, creds=creds))


# keep these for backwards compat with any code that imported the old singleton pattern
def reset_provider() -> None:
    pass  # no-op — no singleton anymore

def set_provider(provider) -> None:
    pass  # no-op — provider is created per-call
