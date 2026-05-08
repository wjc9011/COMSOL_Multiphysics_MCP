"""Material tools for COMSOL MCP Server.

Implements the user-defined and KB-fed material creation API specified
in plans\\mcp_material_tools_spec.md (§3). The KB read path lives in
knowledge\\material_kb_tools.py; this module performs the COMSOL Java
API mutation only.
"""

from pathlib import Path
from typing import Optional, Sequence
from mcp.server.fastmcp import FastMCP

from .session import session_manager


# KB layout (mirrors plans\\mcp_material_tools_spec.md §5):
#   <KB_ROOT>/catalogs/materials_catalog.csv
#   <KB_ROOT>/materials/<source_lib>/<name_with_underscores>.json
# This file lives at:
#   <KB_ROOT>/derived/comsol_agent/upstream/src/tools/material.py
# So parents[0..5] = tools, src, upstream, comsol_agent, derived, KB_ROOT.
KB_ROOT = Path(__file__).resolve().parents[5]
MATERIALS_CATALOG = KB_ROOT / "catalogs" / "materials_catalog.csv"
MATERIALS_JSON_DIR = KB_ROOT / "materials"


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


def _existing_material_labels(comp) -> dict:
    """Map {label_string: java_material_object} for the component."""
    out = {}
    try:
        for m in list(comp.material()):
            try:
                out[str(m.label())] = m
            except Exception:
                continue
    except Exception:
        pass
    return out


def _existing_material_tags(comp) -> set:
    out = set()
    try:
        for m in list(comp.material()):
            try:
                out.add(str(m.tag()))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _auto_mat_tag(comp, prefix: str = "mat") -> str:
    existing = _existing_material_tags(comp)
    i = len(existing) + 1
    while f"{prefix}{i}" in existing:
        i += 1
    return f"{prefix}{i}"


# Friendly descriptions for the small set of propertyGroups COMSOL ships.
# Used as the second arg to mat.propertyGroup().create(<tag>, <descr>) when
# the caller didn't supply one (KB chunk 76973 requires both args).
_PROPERTY_GROUP_DESCR = {
    "def":              "Basic",
    "Enu":              "Young's modulus and Poisson's ratio",
    "KG":               "Bulk modulus and shear modulus",
    "Murnaghan":        "Murnaghan",
    "Lame":             "Lame parameters",
    "Anisotropic":      "Anisotropic",
    "Orthotropic":      "Orthotropic",
    "RefractiveIndex":  "Refractive index",
    "NonlinearModel":   "Nonlinear material model",
    "Cauchy":           "Cauchy",
}


def _create_material_property_group(mat, group_tag: str) -> bool:
    """Create a non-default propertyGroup on a Material via Java API.

    Uses the (tag, descr) two-arg overload documented in
    COMSOL_ProgrammingReferenceManual (chunk 76973):
        MaterialModel mm = mat.propertyGroup().create(<mtag>, <descr>);
    Falls back to the single-arg form for COMSOL versions that may
    accept it. Returns True on success.
    """
    descr = _PROPERTY_GROUP_DESCR.get(group_tag, group_tag)
    try:
        mat.propertyGroup().create(group_tag, descr)
        return True
    except Exception:
        pass
    # Single-arg fallback (some Java overloads / older COMSOL builds).
    try:
        mat.propertyGroup().create(group_tag)
        return True
    except Exception:
        return False


def _find_material_in_model(model, material_name: str):
    """Return (comp_java, mat_java, comp_tag) for the first match across
    components, by label or tag. (None, None, None) if not found."""
    try:
        jm = model.java
        for comp in list(jm.component()):
            for m in list(comp.material()):
                try:
                    if str(m.label()) == material_name or str(m.tag()) == material_name:
                        return comp, m, str(comp.tag())
                except Exception:
                    continue
    except Exception:
        pass
    return None, None, None


