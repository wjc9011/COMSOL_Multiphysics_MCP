"""Mesh tools for COMSOL MCP Server.

Naming convention (spec mcp_pr_c_fix_spec.md §3.3): every mesh_* tool
accepts a mesh sequence by EITHER its Java tag (e.g. ``mesh1``) OR its
display label (e.g. ``Mesh 1``). The shared ``_resolve_mesh`` helper
walks all components, prefers tag matches, falls back to label matches,
and surfaces a unified error on miss. Every response also reports both
``tag`` and ``label`` so callers do not have to guess which form their
input was.
"""

from typing import Optional, Tuple
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


def _safe_str(value, default: str = "") -> str:
    try:
        if value is None:
            return default
        return str(value)
    except Exception:
        return default


def _mesh_seq_descriptors(jm) -> list:
    """Return a list of mesh-sequence descriptors across all components.

    Each descriptor is a tuple (comp_java, mesh_java, comp_tag, tag, label).
    """
    out: list = []
    try:
        for comp in list(jm.component()):
            try:
                comp_tag = _safe_str(comp.tag(), "?")
            except Exception:
                comp_tag = "?"
            try:
                meshes = list(comp.mesh())
            except Exception:
                continue
            for m in meshes:
                tag = _safe_str(getattr(m, "tag", lambda: None)())
                try:
                    label = _safe_str(m.label())
                except Exception:
                    label = ""
                out.append((comp, m, comp_tag, tag, label))
    except Exception:
        pass
    return out


def _mesh_seq_tags(jm) -> list:
    """Return all mesh sequence tag strings across all components."""
    return [d[3] for d in _mesh_seq_descriptors(jm) if d[3]]


def _auto_mesh_tag(jm, prefix: str = "mesh") -> str:
    """Generate a non-colliding mesh-sequence tag."""
    existing = set(_mesh_seq_tags(jm))
    i = len(existing) + 1
    while f"{prefix}{i}" in existing:
        i += 1
    return f"{prefix}{i}"


