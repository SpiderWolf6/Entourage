"""Credentials dataclass — passed through the pipeline stack instead of os.environ.

No credentials are ever written to SQLite or disk. They come from:
  - Supabase user_metadata (normal users, saved creds)
  - os.environ directly (admin path, ADMIN_PASSWORD matches)

The dataclass is immutable and passed explicitly to every LLM call.
"""

from __future__ import annotations
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Credentials:
    azure_openai_api_key:        str
    azure_openai_endpoint:       str
    azure_openai_deployment_full: str
    azure_openai_api_version:    str
    anthropic_api_key:           str

    def validate(self) -> None:
        """Raise ValueError if any required field is missing."""
        missing = [
            f for f in (
                "azure_openai_api_key",
                "azure_openai_endpoint",
                "azure_openai_deployment_full",
                "azure_openai_api_version",
                "anthropic_api_key",
            )
            if not getattr(self, f).strip()
        ]
        if missing:
            raise ValueError(f"Missing credentials: {', '.join(missing)}")

    @classmethod
    def from_env(cls) -> "Credentials":
        """Load credentials from os.environ (admin path only)."""
        return cls(
            azure_openai_api_key         = os.environ.get("AZURE_OPENAI_API_KEY", ""),
            azure_openai_endpoint        = os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
            azure_openai_deployment_full = os.environ.get("AZURE_OPENAI_DEPLOYMENT_FULL", ""),
            azure_openai_api_version     = os.environ.get("AZURE_OPENAI_API_VERSION", ""),
            anthropic_api_key            = os.environ.get("ANTHROPIC_API_KEY", ""),
        )

    @classmethod
    def from_dict(cls, d: dict) -> "Credentials":
        """Build from a user-supplied dict (from Supabase user_metadata saved_creds)."""
        return cls(
            azure_openai_api_key         = str(d.get("azure_openai_api_key", "")).strip(),
            azure_openai_endpoint        = str(d.get("azure_openai_endpoint", "")).strip(),
            azure_openai_deployment_full = str(d.get("azure_openai_deployment_full", "")).strip(),
            azure_openai_api_version     = str(d.get("azure_openai_api_version", "")).strip(),
            anthropic_api_key            = str(d.get("anthropic_api_key", "")).strip(),
        )
