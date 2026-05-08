"""Model management tools for COMSOL MCP Server."""

import os
import tempfile
from typing import Optional
from pathlib import Path
from mcp.server.fastmcp import FastMCP
import mph

from .session import session_manager
from ..utils.versioning import (
    generate_version_path,
    generate_latest_path,
    parse_version_info,
    list_model_versions,
    get_model_directory,
    MODELS_BASE_DIR
)


# space_dim_kind alias map (spec geometry_axisymmetric_support_spec.md §3.2).
# Canonical user-facing name -> COMSOL Java SDimSpec tag string.
_SPACE_DIM_KIND_TO_JAVA = {
    "1D":              "SpaceDim1D",
    "1D-Cartesian":    "SpaceDim1D",
    "1D-Axisymmetric": "AxisymmetricSpaceDim1DAxi",
    "1DAxi":           "AxisymmetricSpaceDim1DAxi",
    "2D":              "SpaceDim2D",
    "2D-Cartesian":    "SpaceDim2D",
    "2D-Axisymmetric": "AxisymmetricSpaceDim2DAxi",
    "2DAxi":           "AxisymmetricSpaceDim2DAxi",
    "3D":              "SpaceDim3D",
}

_SPACE_DIM_KIND_TO_INT = {
    "1D":              1,
    "1D-Cartesian":    1,
    "1D-Axisymmetric": 1,
    "1DAxi":           1,
    "2D":              2,
    "2D-Cartesian":    2,
    "2D-Axisymmetric": 2,
    "2DAxi":           2,
    "3D":              3,
}

# Reverse map for inspection: Java SDimSpec tag -> canonical kind.
_JAVA_TO_SPACE_DIM_KIND = {
    "SpaceDim1D":                "1D",
    "AxisymmetricSpaceDim1DAxi": "1D-Axisymmetric",
    "SpaceDim2D":                "2D",
    "AxisymmetricSpaceDim2DAxi": "2D-Axisymmetric",
    "SpaceDim3D":                "3D",
}


def _normalize_space_dim_kind(s: str) -> Optional[str]:
    """Normalize a user-supplied space dim kind string to a canonical key.

    Tolerant to case, whitespace, hyphen/underscore, and 'axisym'/'axi'.
    Returns None on unrecognized input.
    """
    if not isinstance(s, str) or not s.strip():
        return None

    norm = s.strip().lower().replace("_", "-").replace(" ", "-")
    while "--" in norm:
        norm = norm.replace("--", "-")

    # Normalize "axisym"/"axisymmetric"/"axi" tokens to canonical "axisymmetric"
    # but only as a postfix after a digit-d token.
    canonical_map = {
        "1d":               "1D",
        "1d-cartesian":     "1D",
        "1d-axisymmetric":  "1D-Axisymmetric",
        "1d-axisym":        "1D-Axisymmetric",
        "1d-axi":           "1D-Axisymmetric",
        "1daxi":            "1DAxi",
        "1daxisymmetric":   "1D-Axisymmetric",
        "2d":               "2D",
        "2d-cartesian":     "2D",
        "2d-axisymmetric":  "2D-Axisymmetric",
        "2d-axisym":        "2D-Axisymmetric",
        "2d-axi":           "2D-Axisymmetric",
        "2daxi":            "2DAxi",
        "2daxisymmetric":   "2D-Axisymmetric",
        "3d":               "3D",
        "3d-cartesian":     "3D",
    }
    return canonical_map.get(norm)


def _supported_space_dim_kinds() -> list:
    """List the canonical space_dim_kind names users can pass."""
    return ["1D", "1D-Axisymmetric", "2D", "2D-Axisymmetric", "3D"]


