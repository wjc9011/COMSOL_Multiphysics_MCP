"""Knowledge base tools for COMSOL MCP Server."""

from pathlib import Path
from typing import Optional
from mcp.server.fastmcp import FastMCP

KNOWLEDGE_DIR = Path(__file__).parent / "prompts"

KNOWLEDGE_FILES = {
    "mph_api": {
        "file": "mph_api.md",
        "title": "MPh API Reference",
        "description": "Python API for controlling COMSOL via MPh library",
        "keywords": ["api", "python", "client", "model", "mph", "function", "method"],
    },
    "physics_guide": {
        "file": "physics_guide.md",
        "description": "Guide to physics interfaces and boundary conditions",
        "title": "Physics Interfaces Guide",
        "keywords": ["physics", "electrostatics", "heat", "solid", "fluid", "boundary", "condition"],
    },
    "workflow": {
        "file": "workflow.md",
        "title": "Modeling Workflow Guide",
        "description": "Step-by-step workflows for common simulation tasks",
        "keywords": ["workflow", "example", "tutorial", "step", "process", "howto"],
    },
}

TOPIC_GUIDES = {
    "electrostatics": {
        "physics": "electrostatic",
        "boundary_conditions": ["Ground", "ElectricPotential", "SurfaceChargeDensity", "Terminal"],
        "common_expressions": ["es.normE", "es.V", "es.intWe"],
        "tips": [
            "Use Terminal boundary condition for capacitance calculations",
            "Ground and ElectricPotential are the most common boundary conditions",
            "Capacitance can be calculated as C = 2*es.intWe/U^2",
        ],
    },
    "heat_transfer": {
        "physics": "heat_transfer",
        "boundary_conditions": ["Temperature", "HeatFlux", "ConvectiveHeatFlux", "Radiation"],
        "common_expressions": ["T", "ht.qx", "ht.gradT", "ht.Tmax"],
        "tips": [
            "ConvectiveHeatFlux requires convection coefficient and ambient temperature",
            "Use Symmetry boundary for adiabatic conditions",
            "Time-dependent studies are common for transient heat transfer",
        ],
    },
    "solid_mechanics": {
        "physics": "solid_mechanics",
        "boundary_conditions": ["Fixed", "Roller", "Symmetry", "BoundaryLoad", "Displacement"],
        "common_expressions": ["solid.mises", "solid.disp", "solid.u", "solid.v", "solid.w"],
        "tips": [
            "Von Mises stress (solid.mises) is commonly used for failure analysis",
            "Fixed constraint fully constrains all displacement components",
            "Use symmetry boundary to reduce model size",
        ],
    },
    "fluid_flow": {
        "physics": "laminar_flow",
        "boundary_conditions": ["Wall", "Inlet", "Outlet", "Symmetry", "Slip"],
        "common_expressions": ["u", "v", "w", "p", "spf.U"],
        "tips": [
            "Check Reynolds number to determine if laminar or turbulent flow",
            "Pressure outlet is commonly set to zero gauge pressure",
            "No-slip wall is the default condition for solid surfaces",
        ],
    },
}

TROUBLESHOOTING = {
    "geometry_build_failed": {
        "causes": [
            "Overlapping geometry objects",
            "Undefined parameters in geometry expressions",
            "Invalid geometry operations",
            "CAD import issues",
        ],
        "solutions": [
            "Check for overlapping features in the geometry sequence",
            "Verify all parameters are defined before building geometry",
            "Try building geometry step by step",
            "Use 'Form Union' or 'Form Assembly' appropriately",
            "Check CAD import settings for imported geometry",
        ],
    },
    "mesh_failed": {
        "causes": [
            "Geometry has very small features",
            "Complex geometry without proper defeaturing",
            "Incompatible mesh size settings",
            "Invalid geometry for meshing",
        ],
        "solutions": [
            "Increase mesh size or use coarser mesh",
            "Add virtual operations to simplify geometry",
            "Use mesh control for specific regions",
            "Try different mesh types (free tetrahedral, swept, etc.)",
            "Check for sliver faces or edges in geometry",
        ],
    },
    "solver_no_convergence": {
        "causes": [
            "Poor mesh quality",
            "Incorrect boundary conditions",
            "Highly nonlinear problem",
            "Inappropriate solver settings",
            "Scaling issues",
        ],
        "solutions": [
            "Refine mesh in regions with high gradients",
            "Verify all boundary conditions are correctly applied",
            "Use parametric continuation for nonlinear problems",
            "Try different solver configurations",
            "Check variable scaling in solver settings",
            "Reduce time step for transient problems",
        ],
    },
    "memory_error": {
        "causes": [
            "Mesh too fine",
            "Large 3D domain",
            "Many degrees of freedom",
            "Limited RAM",
        ],
        "solutions": [
            "Coarsen mesh in less important regions",
            "Use symmetry to reduce domain size",
            "Use iterative solvers instead of direct",
            "Enable out-of-core solver option",
            "Solve on machine with more RAM",
        ],
    },
    "license_error": {
        "causes": [
            "COMSOL license not available",
            "License server connection issues",
            "Module not licensed",
            "License expired",
        ],
        "solutions": [
            "Check COMSOL License Manager status",
            "Verify license server connection",
            "Ensure required modules are licensed",
            "Contact administrator for license issues",
        ],
    },
}

