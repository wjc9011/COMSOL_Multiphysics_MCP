"""Geometry tools for COMSOL MCP Server (Java-API based).

Every public tool below reaches the underlying Java model through
``model.java.component(...).geom(...)`` and operates on
``GeomSequence`` / ``GeomFeature`` objects directly. The mph node-path
shortcut (``model / "geometries" / name``) is intentionally avoided
because it requires the display label ("Geometry 1") and cannot resolve
the internal tag ("geom1") that ``geometry_create`` returns.
"""

from typing import Any, Dict, Optional, Sequence
from mcp.server.fastmcp import FastMCP

from .session import session_manager


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_component(model, component_name: Optional[str]):
    """Resolve a component. Returns (comp_java, error). comp is None on error."""
    jm = model.java
    if component_name:
        try:
            comp = jm.component(component_name)
        except Exception as e:
            return None, f"Component lookup failed for '{component_name}': {type(e).__name__}: {e}"
        if comp is None:
            return None, (
                f"Component '{component_name}' not found. "
                "Create it first with model_create_component."
            )
        return comp, None
    try:
        comps = list(jm.component())
    except Exception as e:
        return None, f"Failed to list components: {type(e).__name__}: {e}"
    if not comps:
        return None, "No components in model. Create one first with model_create_component."
    return comps[0], None


def _get_geom(
    model,
    geometry_name: Optional[str],
    component_name: Optional[str] = "comp1",
):
    """Resolve a geometry sequence via Java API.

    Accepts either a tag ('geom1') or a label ('Geometry 1'). Returns
    ``(geom_java, geom_tag, comp_tag, error)``. On error, geom_java is
    None and error is a human-readable message.
    """
    comp, err = _get_component(model, component_name)
    if err:
        return None, None, None, err
    try:
        comp_tag = str(comp.tag())
    except Exception as e:
        return None, None, None, f"Component tag unreadable: {type(e).__name__}: {e}"

    if geometry_name:
        # Tag-first: COMSOL's native addressing
        try:
            geom = comp.geom(geometry_name)
        except Exception:
            geom = None
        if geom is not None:
            try:
                return geom, str(geom.tag()), comp_tag, None
            except Exception:
                return geom, geometry_name, comp_tag, None
        # Fallback: label match (for users who only know "Geometry 1")
        try:
            for g in list(comp.geom()):
                try:
                    if str(g.label()) == geometry_name:
                        return g, str(g.tag()), comp_tag, None
                except Exception:
                    continue
        except Exception:
            pass
        return None, None, comp_tag, (
            f"Geometry '{geometry_name}' not found in component '{comp_tag}'. "
            "Pass the tag (e.g. 'geom1') or the exact label shown in the GUI."
        )

    try:
        geoms = list(comp.geom())
    except Exception as e:
        return None, None, comp_tag, f"Failed to list geometries: {type(e).__name__}: {e}"
    if not geoms:
        return None, None, comp_tag, (
            "No geometry sequences in the component. "
            "Create one first with geometry_create."
        )
    g = geoms[0]
    try:
        return g, str(g.tag()), comp_tag, None
    except Exception:
        return g, None, comp_tag, None


def _feature_tags(geom) -> list:
    """Return a list of existing feature tag strings on a GeomSequence."""
    tags = []
    try:
        for f in geom.feature():
            try:
                tags.append(str(f.tag()))
            except Exception:
                continue
    except Exception:
        pass
    return tags


def _auto_feat_tag(geom, prefix: str) -> str:
    """Generate a non-colliding feature tag using the given prefix.

    Replaces the broken ``len(geom.feature())+1`` pattern — Java's
    ``GeomFeatureListImpl`` does not implement ``__len__`` and raises
    ``TypeError: object of type ... has no len()`` under JPype.
    """
    existing = set(_feature_tags(geom))
    i = len(existing) + 1
    while f"{prefix}{i}" in existing:
        i += 1
    return f"{prefix}{i}"


