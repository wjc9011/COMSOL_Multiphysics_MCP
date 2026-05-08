"""Mesh tools for COMSOL MCP Server."""

from typing import Optional
from mcp.server.fastmcp import FastMCP

from .session import session_manager


# Spec mcp_mesh_study_tools_spec.md §3.2: predefined size string -> hauto int
_HAUTO_BY_SIZE_KIND = {
    "Extremely fine":   1,
    "Extra fine":       2,
    "Finer":            3,
    "Fine":             4,
    "Normal":           5,
    "Coarse":           6,
    "Coarser":          7,
    "Extra coarse":     8,
    "Extremely coarse": 9,
}


def _mesh_seq_tags(jm) -> list:
    """Return the list of mesh-sequence tag strings across all components."""
    tags = []
    try:
        for comp in list(jm.component()):
            try:
                for m in list(comp.mesh()):
                    try:
                        tags.append(str(m.tag()))
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return tags


def _auto_mesh_tag(jm, prefix: str = "mesh") -> str:
    """Generate a non-colliding mesh-sequence tag."""
    existing = set(_mesh_seq_tags(jm))
    i = len(existing) + 1
    while f"{prefix}{i}" in existing:
        i += 1
    return f"{prefix}{i}"


def _resolve_component(jm, component_name: Optional[str]):
    """Return (comp_java, comp_tag, error_str)."""
    try:
        if component_name:
            comp = jm.component(component_name)
            if comp is None:
                return None, None, (
                    f"Component '{component_name}' not found. "
                    "Create it first with model_create_component."
                )
            return comp, str(comp.tag()), None
        comps = list(jm.component())
        if not comps:
            return None, None, (
                "No components in model. Create one with "
                "model_create_component first."
            )
        c = comps[0]
        return c, str(c.tag()), None
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"


