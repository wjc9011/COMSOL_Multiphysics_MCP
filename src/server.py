"""COMSOL MCP Server - Main entry point."""

import logging
import os
from mcp.server.fastmcp import FastMCP

from .tools.session import register_session_tools, session_manager
from .tools.model import register_model_tools
from .tools.parameters import register_parameter_tools
from .tools.geometry import register_geometry_tools
from .tools.physics import register_physics_tools
from .tools.mesh import register_mesh_tools
from .tools.study import register_study_tools
from .tools.results import register_results_tools
from .resources.model_resources import register_model_resources
from .knowledge.embedded import register_knowledge_tools

logging.basicConfig(level=logging.INFO)
# MPh logs at INFO level while the JVM starts, which deadlocks with JPype (reproduced on Windows + COMSOL 6.3)
logging.getLogger('mph').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

mcp = FastMCP("COMSOL MCP")


def register_all_tools() -> None:
    """Register all MCP tools."""
    register_session_tools(mcp)
    register_model_tools(mcp)
    register_parameter_tools(mcp)
    register_geometry_tools(mcp)
    register_physics_tools(mcp)
    register_mesh_tools(mcp)
    register_study_tools(mcp)
    register_results_tools(mcp)
    register_knowledge_tools(mcp)
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
    
    transport = os.environ.get("COMSOL_MCP_TRANSPORT", "stdio").strip().lower()

    if transport == "stdio":
        # The stdio transport immediately spawns a thread blocked on stdin (fd 0), and
        # JPype's startJVM deadlocks forever while fd 0 is read by another thread, so the
        # JVM must be started before mcp.run() (reproduced on Windows + COMSOL 6.3).
        logger.info(f"COMSOL pre-start: {session_manager.start()}")
    else:
        mcp.settings.host = os.environ.get("COMSOL_MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("COMSOL_MCP_PORT", "8765"))
        # Session state lives in the process-level SessionManager, not in the MCP
        # transport session, so run stateless: a restarted server keeps serving
        # clients that still hold a session id from the previous process.
        mcp.settings.stateless_http = True
        logger.info(
            f"HTTP transport '{transport}' on {mcp.settings.host}:{mcp.settings.port}"
            " (COMSOL starts lazily on first tool call)"
        )

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
