"""azure openai provider — calls the azure openai REST api for all planning agents.

retry logic covers the three most common transient failures:
  - 429 rate limit: exponential backoff up to 90s
  - 400 content filter: retry with the same payload (filter trips are usually transient)
  - 5xx server errors: retry with backoff

requires three env vars: AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT_FULL.
"""

import asyncio
import json
import logging
import os

import httpx
from dotenv import load_dotenv

from llm.base_provider import LLMProvider, LLMResponse

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
CALL_TIMEOUT = 180   # seconds before giving up on a single attempt
STREAM_TIMEOUT = 180


class AzureOpenAIProvider(LLMProvider):
    """azure openai chat completions provider (REST, not the azure-openai SDK).

    we call the REST API directly via httpx to avoid SDK version conflicts and
    keep the dependency list small. headers use api-key auth (not Bearer tokens).
    """

    def __init__(self):
        self.api_key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
        base = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "").strip()
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL", "").strip()

        def _build_url(base_url: str, dep: str, version: str) -> str:
            # if the base already contains a deployment path, just append the api-version
            if "chat/completions" in base_url:
                return base_url
            if "/deployments/" in base_url:
                return base_url if "?" in base_url else f"{base_url}?api-version={version}"
            # standard case: build from base endpoint + deployment name
            return f"{base_url}/openai/deployments/{dep}/chat/completions?api-version={version}"

        self.endpoint = _build_url(base, deployment, api_version)
        logger.info("azure endpoint: %s", self.endpoint)

    def _get_endpoint(self, model: str) -> str:
        return self.endpoint

    async def call(self, system_prompt: str, user_prompt: str,
                   max_tokens: int = 4096, model: str = "full") -> LLMResponse:
        endpoint = self._get_endpoint(model)
        headers = {"Content-Type": "application/json", "api-key": self.api_key}
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_completion_tokens": max_tokens,
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        endpoint, headers=headers, json=payload, timeout=CALL_TIMEOUT
                    )

                if response.status_code == 429:
                    # rate limited — back off and retry
                    wait = min(15 * attempt, 90)
                    logger.warning("rate limited (429). waiting %ds (attempt %d/%d)",
                                   wait, attempt, MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue

                if response.status_code == 400 and "content_filter" in response.text:
                    # content filter — usually transient, retry
                    wait = min(10 * attempt, 60)
                    logger.warning("content filter (400). waiting %ds (attempt %d/%d)",
                                   wait, attempt, MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue

                if response.status_code >= 500:
                    wait = min(10 * attempt, 60)
                    logger.warning("server error (%d). waiting %ds (attempt %d/%d)",
                                   response.status_code, wait, attempt, MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue

                if not response.is_success:
                    logger.error("llm error %d — body: %s",
                                 response.status_code, response.text[:500])
                    response.raise_for_status()

                raw_body = response.text
                if not raw_body.strip():
                    logger.warning("empty response body (status=%d). retry %d/%d",
                                   response.status_code, attempt, MAX_RETRIES)
                    await asyncio.sleep(min(5 * attempt, 30))
                    continue

                try:
                    data = response.json()
                except Exception as json_err:
                    logger.error("failed to parse response as json: %s\nbody: %s",
                                 json_err, raw_body[:500])
                    await asyncio.sleep(min(5 * attempt, 30))
                    continue

                content = data["choices"][0]["message"]["content"]

                if not content:
                    # empty response — usually reasoning-model token budget exhaustion
                    logger.warning("empty content from llm. retry %d/%d", attempt, MAX_RETRIES)
                    payload["max_completion_tokens"] = max_tokens + 4096
                    await asyncio.sleep(2)
                    continue

                usage = data.get("usage", {})
                return LLMResponse(
                    text=content,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                )

            except httpx.TimeoutException:
                logger.warning("llm call timed out after %ds. retry %d/%d",
                               CALL_TIMEOUT, attempt, MAX_RETRIES)
                await asyncio.sleep(min(5 * attempt, 30))
            except httpx.HTTPStatusError as e:
                logger.error("llm http error: %s", e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(min(5 * attempt, 30))
                else:
                    raise
            except (httpx.ConnectError, httpx.ReadError) as e:
                logger.warning("connection error (%s). retry %d/%d",
                               type(e).__name__, attempt, MAX_RETRIES)
                await asyncio.sleep(min(10 * attempt, 60))

        raise RuntimeError(f"llm call failed after {MAX_RETRIES} retries")

    async def stream(self, system_prompt: str, user_prompt: str,
                     max_tokens: int = 4096, model: str = "full"):
        """streaming variant — yields raw text chunks as SSE data arrives."""
        endpoint = self._get_endpoint(model)
        headers = {"Content-Type": "application/json", "api-key": self.api_key}
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_completion_tokens": max_tokens,
            "stream": True,
        }

        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST", endpoint, headers=headers, json=payload, timeout=STREAM_TIMEOUT
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        chunk = json.loads(line[6:])
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