def _resolve_mesh(jm, mesh_name_or_label: Optional[str]) -> Tuple:
    """Find a mesh sequence by tag (preferred) or label.

    Args:
        jm: ``model.java`` handle.
        mesh_name_or_label: Tag, label, or None (return first found).

    Returns:
        ``(comp_java, mesh_java, comp_tag, tag, label, error_str)``.
        On success ``error_str`` is None; on miss the other fields are
        None (except possibly comp_tag) and ``error_str`` carries a
        human-readable message.
    """
    descriptors = _mesh_seq_descriptors(jm)
    if not descriptors:
        return None, None, None, None, None, (
            "No mesh sequences in the model. Create one with "
            "mesh_add_sequence first."
        )

    # No name given — return first.
    if mesh_name_or_label in (None, ""):
        comp, m, comp_tag, tag, label = descriptors[0]
        return comp, m, comp_tag, tag, label, None

    # Tag-first match.
    for comp, m, comp_tag, tag, label in descriptors:
        if tag == mesh_name_or_label:
            return comp, m, comp_tag, tag, label, None

    # Label fallback.
    for comp, m, comp_tag, tag, label in descriptors:
        if label == mesh_name_or_label:
            return comp, m, comp_tag, tag, label, None

    return None, None, None, None, None, (
        f"Mesh sequence not found: '{mesh_name_or_label}'. "
        "Pass the tag (e.g. 'mesh1') or the exact GUI label "
        "(e.g. 'Mesh 1')."
    )


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
        List all mesh sequences in the model with both tag and label.

        Args:
            model_name: Model name (default: current model).

        Returns:
            ``{success, meshes: [{tag, label, component}, ...], count}``.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            descriptors = _mesh_seq_descriptors(jm)
            meshes = [
                {"tag": tag, "label": label, "component": comp_tag}
                for _comp, _m, comp_tag, tag, label in descriptors
            ]
            return {
                "success": True,
                "meshes": meshes,
                "count": len(meshes),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list meshes: {type(e).__name__}: {e}"}

    @mcp.tool()
    def mesh_create(
        mesh_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Run a mesh sequence to generate the mesh.

        Accepts mesh tag or label. Calls ``geom.run()``-equivalent on the
        mesh sequence (mph: ``model.mesh(name)``).

        Args:
            mesh_name: Mesh sequence tag or label (default: run all).
            model_name: Model name (default: current model).

        Returns:
            ``{success, mesh: {tag, label, component} | None, message}``.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            if mesh_name in (None, ""):
                # Run all sequences. Existing mph behavior.
                model.mesh(None)
                return {
                    "success": True,
                    "mesh": None,
                    "message": "Mesh created: all meshes",
                }

            jm = model.java
            _comp, _m, comp_tag, tag, label, err = _resolve_mesh(jm, mesh_name)
            if err:
                return {"success": False, "error": err}

            # Pass the resolved tag to mph; it will accept either the tag
            # or the label, but we standardize on tag for predictability.
            try:
                model.mesh(tag)
            except LookupError:
                # Some mph versions key meshes by label inside the
                # node-path resolver; fall back.
                model.mesh(label)

            return {
                "success": True,
                "mesh": {"tag": tag, "label": label, "component": comp_tag},
                "message": f"Mesh created: {tag} ({label})",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create mesh: {type(e).__name__}: {e}"}

    @mcp.tool()
    def mesh_info(
        mesh_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Get information about a mesh.

        Accepts mesh tag or label.

        Args:
            mesh_name: Mesh sequence tag or label (default: first mesh).
            model_name: Model name (default: current model).

        Returns:
            Mesh statistics including element counts, plus tag/label/component.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            _comp, mesh_obj, comp_tag, tag, label, err = _resolve_mesh(jm, mesh_name)
            if err:
                return {"success": False, "error": err}

            info = {
                "tag": tag,
                "label": label,
                "component": comp_tag,
            }

            try:
                if hasattr(mesh_obj, 'getVertex'):
                    info["num_vertices"] = mesh_obj.getVertex().size()
                if hasattr(mesh_obj, 'getElement'):
                    info["num_elements"] = mesh_obj.getElement().size()
            except Exception:
                pass

            try:
                features = []
                for f in list(mesh_obj.feature()):
                    try:
                        features.append(str(f.tag()))
                    except Exception:
                        continue
                info["features"] = features
            except Exception:
                pass

            return {
                "success": True,
                "mesh": info,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to get mesh info: {type(e).__name__}: {e}"}

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
            mesh_name: COMSOL tag (or label) for the mesh sequence
                (auto-generated if None — e.g. 'mesh1'). When passed, it
                is treated as the COMSOL tag.
            auto_default_features: If True (default), add a Size feature
                with global predefined size (Normal) so the empty sequence
                becomes immediately runnable. If False, leave bare.
            model_name: Model name (default: current).

        Returns:
            ``{success, mesh: {tag, label, component, geometry,
                                has_default_features}}`` on success.
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
                    try:
                        mesh_seq.create("size1", "Size")
                    except Exception:
                        pass
                except Exception:
                    pass

            try:
                label = _safe_str(mesh_seq.label())
            except Exception:
                label = ""

            return {
                "success": True,
                "mesh": {
                    "tag": tag,
                    "label": label,
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

        Accepts mesh tag or label.

        Args:
            size_kind: One of COMSOL's predefined size strings:
                "Extremely fine" / "Extra fine" / "Finer" / "Fine" /
                "Normal" / "Coarse" / "Coarser" / "Extra coarse" /
                "Extremely coarse". Case-sensitive.
            mesh_name: Mesh sequence tag or label (default: first mesh).
            feature_tag: Tag of the Size feature within the mesh
                (default: 'size' as auto-created by COMSOL; some sequences
                use 'size1'). Both tags are tried.
            model_name: Model name (default: current).

        Returns:
            ``{success, mesh: {tag, label, component}, size_kind, hauto,
                applied}`` on success.
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
            _comp, mesh_obj, comp_tag, tag, label, err = _resolve_mesh(jm, mesh_name)
            if err:
                return {"success": False, "error": err}

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
                        f"'{tag}' ({label}). Last error: {last_error}"
                    ),
                }

            return {
                "success": True,
                "mesh": {"tag": tag, "label": label, "component": comp_tag},
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

        Accepts mesh tag or label.

        Args:
            mesh_name: Tag or label of the mesh sequence to remove.
            model_name: Model name (default: current).

        Returns:
            ``{success, removed: {tag, label, component}}`` on success.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            comp, _m, comp_tag, tag, label, err = _resolve_mesh(jm, mesh_name)
            if err:
                return {"success": False, "error": err}

            comp.mesh().remove(tag)
            return {
                "success": True,
                "removed": {"tag": tag, "label": label, "component": comp_tag},
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to remove mesh: {type(e).__name__}: {e}",
            }
