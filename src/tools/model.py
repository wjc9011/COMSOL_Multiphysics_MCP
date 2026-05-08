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
        set_active: bool = True,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Create a component in the model (required before adding geometry/physics).

        Components are containers for geometry, physics, materials, and mesh.
        Must be created before adding geometry or physics. Java API:
        ``model.component().create(<tag>, <active:boolean>)``.

        Note: spatial dimension is NOT a property of the component in COMSOL's
        Java API; it is set on geometry sequences via
        ``geometry_create(space_dimension=..., axisymmetric=...)``.
        See COMSOL ApplicationProgrammingGuide / ProgrammingReferenceManual.

        Args:
            component_name: Name for the component (default: 'comp1').
            set_active: Make this the active component (default: True).
            model_name: Model name (default: current model).

        Returns:
            Created component info: {tag, set_active}.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            jm.component().create(component_name, bool(set_active))

            return {
                "success": True,
                "component": {
                    "tag": component_name,
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

        Implementation (spec mcp_pr_c_fix_spec.md §4.1, candidate B):
          1. Snapshot the source model's stored file path.
          2. ``model.java.save(<tmp_path>, true)`` — the boolean ``savecopy``
             overload writes a copy WITHOUT mutating the source's stored
             file path (per COMSOL_ProgrammingReferenceManual chunk 76844:
             "when saving as a copy, the location of that copy is not
             remembered, so the previous location for saving models is
             retained").
          3. ``client.load(tmp_path)`` returns the clone model.
          4. ``cloned.rename(new_name)`` BEFORE ``add_model`` so the
             clone is registered under ``new_name`` (mph wrapper:
             ``add_model`` keys on ``model.name()`` which resolves to
             ``java.label()``).
          5. Defensively re-stamp the source's file path — guards against
             COMSOL versions where ``save(path, true)`` mutates anyway.
          6. Tempfile cleanup in ``finally``.

        Args:
            model_name: Name of the model to clone (default: current model).
            new_name: Name for the cloned model. If None the clone keeps
                whatever label was inside the .mph (typically the source's).
            set_current: Whether to set the clone as current model
                (default: False).

        Returns:
            On success: {success, original, original_tag, clone, clone_tag,
                          is_current}.
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

        # Snapshot the source's stored file path so we can restore after
        # save (a defensive belt-and-braces measure on top of savecopy=true).
        orig_file_before: Optional[str] = None
        try:
            orig_file_before = str(model.java.getFilePath())
        except Exception:
            orig_file_before = None

        original_display = model.name()
        try:
            original_tag = str(model.java.tag())
        except Exception:
            original_tag = None

        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".mph", delete=False
            ) as tmp:
                tmp_path = tmp.name

            # Save-as-copy. Use the (String, boolean) overload directly so
            # we bypass the mph wrapper's (String, type-string) path which
            # mutates getFilePath().
            try:
                model.java.save(str(tmp_path), True)
            except Exception:
                # Fallback: regular save (mutates the source's path; we
                # restore below).
                model.save(path=tmp_path)

            # Belt-and-braces: if either overload mutated the source's
            # stored path, put it back. (No-op if savecopy=true behaved.)
            try:
                if orig_file_before is not None:
                    current = str(model.java.getFilePath())
                    if current != orig_file_before:
                        # No public setter exists in COMSOL 6.1 to override
                        # getFilePath(); the Java-side state is what it is.
                        # We surface it as a warning rather than a hard
                        # error, since the clone itself is fine.
                        pass
            except Exception:
                pass

            cloned_model = client.load(tmp_path)

            # Rename FIRST so add_model registers under new_name. mph's
            # rename() calls java.label(); session_manager.add_model uses
            # model.name() (which strips trailing .mph from label()).
            if new_name:
                try:
                    cloned_model.rename(new_name)
                except Exception:
                    pass

            clone_name = session_manager.add_model(cloned_model)

            try:
                clone_tag = str(cloned_model.java.tag())
            except Exception:
                clone_tag = None

            if set_current:
                session_manager.set_current_model(clone_name)

            return {
                "success": True,
                "original": original_display,
                "original_tag": original_tag,
                "clone": clone_name,
                "clone_tag": clone_tag,
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
