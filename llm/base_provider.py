"""abstract base class for llm providers.

every provider (azure, openai, etc.) must implement call() and stream().
the LLMResponse dataclass carries text + token counts so callers can track cost.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    """result of a single llm call — text plus token usage."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost_usd(self, model: str = "full") -> float:
        # azure openai gpt-4.1 pricing (as of 2025):
        # input:  $2.00 / 1M tokens
        # output: $8.00 / 1M tokens
        return (self.input_tokens * 2.00 + self.output_tokens * 8.00) / 1_000_000


class LLMProvider(ABC):
    """interface all llm backends must implement."""

    @abstractmethod
    async def call(self, system_prompt: str, user_prompt: str,
                   max_tokens: int = 4096, model: str = "full") -> LLMResponse:
        ...

    @abstractmethod
    async def stream(self, system_prompt: str, user_prompt: str,
                     max_tokens: int = 4096, model: str = "full"):
        """async generator — yields string chunks as they arrive."""
        ...
