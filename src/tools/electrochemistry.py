"""Electrochemistry tools for COMSOL MCP Server.

All interface and feature type names are drawn from a registry that was
verified by creating every type in a real COMSOL 6.3 session (Battery Design,
Electrochemistry, Fuel Cell & Electrolyzer, Electrodeposition, and Corrosion
modules). See electrochemistry_data for the registry itself.
"""

from typing import Optional, Sequence

from mcp.server.fastmcp import FastMCP

from .electrochemistry_data import (
    COMSOL_VERSION,
    ELECTROCHEMISTRY_INTERFACES,
    INTERFACE_CATEGORIES,
)
from .physics import (
    PHYSICS_INTERFACES,
    _find_physics_context,
    _get_component_java,
    _get_geometry_tag,
    _make_tag,
    _set_feature_properties,
)


def _geometry_sdim(comp, geom_tag):
    """Return the space dimension of a geometry (3 for 3D, 2 for 2D/2D-axi)."""
    try:
        return int(comp.geom(geom_tag).getSDim())
    except Exception:
        return None


def _is_axisymmetric(comp, geom_tag):
    try:
        return bool(comp.geom(geom_tag).isAxisymmetric())
    except Exception:
        return False


def _interface_base_dim(phys, comp, geom_tag, sdim):
    """Entity dimension counting as 'domain' for the interface.

    Regular interfaces select whole domains (base = sdim); shell interfaces
    select boundaries (base = sdim - 1), so their feature dimensions shift
    down by one relative to a plain interface.
    """
    try:
        return int(phys.selection().dim())
    except Exception:
        return sdim if sdim is not None else 3


def _registry_entry(physics_type):
    return ELECTROCHEMISTRY_INTERFACES.get(physics_type)


def _feature_registry_entry(physics_type, feature_type):
    entry = _registry_entry(physics_type)
    if not entry:
        return None
    feat = entry["features"].get(feature_type)
    if feat:
        return feat
    for candidate in entry["features"].values():
        subs = candidate.get("subfeatures") or {}
        if feature_type in subs:
            return subs[feature_type]
    return None


def _candidate_dims(reg, base_dim, sdim, axisymmetric):
    """Ordered dimension candidates for a feature on the current geometry."""
    dims = []
    if reg:
        dims_map = reg.get("dims") or {}
        if axisymmetric and "axi" in dims_map:
            dims.extend(d for d in dims_map["axi"] if d not in dims)
        if "3D" in dims_map and sdim == 3:
            dims.extend(d for d in dims_map["3D"] if d not in dims)
        if reg.get("dim_3d") is not None and reg["dim_3d"] not in dims:
            if sdim == 3:
                dims.append(reg["dim_3d"])
        levels = reg.get("levels")
        if levels:
            mapping = {
                "domain": base_dim,
                "boundary": base_dim - 1,
                "edge": 1,
                "point": 0,
            }
            if levels in mapping and mapping[levels] not in dims:
                dims.append(mapping[levels])
    for d in (base_dim, base_dim - 1, 1, 0):
        if d >= 0 and d not in dims:
            dims.append(d)
    return dims


def _resolve_physics(model, physics_name):
    jm = model.java
    context = _find_physics_context(jm, physics_name)
    if context is None:
        return None, None, None
    comp, phys = context
    return jm, comp, phys


def _find_feature_by_ref(owner, ref):
    """Find a feature on an interface (or parent feature) by tag or label."""
    for t in owner.feature().tags():
        tag = str(t)
        feat = owner.feature(tag)
        if tag == ref or str(feat.label()) == ref:
            return feat
    return None


def _find_feature_by_type(phys, feature_type):
    for t in phys.feature().tags():
        tag = str(t)
        feat = phys.feature(tag)
        try:
            if str(feat.getType()) == feature_type:
                return feat
        except Exception:
            continue
    return None