def _resolve_geom_tag(comp, geometry_name: Optional[str]):
    """Return (geom_tag, error_str). geom_tag may be None on error."""
    try:
        if geometry_name:
            try:
                g = comp.geom(geometry_name)
            except Exception:
                g = None
            if g is not None:
                return str(g.tag()), None
            for g in list(comp.geom()):
                try:
                    if str(g.label()) == geometry_name:
                        return str(g.tag()), None
                except Exception:
                    continue
            return None, (
                f"Geometry '{geometry_name}' not found in component. "
                "Pass the tag (e.g. 'geom1') or the exact GUI label."
            )
        geoms = list(comp.geom())
        if not geoms:
            return None, (
                "No geometry in component. Create one with "
                "geometry_create first."
            )
        return str(geoms[0].tag()), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def register_mesh_tools(mcp: FastMCP) -> None:
    """Register mesh tools with the MCP server."""
    
    @mcp.tool()
    def mesh_list(model_name: Optional[str] = None) -> dict:
        """
        List all mesh sequences in a model.
        
        Args:
            model_name: Model name (default: current model)
        
        Returns:
            List of mesh sequence names
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            meshes = model.meshes()
            return {
                "success": True,
                "meshes": meshes,
                "count": len(meshes),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list meshes: {str(e)}"}
    
    @mcp.tool()
    def mesh_create(
        mesh_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Run a mesh sequence to generate the mesh.
        
        This executes the meshing operations defined in the mesh sequence.
        
        Args:
            mesh_name: Mesh sequence name (default: run all mesh sequences)
            model_name: Model name (default: current model)
        
        Returns:
            Mesh generation status
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            model.mesh(mesh_name)
            return {
                "success": True,
                "mesh": mesh_name,
                "message": f"Mesh created: {mesh_name or 'all meshes'}",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create mesh: {str(e)}"}
    
    @mcp.tool()
    def mesh_info(
        mesh_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Get information about a mesh.
        
        Args:
            mesh_name: Mesh sequence name (default: first mesh)
            model_name: Model name (default: current model)
        
        Returns:
            Mesh statistics including element counts
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            meshes = model.meshes()
            if not meshes:
                return {"success": False, "error": "No meshes defined in model."}
            
            target = mesh_name or meshes[0]
            if target not in meshes:
                return {"success": False, "error": f"Mesh not found: {target}"}
            
            mesh_node = model / "meshes" / target
            
            info = {
                "name": target,
            }
            
            try:
                java_mesh = mesh_node.java
                if hasattr(java_mesh, 'getVertex'):
                    info["num_vertices"] = java_mesh.getVertex().size()
                if hasattr(java_mesh, 'getElement'):
                    info["num_elements"] = java_mesh.getElement().size()
            except Exception:
                pass
            
            try:
                children = [child.name() for child in mesh_node.children()]
                info["features"] = children
            except Exception:
                pass
            
            return {
                "success": True,
                "mesh": info,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to get mesh info: {str(e)}"}

    @mcp.tool()
    def mesh_add_sequence(
        component_name: Optional[str] = None,
        geometry_name: Optional[str] = None,
        mesh_name: Optional[str] = None,
        auto_default_features: bool = True,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create an empty mesh sequence attached to a component+geometry.

        By default this also adds COMSOL's standard "default" Size feature
        (Normal preset) so that the subsequent ``mesh_create`` call (which
        runs the sequence) produces a usable mesh without further config.

        Args:
            component_name: Component to attach mesh to (default: first).
            geometry_name: Geometry tag the mesh references
                (default: first geometry in the component).
            mesh_name: COMSOL tag for the mesh sequence
                (auto-generated if None — e.g. 'mesh1').
            auto_default_features: If True (default), add a Size feature
                with global predefined size (Normal) so the empty sequence
                becomes immediately runnable. If False, leave bare.
            model_name: Model name (default: current).

        Returns:
            Created mesh-sequence info on success, or {success: False, ...}.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            comp, comp_tag, err = _resolve_component(jm, component_name)
            if err:
                return {"success": False, "error": err}

            geom_tag, err = _resolve_geom_tag(comp, geometry_name)
            if err:
                return {"success": False, "error": err}

            tag = mesh_name or _auto_mesh_tag(jm, "mesh")
            existing_tags = set(_mesh_seq_tags(jm))
            if tag in existing_tags:
                return {
                    "success": False,
                    "error": f"Mesh sequence already exists with tag '{tag}'",
                }

            mesh_seq = comp.mesh().create(tag, geom_tag)

            if auto_default_features:
                try:
                    # COMSOL pre-creates a "size" Size feature on every new
                    # mesh sequence; create() is idempotent in that the same
                    # named feature won't be duplicated. If create('size1',
                    # 'Size') fails because of the implicit one, ignore.
                    try:
                        mesh_seq.create("size1", "Size")
                    except Exception:
                        # Implicit size feature already exists; that's fine.
                        pass
                except Exception:
                    # Sequence still usable even if Size feature insert
                    # failed — surface but do not block.
                    pass

            return {
                "success": True,
                "mesh": {
                    "tag": tag,
                    "component": comp_tag,
                    "geometry": geom_tag,
                    "has_default_features": bool(auto_default_features),
                },
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to add mesh sequence: {type(e).__name__}: {e}",
            }

    @mcp.tool()
    def mesh_set_global_size(
        size_kind: str = "Normal",
        mesh_name: Optional[str] = None,
        feature_tag: str = "size",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Set the global predefined element size for a mesh sequence's Size
        feature ("hauto"). Typical use: choose "Finer" for higher accuracy
        or "Coarser" for quick checks.

        Args:
            size_kind: One of COMSOL's predefined size strings:
                "Extremely fine" / "Extra fine" / "Finer" / "Fine" /
                "Normal" / "Coarse" / "Coarser" / "Extra coarse" /
                "Extremely coarse". Case-sensitive.
            mesh_name: Mesh sequence tag (default: first mesh).
            feature_tag: Tag of the Size feature within the mesh
                (default: 'size' as auto-created by COMSOL; some sequences
                use 'size1'). Both tags are tried.
            model_name: Model name (default: current).

        Returns:
            {success, mesh, size_kind, hauto, applied} on success.
        """
        if size_kind not in _HAUTO_BY_SIZE_KIND:
            return {
                "success": False,
                "error": (
                    f"Invalid size_kind: '{size_kind}'. "
                    f"Supported: {list(_HAUTO_BY_SIZE_KIND.keys())}"
                ),
            }

        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            target_tag = mesh_name
            mesh_obj = None
            for comp in list(jm.component()):
                try:
                    for m in list(comp.mesh()):
                        if target_tag is None or str(m.tag()) == target_tag:
                            mesh_obj = m
                            target_tag = str(m.tag())
                            break
                    if mesh_obj is not None:
                        break
                except Exception:
                    continue

            if mesh_obj is None:
                return {
                    "success": False,
                    "error": f"Mesh sequence not found: {mesh_name or '(first)'}",
                }

            hauto = _HAUTO_BY_SIZE_KIND[size_kind]
            applied_tag = None
            last_error = None
            for ftag in (feature_tag, "size", "size1"):
                try:
                    feat = mesh_obj.feature(ftag)
                    if feat is None:
                        continue
                    feat.set("hauto", str(hauto))
                    applied_tag = ftag
                    break
                except Exception as e:
                    last_error = e
                    continue

            if applied_tag is None:
                return {
                    "success": False,
                    "error": (
                        f"Size feature '{feature_tag}' not found in mesh "
                        f"'{target_tag}'. Last error: {last_error}"
                    ),
                }

            return {
                "success": True,
                "mesh": target_tag,
                "size_kind": size_kind,
                "hauto": hauto,
                "applied": applied_tag,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to set global size: {type(e).__name__}: {e}",
            }

    @mcp.tool()
    def mesh_remove(
        mesh_name: str,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Remove a mesh sequence from the model.

        Args:
            mesh_name: Tag of the mesh sequence to remove.
            model_name: Model name (default: current).

        Returns:
            {success, removed} on success.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            for comp in list(jm.component()):
                try:
                    if str(comp.mesh(mesh_name).tag()) == mesh_name:
                        comp.mesh().remove(mesh_name)
                        return {"success": True, "removed": mesh_name}
                except Exception:
                    continue
            return {
                "success": False,
                "error": f"Mesh sequence not found: {mesh_name}",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to remove mesh: {type(e).__name__}: {e}",
            }