def register_model_tools(mcp: FastMCP) -> None:
    """Register model management tools with the MCP server."""
    
    @mcp.tool()
    def model_load(file_path: str, set_current: bool = True) -> dict:
        """
        Load a COMSOL model from a .mph file.
        
        Args:
            file_path: Absolute or relative path to the .mph model file
            set_current: Whether to set this as the current active model (default: True)
        
        Returns:
            Model info including name, file path, and version, or error message
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session. Start with comsol_start first."}
        
        client = session_manager.client
        if client is None:
            return {"success": False, "error": "Client not available."}
        
        try:
            path = Path(file_path)
            if not path.exists():
                return {"success": False, "error": f"File not found: {file_path}"}
            if not path.suffix.lower() == ".mph":
                return {"success": False, "error": f"File must be a .mph file: {file_path}"}
            
            model = client.load(str(path.absolute()))
            name = session_manager.add_model(model)
            
            if set_current:
                session_manager.set_current_model(name)
            
            version_info = parse_version_info(name)
            
            return {
                "success": True,
                "model": {
                    "name": name,
                    "file": str(path.absolute()),
                    "comsol_version": model.version(),
                    "is_versioned": version_info is not None,
                    "version_info": version_info,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to load model: {str(e)}"}
    
    @mcp.tool()
    def model_create(name: Optional[str] = None, set_current: bool = True) -> dict:
        """
        Create a new empty COMSOL model.
        
        Args:
            name: Optional name for the model (auto-generated if not provided)
            set_current: Whether to set this as the current active model (default: True)
        
        Returns:
            Model info including name, or error message
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session. Start with comsol_start first."}
        
        client = session_manager.client
        if client is None:
            return {"success": False, "error": "Client not available."}
        
        try:
            model = client.create(name)
            model_name = session_manager.add_model(model)
            
            if set_current:
                session_manager.set_current_model(model_name)
            
            return {
                "success": True,
                "model": {
                    "name": model_name,
                    "is_new": True,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create model: {str(e)}"}
    
    @mcp.tool()
    def model_create_component(
        component_name: str = "comp1",
        space_dim_kind: str = "3D",
        set_active: bool = True,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Create a component in the model (required before adding geometry/physics).

        Components are containers for geometry, physics, materials, and mesh.
        Must be created before adding geometry or physics.

        Args:
            component_name: Name for the component (default: 'comp1')
            space_dim_kind: Space dimension kind. One of:
                "1D" / "1D-Cartesian"
                "1D-Axisymmetric" / "1DAxi"
                "2D" / "2D-Cartesian"
                "2D-Axisymmetric" / "2DAxi"
                "3D" (default — preserves prior behavior)
                Case-insensitive, hyphen/space/underscore tolerant.
            set_active: Make this the active component (default: True;
                preserves prior behavior).
            model_name: Model name (default: current model)

        Returns:
            Created component info including normalized space_dim_kind,
            corresponding Java SDimSpec tag, and integer space dimension.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        canonical = _normalize_space_dim_kind(space_dim_kind)
        if canonical is None:
            return {
                "success": False,
                "error": (
                    f"Invalid space_dim_kind: '{space_dim_kind}'. "
                    f"Supported: {_supported_space_dim_kinds()}"
                ),
            }

        java_kind = _SPACE_DIM_KIND_TO_JAVA[canonical]
        sdim_int = _SPACE_DIM_KIND_TO_INT[canonical]

        try:
            jm = model.java
            # Default 3D (canonical=="3D") path: keep the legacy two-arg
            # (tag, active) overload to preserve byte-identical behavior
            # for prior callers that did not pass space_dim_kind.
            if canonical == "3D":
                comp = jm.component().create(component_name, bool(set_active))
            else:
                # Try the (tag, active, kind) three-arg overload first;
                # fall back to (tag, kind) if the JVM rejects it.
                try:
                    comp = jm.component().create(
                        component_name, bool(set_active), java_kind
                    )
                except Exception:
                    comp = jm.component().create(component_name, java_kind)

            return {
                "success": True,
                "component": {
                    "tag": component_name,
                    "space_dim_kind": canonical,
                    "java_kind_string": java_kind,
                    "space_dimension_int": sdim_int,
                    "set_active": bool(set_active),
                },
                "model": model.name(),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create component: {str(e)}"}
    
    @mcp.tool()
    def model_list_components(
        model_name: Optional[str] = None
    ) -> dict:
        """
        List all components in a model.
        
        Args:
            model_name: Model name (default: current model)
        
        Returns:
            List of component names
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            jm = model.java
            components = []
            
            for i in range(jm.component().size()):
                comp = jm.component().get(i)
                if comp is not None:
                    components.append({
                        "name": comp.tag(),
                        "label": comp.label() if hasattr(comp, 'label') else comp.tag()
                    })
            
            return {
                "success": True,
                "components": components,
                "count": len(components),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list components: {str(e)}"}
    
    @mcp.tool()
    def model_save(
        model_name: Optional[str] = None,
        file_path: Optional[str] = None,
        format: Optional[str] = None
    ) -> dict:
        """
        Save a COMSOL model to file.
        
        Args:
            model_name: Name of the model to save (default: current model)
            file_path: Path to save to (default: original file path)
            format: Save format - 'Comsol', 'Java', 'Matlab', or 'VBA' (default: Comsol/.mph)
        
        Returns:
            Save confirmation with file path, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            model.save(path=file_path, format=format)
            saved_path = file_path or model.file()
            
            return {
                "success": True,
                "model": model.name(),
                "saved_to": str(saved_path),
                "format": format or "Comsol",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to save model: {str(e)}"}
    
    @mcp.tool()
    def model_save_version(
        model_name: Optional[str] = None,
        description: Optional[str] = None
    ) -> dict:
        """
        Save a model with a timestamp version suffix.
        
        Creates a new file with structured path: 
        ./comsol_models/{model_name}/{model_name}_{timestamp}.mph
        
        Also saves a 'latest' copy: 
        ./comsol_models/{model_name}/{model_name}_latest.mph
        
        Useful for version control and design iterations.
        
        Args:
            model_name: Name of the model to save (default: current model)
            description: Optional description for this version (stored in metadata)
        
        Returns:
            Save confirmation with versioned file path, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            # Get model name for directory structure
            name = model.name()
            
            # Generate versioned path using new structure
            versioned_path = generate_version_path(name)
            
            # Save versioned copy
            model.save(path=versioned_path)
            
            # Also save as 'latest'
            latest_path = generate_latest_path(name)
            model.save(path=latest_path)
            
            return {
                "success": True,
                "model": name,
                "version_path": versioned_path,
                "latest_path": latest_path,
                "description": description,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to save version: {str(e)}"}
    
    @mcp.tool()
    def model_list() -> dict:
        """
        List all models currently loaded in the COMSOL session.
        
        Returns:
            List of models with their names, file paths, and status
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session."}
        
        models = session_manager.models
        current = session_manager.current_model
        
        model_list = []
        for name, model in models.items():
            info = {
                "name": name,
                "is_current": name == current,
            }
            try:
                info["file"] = model.file()
                info["comsol_version"] = model.version()
            except Exception:
                pass
            model_list.append(info)
        
        return {
            "success": True,
            "models": model_list,
            "count": len(model_list),
            "current_model": current,
        }
    
    @mcp.tool()
    def model_set_current(model_name: str) -> dict:
        """
        Set the current active model for subsequent operations.
        
        Args:
            model_name: Name of the model to set as current
        
        Returns:
            Confirmation or error message
        """
        if session_manager.set_current_model(model_name):
            return {
                "success": True,
                "current_model": model_name,
            }
        return {
            "success": False,
            "error": f"Model not found: {model_name}"
        }
    
    @mcp.tool()
    def model_clone(
        model_name: Optional[str] = None,
        new_name: Optional[str] = None,
        set_current: bool = False
    ) -> dict:
        """
        Clone a model to create a copy for comparison or modification.
        
        Args:
            model_name: Name of the model to clone (default: current model)
            new_name: Name for the cloned model (auto-generated if not provided)
            set_current: Whether to set the clone as current model (default: False)
        
        Returns:
            Info about the cloned model, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        client = session_manager.client
        if client is None:
            return {"success": False, "error": "Client not available."}

        # COMSOL 6.1 ModelImpl has no createCopy(). Use a temp save/load
        # round-trip — works on every COMSOL version, at the cost of one
        # disk I/O per clone. Spec: mcp_model_clone_bugfix_spec.md candidate B.
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".mph", delete=False
            ) as tmp:
                tmp_path = tmp.name

            model.save(path=tmp_path)
            cloned_model = client.load(tmp_path)
            clone_name = session_manager.add_model(cloned_model)

            if new_name:
                try:
                    cloned_model.java.label(new_name)
                except Exception:
                    pass

            if set_current:
                session_manager.set_current_model(clone_name)

            return {
                "success": True,
                "original": model.name(),
                "clone": clone_name,
                "is_current": clone_name == session_manager.current_model,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to clone model: {str(e)}"}
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    
    @mcp.tool()
    def model_remove(model_name: str) -> dict:
        """
        Remove a model from memory.
        
        Args:
            model_name: Name of the model to remove
        
        Returns:
            Confirmation or error message
        """
        if model_name == session_manager.current_model:
            new_current = None
            for name in session_manager.models.keys():
                if name != model_name:
                    new_current = name
                    break
        
        if session_manager.remove_model(model_name):
            return {
                "success": True,
                "removed": model_name,
                "current_model": session_manager.current_model,
            }
        return {
            "success": False,
            "error": f"Failed to remove model: {model_name}"
        }
    
    @mcp.tool()
    def model_inspect(model_name: Optional[str] = None) -> dict:
        """
        Get detailed information about a model's structure and contents.
        
        Args:
            model_name: Name of the model to inspect (default: current model)
        
        Returns:
            Detailed model structure including parameters, physics, studies, etc.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            info = {
                "name": model.name(),
                "file": model.file(),
                "comsol_version": model.version(),
                "parameters": dict(model.parameters()) if model.parameters() else {},
                "functions": model.functions(),
                "components": model.components(),
                "geometries": model.geometries(),
                "selections": model.selections(),
                "physics": model.physics(),
                "multiphysics": model.multiphysics(),
                "materials": model.materials(),
                "meshes": model.meshes(),
                "studies": model.studies(),
                "solutions": model.solutions(),
                "datasets": model.datasets(),
                "plots": model.plots(),
                "exports": model.exports(),
                "modules": model.modules(),
            }
            
            problems = model.problems()
            if problems:
                info["problems"] = problems
            
            return {"success": True, "model": info}
        except Exception as e:
            return {"success": False, "error": f"Failed to inspect model: {str(e)}"}