def _create_material_with_properties(
    comp,
    name: str,
    properties: dict,
    property_group: str,
    tag: Optional[str],
) -> dict:
    """Internal: create a Material node and write all properties.

    Returns the success-shaped response dict.
    """
    if not isinstance(properties, dict) or not properties:
        return {
            "success": False,
            "error": "properties must be a non-empty dict of {prop_key: value_expr}.",
        }

    existing_labels = _existing_material_labels(comp)
    if name in existing_labels:
        return {
            "success": False,
            "error": f"Material with name '{name}' already exists",
        }

    use_tag = tag or _auto_mat_tag(comp, "mat")
    if use_tag in _existing_material_tags(comp):
        return {
            "success": False,
            "error": f"Material with tag '{use_tag}' already exists",
        }

    try:
        mat = comp.material().create(use_tag, "Common")
        mat.label(name)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to create material: {type(e).__name__}: {e}",
        }

    # Try to obtain the requested propertyGroup; create if missing.
    try:
        pg = mat.propertyGroup(property_group)
    except Exception:
        pg = None

    if pg is None and property_group != "def":
        # COMSOL ships 'def' on every Common material; other groups must
        # be created (e.g. 'Enu' for elasticity).
        # KB COMSOL_ProgrammingReferenceManual chunk 76973:
        #   MaterialModel mm = ...material(<tag>).propertyGroup()
        #     .create(<mtag>, <descr>);
        # The create() method requires BOTH a tag and a description string.
        if not _create_material_property_group(mat, property_group):
            pg = None
        else:
            try:
                pg = mat.propertyGroup(property_group)
            except Exception:
                pg = None

    if pg is None:
        return {
            "success": False,
            "error": (
                f"Property group '{property_group}' not found and could "
                f"not be created on material '{name}'."
            ),
        }

    written = {}
    skipped = []
    for prop_key, prop_value in properties.items():
        try:
            pg.set(str(prop_key), str(prop_value))
            written[str(prop_key)] = str(prop_value)
        except Exception as e:
            skipped.append({"property": str(prop_key), "error": f"{type(e).__name__}: {e}"})

    return {
        "success": True,
        "material": {
            "tag": use_tag,
            "name": name,
            "component": str(comp.tag()),
            "property_group": property_group,
            "properties": written,
        },
        **({"warnings": {"skipped_properties": skipped}} if skipped else {}),
    }


def _load_kb_material_record(kb_name: str) -> Optional[dict]:
    """Load a per-material JSON record from the KB.

    Tries exact then case-insensitive name match. Returns the parsed
    dict or None on miss.
    """
    import json
    if not MATERIALS_JSON_DIR.exists():
        return None

    target_norm = kb_name.replace(" ", "_").replace("/", "_")
    candidates = []
    for sub in MATERIALS_JSON_DIR.iterdir():
        if not sub.is_dir():
            continue
        # exact match first
        exact = sub / f"{target_norm}.json"
        if exact.exists():
            try:
                return json.loads(exact.read_text(encoding="utf-8"))
            except Exception:
                continue
        candidates.extend(sub.glob("*.json"))

    target_lower = kb_name.lower()
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        nm = str(data.get("name", "")).lower()
        if nm == target_lower or path.stem.lower() == target_lower or path.stem.replace("_", " ").lower() == target_lower:
            return data
    return None


