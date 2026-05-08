"""COMSOL MCP Server - Main entry point."""

import logging
import sys
from mcp.server.fastmcp import FastMCP

from .tools.session import register_session_tools
from .tools.model import register_model_tools
from .tools.parameters import register_parameter_tools
from .tools.geometry import register_geometry_tools
from .tools.physics import register_physics_tools
from .tools.material import register_material_tools
from .tools.mesh import register_mesh_tools
from .tools.study import register_study_tools
from .tools.results import register_results_tools
from .resources.model_resources import register_model_resources
from .knowledge.embedded import register_knowledge_tools
from .knowledge.material_kb_tools import register_material_kb_tools

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

mcp = FastMCP("comsol61-ops", host="127.0.0.1", port=8765)


def register_all_tools() -> None:
    """Register all MCP tools."""
    register_session_tools(mcp)
    register_model_tools(mcp)
    register_parameter_tools(mcp)
    register_geometry_tools(mcp)
    register_physics_tools(mcp)
    register_material_tools(mcp)
    register_mesh_tools(mcp)
    register_study_tools(mcp)
    register_results_tools(mcp)
    register_knowledge_tools(mcp)
    register_material_kb_tools(mcp)
    logger.info("Registered all tools")


def register_all_resources() -> None:
    """Register all MCP resources."""
    register_model_resources(mcp)
    logger.info("Registered all resources")


def main() -> None:
    """Run the MCP server."""
    logger.info("Starting COMSOL MCP Server...")
    
    register_all_tools()
    register_all_resources()
    
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
