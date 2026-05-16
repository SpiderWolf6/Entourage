"""Application configuration via pydantic-settings."""

import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "sqlite+aiosqlite:///./entourage.db"

    # Azure OpenAI (planning pipeline)
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_deployment: str = ""
    azure_openai_deployment_full: str = ""
    azure_openai_api_version: str = "2024-12-01-preview"

    # Anthropic (Claude Code CLI — used by dev agents and reviewer)
    anthropic_api_key: str = ""

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    # In production set CORS_ORIGINS="https://yourdomain.com" in env
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000", "http://localhost:3001"]

    # Workspace
    workspace_dir: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspaces"
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
