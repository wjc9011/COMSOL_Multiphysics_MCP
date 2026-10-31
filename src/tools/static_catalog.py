"""Static COMSOL catalog tools (no live session required).

Data source: the declarative registries shipped with every COMSOL install -
plugins/com.comsol.*.jar plugin.xml (OSGi extension points) and
data/completion/*.xml - extracted offline by scripts/generate_static_catalog.py,
including RESOLVED property schemas: completion tokens are decoded via the
in-file <Properties>/<Features> dictionaries, so every physics feature type
carries its property names, types and defaults (117k property rows on a full
6.3 install). Covers ALL installed modules, not just electrochemistry.

Use these tools to validate type names AND discover property names BEFORE
calling the live create/feature tools (physics_add, physics_feature_create,
physics_feature_set, electrochemistry_add_feature, multiphysics_add, ...).
"""

import json
import os
from typing import Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "resources", "static_catalog")

_LAYER_DOC = {
    "geometry": "geometry feature types (geom.xml)",
    "mesh": "mesh feature types (mesh.xml)",
    "solver": "top-level solver feature types (sol.xml)",
    "solver_sub": "solver sub-feature types (linear/nonlinear solvers, adaption...)",
    "study": "study step types (study.xml)",
    "function": "function types incl. DNN/GaussianProcess/PolynomialChaosExpansion (func.xml)",
    "plot": "plot types (result.xml)",
    "numerical": "numerical evaluation node types (numerical.xml)",
    "dataset": "dataset types (dataset.xml)",
    "export": "export types (export.xml)",
}


