"""Mesh tools for COMSOL MCP Server.

Naming convention (spec mcp_pr_c_fix_spec.md §3.3): every mesh_* tool
accepts a mesh sequence by EITHER its Java tag (e.g. ``mesh1``) OR its
display label (e.g. ``Mesh 1``). The shared ``_resolve_mesh`` helper
walks all components, prefers tag matches, falls back to label matches,
and surfaces a unified error on miss. Every response also reports both
``tag`` and ``label`` so callers do not have to guess which form their
input was.

Operation auto-injection (spec mcp_pr_c_fix_v2_spec.md §3.1): an empty
mesh sequence with only a Size *attribute* meshes nothing — KB
ProgrammingReferenceManual chunks 77425/77427/77428 show that mesh
generation requires an *operation* feature (``FreeTri``, ``FreeTet``,
``Edge`` ...) to drive cell creation. ``mesh_add_sequence`` therefore
infers the right operation type from the geometry's space dimension
and adds it under the new sequence by default.

Operation type strings are case-sensitive in COMSOL's Java API
(verified 2026-05-11 via Cowork sanity06: lowercase ``freetri`` raises
``com.comsol.util.exceptions.FlException`` "The operation is case
sensitive — FreeTri is an allowed operation"). Internal mapping +
caller kwargs are normalized to canonical PascalCase via
``_normalize_op_type``.
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


# Spec mcp_pr_c_fix_v2_spec.md §2.2 — sdim → default operation type.
# Source: KB ProgrammingReferenceManual chunks 77427/77428 (FreeTri/FreeTet
# syntax tables) plus the COMSOL UI default that picks FreeTri for any 2D
# (planar or axisymmetric) and FreeTet for 3D.
#
# Operation type strings are *case-sensitive* in COMSOL's Java API
# (verified 2026-05-11 via Cowork sanity06: lowercase 'freetri' raises
# com.comsol.util.exceptions.FlException with message "The operation is
# case sensitive — FreeTri is an allowed operation"). Use the canonical
# PascalCase strings from the Programming Reference.
_DEFAULT_OP_BY_SDIM = {
    1: "Edge",
    2: "FreeTri",
    3: "FreeTet",
}

# Stable per-operation tag prefixes that mirror what the COMSOL GUI emits.
# Keys are the canonical PascalCase op_type strings (case-sensitive in
# COMSOL's Java API).
_OP_TAG_PREFIX = {
    "FreeTri":  "ftri",
    "FreeQuad": "fq",
    "FreeTet":  "ftet",
    "Edge":     "edg",
    "BndLayer": "bl",
    "Sweep":    "swe",
    "Map":      "map",
    "Copy":     "cp",
    "Refine":   "ref",
}

# Lowercase alias → canonical PascalCase. Lets callers pass lowercase
# (legacy / typo-friendly) op_type values and still hit the Java API
# with the case it requires.
_OP_TYPE_ALIASES = {op.lower(): op for op in _OP_TAG_PREFIX}


def _normalize_op_type(op_type: Optional[str]) -> Optional[str]:
    """Map a caller-supplied op_type to its canonical PascalCase form.

    COMSOL's Java API rejects mixed-case variants
    (``com.comsol.util.exceptions.FlException: "The operation is case
    sensitive"``). Accept lowercase / mixed-case input and resolve to
    the canonical string. Unknown types pass through unchanged so
    callers can still try operations not in the alias table.
    """
    if not op_type:
        return op_type
    return _OP_TYPE_ALIASES.get(op_type.lower(), op_type)


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


def _geom_sdim(comp, geom_tag: str) -> Optional[int]:
    """Return the space dimension of a geometry, or None if unknown.

    Tries the canonical COMSOL Java accessor ``GeomSequence.sDim()``
    (camelCase D — JPype is case-sensitive, so the lowercase ``sdim``
    alias is NOT always present) and the mph-wrapper ``dimension()``
    accessor before falling back to legacy aliases. Cowork Pilot 07 v2
    measured: 1D Interval geometries silently fell through every
    accessor here, returned ``None``, and ``mesh_add_sequence`` then
    defaulted to ``FreeTri`` (sdim=2 dict lookup) — which COMSOL
    rejects with "Operation cannot be created in this context: FreeTri".
    Detecting sdim=1 here lets the auto-mapping pick ``Edge``.

    Some bridges return the dimension as a string like ``"3D"`` /
    ``"2D"`` / ``"1D"`` / ``"2Daxi"``; we extract the leading digit
    in that case.
    """
    try:
        g = comp.geom(geom_tag)
    except Exception:
        return None
    for accessor in (
        "sdim",            # legacy / lowercase alias (kept first for
                           # mph-wrapped paths that already expose it)
        "sDim",            # canonical COMSOL Java (camelCase D —
                           # JPype is case-sensitive; sdim above may
                           # not resolve on the raw Java handle)
        "dimension",       # mph wrapper alias on the Geometry node
        "getSDim",         # explicit Java getter
        "geomDim",
        "space_dimension",
    ):
        try:
            fn = getattr(g, accessor, None)
            if fn is None:
                continue
            v = fn()
            if v is None:
                continue
            if isinstance(v, str):
                v = v.strip()
                if v and v[0].isdigit():
                    return int(v[0])
                continue
            iv = int(v)
            if iv in (1, 2, 3):
                return iv
            # Out-of-range or zero — likely an unrelated accessor that
            # happened to share a name. Try the next one rather than
            # propagating a bogus dimension.
        except Exception:
            continue
    return None


def _auto_op_tag(mesh_seq, op_type: str) -> str:
    """Generate a non-colliding feature tag for a mesh operation."""
    prefix = _OP_TAG_PREFIX.get(op_type, op_type)
    existing = set()
    try:
        for f in list(mesh_seq.feature()):
            try:
                existing.add(str(f.tag()))
            except Exception:
                continue
    except Exception:
        pass
    i = 1
    while f"{prefix}{i}" in existing:
        i += 1
    return f"{prefix}{i}"


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
                    n_elems = mesh_obj.getElement().size()
                    info["num_elements"] = n_elems
                    # Spec mcp_pr_c_fix_v2_spec.md §5.1 alias.
                    info["element_count"] = n_elems
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
        default_operation_type: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a mesh sequence with default operation + Size attribute.

        Spec mcp_pr_c_fix_v2_spec.md §3.1: KB ProgrammingReferenceManual
        chunks 77425/77427/77428 show that the Size *attribute* alone
        cannot mesh anything — an *operation* feature
        (``FreeTri`` / ``FreeTet`` / ``Edge`` / ...) must be present
        to drive cell creation. With ``auto_default_features=True`` we
        therefore add:
          - The default operation matching the geometry's space
            dimension: sdim=1 → ``Edge``, sdim=2 (planar or axi) →
            ``FreeTri``, sdim=3 → ``FreeTet``. Override with
            ``default_operation_type``.
          - A ``size`` attribute *under the operation* (so the predefined
            size applies to the cells the operation generates). The
            global mesh-level Size (``size1``) is also added as a
            backstop because some COMSOL versions key the predefined
            size off the global attribute.
          - For 3D operations the default selection is empty per KB —
            we call ``selection().all()`` so all domains are meshed.

        Java API per KB chunk 77427:
            model.component(c).mesh(m).create(<op_tag>, <op_type>);
            model.component(c).mesh(m).feature(<op_tag>).create("size", "Size");

        Args:
            component_name: Component to attach mesh to (default: first).
            geometry_name: Geometry tag the mesh references
                (default: first geometry in the component).
            mesh_name: COMSOL tag (or label) for the mesh sequence
                (auto-generated if None — e.g. 'mesh1'). When passed, it
                is treated as the COMSOL tag.
            auto_default_features: If True (default), inject the default
                operation + Size attribute described above. If False,
                leave the sequence bare.
            default_operation_type: Override the operation auto-mapping.
                Canonical PascalCase: ``"FreeTri"`` / ``"FreeQuad"`` /
                ``"FreeTet"`` / ``"Edge"`` (KB Table 4-1). Lowercase
                input is accepted and normalized internally. ``None``
                (default) → infer from geometry sdim.
            model_name: Model name (default: current).

        Returns:
            ``{success, mesh: {tag, label, component, geometry,
                                has_default_features, default_operation,
                                size_attribute_attached_to}}`` on
            success. ``default_operation`` is ``{tag, type}`` or ``None``
            if injection was skipped or failed.
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

            default_op_info = None
            size_attached_to = None
            silent_exception = None

            if auto_default_features:
                # Resolve the operation type. Caller override wins;
                # otherwise infer from the geometry's space dimension.
                op_type = default_operation_type
                if not op_type:
                    sdim = _geom_sdim(comp, geom_tag)
                    op_type = _DEFAULT_OP_BY_SDIM.get(
                        sdim if sdim in _DEFAULT_OP_BY_SDIM else 2,
                        "FreeTri",
                    )
                # Normalize lowercase/legacy input to the case-sensitive
                # canonical form COMSOL's Java API requires.
                op_type = _normalize_op_type(op_type)

                op_tag = _auto_op_tag(mesh_seq, op_type)
                op_obj = None
                try:
                    op_obj = mesh_seq.create(op_tag, op_type)
                    default_op_info = {"tag": op_tag, "type": op_type}
                except Exception as e:
                    # Fall through — no operation; we still add the size
                    # attribute below so users can hand-add an operation.
                    op_obj = None
                    silent_exception = f"{type(e).__name__}: {e}"

                # 3D: KB chunk 77427 says default selection is empty.
                # Set selection().all() so the FreeTet covers everything.
                if op_obj is not None and op_type == "FreeTet":
                    try:
                        op_obj.selection().all()
                    except Exception:
                        pass

                # Attach the predefined Size attribute under the
                # operation (KB chunk 77428 confirms this is the
                # canonical location).
                if op_obj is not None:
                    try:
                        op_obj.create("size", "Size")
                        size_attached_to = op_tag
                    except Exception:
                        size_attached_to = None

                # Backstop: also add a mesh-level Size so callers that
                # later invoke mesh_set_global_size still find a target,
                # and so legacy (mph) consumers that read the global
                # size keep working. Tag 'size1' to avoid colliding
                # with the auto-created 'size' COMSOL emits.
                try:
                    mesh_seq.create("size1", "Size")
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
                    "default_operation": default_op_info,
                    "size_attribute_attached_to": size_attached_to,
                    "silent_exception": silent_exception,
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