def register_material_tools(mcp: FastMCP) -> None:
    """Register material tools with the MCP server."""

    @mcp.tool()
    def material_create_user_defined(
        name: str,
        properties: dict,
        component_name: Optional[str] = None,
        property_group: str = "def",
        tag: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a user-defined Material in the model and assign all
        properties under the given propertyGroup (default: 'def').

        Args:
            name: Display name (becomes mat.label()).
            properties: Mapping of COMSOL property keys to value
                expressions (with units), e.g.:
                  {"thermalconductivity": "52[W/(m*K)]",
                   "density": "7870[kg/m^3]",
                   "heatcapacity": "449[J/(kg*K)]"}
                Common keys: thermalconductivity, density, heatcapacity,
                youngsmodulus, poissonsratio, electricconductivity,
                relpermittivity, relpermeability.
            component_name: Component (default: first).
            property_group: COMSOL property group tag (default: 'def').
                Use 'Enu' for Young's modulus / Poisson's ratio.
            tag: COMSOL tag for the material (auto if None, e.g. 'mat1').
            model_name: Model name (default: current).

        Returns:
            On success: {success: True, material: {...}}.
            On failure: {success: False, error: str}.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
        except Exception as e:
            return {"success": False, "error": f"Java bridge failure: {e}"}

        comp, comp_tag, err = _resolve_component(jm, component_name)
        if err:
            return {"success": False, "error": err}

        return _create_material_with_properties(
            comp, name, properties, property_group, tag
        )

    @mcp.tool()
    def material_create_from_kb(
        kb_name: str,
        target_name: Optional[str] = None,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Look up a material in the KB catalog and create it in the current
        model via the same Java API path as material_create_user_defined.

        Args:
            kb_name: Material name as it appears in the KB catalog.
            target_name: Display name in the model (default: same as kb_name).
            component_name: Component (default: first).
            model_name: Model name (default: current).

        Returns:
            On success: {success: True, material: {...},
                          source: {catalog_path, json_path, source_lib}}.
            On failure: {success: False, error: str}.
        """
        record = _load_kb_material_record(kb_name)
        if record is None:
            return {
                "success": False,
                "error": (
                    f"KB material not found: {kb_name}. "
                    f"Looked under {MATERIALS_JSON_DIR} (run the KB-side "
                    "extraction script first if the catalog is empty)."
                ),
            }

        property_groups = record.get("propertyGroups") or {}
        if not isinstance(property_groups, dict) or not property_groups:
            return {
                "success": False,
                "error": (
                    f"KB material '{kb_name}' has no propertyGroups data. "
                    "Re-run the extraction script to refresh."
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
        except Exception as e:
            return {"success": False, "error": f"Java bridge failure: {e}"}

        comp, comp_tag, err = _resolve_component(jm, component_name)
        if err:
            return {"success": False, "error": err}

        display_name = target_name or record.get("name") or kb_name

        # Use 'def' as the canonical group for the create+label step;
        # any extra groups beyond 'def' are written next.
        primary_group = "def" if "def" in property_groups else next(iter(property_groups))
        primary_props = dict(property_groups[primary_group])

        result = _create_material_with_properties(
            comp, display_name, primary_props, primary_group, None
        )
        if not result.get("success"):
            return result

        # Write any extra groups (e.g. 'Enu', 'RefractiveIndex'). Uses
        # the two-arg propertyGroup().create(<tag>, <descr>) Java API per
        # KB COMSOL_ProgrammingReferenceManual chunk 76973.
        extra_warnings = []
        try:
            comp_for_write, mat_obj, _ = _find_material_in_model(model, display_name)
            if mat_obj is not None:
                for grp_name, grp_data in property_groups.items():
                    if grp_name == primary_group:
                        continue
                    if not isinstance(grp_data, dict) or not grp_data:
                        continue
                    try:
                        try:
                            pg = mat_obj.propertyGroup(grp_name)
                        except Exception:
                            pg = None
                        if pg is None:
                            if _create_material_property_group(mat_obj, grp_name):
                                try:
                                    pg = mat_obj.propertyGroup(grp_name)
                                except Exception:
                                    pg = None
                        if pg is None:
                            extra_warnings.append(
                                {"propertyGroup": grp_name,
                                 "error": "could not create propertyGroup"}
                            )
                            continue
                        for k, v in grp_data.items():
                            try:
                                pg.set(str(k), str(v))
                            except Exception as e:
                                extra_warnings.append(
                                    {"propertyGroup": grp_name, "property": k,
                                     "error": f"{type(e).__name__}: {e}"}
                                )
                    except Exception as e:
                        extra_warnings.append(
                            {"propertyGroup": grp_name,
                             "error": f"{type(e).__name__}: {e}"}
                        )
        except Exception:
            pass

        result["source"] = {
            "catalog_path": str(MATERIALS_CATALOG),
            "json_dir": str(MATERIALS_JSON_DIR),
            "source_lib": record.get("source_lib"),
            "source_path": record.get("source_path"),
        }
        if extra_warnings:
            result.setdefault("warnings", {})["extra_groups"] = extra_warnings
        return result

    @mcp.tool()
    def material_assign_to_domain(
        material_name: str,
        domain_selection: Sequence[int],
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Restrict a material's domain selection.

        Args:
            material_name: Name (label) of the material in the current model.
            domain_selection: 1-based domain numbers to assign.
            model_name: Model name (default: current).

        Returns:
            {success, material, domain_selection} on success.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        comp, mat, comp_tag = _find_material_in_model(model, material_name)
        if mat is None:
            return {
                "success": False,
                "error": f"Material not found in model: {material_name}",
            }

        try:
            domains = [int(d) for d in domain_selection]
        except (TypeError, ValueError) as e:
            return {
                "success": False,
                "error": f"Invalid domain selection: {e}",
            }
        if not domains:
            return {
                "success": False,
                "error": "Invalid domain selection: empty list",
            }
        if any(d < 1 for d in domains):
            return {
                "success": False,
                "error": (
                    f"Invalid domain selection: domain numbers must be >= 1 "
                    f"(got {domains})."
                ),
            }

        try:
            mat.selection().set(domains)
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to assign domains: {type(e).__name__}: {e}",
            }

        return {
            "success": True,
            "material": material_name,
            "component": comp_tag,
            "domain_selection": domains,
        }

    @mcp.tool()
    def material_get_property(
        material_name: str,
        property_key: str,
        property_group: str = "def",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Read back a property value from a material in the current model.

        Args:
            material_name: Name (label) or tag of the material.
            property_key: COMSOL property key (e.g. 'thermalconductivity').
            property_group: COMSOL property group tag (default: 'def').
            model_name: Model name (default: current).

        Returns:
            On success: {success: True, value: str, evaluated_value: ...}.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        _, mat, comp_tag = _find_material_in_model(model, material_name)
        if mat is None:
            return {
                "success": False,
                "error": f"Material not found in model: {material_name}",
            }

        try:
            pg = mat.propertyGroup(property_group)
        except Exception:
            pg = None
        if pg is None:
            return {
                "success": False,
                "error": (
                    f"Property group '{property_group}' not found on "
                    f"material '{material_name}'."
                ),
            }

        try:
            value = pg.getString(property_key)
        except Exception:
            try:
                value = pg.get(property_key)
            except Exception as e:
                return {
                    "success": False,
                    "error": (
                        f"Property '{property_key}' not found in group "
                        f"'{property_group}': {type(e).__name__}: {e}"
                    ),
                }

        value_str = str(value) if value is not None else None

        evaluated = None
        try:
            evaluated = float(pg.getDouble(property_key))
        except Exception:
            evaluated = None

        return {
            "success": True,
            "material": material_name,
            "component": comp_tag,
            "property_group": property_group,
            "property_key": property_key,
            "value": value_str,
            "evaluated_value": evaluated,
        }

    @mcp.tool()
    def material_list(
        model_name: Optional[str] = None,
        include_properties: bool = False,
    ) -> dict:
        """
        List materials in the current model.

        Args:
            model_name: Model name (default: current).
            include_properties: If True, include propertyGroup contents.

        Returns:
            {success: True, materials: [...]}
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            materials_out = []
            for comp in list(jm.component()):
                try:
                    comp_tag = str(comp.tag())
                except Exception:
                    comp_tag = "?"
                for m in list(comp.material()):
                    item = {
                        "tag": str(m.tag()) if hasattr(m, "tag") else None,
                        "component": comp_tag,
                    }
                    try:
                        item["name"] = str(m.label())
                    except Exception:
                        item["name"] = item["tag"]
                    try:
                        sel = m.selection()
                        try:
                            domains = list(sel.entities(3)) if sel is not None else []
                        except Exception:
                            domains = []
                        if domains:
                            item["domain_selection"] = [int(x) for x in domains]
                    except Exception:
                        pass

                    if include_properties:
                        groups_out = {}
                        try:
                            for pg in list(m.propertyGroup()):
                                try:
                                    pg_tag = str(pg.tag())
                                except Exception:
                                    continue
                                props = {}
                                try:
                                    for prop in list(pg.properties()):
                                        try:
                                            k = str(prop)
                                            v = pg.getString(k)
                                            props[k] = str(v) if v is not None else None
                                        except Exception:
                                            continue
                                except Exception:
                                    pass
                                groups_out[pg_tag] = props
                        except Exception:
                            pass
                        item["property_groups"] = groups_out

                    materials_out.append(item)

            return {
                "success": True,
                "materials": materials_out,
                "count": len(materials_out),
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to list materials: {type(e).__name__}: {e}",
            }