def _load(name):
    with open(os.path.join(_DATA_DIR, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def _catalog_info() -> dict:
    return _load("meta")


def _search_interfaces(query: str) -> list:
    q = query.lower()
    interfaces = _load("physics_interfaces")
    out = []
    for i in interfaces:
        hay = " ".join(filter(None, [i.get("id"), i.get("shortname"), i.get("module")])).lower()
        if q in hay:
            out.append({
                "id": i["id"],
                "shortname": i.get("shortname"),
                "module": i["module"],
                "sdimtypes": i.get("sdimtypes"),
                "license": i.get("license"),
                "n_features": len(i["features"]),
            })
    return out


def _interface_features(interface_id: str) -> Optional[dict]:
    interfaces = _load("physics_interfaces")
    for i in interfaces:
        if i["id"].lower() == interface_id.lower():
            return {
                "id": i["id"],
                "module": i["module"],
                "sdimtypes": i.get("sdimtypes"),
                "license": i.get("license"),
                "features": i["features"],
            }
    return None


def _search_couplings(query: str) -> list:
    q = query.lower()
    out = []
    for c in _load("couplings"):
        hay = " ".join(filter(None, [c.get("id"), c.get("descr"), c.get("module")])).lower()
        if q in hay:
            out.append(c)
    return out


def _layer_types(layer: str) -> list:
    layers = _load("layer_types")
    return layers.get(layer)


def _physics_schema(interface_id: str) -> Optional[dict]:
    schemas = _load("physics_schemas")
    if interface_id in schemas:
        return schemas[interface_id]
    low = interface_id.lower()
    for k, v in schemas.items():
        if k.lower() == low:
            return v
    return None


def _layer_schema(layer: str, type_name: str) -> Optional[list]:
    schemas = _load("layer_property_schemas")
    sch = schemas.get(layer)
    if not sch:
        return None
    if type_name in sch:
        return sch[type_name]
    low = type_name.lower()
    for k, v in sch.items():
        if k.lower() == low:
            return v
    return None


def _variables_search(query: str, module: Optional[str] = None,
                      name_only: bool = False, limit: int = 50) -> list:
    """Search the extracted physics variable dictionary (19k variables with
    defining expressions, extracted offline from the decompiled mphphb
    builder classes). Query matches variable name (and expression unless
    name_only); module filters by com.comsol module (bfc, cfd, heat, ...)."""
    db = _load("physics_variables")
    q = query.lower()
    out = []
    for cls, data in db.items():
        mod = cls.split("/")[2] if cls.count("/") >= 2 else ""
        if module and module.lower() not in mod:
            continue
        cname = cls.split("/")[-1]
        for v in data.get("variables", []):
            hay = v["name"].lower() if name_only else (
                v["name"] + " " + (v.get("expression") or "") + " " +
                (v.get("descr") or "")).lower()
            if q in hay:
                out.append({"class": cname, "module": mod, "name": v["name"],
                            "expression": (v.get("expression") or "")[:160],
                            "statevar": v.get("label") == "STATEVAR"})
                if len(out) >= limit:
                    return out
    return out


def _discretization(interface_id: str) -> Optional[dict]:
    disc = _load("discretization_index")
    if interface_id in disc:
        return disc[interface_id]
    low = interface_id.lower()
    for k, v in disc.items():
        if k.lower() == low:
            return v
    return None


def _merged_interface(interface_id: str) -> Optional[dict]:
    """Registry entry (plugin.xml tree) merged with resolved property schemas
    from completion data (per-feature props/subfeatures, interface-level props)."""
    iface = _interface_features(interface_id)
    if iface is None:
        return None
    schema = _physics_schema(iface["id"])
    if schema is None:
        return iface
    iface["interface_props"] = schema.get("interface_props", {})
    by_name = {f["name"]: f for f in schema.get("features", []) if "name" in f}
    for f in iface["features"]:
        s = by_name.get(f["id"])
        if s:
            if s.get("props"):
                f["props"] = s["props"]
            if s.get("subfeatures"):
                f["subfeatures"] = s["subfeatures"]
    extra = [f for f in schema.get("features", []) if f.get("name") not in
             {x["id"] for x in iface["features"]}]
    if extra:
        iface["completion_only_features"] = extra
    return iface


def register_static_catalog_tools(mcp) -> None:
    """Register static catalog tools with the MCP server."""
    from mcp.server.fastmcp import FastMCP  # noqa: F401  (type kept local)

    @mcp.tool()
    def static_catalog_info() -> dict:
        """Show what the offline static COMSOL catalog covers (version, counts,
        data provenance). Works without a running COMSOL session."""
        info = _catalog_info()
        info["layers"] = _LAYER_DOC
        return info

    @mcp.tool()
    def static_catalog_search(query: str, kind: Optional[str] = None) -> dict:
        """Search physics interfaces and multiphysics couplings in the offline
        static catalog (all modules). Matches interface id / shortname / module
        (e.g. "battery", "LithiumIonBatteryMPH", "NonIsothermalFlow").

        kind: "interface", "coupling", or None for both.

        Returns id, module jar, supported space dimensions (sdimtypes), license
        feature string, and feature count per interface. Use before
        physics_add / multiphysics_add to validate type names and license needs.
        """
        kind = (kind or "both").lower()
        result = {}
        if kind in ("interface", "both"):
            result["interfaces"] = _search_interfaces(query)[:50]
        if kind in ("coupling", "both"):
            result["couplings"] = _search_couplings(query)[:50]
        return result

    @mcp.tool()
    def static_catalog_features(interface_id: str) -> dict:
        """List the feature types of a physics interface AND their property
        schemas from the offline static catalog: feature names, nesting
        (plugin.xml registry), domain levels, singleton flags, plus RESOLVED
        property lists (name/type/default) per feature from completion data.
        No live session needed.

        Example: interface_id="LithiumIonBatteryMPH" shows PorousElectrode
        with its 80+ properties (sigmal, Dl, taul, epsl, ...) - exactly what
        physics_feature_set accepts for that feature.
        """
        iface = _merged_interface(interface_id)
        if iface is None:
            return {"error": f"interface '{interface_id}' not found; use "
                             "static_catalog_search to discover ids"}
        return iface

    @mcp.tool()
    def static_catalog_properties(layer: str, type_name: str) -> dict:
        """Resolved property schema (name/type/default) for ONE feature type
        of a modeling layer, from the offline static catalog. Layers:
        geometry, mesh, solver, solver_sub, study, function, plot (limited),
        numerical, dataset, export.

        Example: layer="mesh", type_name="Size" -> hmax/hmin/hcurve/hgrad/
        hnarrow/hauto/custom with types and defaults.
        """
        props = _layer_schema(layer, type_name)
        if props is None:
            available = sorted(_load("layer_property_schemas").get(layer, {}))
            return {"error": f"no schema for '{type_name}' in layer '{layer}'",
                    "layer_types_with_schema": available[:80]}
        return {"layer": layer, "type": type_name, "properties": props,
                "note": _LAYER_DOC.get(layer, "")}

    @mcp.tool()
    def static_catalog_variables(query: str, module: Optional[str] = None,
                                 name_only: bool = False) -> dict:
        """Search the offline physics VARIABLE dictionary: ~19,000 variables
        with their defining expressions, extracted from the decompiled
        physics-builder classes (all modules). Use it before writing
        postprocessing expressions to discover what an interface defines.

        Examples: query="eta" module="bfc" finds overpotential definitions
        (eta = Ect-item.Eeq); query="itotavg" finds averaged current density.
        statevar=true marks state variables (initial/update expressions).
        Set name_only=True to match variable names only.
        """
        hits = _variables_search(query, module, name_only)
        return {"query": query, "module": module, "n_hits": len(hits),
                "variables": hits}

    @mcp.tool()
    def static_catalog_discretization(interface_id: str) -> dict:
        """Default discretization (element orders per field + boundary-flux
        switches) for a physics interface, from the offline static catalog.
        Explains e.g. why LaminarFlow needs stabilization (P1P1 equal-order).

        Example: interface_id="LithiumIonBatteryMPH" -> order_concentration=2,
        order_electricpotential=2.
        """
        d = _discretization(interface_id)
        if d is None:
            disc = _load("discretization_index")
            return {"error": f"no discretization entry for '{interface_id}'",
                    "n_known": len(disc)}
        return {"interface": interface_id, **d}

    @mcp.tool()
    def static_catalog_layer(layer: str) -> dict:
        """List known type names for one modeling layer from the offline static
        catalog: geometry, mesh, solver, solver_sub, study, function, plot,
        numerical, dataset, export. Use to validate feature/study/function type
        strings before creating them in a live session."""
        types = _layer_types(layer)
        if types is None:
            return {"error": f"unknown layer '{layer}'", "available": sorted(_LAYER_DOC)}
        return {"layer": layer, "types": types,
                "note": _LAYER_DOC.get(layer, "")}