BEST_PRACTICES = {
    "geometry": {
        "tips": [
            "Start with simplified geometry and add complexity as needed",
            "Use parameters for all dimensions to enable parametric studies",
            "Import CAD with appropriate import settings",
            "Use work planes for complex 2D-in-3D geometries",
            "Check geometry after each major operation",
        ],
        "common_mistakes": [
            "Creating geometry that is too complex initially",
            "Not using parameters for key dimensions",
            "Overlapping objects that cause mesh issues",
        ],
    },
    "mesh": {
        "tips": [
            "Start with a coarse mesh and refine as needed",
            "Use finer mesh in regions with high gradients",
            "Use boundary layer meshing for fluid flow near walls",
            "Consider swept mesh for prismatic geometries",
            "Check mesh quality statistics before solving",
        ],
        "common_mistakes": [
            "Using unnecessarily fine mesh everywhere",
            "Ignoring mesh quality metrics",
            "Not adapting mesh to solution features",
        ],
    },
    "physics": {
        "tips": [
            "Start with the simplest physics that captures the phenomena",
            "Apply boundary conditions to the smallest necessary selection",
            "Verify material properties are correctly defined",
            "Use consistent units throughout the model",
            "Add multiphysics couplings after individual physics work",
        ],
        "common_mistakes": [
            "Over-constraining the problem",
            "Missing essential boundary conditions",
            "Using incorrect material properties",
        ],
    },
    "solver": {
        "tips": [
            "Start with default solver settings",
            "Use parametric sweep for design exploration",
            "Monitor solver progress for convergence issues",
            "Save results frequently for long simulations",
            "Use appropriate study type for the physics",
        ],
        "common_mistakes": [
            "Changing solver settings without understanding effects",
            "Not checking solution convergence",
            "Using wrong study type for time-dependent problems",
        ],
    },
    "results": {
        "tips": [
            "Use derived values for common quantities",
            "Create probe plots for monitoring specific points",
            "Export data in appropriate format for post-processing",
            "Use table joins for combining multiple datasets",
            "Create reusable plot templates",
        ],
        "common_mistakes": [
            "Not verifying results with analytical solutions when possible",
            "Drawing conclusions from non-converged solutions",
            "Not exporting results before closing model",
        ],
    },
}


def _load_knowledge_file(name: str) -> str:
    """Load content from a knowledge file."""
    if name not in KNOWLEDGE_FILES:
        return ""
    
    file_path = KNOWLEDGE_DIR / KNOWLEDGE_FILES[name]["file"]
    if not file_path.exists():
        return ""
    
    return file_path.read_text(encoding="utf-8")


# Module-level functions for testing and direct use
def get_docs(topic: str) -> dict:
    """Get documentation on a specific topic."""
    if topic not in KNOWLEDGE_FILES:
        available = list(KNOWLEDGE_FILES.keys())
        return {
            "success": False,
            "error": f"Unknown topic: {topic}",
            "available_topics": available,
        }
    
    content = _load_knowledge_file(topic)
    if not content:
        return {
            "success": False,
            "error": f"Could not load documentation for: {topic}",
        }
    
    info = KNOWLEDGE_FILES[topic]
    return {
        "success": True,
        "topic": topic,
        "title": info["title"],
        "description": info["description"],
        "content": content,
    }


