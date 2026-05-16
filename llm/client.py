"""llm client — thin routing layer over the active provider.

all agent llm calls go through async_call_llm_tracked() or async_stream_llm().
the provider singleton is created lazily so credential patching (done in
planning.py _apply_credentials) takes effect before the first real call.
"""

import asyncio
import os

from dotenv import load_dotenv

from llm.base_provider import LLMProvider, LLMResponse
from llm.azure_openai import AzureOpenAIProvider

# load .env from project root so local dev works without export AZURE_OPENAI_API_KEY=...
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# module-level singleton — reset to None whenever credentials change
_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """return the active provider, creating it on first call.

    lazy init is intentional: _apply_credentials() patches os.environ before
    the pipeline runs, so the provider is always created with the right keys.
    """
    global _provider
    if _provider is None:
        _provider = AzureOpenAIProvider()
    return _provider


def reset_provider() -> None:
    """force a fresh provider on the next call — used after credential changes."""
    global _provider
    _provider = None


def set_provider(provider: LLMProvider) -> None:
    """override the active provider (testing / alternate backends)."""
    global _provider
    _provider = provider


# ── async interface (used by all agents) ──────────────────────────────────────

async def async_call_llm(system_prompt: str, user_prompt: str,
                         max_tokens: int = 4096, model: str = "full") -> str:
    """async llm call — returns text only."""
    resp = await get_provider().call(system_prompt, user_prompt, max_tokens, model=model)
    return resp.text


async def async_call_llm_tracked(system_prompt: str, user_prompt: str,
                                  max_tokens: int = 4096, model: str = "full") -> LLMResponse:
    """async llm call — returns full LLMResponse with token counts and cost."""
    return await get_provider().call(system_prompt, user_prompt, max_tokens, model=model)


async def async_stream_llm(system_prompt: str, user_prompt: str,
                            max_tokens: int = 4096, model: str = "full"):
    """async streaming call — yields text chunks."""
    async for chunk in get_provider().stream(system_prompt, user_prompt, max_tokens, model=model):
        yield chunk


# ── sync interface (kept for scripts / one-off calls) ─────────────────────────

def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 4096,
             model: str = "full") -> str:
    """synchronous wrapper — spins up a new event loop. don't use inside async code."""
    return asyncio.run(async_call_llm(system_prompt, user_prompt, max_tokens, model=model))