def _apply_selection(feature, boundaries, domains, edges, points, selection_name):
    """Apply a selection to a created feature; returns (mode, entities)."""
    if selection_name:
        feature.selection().named(selection_name)
        return "named", selection_name
    if domains:
        feature.selection().set([int(d) for d in domains])
        return "domains", list(domains)
    if boundaries:
        feature.selection().set([int(b) for b in boundaries])
        return "boundaries", list(boundaries)
    if edges:
        feature.selection().set([int(e) for e in edges])
        return "edges", list(edges)
    if points:
        feature.selection().set([int(p) for p in points])
        return "points", list(points)
    try:
        feature.selection().all()
    except Exception:
        pass
    return "all", None


def _get_model(model_name):
    from .session import session_manager

    return session_manager.get_model(model_name)


def register_electrochemistry_tools(mcp: FastMCP) -> None:
    """Register electrochemistry tools with the MCP server."""

    # Advertise the verified electrochemistry interfaces in the generic
    # physics catalog returned by physics_get_available, so the dedicated
    # tools are discoverable without modifying physics.py itself.
    PHYSICS_INTERFACES.setdefault(
        "Electrochemistry",
        {
            "note": "Use the dedicated electrochemistry_* tools for verified "
                    "battery, fuel cell, electrodeposition, and corrosion "
                    "interfaces (e.g. LithiumIonBatteryMPH, "
                    "SecondaryCurrentDistribution).",
            "lithium_ion_battery": "LithiumIonBatteryMPH",
            "lumped_battery": "LumpedBattery",
            "secondary_current_distribution": "SecondaryCurrentDistribution",
            "electroanalysis": "Electroanalysis",
        },
    )

    @mcp.tool()
    def electrochemistry_get_interfaces(category: Optional[str] = None) -> dict:
        """
        List verified electrochemistry physics interface types for COMSOL 6.3.

        Categories: battery, current_distribution, electroanalysis,
        shell_bem_pipe, corrosion, electrodeposition, fuel_cell_electrolyzer,
        transport. Every interface in this registry was created successfully in
        a real COMSOL 6.3 session.

        Args:
            category: Optional category filter (e.g. "battery")

        Returns:
            Registry of interface types with labels, modules, and feature counts
        """
        result = {}
        for itype, entry in ELECTROCHEMISTRY_INTERFACES.items():
            if category and entry["category"] != category:
                continue
            result[itype] = {
                "label_en": entry["label_en"],
                "label_zh": entry["label_zh"],
                "category": entry["category"],
                "modules": entry["modules"],
                "feature_count": len(entry["features"]),
            }
        return {
            "success": True,
            "comsol_version": COMSOL_VERSION,
            "categories": INTERFACE_CATEGORIES,
            "interfaces": result,
            "note": (
                "Use electrochemistry_add_interface to add one of these types. "
                "Use electrochemistry_get_feature_types to see the verified "
                "feature types of a specific interface."
            ),
        }

    @mcp.tool()
    def electrochemistry_get_feature_types(
        physics_type: str,
        include_subfeatures: bool = True,
    ) -> dict:
        """
        List verified feature types for an electrochemistry physics interface.

        Feature dimensions ("dim_3d") refer to a 3D geometry; "levels" gives the
        entity kind (domain/boundary/edge/point/global). "singleton": true means
        the interface already contains one instance by default - configure the
        existing feature instead of creating a second one. Types listed under
        "design_only_types" appear in the COMSOL GUI as compound items but do
        not exist as raw feature types.

        Args:
            physics_type: Interface type, e.g. "LithiumIonBatteryMPH" or
                "SecondaryCurrentDistribution" (see electrochemistry_get_interfaces)
            include_subfeatures: Include subfeature types (e.g. ElectrodeReaction
                inside ElectrodeSurface)

        Returns:
            Registry of feature types with dimensions and descriptions
        """
        entry = _registry_entry(physics_type)
        if entry is None:
            known = ", ".join(sorted(ELECTROCHEMISTRY_INTERFACES))
            return {
                "success": False,
                "error": f"Unknown electrochemistry interface type: {physics_type}",
                "known_types": known,
            }
        features = {}
        for ftype, feat in entry["features"].items():
            item = dict(feat)
            if not include_subfeatures:
                item.pop("subfeatures", None)
            features[ftype] = item
        return {
            "success": True,
            "interface": physics_type,
            "label_en": entry["label_en"],
            "label_zh": entry["label_zh"],
            "features": features,
            "design_only_types": entry["design_only_types"],
            "note": (
                "Create features with electrochemistry_add_feature. Subfeatures "
                "require parent_feature to point at their parent feature."
            ),
        }

    @mcp.tool()
    def electrochemistry_add_interface(
        physics_type: str,
        physics_tag: Optional[str] = None,
        component_name: Optional[str] = None,
        geometry_name: Optional[str] = None,
        domain_selection: Optional[Sequence[int]] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Add a verified electrochemistry physics interface to the model.

        Examples: LithiumIonBatteryMPH (lithium-ion battery), LumpedBattery,
        SingleParticleBattery, SecondaryCurrentDistribution, HydrogenFuelCell,
        ElectrodepositionSecondary, CorrosionSecondary, Electroanalysis.
        See electrochemistry_get_interfaces for the full verified list.

        Args:
            physics_type: Interface type from the electrochemistry registry
            physics_tag: Physics interface tag (default: generated from type)
            component_name: Component to add physics to (default: first component)
            geometry_name: Geometry sequence tag (default: first geometry)
            domain_selection: Domain numbers (default: all domains)
            model_name: Model name (default: current model)

        Returns:
            Created interface info including its default features
        """
        model = _get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        reg = _registry_entry(physics_type)
        if reg is None:
            return {
                "success": False,
                "error": (
                    f"'{physics_type}' is not in the verified electrochemistry "
                    "registry. Call electrochemistry_get_interfaces for valid "
                    "types, or use the generic physics_add tool to bypass the "
                    "registry."
                ),
            }

        try:
            jm = model.java
            comp = _get_component_java(jm, component_name)
            geom_tag = _get_geometry_tag(comp, geometry_name)
            tag = physics_tag or _make_tag(physics_type.lower()[:12])
            phys = comp.physics().create(tag, physics_type, geom_tag)
            if domain_selection:
                phys.selection().set([int(d) for d in domain_selection])
        except Exception as e:
            return {"success": False, "error": f"Failed to add interface: {str(e)}"}

        defaults = {}
        try:
            for t in phys.feature().tags():
                ftag = str(t)
                try:
                    defaults[ftag] = str(phys.feature(ftag).getType())
                except Exception:
                    defaults[ftag] = None
        except Exception:
            pass

        return {
            "success": True,
            "physics": {
                "type": physics_type,
                "label": str(phys.label()),
                "tag": tag,
                "component": str(comp.tag()),
                "geometry": geom_tag,
                "label_en": reg["label_en"],
                "label_zh": reg["label_zh"],
                "domain_selection": list(domain_selection) if domain_selection else "all",
                "default_features": defaults,
            },
            "next_steps": (
                "Configure features with electrochemistry_add_feature (e.g. "
                "ElectrodeSurface on electrode boundaries, PorousElectrode on "
                "electrode domains). Singleton features are present by default; "
                "see electrochemistry_get_feature_types."
            ),
        }

    @mcp.tool()
    def electrochemistry_list_features(
        physics_name: str,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        List all features of a physics interface with their actual COMSOL types.

        Uses feature.getType() introspection, so it works for any physics
        interface, including those added with the generic physics tools.

        Args:
            physics_name: Physics interface name, tag, or label
            model_name: Model name (default: current model)

        Returns:
            Feature list: tag, label, type, selection info
        """
        model = _get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        try:
            jm, comp, phys = _resolve_physics(model, physics_name)
            if phys is None:
                return {
                    "success": False,
                    "error": f"Physics interface not found: {physics_name}",
                }
        except Exception as e:
            return {"success": False, "error": f"Failed to find physics: {str(e)}"}

        features = []
        try:
            for t in phys.feature().tags():
                ftag = str(t)
                feat = phys.feature(ftag)
                item = {"tag": ftag, "label": str(feat.label())}
                try:
                    item["type"] = str(feat.getType())
                except Exception:
                    item["type"] = None
                try:
                    sel = feat.selection()
                    try:
                        item["selection_entities"] = [int(x) for x in sel.entities()]
                    except Exception:
                        item["selection_entities"] = None
                    try:
                        item["selection_dim"] = int(sel.dim())
                    except Exception:
                        pass
                except Exception:
                    pass
                features.append(item)
        except Exception as e:
            return {"success": False, "error": f"Failed to list features: {str(e)}"}

        interface_type = None
        try:
            interface_type = str(phys.getType())
        except Exception:
            pass

        return {
            "success": True,
            "physics": physics_name,
            "interface_type": interface_type,
            "features": features,
            "count": len(features),
        }

    @mcp.tool()
    def electrochemistry_add_feature(
        physics_name: str,
        feature_type: str,
        physics_type_hint: Optional[str] = None,
        parent_feature: Optional[str] = None,
        boundaries: Optional[Sequence[int]] = None,
        domains: Optional[Sequence[int]] = None,
        edges: Optional[Sequence[int]] = None,
        points: Optional[Sequence[int]] = None,
        selection_name: Optional[str] = None,
        dimension: Optional[int] = None,
        properties: Optional[dict] = None,
        feature_tag: Optional[str] = None,
        label: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Add a verified electrochemistry feature to a physics interface.

        Feature types come from electrochemistry_get_feature_types. The feature
        dimension is resolved automatically from the verified registry and the
        model geometry; pass "dimension" explicitly to override.

        Subfeatures (e.g. ElectrodeReaction or DoubleLayerCapacitance inside
        ElectrodeSurface, PorousElectrodeReaction inside PorousElectrode)
        require parent_feature set to the parent feature's tag or label.

        If the registry marks the type as singleton, the existing default
        instance is reused (and configured) instead of creating a duplicate.

        Args:
            physics_name: Physics interface name, tag, or label
            feature_type: Verified feature type, e.g. "ElectrodeSurface",
                "PorousElectrode", "ExternalShort", "ElectrodeReaction"
            physics_type_hint: Interface type from the registry (e.g.
                "LithiumIonBatteryMPH"); enables registry-driven dimension
                resolution and singleton handling
            parent_feature: Tag or label of the parent feature for subfeatures
            boundaries: Boundary numbers for boundary features
            domains: Domain numbers for domain features
            edges: Edge numbers for edge features
            points: Point numbers for point features
            selection_name: Named selection instead of explicit numbers
            dimension: Explicit geometric entity dimension override
            properties: Property dict; rejected properties are reported, not
                silently ignored
            feature_tag: Explicit feature tag (default: generated)
            label: Display label for the feature
            model_name: Model name (default: current model)

        Returns:
            Created (or reused) feature info, including type readback and any
            property-setting failures
        """
        model = _get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        try:
            jm, comp, phys = _resolve_physics(model, physics_name)
            if phys is None:
                return {
                    "success": False,
                    "error": f"Physics interface not found: {physics_name}",
                }
        except Exception as e:
            return {"success": False, "error": f"Failed to find physics: {str(e)}"}

        reg = None
        if physics_type_hint:
            reg = _feature_registry_entry(physics_type_hint, feature_type)
            if reg is None:
                return {
                    "success": False,
                    "error": (
                        f"'{feature_type}' is not a verified feature of "
                        f"{physics_type_hint}. Call "
                        "electrochemistry_get_feature_types for the verified list."
                    ),
                }

        parent = None
        if parent_feature:
            parent = _find_feature_by_ref(phys, parent_feature)
            if parent is None:
                return {"success": False, "error": f"Parent feature not found: {parent_feature}"}

        geom_tag = _get_geometry_tag(comp)
        sdim = _geometry_sdim(comp, geom_tag) or 3
        axi = _is_axisymmetric(comp, geom_tag)
        base_dim = _interface_base_dim(phys, comp, geom_tag, sdim)

        # singleton handling: reuse the existing default instance if present
        singleton = bool(reg and reg.get("singleton"))
        if singleton:
            search_owner = parent if parent is not None else phys
            existing = _find_feature_by_type(search_owner, feature_type)
            if existing is not None:
                property_failures = {}
                if properties:
                    property_failures = _set_feature_properties(existing, properties)
                result = {
                    "success": not property_failures,
                    "feature": {
                        "tag": str(existing.tag()),
                        "type": feature_type,
                        "reused_default_instance": True,
                        "parent_feature": parent_feature,
                        "properties": properties or {},
                    },
                    "note": (
                        f"'{feature_type}' is a singleton feature; the existing "
                        "default instance was reused. COMSOL does not allow a "
                        "second instance."
                    ),
                }
                if property_failures:
                    result["property_errors"] = property_failures
                return result

        if dimension is not None:
            dims = [int(dimension)]
        else:
            dims = _candidate_dims(reg, base_dim, sdim, axi)
            if parent is not None:
                try:
                    pdim = int(parent.selection().dim())
                    dims = [pdim] + [d for d in dims if d != pdim]
                except Exception:
                    pass

        tag = feature_tag or _make_tag(feature_type.lower()[:12])
        created = None
        used_dim = None
        errors = []
        for dim in dims:
            try:
                if parent is not None:
                    feat = parent.create(tag, feature_type, dim)
                else:
                    feat = phys.create(tag, feature_type, dim)
                created = feat
                used_dim = dim
                break
            except Exception as e:
                errors.append(f"dim {dim}: {str(e).replace(chr(10), ' ')[:110]}")
        if created is None and parent is None and dimension is None:
            # global features are created without a dimension argument
            try:
                created = phys.create(tag, feature_type)
                used_dim = "global"
            except Exception as e:
                errors.append(f"global: {str(e).replace(chr(10), ' ')[:110]}")

        if created is None:
            detail = "; ".join(errors)
            hint = ""
            if reg:
                hint = (
                    "Verified dimensions for this feature: "
                    f"{reg.get('dims') or reg.get('dim_3d')}"
                )
            return {
                "success": False,
                "error": f"Could not create '{feature_type}'. Tried: {detail}",
                "hint": hint,
            }

        sel_mode, sel_value = _apply_selection(
            created, boundaries, domains, edges, points, selection_name,
        )
        property_failures = {}
        if properties:
            property_failures = _set_feature_properties(created, properties)
        if label:
            created.label(label)

        try:
            type_readback = str(created.getType())
        except Exception:
            type_readback = None

        result = {
            "success": not property_failures,
            "feature": {
                "tag": tag,
                "type": feature_type,
                "dimension": used_dim,
                "selection_mode": sel_mode,
                "selection": sel_value,
                "properties": properties or {},
                "parent_feature": parent_feature,
                "type_readback": type_readback,
            },
        }
        if property_failures:
            result["property_errors"] = property_failures
            result["hint"] = (
                "Some properties were rejected. Call "
                "electrochemistry_list_feature_properties to see the valid "
                "property names for this feature."
            )
        return result

    @mcp.tool()
    def electrochemistry_set_feature_selection(
        physics_name: str,
        feature_tag: str,
        boundaries: Optional[Sequence[int]] = None,
        domains: Optional[Sequence[int]] = None,
        edges: Optional[Sequence[int]] = None,
        points: Optional[Sequence[int]] = None,
        selection_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Set the geometric selection of an existing physics feature.

        Mainly used to assign domains/boundaries to the default features that
        every electrochemistry interface creates automatically (e.g. the
        Separator feature of a lithium-ion battery interface).

        Args:
            physics_name: Physics interface name, tag, or label
            feature_tag: Feature tag (or label) to reselect
            boundaries: Boundary numbers to select
            domains: Domain numbers to select
            edges: Edge numbers to select
            points: Point numbers to select
            selection_name: Named selection instead of explicit numbers
            model_name: Model name (default: current model)

        Returns:
            Applied selection info
        """
        model = _get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        try:
            jm, comp, phys = _resolve_physics(model, physics_name)
            if phys is None:
                return {
                    "success": False,
                    "error": f"Physics interface not found: {physics_name}",
                }
        except Exception as e:
            return {"success": False, "error": f"Failed to find physics: {str(e)}"}

        target = _find_feature_by_ref(phys, feature_tag)
        if target is None:
            return {"success": False, "error": f"Feature not found: {feature_tag}"}

        try:
            mode, value = _apply_selection(
                target, boundaries, domains, edges, points, selection_name,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to set selection: {str(e)}"}
        return {
            "success": True,
            "feature": feature_tag,
            "type": str(target.getType()) if hasattr(target, "getType") else None,
            "selection_mode": mode,
            "selection": value,
        }

    @mcp.tool()
    def electrochemistry_list_interface_properties(
        physics_name: str,
        property_group: str,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        List the properties of an interface-level property group.

        Electrochemistry interfaces keep their main settings in interface-level
        property groups rather than features. The most important group of the
        battery interfaces is "BatterySettings" (cell capacity Q_cell0, initial
        state of charge SOC_cell0, applied current I_app, charge/discharge
        cycling, ...). Other groups include "CellEquilibriumPotentialProp".
        Use electrochemistry_get_interfaces first if unsure which interface
        you have.

        Args:
            physics_name: Physics interface name, tag, or label
            property_group: Group tag, e.g. "BatterySettings"
            model_name: Model name (default: current model)

        Returns:
            Property names and current values of the group
        """
        model = _get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        try:
            jm, comp, phys = _resolve_physics(model, physics_name)
            if phys is None:
                return {
                    "success": False,
                    "error": f"Physics interface not found: {physics_name}",
                }
            group = phys.prop(property_group)
            names = [str(n) for n in group.properties()]
        except Exception as e:
            return {"success": False, "error": f"Failed to access property group: {str(e)}"}

        values = {}
        for name in names:
            if name.startswith(("minput_", "showPhysics", "StudyStep", "pairContrib",
                                "constraint", "item.")):
                continue
            try:
                values[name] = str(group.getString(name))
            except Exception:
                values[name] = None
        return {
            "success": True,
            "physics": physics_name,
            "property_group": property_group,
            "properties": values,
            "count": len(values),
        }

    @mcp.tool()
    def electrochemistry_set_interface_properties(
        physics_name: str,
        property_group: str,
        properties: dict,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Set properties on an interface-level property group of an
        electrochemistry interface.

        This is how the main battery settings are configured - they are NOT
        feature properties. Common example (LumpedBattery "BatterySettings"):
        - Q_cell0: cell capacity, e.g. "2.3[A*h]"
        - SOC_cell0: initial state of charge, e.g. "1.0"
        - I_app: applied current; negative = discharge, e.g. "-2.3[A]"
        - AddFormationLoss: "0" to disable the default 5% formation loss
        - LumpedBatModel: "LumpedCell" (default) or "TwoElectrodes"

        Args:
            physics_name: Physics interface name, tag, or label
            property_group: Group tag, e.g. "BatterySettings"
            properties: Property dict, e.g. {"I_app": "-2.3[A]"}
            model_name: Model name (default: current model)

        Returns:
            Per-property acceptance report with the new values
        """
        model = _get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        try:
            jm, comp, phys = _resolve_physics(model, physics_name)
            if phys is None:
                return {
                    "success": False,
                    "error": f"Physics interface not found: {physics_name}",
                }
            group = phys.prop(property_group)
        except Exception as e:
            return {"success": False, "error": f"Failed to access property group: {str(e)}"}

        failures = _set_feature_properties(group, properties)
        updated = {}
        for name in properties:
            try:
                updated[name] = str(group.getString(name))
            except Exception:
                updated[name] = None
        return {
            "success": not failures,
            "physics": physics_name,
            "property_group": property_group,
            "requested": properties,
            "updated_values": updated,
            "property_errors": failures or None,
        }

    @mcp.tool()
    def electrochemistry_set_feature_properties(
        physics_name: str,
        feature_tag: str,
        properties: dict,
        parent_tag: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Set properties on an existing electrochemistry feature.

        Rejected properties are reported explicitly. Use
        electrochemistry_list_feature_properties to discover valid property
        names for a feature.

        Args:
            physics_name: Physics interface name, tag, or label
            feature_tag: Feature tag (or label) to configure
            properties: Property dict, e.g. {"epsilons": "0.6", "epsilonl": "0.3"}
            parent_tag: Parent feature tag if feature_tag is a subfeature
            model_name: Model name (default: current model)

        Returns:
            Per-property acceptance report
        """
        model = _get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        try:
            jm, comp, phys = _resolve_physics(model, physics_name)
            if phys is None:
                return {
                    "success": False,
                    "error": f"Physics interface not found: {physics_name}",
                }
        except Exception as e:
            return {"success": False, "error": f"Failed to find physics: {str(e)}"}

        if parent_tag:
            parent = _find_feature_by_ref(phys, parent_tag)
            if parent is None:
                return {"success": False, "error": f"Parent feature not found: {parent_tag}"}
            target = _find_feature_by_ref(parent, feature_tag)
        else:
            target = _find_feature_by_ref(phys, feature_tag)
        if target is None:
            return {"success": False, "error": f"Feature not found: {feature_tag}"}

        failures = _set_feature_properties(target, properties)
        return {
            "success": not failures,
            "feature": feature_tag,
            "properties": properties,
            "property_errors": failures or None,
            "hint": (
                "Call electrochemistry_list_feature_properties for valid names."
                if failures else None
            ),
        }

    @mcp.tool()
    def electrochemistry_list_feature_properties(
        physics_name: str,
        feature_tag: str,
        parent_tag: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        List the property names of an electrochemistry feature.

        Returns COMSOL's own list of valid property names for the feature, so
        use it before setting properties on unfamiliar features.

        Args:
            physics_name: Physics interface name, tag, or label
            feature_tag: Feature tag (or label) to introspect
            parent_tag: Parent feature tag if feature_tag is a subfeature
            model_name: Model name (default: current model)

        Returns:
            Sorted list of valid property names
        """
        model = _get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }
        try:
            jm, comp, phys = _resolve_physics(model, physics_name)
            if phys is None:
                return {
                    "success": False,
                    "error": f"Physics interface not found: {physics_name}",
                }
        except Exception as e:
            return {"success": False, "error": f"Failed to find physics: {str(e)}"}

        if parent_tag:
            parent = _find_feature_by_ref(phys, parent_tag)
            if parent is None:
                return {"success": False, "error": f"Parent feature not found: {parent_tag}"}
            target = _find_feature_by_ref(parent, feature_tag)
        else:
            target = _find_feature_by_ref(phys, feature_tag)
        if target is None:
            return {"success": False, "error": f"Feature not found: {feature_tag}"}

        try:
            names = [str(n) for n in target.properties()]
        except Exception as e:
            return {"success": False, "error": f"Failed to list properties: {str(e)}"}
        try:
            ftype = str(target.getType())
        except Exception:
            ftype = None
        return {
            "success": True,
            "feature": feature_tag,
            "type": ftype,
            "properties": sorted(names),
            "count": len(names),
        }