def _coerce_prop(value: Any):
    """Coerce a Python value to what COMSOL's .set() expects.

    Scalars become str; sequences become list-of-str; bools become
    'on'/'off' (COMSOL's convention for boolean properties). None is
    returned as-is so the caller can skip the write.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return str(value)


def _set_feature_prop(feat, name: str, value: Any) -> None:
    coerced = _coerce_prop(value)
    if coerced is None:
        return
    feat.set(name, coerced)


def _feature_type(feat) -> str:
    for attr in ("getType", "type"):
        try:
            m = getattr(feat, attr, None)
            if m is None:
                continue
            out = m() if callable(m) else m
            if out is not None:
                return str(out)
        except Exception:
            continue
    return "unknown"


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

def register_geometry_tools(mcp: FastMCP) -> None:
    """Register geometry tools with the MCP server."""

    @mcp.tool()
    def geometry_list(model_name: Optional[str] = None) -> dict:
        """
        List all geometry sequences in a model, across every component.

        Each entry reports the Java ``tag`` (e.g. 'geom1'), the display
        ``label`` (e.g. 'Geometry 1'), the owning component tag, and the
        space dimension.

        Args:
            model_name: Model name (default: current model)

        Returns:
            List of geometry sequence descriptors.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            result = []
            for comp in list(jm.component()):
                try:
                    comp_tag = str(comp.tag())
                except Exception:
                    comp_tag = "?"
                for g in list(comp.geom()):
                    item: Dict[str, Any] = {"component": comp_tag}
                    try:
                        item["tag"] = str(g.tag())
                    except Exception:
                        item["tag"] = None
                    try:
                        item["label"] = str(g.label())
                    except Exception:
                        pass
                    try:
                        item["sdim"] = int(g.getSDim())
                    except Exception:
                        pass
                    result.append(item)
            return {
                "success": True,
                "geometries": result,
                "count": len(result),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list geometries: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_create(
        geometry_name: Optional[str] = None,
        space_dimension: int = 3,
        axisymmetric: bool = False,
        component_name: str = "comp1",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a new geometry sequence in the model's component.

        IMPORTANT: A component must exist first. Use model_create_component.

        Java API (per COMSOL ApplicationProgrammingGuide / ProgrammingReferenceManual)::

            geom = model.component(<ctag>).geom().create(<gtag>, <sdim>);
            if axisymmetric:
                model.component(<ctag>).geom(<gtag>).axisymmetric(true);

        The ``axisymmetric`` boolean property is only valid for
        ``space_dimension`` ∈ {1, 2}. Passing ``axisymmetric=True`` with
        ``space_dimension=3`` (or 0) returns an error.

        Args:
            geometry_name: Requested tag for the sequence (default: 'geom1').
                COMSOL may append a numeric suffix to avoid collisions —
                the actual tag is reported in the response.
            space_dimension: 0, 1, 2, or 3 (default: 3).
            axisymmetric: If True, mark the geometry axisymmetric. Only
                valid for 1D or 2D geometries (default: False).
            component_name: Component name (default: 'comp1').
            model_name: Model name (default: current model).

        Returns:
            Created geometry info, including the *actual* tag COMSOL
            assigned (use this tag in subsequent geometry_* calls).
        """
        sdim = int(space_dimension)
        if axisymmetric and sdim not in (1, 2):
            return {
                "success": False,
                "error": (
                    f"axisymmetric=True is only valid for space_dimension "
                    f"in {{1, 2}} (1D/2D); got space_dimension={sdim}. "
                    "Per COMSOL ApplicationProgrammingGuide, the "
                    "axisymmetric property is only applicable to models "
                    "of spatial dimension 1 or 2."
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
            geom_name = geometry_name or "geom1"

            comp = jm.component(component_name)
            if comp is None:
                return {
                    "success": False,
                    "error": (
                        f"Component '{component_name}' not found. "
                        "Create it first with model_create_component."
                    )
                }

            geom = comp.geom().create(geom_name, sdim)
            try:
                actual_tag = str(geom.tag())
            except Exception:
                actual_tag = geom_name

            if axisymmetric:
                try:
                    comp.geom(actual_tag).axisymmetric(True)
                except Exception as ax_e:
                    return {
                        "success": False,
                        "error": (
                            f"Geometry created but axisymmetric flag could "
                            f"not be set: {type(ax_e).__name__}: {ax_e}"
                        ),
                    }

            return {
                "success": True,
                "geometry": actual_tag,
                "tag": actual_tag,
                "component": component_name,
                "space_dimension": sdim,
                "axisymmetric": bool(axisymmetric),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create geometry: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_add_feature(
        feature_type: str,
        geometry_name: Optional[str] = None,
        feature_name: Optional[str] = None,
        component_name: str = "comp1",
        properties: Optional[Dict[str, Any]] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Add a geometry feature to a geometry sequence (Java API).

        Common feature types: Block, Cylinder, Sphere, Cone, WorkPlane,
        Rectangle, Circle, Point, Polygon, Import, Union, Intersection,
        Difference.

        Args:
            feature_type: COMSOL feature type (e.g. 'Block', 'Point').
            geometry_name: Geometry tag or label (default: first).
            feature_name: Feature tag (auto-generated if None).
            component_name: Component tag (default: 'comp1').
            properties: Feature-specific properties as an object.
                Values are stringified before calling COMSOL's .set().
                Example: {"pos": [0, 0], "size": [1, 1]}.
                (Replaces the earlier **kwargs form, which the MCP
                transport serialized as NoneType.)
            model_name: Model name (default: current model).

        Returns:
            Created feature info with the actual tag.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            feat_name = feature_name or _auto_feat_tag(geom, "f")
            feat = geom.feature().create(feat_name, feature_type)

            skipped: list = []
            if properties:
                for pname, pval in properties.items():
                    if pval is None:
                        continue
                    try:
                        _set_feature_prop(feat, pname, pval)
                    except Exception as se:
                        skipped.append({"property": pname, "error": f"{type(se).__name__}: {se}"})

            try:
                actual_tag = str(feat.tag())
            except Exception:
                actual_tag = feat_name

            result = {
                "success": True,
                "feature": {
                    "name": actual_tag,
                    "tag": actual_tag,
                    "type": feature_type,
                    "geometry": geom_tag,
                    "component": comp_tag,
                },
            }
            if skipped:
                result["warnings"] = {"skipped_properties": skipped}
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to add geometry feature: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_add_block(
        position: Sequence[float] = (0, 0, 0),
        size: Sequence[float] = (1, 1, 1),
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a block (rectangular cuboid) to a 3D geometry.

        Args:
            position: Base position [x, y, z] in meters.
            size: Dimensions [width, depth, height] in meters.
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            feature_name: Feature tag (auto-generated if None).
            model_name: Model name (default: current model).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            feat_name = feature_name or _auto_feat_tag(geom, "blk")
            block = geom.feature().create(feat_name, "Block")
            _set_feature_prop(block, "pos", position)
            _set_feature_prop(block, "size", size)

            return {
                "success": True,
                "feature": {
                    "name": str(block.tag()),
                    "tag": str(block.tag()),
                    "type": "Block",
                    "geometry": geom_tag,
                    "component": comp_tag,
                    "position": list(position),
                    "size": list(size),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add block: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_add_cylinder(
        position: Sequence[float] = (0, 0, 0),
        radius: float = 0.5,
        height: float = 1.0,
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a cylinder to a 3D geometry.

        Args:
            position: Center of base [x, y, z] in meters.
            radius: Radius in meters (default: 0.5).
            height: Height in meters (default: 1.0).
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            feature_name: Feature tag (auto-generated if None).
            model_name: Model name (default: current model).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            feat_name = feature_name or _auto_feat_tag(geom, "cyl")
            cyl = geom.feature().create(feat_name, "Cylinder")
            _set_feature_prop(cyl, "pos", position)
            _set_feature_prop(cyl, "r", radius)
            _set_feature_prop(cyl, "h", height)

            return {
                "success": True,
                "feature": {
                    "name": str(cyl.tag()),
                    "tag": str(cyl.tag()),
                    "type": "Cylinder",
                    "geometry": geom_tag,
                    "component": comp_tag,
                    "position": list(position),
                    "radius": radius,
                    "height": height,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add cylinder: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_add_sphere(
        position: Sequence[float] = (0, 0, 0),
        radius: float = 0.5,
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a sphere to a 3D geometry.

        Args:
            position: Center [x, y, z] in meters.
            radius: Radius in meters (default: 0.5).
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            feature_name: Feature tag (auto-generated if None).
            model_name: Model name (default: current model).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            feat_name = feature_name or _auto_feat_tag(geom, "sph")
            sphere = geom.feature().create(feat_name, "Sphere")
            _set_feature_prop(sphere, "pos", position)
            _set_feature_prop(sphere, "r", radius)

            return {
                "success": True,
                "feature": {
                    "name": str(sphere.tag()),
                    "tag": str(sphere.tag()),
                    "type": "Sphere",
                    "geometry": geom_tag,
                    "component": comp_tag,
                    "position": list(position),
                    "radius": radius,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add sphere: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_add_rectangle(
        position: Sequence[float] = (0, 0),
        size: Sequence[float] = (1, 1),
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a rectangle to a 2D geometry or work plane.

        Args:
            position: Base position [x, y] in meters.
            size: Dimensions [width, height] in meters.
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            feature_name: Feature tag (auto-generated if None).
            model_name: Model name (default: current model).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            feat_name = feature_name or _auto_feat_tag(geom, "r")
            rect = geom.feature().create(feat_name, "Rectangle")
            _set_feature_prop(rect, "pos", position)
            _set_feature_prop(rect, "size", size)

            return {
                "success": True,
                "feature": {
                    "name": str(rect.tag()),
                    "tag": str(rect.tag()),
                    "type": "Rectangle",
                    "geometry": geom_tag,
                    "component": comp_tag,
                    "position": list(position),
                    "size": list(size),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add rectangle: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_add_circle(
        position: Sequence[float] = (0, 0),
        radius: float = 0.5,
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a circle to a 2D geometry or work plane.

        Args:
            position: Center [x, y] in meters.
            radius: Radius in meters (default: 0.5).
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            feature_name: Feature tag (auto-generated if None).
            model_name: Model name (default: current model).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            feat_name = feature_name or _auto_feat_tag(geom, "c")
            circle = geom.feature().create(feat_name, "Circle")
            _set_feature_prop(circle, "pos", position)
            _set_feature_prop(circle, "r", radius)

            return {
                "success": True,
                "feature": {
                    "name": str(circle.tag()),
                    "tag": str(circle.tag()),
                    "type": "Circle",
                    "geometry": geom_tag,
                    "component": comp_tag,
                    "position": list(position),
                    "radius": radius,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add circle: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_boolean_union(
        input_objects: Sequence[str],
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Create a boolean union of geometry objects.

        Args:
            input_objects: Tags of objects to unite (e.g. ['blk1', 'cyl1']).
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            feature_name: Feature tag (auto-generated if None).
            model_name: Model name (default: current model).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            feat_name = feature_name or _auto_feat_tag(geom, "uni")
            union = geom.feature().create(feat_name, "Union")
            union.selection("input").set([str(o) for o in input_objects])

            return {
                "success": True,
                "feature": {
                    "name": str(union.tag()),
                    "tag": str(union.tag()),
                    "type": "Union",
                    "geometry": geom_tag,
                    "component": comp_tag,
                    "input_objects": list(input_objects),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create union: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_boolean_difference(
        input_object: str,
        objects_to_subtract: Sequence[str],
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Create a boolean difference (subtract objects from another).

        Args:
            input_object: Tag of the object to subtract from (e.g. 'blk1').
            objects_to_subtract: Tags of objects to remove (e.g. ['cyl1']).
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            feature_name: Feature tag (auto-generated if None).
            model_name: Model name (default: current model).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            feat_name = feature_name or _auto_feat_tag(geom, "dif")
            diff = geom.feature().create(feat_name, "Difference")
            diff.selection("input").set([str(input_object)])
            diff.selection("input2").set([str(o) for o in objects_to_subtract])

            return {
                "success": True,
                "feature": {
                    "name": str(diff.tag()),
                    "tag": str(diff.tag()),
                    "type": "Difference",
                    "geometry": geom_tag,
                    "component": comp_tag,
                    "input_object": input_object,
                    "subtracted": list(objects_to_subtract),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create difference: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_import(
        file_path: str,
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        import_type: str = "CAD",
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Import geometry from a CAD file via an Import feature.

        Supported formats: STEP, IGES, STL, NASTRAN, and others COMSOL supports.

        Args:
            file_path: Path to the CAD file.
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            import_type: Informational only (recorded in the response).
            feature_name: Feature tag (auto-generated if None).
            model_name: Model name (default: current model).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            feat_name = feature_name or _auto_feat_tag(geom, "imp")
            imp = geom.feature().create(feat_name, "Import")
            _set_feature_prop(imp, "filename", file_path)

            return {
                "success": True,
                "feature": {
                    "name": str(imp.tag()),
                    "tag": str(imp.tag()),
                    "type": "Import",
                    "geometry": geom_tag,
                    "component": comp_tag,
                    "file": file_path,
                    "import_type": import_type,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to import geometry: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_build(
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Build (run) the geometry sequence to generate the final geometry.

        Must be called after adding or modifying geometry features for
        boundary/domain information to become available.

        Args:
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            model_name: Model name (default: current model).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            geom.run()

            return {
                "success": True,
                "geometry": geom_tag,
                "component": comp_tag,
                "message": "Geometry built successfully.",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to build geometry: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_list_features(
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None
    ) -> dict:
        """
        List all features in a geometry sequence (Java API).

        Args:
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            model_name: Model name (default: current model).

        Returns:
            List of features with tag, type, and label (when available).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            features = []
            for f in geom.feature():
                item: Dict[str, Any] = {}
                try:
                    item["tag"] = str(f.tag())
                    item["name"] = item["tag"]
                except Exception:
                    pass
                item["type"] = _feature_type(f)
                try:
                    item["label"] = str(f.label())
                except Exception:
                    pass
                features.append(item)

            return {
                "success": True,
                "geometry": geom_tag,
                "component": comp_tag,
                "features": features,
                "count": len(features),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list features: {type(e).__name__}: {e}"}

    @mcp.tool()
    def geometry_get_boundaries(
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Get boundary and domain counts for a (built) geometry.

        Uses the ``getNBoundaries`` / ``getNDomains`` / ``getSDim`` Java
        API on ``GeomSequence``. Calls ``geom.run()`` first to ensure
        the geometry is built; the counts are meaningful only after a
        successful build.

        Args:
            geometry_name: Geometry tag or label (default: first).
            component_name: Component tag (default: 'comp1').
            model_name: Model name (default: current model).

        Returns:
            total_boundaries, total_domains, sdim, and a per-boundary
            list of descriptors (currently just the 1-based number —
            extended adjacency/up-down info can be added later).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            geom, geom_tag, comp_tag, err = _get_geom(model, geometry_name, component_name)
            if err:
                return {"success": False, "error": err}

            try:
                geom.run()
            except Exception as run_e:
                return {
                    "success": False,
                    "error": f"Geometry build failed: {type(run_e).__name__}: {run_e}",
                }

            try:
                n_boundaries = int(geom.getNBoundaries())
            except Exception as be:
                return {
                    "success": False,
                    "error": f"getNBoundaries failed: {type(be).__name__}: {be}",
                }
            try:
                n_domains = int(geom.getNDomains())
            except Exception:
                n_domains = None
            try:
                sdim = int(geom.getSDim())
            except Exception:
                sdim = None

            boundaries = [{"boundary_number": i} for i in range(1, n_boundaries + 1)]

            return {
                "success": True,
                "geometry": geom_tag,
                "component": comp_tag,
                "sdim": sdim,
                "total_boundaries": n_boundaries,
                "total_domains": n_domains,
                "boundaries": boundaries,
                "hint": (
                    "Use boundary_number to configure boundary conditions "
                    "with physics_configure_boundary."
                ),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to get boundaries: {type(e).__name__}: {e}"}