def list_docs() -> dict:
    """List all available documentation topics."""
    topics = []
    for name, info in KNOWLEDGE_FILES.items():
        topics.append({
            "name": name,
            "title": info["title"],
            "description": info["description"],
            "keywords": info["keywords"],
        })
    
    return {
        "success": True,
        "topics": topics,
        "count": len(topics),
    }


def get_physics_guide(physics_type: str) -> dict:
    """Get a quick guide for a specific physics type."""
    if physics_type not in TOPIC_GUIDES:
        available = list(TOPIC_GUIDES.keys())
        return {
            "success": False,
            "error": f"Unknown physics type: {physics_type}",
            "available_types": available,
        }
    
    guide = TOPIC_GUIDES[physics_type]
    
    return {
        "success": True,
        "physics_type": physics_type,
        "guide": {
            "tool_to_add": f"physics_add_{guide['physics']}",
            "common_boundary_conditions": guide["boundary_conditions"],
            "common_expressions": guide["common_expressions"],
            "tips": guide["tips"],
        },
    }


def get_troubleshoot(error_type: str, context: Optional[str] = None) -> dict:
    """Get troubleshooting suggestions for common issues."""
    if error_type not in TROUBLESHOOTING:
        available = list(TROUBLESHOOTING.keys())
        return {
            "success": False,
            "error": f"Unknown error type: {error_type}",
            "available_types": available,
        }
    
    info = TROUBLESHOOTING[error_type]
    
    return {
        "success": True,
        "error_type": error_type,
        "context": context,
        "causes": info["causes"],
        "solutions": info["solutions"],
    }


def get_best_practices(category: str) -> dict:
    """Get best practices for different modeling categories."""
    if category not in BEST_PRACTICES:
        available = list(BEST_PRACTICES.keys())
        return {
            "success": False,
            "error": f"Unknown category: {category}",
            "available_categories": available,
        }
    
    return {
        "success": True,
        "category": category,
        "best_practices": BEST_PRACTICES[category],
    }


# Module-level PDF search functions for direct import and testing


def register_knowledge_tools(mcp: FastMCP) -> None:
    """Register knowledge base tools with the MCP server."""
    
    @mcp.tool()
    def docs_get(topic: str) -> dict:
        """
        Get documentation on a specific topic.
        
        Available topics:
        - "mph_api": MPh Python API reference
        - "physics_guide": Physics interfaces and boundary conditions
        - "workflow": Step-by-step modeling workflows
        
        Args:
            topic: Documentation topic to retrieve
        
        Returns:
            Documentation content for the topic
        """
        return get_docs(topic)
    
    @mcp.tool()
    def docs_list() -> dict:
        """
        List all available documentation topics.
        
        Returns:
            List of available documentation topics with descriptions
        """
        return list_docs()
    
    @mcp.tool()
    def physics_get_guide(physics_type: str) -> dict:
        """
        Get a quick guide for a specific physics type.
        
        Available physics types:
        - "electrostatics": Electric field and capacitance
        - "heat_transfer": Thermal analysis
        - "solid_mechanics": Stress and deformation
        - "fluid_flow": CFD analysis
        
        Args:
            physics_type: Type of physics to get guide for
        
        Returns:
            Quick reference guide for the physics type
        """
        return get_physics_guide(physics_type)
    
    @mcp.tool()
    def troubleshoot(error_type: str, context: Optional[str] = None) -> dict:
        """
        Get troubleshooting suggestions for common issues.
        
        Common error types:
        - "geometry_build_failed": Geometry sequence failed to build
        - "mesh_failed": Mesh generation failed
        - "solver_no_convergence": Solver did not converge
        - "memory_error": Out of memory
        - "license_error": COMSOL license issues
        
        Args:
            error_type: Type of error encountered
            context: Additional context about the error
        
        Returns:
            Troubleshooting suggestions
        """
        return get_troubleshoot(error_type, context)
    
    @mcp.tool()
    def modeling_best_practices(category: str) -> dict:
        """
        Get best practices for different modeling categories.
        
        Categories:
        - "geometry": Geometry creation and import
        - "mesh": Mesh generation strategies
        - "physics": Physics interface configuration
        - "solver": Solver configuration and optimization
        - "results": Results evaluation and visualization
        
        Args:
            category: Category to get best practices for
        
        Returns:
            Best practices for the specified category
        """
        return get_best_practices(category)
    
    
