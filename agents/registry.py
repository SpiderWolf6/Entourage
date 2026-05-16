"""agent registry — maps string names to agent classes via @register decorator.

agents self-register at import time. discover_agents() imports all agent modules
so their decorators run; after that, AgentRegistry.create("architect") works anywhere.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.base import BaseAgent


class AgentRegistry:
    # class-level dict so all code shares the same registry
    _agents: dict[str, type[BaseAgent]] = {}

    @classmethod
    def register(cls, name: str):
        """decorator — call as @AgentRegistry.register("architect") above the class."""
        def decorator(agent_class: type[BaseAgent]) -> type[BaseAgent]:
            cls._agents[name] = agent_class
            return agent_class
        return decorator

    @classmethod
    def get(cls, name: str) -> type[BaseAgent]:
        if name not in cls._agents:
            raise KeyError(f"no agent registered as '{name}'. available: {list(cls._agents.keys())}")
        return cls._agents[name]

    @classmethod
    def create(cls, name: str, **kwargs) -> BaseAgent:
        """instantiate an agent by name."""
        return cls.get(name)(**kwargs)

    @classmethod
    def list_agents(cls) -> list[str]:
        return list(cls._agents.keys())


def discover_agents() -> None:
    """import all planning agent modules so their @register decorators fire.

    idempotent — safe to call multiple times (python module cache prevents re-execution).
    """
    import agents.engineering_manager  # noqa: F401
    import agents.product_owner        # noqa: F401
    import agents.architect            # noqa: F401
    import agents.project_lead         # noqa: F401
    import agents.hr                   # noqa: F401
