from .registry import AgentRegistry, discover_agents
from .engineering_manager import EngineeringManagerAgent
from .product_owner import ProductOwnerAgent
from .architect import ArchitectAgent
from .project_lead import ProjectLeadAgent
from .hr import HRAgent

__all__ = [
    "AgentRegistry",
    "discover_agents",
    "EngineeringManagerAgent",
    "ProductOwnerAgent",
    "ArchitectAgent",
    "ProjectLeadAgent",
    "HRAgent",
]
