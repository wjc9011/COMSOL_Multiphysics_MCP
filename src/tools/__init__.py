"""MCP Tools for COMSOL operations."""

from .session import register_session_tools
from .model import register_model_tools
from .parameters import register_parameter_tools
from .geometry import register_geometry_tools
from .physics import register_physics_tools
from .material import register_material_tools
from .mesh import register_mesh_tools
from .study import register_study_tools
from .results import register_results_tools

__all__ = [
    "register_session_tools",
    "register_model_tools",
    "register_parameter_tools",
    "register_geometry_tools",
    "register_physics_tools",
    "register_material_tools",
    "register_mesh_tools",
    "register_study_tools",
    "register_results_tools",
]
