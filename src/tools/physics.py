"""Physics tools for COMSOL MCP Server."""

from typing import Optional, Sequence
from mcp.server.fastmcp import FastMCP

from .session import session_manager


PHYSICS_INTERFACES = {
    "AC/DC": {
        "electrostatic": "Electrostatics (es)",
        "electric_currents": "Electric Currents (ec)",
        "magnetic_fields": "Magnetic Fields (mf)",
        "electromagnetic_waves": "Electromagnetic Waves (emw)",
    },
    "Structural": {
        "solid_mechanics": "Solid Mechanics (solid)",
        "shell": "Shell (shell)",
        "beam": "Beam (beam)",
        "membrane": "Membrane (memb)",
    },
    "Heat Transfer": {
        "heat_transfer": "Heat Transfer in Solids (ht)",
        "conjugate_ht": "Conjugate Heat Transfer (cht)",
        "radiation": "Radiation (rad)",
    },
    "Fluid Flow": {
        "laminar_flow": "Laminar Flow (spf)",
        "turbulent_flow": "Turbulent Flow (spf)",
        "creeping_flow": "Creeping Flow (brinkman)",
    },
    "Acoustics": {
        "pressure_acoustics": "Pressure Acoustics (acpr)",
        "thermoacoustics": "Thermoacoustics (ta)",
    },
    "Chemical": {
        "transport_diluted": "Transport of Diluted Species (tds)",
        "reaction_engineering": "Reaction Engineering (re)",
    },
    "Optics": {
        "ray_optics": "Geometrical Optics (gop)",
        "wave_optics": "Wave Optics (ewfd)",
    },
    "Multiphysics": {
        "thermal_stress": "Thermal Stress (ts)",
        "fluid_structure": "Fluid-Structure Interaction (fsi)",
        "electromechanical": "Electromechanical Forces",
        "joule_heating": "Joule Heating (jh)",
    },
}


# Maps user-facing physics_type -> (java_type_name, default_tag).
# Accepts the canonical Java type name (e.g. "HeatTransfer"), the
# short tag (e.g. "ht"), and the snake_case key used by
# PHYSICS_INTERFACES / physics_get_available (e.g. "heat_transfer").
# Step 6b: introduced so physics_get_available output can be fed
# directly into physics_add without extra translation.
PHYSICS_TYPE_ALIASES: dict[str, tuple[str, str]] = {
    # Electrostatics
    "es": ("Electrostatics", "es"),
    "electrostatic": ("Electrostatics", "es"),
    "electrostatics": ("Electrostatics", "es"),
    "Electrostatics": ("Electrostatics", "es"),
    # Electric Currents
    "ec": ("ElectricCurrents", "ec"),
    "electric_currents": ("ElectricCurrents", "ec"),
    "ElectricCurrents": ("ElectricCurrents", "ec"),
    # Magnetic Fields
    "mf": ("MagneticFields", "mf"),
    "magnetic_fields": ("MagneticFields", "mf"),
    "MagneticFields": ("MagneticFields", "mf"),
    # Solid Mechanics
    "solid": ("SolidMechanics", "solid"),
    "solid_mechanics": ("SolidMechanics", "solid"),
    "SolidMechanics": ("SolidMechanics", "solid"),
    # Heat Transfer
    "ht": ("HeatTransfer", "ht"),
    "heat_transfer": ("HeatTransfer", "ht"),
    "HeatTransfer": ("HeatTransfer", "ht"),
    # Laminar Flow
    "spf": ("LaminarFlow", "spf"),
    "laminar_flow": ("LaminarFlow", "spf"),
    "LaminarFlow": ("LaminarFlow", "spf"),
}


def _resolve_physics_type(physics_type: str) -> tuple[Optional[str], Optional[str]]:
    """Return (java_type_name, default_tag) for a user-facing physics
    type string, or (None, None) if unknown."""
    return PHYSICS_TYPE_ALIASES.get(physics_type, (None, None))


def _get_comp_and_geom_tag(model, component_name: Optional[str] = None):
    """Resolve the target Java component + first geometry tag.

    Returns (comp_java, geom_tag, error_str). On error, comp/tag
    are None and error_str describes the failure.
    """
    try:
        jm = model.java
        if component_name:
            comp = jm.component(component_name)
            if comp is None:
                return None, None, (
                    f"Component '{component_name}' not found. "
                    "Create it with model_create_component first."
                )
        else:
            comps = list(jm.component())
            if not comps:
                return None, None, (
                    "No components in model. Create one with "
                    "model_create_component first."
                )
            comp = comps[0]
        geom_tags = [str(g.tag()) for g in comp.geom()]
        if not geom_tags:
            return comp, None, (
                "No geometry in component. Create one with "
                "geometry_create first."
            )
        return comp, geom_tags[0], None
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"


def _geom_sdim(comp, geom_tag: str) -> Optional[int]:
    """Return the space dimension of a geometry sequence, or None.

    Mirrors ``mesh.py::_geom_sdim`` (Pilot 07 v2 caveat E1 fix). The
    canonical Java accessor is ``GeomSequence.sDim()`` (camelCase D);
    JPype is case-sensitive and the lowercase ``sdim`` alias is not
    always present on raw Java handles. Falls back through every alias
    we have observed across mph wrapper / Java bridge versions, and
    extracts the leading digit from string returns like ``"3D"`` /
    ``"2Daxi"``.

    Used by ``physics_boundary_selection`` and ``physics_configure_
    boundary`` to auto-infer ``selection_dim = sdim - 1`` (the boundary
    dim) when the caller does not pass it explicitly. The Solid
    Mechanics fix needs the dim because Pilot 08 (comsol_12681_force)
    measured: a PointLoad (natural dim=0) cannot be created with the
    boundary-default 2-arg ``physics.create(tag, type)`` path on a 3D
    geometry — COMSOL maps the selection to dim=2 (faces) and rejects
    point indices. The 3-arg ``physics.create(tag, type, dim)`` form
    fixes this and is what the ground-truth Java exports use.
    """
    try:
        g = comp.geom(geom_tag)
    except Exception:
        return None
    for accessor in (
        "sdim",
        "sDim",
        "dimension",
        "getSDim",
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
        except Exception:
            continue
    return None


def _resolve_selection_dim(
    comp,
    geom_tag: Optional[str],
    selection_dim: Optional[int],
) -> Optional[int]:
    """Resolve the selection dimension for a boundary-condition feature.

    If ``selection_dim`` is explicitly provided (including 0 for
    point-level selections — Pilot 08 PointLoad path), return it as-is
    after a 0–3 sanity clamp. Otherwise, auto-infer ``sdim - 1`` from
    the geometry (the boundary dim), matching the natural dim of
    boundary BCs like Fixed / BoundaryLoad / HeatFlux.

    Returns ``None`` if neither path resolves a valid dim — in that
    case callers should fall back to the 2-arg ``physics.create``
    form (let COMSOL pick the feature's natural dim).
    """
    if selection_dim is not None:
        try:
            d = int(selection_dim)
        except (TypeError, ValueError):
            return None
        if d in (0, 1, 2, 3):
            return d
        return None
    if geom_tag is None:
        return None
    sdim = _geom_sdim(comp, geom_tag)
    if sdim is None or sdim not in (1, 2, 3):
        return None
    return sdim - 1


def _add_physics_interface(
    model,
    physics_type: str,
    component_name: Optional[str],
    explicit_tag: Optional[str] = None,
    geometry_tag: Optional[str] = None,
) -> dict:
    """Internal: create a physics interface via the Java API.

    Implements the canonical
        ``comp.physics().create(tag, type, geom_tag)``
    pattern. Returns the standard {"success": ..., ...} dict shape
    used by the public MCP tools.
    """
    type_name, default_tag = _resolve_physics_type(physics_type)
    if type_name is None:
        return {
            "success": False,
            "error": (
                f"Unknown physics type '{physics_type}'. Known: "
                f"{sorted(set(PHYSICS_TYPE_ALIASES))}"
            ),
        }

    comp, geom_tag, err = _get_comp_and_geom_tag(model, component_name)
    if err:
        return {"success": False, "error": err}
    if geometry_tag:
        geom_tag = geometry_tag
    use_tag = explicit_tag or default_tag

    try:
        phys = comp.physics().create(use_tag, type_name, geom_tag)
        try:
            label = str(phys.label())
        except Exception:
            label = use_tag
        return {
            "success": True,
            "physics": {
                "name": label,
                "type": type_name,
                "tag": use_tag,
                "component": str(comp.tag()),
                "geometry": geom_tag,
            },
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to add physics: {type(e).__name__}: {e}",
        }


def register_physics_tools(mcp: FastMCP) -> None:
    """Register physics tools with the MCP server."""
    
    @mcp.tool()
    def physics_list(model_name: Optional[str] = None) -> dict:
        """
        List all physics interfaces defined in a model.
        
        Args:
            model_name: Model name (default: current model)
        
        Returns:
            List of physics interface names
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            physics = model.physics()
            multiphysics = model.multiphysics()
            
            return {
                "success": True,
                "physics": physics,
                "multiphysics": multiphysics,
                "physics_count": len(physics),
                "multiphysics_count": len(multiphysics),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list physics: {str(e)}"}
    
    @mcp.tool()
    def physics_get_available() -> dict:
        """
        Get a list of available physics interfaces organized by category.
        
        Returns:
            Dictionary of physics categories and their interfaces
        """
        return {
            "success": True,
            "interfaces": PHYSICS_INTERFACES,
            "note": "Interface identifiers (in parentheses) are used when adding physics.",
        }
    
    @mcp.tool()
    def physics_add(
        physics_type: str,
        component_name: Optional[str] = None,
        geometry_tag: Optional[str] = None,
        tag: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a physics interface to the model.

        Accepts the canonical Java type ("HeatTransfer"), the short
        tag ("ht"), or the snake_case key from physics_get_available
        ("heat_transfer"). All three resolve to the same interface.

        Args:
            physics_type: Type identifier (e.g. "HeatTransfer", "ht",
                "heat_transfer")
            component_name: Component to add physics to
                (default: first component)
            geometry_tag: Geometry sequence tag to bind the physics to
                (default: first geometry in the component)
            tag: Override the default tag for the new physics node
                (default: type-specific, e.g. "ht" for HeatTransfer)
            model_name: Model name (default: current model)

        Returns:
            Created physics interface info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        return _add_physics_interface(
            model, physics_type, component_name,
            explicit_tag=tag, geometry_tag=geometry_tag,
        )
    
    @mcp.tool()
    def physics_add_electrostatics(
        domain_selection: Optional[str] = None,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Electrostatics physics interface for electric field analysis.

        Args:
            domain_selection: Reserved (selection scoping not yet wired)
            component_name: Component to add physics to
                (default: first component)
            model_name: Model name (default: current model)

        Returns:
            Created physics info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        return _add_physics_interface(
            model, "Electrostatics", component_name,
        )
    
    @mcp.tool()
    def physics_add_solid_mechanics(
        domain_selection: Optional[str] = None,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Solid Mechanics physics for structural analysis.

        Args:
            domain_selection: Reserved (selection scoping not yet wired)
            component_name: Component to add physics to
                (default: first component)
            model_name: Model name (default: current model)

        Returns:
            Created physics info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        return _add_physics_interface(
            model, "SolidMechanics", component_name,
        )
    
    @mcp.tool()
    def physics_add_heat_transfer(
        domain_selection: Optional[str] = None,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Heat Transfer physics for thermal analysis.

        Args:
            domain_selection: Reserved (selection scoping not yet wired)
            component_name: Component to add physics to
                (default: first component)
            model_name: Model name (default: current model)

        Returns:
            Created physics info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        return _add_physics_interface(
            model, "HeatTransfer", component_name,
        )
    
    @mcp.tool()
    def physics_add_laminar_flow(
        domain_selection: Optional[str] = None,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Laminar Flow physics for fluid dynamics.

        Args:
            domain_selection: Reserved (selection scoping not yet wired)
            component_name: Component to add physics to
                (default: first component)
            model_name: Model name (default: current model)

        Returns:
            Created physics info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        return _add_physics_interface(
            model, "LaminarFlow", component_name,
        )
    
    @mcp.tool()
    def physics_configure_boundary(
        physics_name: str,
        boundary_condition: str,
        boundary_selection: Sequence[int],
        properties: Optional[dict] = None,
        selection_dim: Optional[int] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Configure a boundary/edge/point/domain feature for a physics
        interface via the canonical Java API path.

        Pilot 07 + Pilot 08 fix: the previous implementation went
        through the mph wrapper's ``bc_node.property("selection",
        list)`` which raises ``UnknownEntityException: "Unknown
        parameter X#selection"`` for many feature types
        (SurfaceToAmbientRadiation, PointLoad, ...). The canonical
        path is ``bc.selection().set(int[])`` on the raw Java handle,
        with the feature's selection-dim resolved either explicitly
        via ``selection_dim`` or auto-inferred as ``geom.sdim - 1``.

        For non-default dims (PointLoad on 3D = dim 0, EdgeLoad on
        3D = dim 1) the canonical 3-arg ``physics.create(tag, type,
        dim)`` form is used (KB ProgrammingReferenceManual chunk
        77014); this binds the selection at the requested dim from
        the start.

        Common feature types for Electrostatics:
        - "Ground": Zero potential boundary
        - "ElectricPotential": Specified voltage
        - "SurfaceChargeDensity": Surface charge
        - "ZeroCharge": Zero normal displacement field

        Common for Solid Mechanics:
        - "Fixed": Fixed constraint (boundary)
        - "Roller": Roller constraint (boundary)
        - "Symmetry": Symmetry plane (boundary)
        - "BoundaryLoad": Applied force/pressure (boundary)
        - "PointLoad": Point force — pass ``selection_dim=0``
        - "EdgeLoad": Edge load — pass ``selection_dim=1`` on 3D

        Common for Heat Transfer:
        - "Temperature": Fixed temperature
        - "HeatFlux": Heat flux boundary
        - "ConvectiveHeatFlux": Convection cooling
        - "Symmetry": Symmetry (adiabatic)

        Property value formats — KB scripting_completion_text/physics.md
        (data/completion/physics.xml) is authoritative for type. Use the
        ``silent_exceptions`` field in the response to confirm each
        property landed cleanly:

        - Scalar properties (``type="String"``): pass a Python ``str``
          or numeric. Examples: ``"LoadType": "ForceArea"``,
          ``"Fp": "100[N]"``, ``"T0": "293.15[K]"``.
        - Vector properties (``type="StringArray"``): pass a Python
          ``list`` of strings, one per geometry component. Examples
          (3D): ``"FperArea": ["0", "0", "-1e6[N/m^2]"]``,
          ``"FperLength": ["0", "0", "-1e3[N/m]"]``,
          ``"Fp": ["100[N]", "0", "0"]``. The Java side expects a
          ``String[]`` of length = sdim; JPype routes a Python list
          through the typed setter automatically.
        - LoadType selectors (Solid Mechanics): the BoundaryLoad
          ``LoadType`` strings vary per parent physics — e.g. for the
          3D ``solid`` interface the values are ``"ForceArea" |
          "TotalForce" | "FollowerPressure" | "Resultant"``; for plate /
          shell variants ``"ForceLength"`` and ``"ForceVolume"`` also
          appear. See KB physics.md tokens ``asewtbfb`` / ``axetxqxx`` /
          ``bwfqxdxb`` for the canonical value lists.

        Args:
            physics_name: Name of the physics interface
            boundary_condition: Java feature class (e.g. "Fixed",
                "PointLoad", "BoundaryLoad", "HeatFluxBoundary")
            boundary_selection: Selected entity numbers
            properties: Dictionary of property names and values
                (see *Property value formats* above)
            selection_dim: 0=points, 1=edges, 2=faces, 3=domains.
                Default ``None`` → auto-infer ``geom.sdim - 1``.
            model_name: Model name (default: current model)

        Returns:
            Created feature info, including the resolved
            ``selection_dim`` for caller audit. The
            ``boundary_condition.silent_exceptions`` field is a dict
            mapping each property key to ``None`` (set succeeded) or
            ``"ExceptionName: message"`` (set failed silently — feature
            still created, but the property did NOT land on the Java
            node and the caller should treat the BC as misconfigured).
            This is the per-property analogue of the sar BC
            ``silent_exception`` field in
            ``physics_setup_heat_boundaries`` (commit 053f48a pattern).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java

            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics interface not found: {physics_name}"}

            comp = None
            physics = None
            for c in jm.component():
                for p in c.physics():
                    try:
                        if physics_name in p.label():
                            comp = c
                            physics = p
                            break
                    except Exception:
                        continue
                if comp:
                    break

            if comp is None or physics is None:
                return {
                    "success": False,
                    "error": (
                        f"Could not locate physics interface "
                        f"'{physics_name}' on any component."
                    ),
                }

            # Resolve geometry tag for sdim auto-inference.
            geom_tag = None
            try:
                geoms = list(comp.geom())
                if geoms:
                    geom_tag = str(geoms[0].tag())
            except Exception:
                geom_tag = None

            resolved_dim = _resolve_selection_dim(
                comp, geom_tag, selection_dim
            )

            # Generate a non-colliding tag for the feature.
            import random
            tag = f'bc_{random.randint(1000, 9999)}'
            if resolved_dim is not None:
                bc = physics.create(
                    tag, boundary_condition, int(resolved_dim)
                )
            else:
                bc = physics.create(tag, boundary_condition)

            # Canonical selection path: bc.selection().set(int[]) — NOT
            # bc.set('selection', list) and NOT
            # bc.property('selection', list). Both of those raise
            # UnknownEntityException for many feature classes.
            bc.selection().set([int(b) for b in boundary_selection])

            # Per-property silent_exceptions diagnostic (mirrors the
            # sar1 pattern in physics_setup_heat_boundaries, commit
            # 053f48a). Pilot 08 v2 fix: previously the inner setter
            # swallowed exceptions unconditionally with `except: pass`,
            # so a BoundaryLoad call with a misformatted vector
            # property (e.g. a Python list passed where COMSOL expects
            # a String[][] 3-component literal, or a Python str passed
            # where a StringArray is required) appeared to succeed
            # while the property silently never landed on the Java
            # node — yielding displacement=0 / stress=0 at solve time
            # with no error trail. The dict here surfaces each set()
            # outcome so callers can self-diagnose without a live mph
            # probe.
            silent_exceptions: dict = {}
            if properties:
                for prop_name, prop_value in properties.items():
                    try:
                        bc.set(prop_name, prop_value)
                        silent_exceptions[prop_name] = None
                    except Exception as set_e:
                        silent_exceptions[prop_name] = (
                            f"{type(set_e).__name__}: {set_e}"
                        )

            try:
                bc.label(
                    f'{boundary_condition} (Entities '
                    f'{list(boundary_selection)})'
                )
            except Exception:
                pass

            return {
                "success": True,
                "boundary_condition": {
                    "name": str(tag),
                    "tag": str(tag),
                    "type": boundary_condition,
                    "physics": physics_name,
                    "selection": list(boundary_selection),
                    "selection_dim": resolved_dim,
                    "properties": properties,
                    "silent_exceptions": silent_exceptions,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to configure boundary: {type(e).__name__}: {e}"}
    
    @mcp.tool()
    def physics_set_material(
        physics_name: str,
        material_name: str,
        domain_selection: Optional[Sequence[int]] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        [DEPRECATED — advisory only]

        In COMSOL the material → domain binding lives on the material
        object itself, NOT on the physics interface. There is no canonical
        Java API call of the form ``physics.setMaterial(mat)``. The proper
        workflow is:

            1. Create the material with material_create_user_defined
               or material_create_from_kb (this sets a component-wide
               default selection = all domains).
            2. If needed, restrict the material to specific domains with
               material_assign_to_domain(material_name, domain_selection).
            3. Physics interfaces implicitly use the material that covers
               their domain — no explicit physics-side binding is required.

        This tool is retained for backwards compatibility with prompts /
        sequences from wjc9011/comsol-mcp v0.1, but performs no Java API
        mutation. It only validates that the named physics and material
        exist, and returns a ``redirect_tool`` field pointing to the proper
        tool. The ``domain_selection`` argument is accepted but ignored.

        Returns:
            ``{success, deprecated: True, advisory, physics, material,
               redirect_tool, validated}``.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            materials = model.materials()
            material_exists = material_name in materials
            physics_interfaces = model.physics()
            physics_exists = physics_name in physics_interfaces

            if not material_exists:
                return {
                    "success": False,
                    "error": f"Material not found: {material_name}",
                }
            if not physics_exists:
                return {
                    "success": False,
                    "error": f"Physics interface not found: {physics_name}",
                }

            return {
                "success": True,
                "deprecated": True,
                "advisory": (
                    "physics_set_material is advisory-only and performs "
                    "no Java API mutation. In COMSOL, material → domain "
                    "binding is set on the material itself "
                    "(mat.selection().set(...)), not on the physics "
                    "interface. Use material_assign_to_domain to bind a "
                    "material to specific domains, or rely on the "
                    "component-wide default selection (= all domains) "
                    "automatically set by material_create_user_defined / "
                    "material_create_from_kb."
                ),
                "physics": physics_name,
                "material": material_name,
                "redirect_tool": "material_assign_to_domain",
                "validated": {
                    "physics_exists": True,
                    "material_exists": True,
                    "domain_selection_arg_ignored": (
                        domain_selection is not None
                    ),
                },
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to validate set_material: {type(e).__name__}: {e}",
            }
    
    @mcp.tool()
    def multiphysics_add(
        coupling_type: str,
        physics_list: Sequence[str],
        component_name: Optional[str] = None,
        tag: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a multiphysics coupling between physics interfaces.

        Common coupling types:
        - "ThermalStress": Couples Heat Transfer and Solid Mechanics
        - "FluidStructureInteraction": Couples Fluid Flow and Solid Mechanics
        - "ElectromechanicalForces": Couples Electrostatics and Solid Mechanics
        - "JouleHeating": Couples Electric Currents and Heat Transfer

        Args:
            coupling_type: Type of multiphysics coupling
                (Java type name, e.g. "ThermalStress")
            physics_list: Tags of physics interfaces to couple
                (informational; actual binding by COMSOL on first build)
            component_name: Component to add coupling to
                (default: first component)
            tag: Override the coupling node tag
                (default: lowercase coupling_type)
            model_name: Model name (default: current model)

        Returns:
            Created coupling info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        comp, geom_tag, err = _get_comp_and_geom_tag(model, component_name)
        if err:
            return {"success": False, "error": err}

        use_tag = tag or coupling_type.lower()
        try:
            coupling = comp.multiphysics().create(use_tag, coupling_type, geom_tag)
            try:
                label = str(coupling.label())
            except Exception:
                label = use_tag
            return {
                "success": True,
                "coupling": {
                    "name": label,
                    "type": coupling_type,
                    "tag": use_tag,
                    "component": str(comp.tag()),
                    "geometry": geom_tag,
                    "physics": list(physics_list),
                },
            }
        except Exception as e:
            return {
                "success": False,
                "error": (
                    f"Failed to add multiphysics: {type(e).__name__}: {e}"
                ),
            }
    
    @mcp.tool()
    def physics_list_features(
        physics_name: str,
        model_name: Optional[str] = None
    ) -> dict:
        """
        List all features (boundary conditions, domain settings) in a physics interface.
        
        Args:
            physics_name: Name of the physics interface
            model_name: Model name (default: current model)
        
        Returns:
            List of physics features
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics interface not found: {physics_name}"}
            
            physics_node = model / "physics" / physics_name
            features = []
            
            for child in physics_node.children():
                feat_info = {"name": child.name()}
                try:
                    feat_info["type"] = child.type() if hasattr(child, 'type') else "unknown"
                except Exception:
                    pass
                features.append(feat_info)
            
            return {
                "success": True,
                "physics": physics_name,
                "features": features,
                "count": len(features),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list features: {str(e)}"}
    
    @mcp.tool()
    def physics_remove(
        physics_name: str,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Remove a physics interface from the model.
        
        Args:
            physics_name: Name of the physics interface to remove
            model_name: Model name (default: current model)
        
        Returns:
            Removal confirmation
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics interface not found: {physics_name}"}

            physics_node = model / "physics" / physics_name
            model.remove(physics_node)

            return {
                "success": True,
                "removed": physics_name,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to remove physics: {str(e)}"}

    @mcp.tool()
    def physics_set_property(
        physics_name: str,
        property_group: str,
        property_name: str,
        value,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Set a scalar interface-level property on a physics interface.

        This is the canonical Java path for properties that live on the
        physics interface itself (rather than on a feature/BC node):

            model.component(<ctag>).physics(<tag>)
                 .prop(<property_group>).set(<property_name>, <value>);

        See KB ProgrammingReferenceManual chunk 77014 for the API
        signature. Common Solid Mechanics use cases the Pilot 08
        comsol_12681_force fix needs:

        - 2D out-of-plane thickness:
            ``physics_set_property(physics, "d", "d", "1[m]")``
        - Reference temperature for thermal expansion:
            ``physics_set_property(physics, "Tref", "Tref", "293.15[K]")``
        - Equation form override:
            ``physics_set_property(physics, "EquationForm",
              "form", "Stationary")``
        - Shape function order:
            ``physics_set_property(physics, "ShapeProperty",
              "order_displacement", 2)``

        Args:
            physics_name: Name of the physics interface (label or tag
                that ``model.physics()`` enumerates).
            property_group: The ``prop()`` group id (e.g. "d", "Tref",
                "ShapeProperty", "EquationForm").
            property_name: The key inside the group (e.g. "d", "Tref",
                "order_displacement", "form").
            value: The value to set. Strings, ints, and string
                expressions like ``"1[m]"`` are passed through to Java
                without coercion.
            model_name: Model name (default: current model).

        Returns:
            ``{success, physics, property_group, property_name, value}``
            on success; ``{success: False, error}`` otherwise.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java

            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {
                    "success": False,
                    "error": (
                        f"Physics '{physics_name}' not found. Available: "
                        f"{physics_interfaces}"
                    ),
                }

            physics = None
            for c in jm.component():
                for p in c.physics():
                    try:
                        if physics_name in p.label():
                            physics = p
                            break
                    except Exception:
                        continue
                if physics is not None:
                    break

            if physics is None:
                return {
                    "success": False,
                    "error": (
                        f"Could not locate physics interface "
                        f"'{physics_name}' on any component."
                    ),
                }

            try:
                physics.prop(property_group).set(property_name, value)
            except Exception as set_e:
                return {
                    "success": False,
                    "error": (
                        f"physics.prop({property_group!r})"
                        f".set({property_name!r}, ...) failed: "
                        f"{type(set_e).__name__}: {set_e}"
                    ),
                }

            return {
                "success": True,
                "physics": physics_name,
                "property_group": property_group,
                "property_name": property_name,
                "value": value,
            }
        except Exception as e:
            return {
                "success": False,
                "error": (
                    f"Failed to set physics property: "
                    f"{type(e).__name__}: {e}"
                ),
            }

    # NOTE: the public MCP tool ``geometry_get_boundaries`` is now registered
    # from tools/geometry.py against the Java getNBoundaries/getNDomains API.
    # The closure below is kept only because
    # ``physics_interactive_setup_flow`` and ``physics_interactive_setup_heat``
    # call it by name. It mirrors the geometry.py implementation.
    def geometry_get_boundaries(
        geometry_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """Internal helper: boundary/domain counts via getNBoundaries/getNDomains."""
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            target = geometry_name

            comp = None
            geom = None
            for c in list(jm.component()):
                try:
                    geoms_in_c = list(c.geom())
                except Exception:
                    continue
                if target:
                    for g in geoms_in_c:
                        try:
                            if str(g.tag()) == target or (
                                hasattr(g, "label") and str(g.label()) == target
                            ):
                                comp = c
                                geom = g
                                break
                        except Exception:
                            continue
                    if geom is not None:
                        break
                else:
                    if geoms_in_c:
                        comp = c
                        geom = geoms_in_c[0]
                        break

            if geom is None:
                return {
                    "success": False,
                    "error": (
                        f"Geometry '{target}' not found in any component."
                        if target else "No geometry sequences found."
                    ),
                }

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

            boundaries = [{"boundary_number": i} for i in range(1, n_boundaries + 1)]

            return {
                "success": True,
                "geometry": str(geom.tag()),
                "component": str(comp.tag()) if comp is not None else None,
                "total_boundaries": n_boundaries,
                "total_domains": n_domains,
                "boundaries": boundaries,
                "hint": "Use boundary_number to set boundary conditions with physics_configure_boundary",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to get boundaries: {type(e).__name__}: {e}"}
    
    @mcp.tool()
    def physics_interactive_setup_flow(
        physics_name: str = "Laminar Flow",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Interactive setup wizard for Laminar Flow boundary conditions.
        
        This tool helps identify and configure flow boundary conditions:
        1. Lists all available boundaries
        2. Prompts user to select inlet, outlet, and wall boundaries
        3. Configures appropriate boundary conditions
        
        Args:
            physics_name: Name of the Laminar Flow physics interface
            model_name: Model name (default: current model)
        
        Returns:
            Boundary information and setup instructions
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            # Get geometry boundaries
            boundaries_info = geometry_get_boundaries(None, model_name)
            if not boundaries_info.get("success"):
                return boundaries_info
            
            return {
                "success": True,
                "message": "Interactive Flow Setup - Please specify boundaries",
                "available_boundaries": boundaries_info["total_boundaries"],
                "boundaries": boundaries_info["boundaries"],
                "setup_instructions": {
                    "step1": "Identify which boundary numbers are INLETS (flow enters)",
                    "step2": "Identify which boundary numbers are OUTLETS (flow exits)",
                    "step3": "Use physics_configure_boundary to set conditions",
                },
                "boundary_condition_types": {
                    "InletBoundary": "Set inlet velocity (U0 parameter)",
                    "OutletBoundary": "Set outlet pressure (p0 parameter, default 0)",
                    "Wall": "No-slip wall (default for unspecified boundaries)",
                    "Symmetry": "Symmetry plane",
                },
                "example_usage": {
                    "inlet": "physics_configure_boundary(physics_name='Laminar Flow', boundary_condition='InletBoundary', boundary_selection=[1, 2], properties={'U0': '1[mm/s]'})",
                    "outlet": "physics_configure_boundary(physics_name='Laminar Flow', boundary_condition='OutletBoundary', boundary_selection=[3])",
                },
                "next_step": "Please tell me which boundary numbers to use for inlet(s) and outlet(s)",
            }
        except Exception as e:
            return {"success": False, "error": f"Interactive setup failed: {str(e)}"}
    
    @mcp.tool()
    def physics_setup_flow_boundaries(
        physics_name: str,
        inlet_boundaries: Sequence[int],
        outlet_boundaries: Sequence[int],
        inlet_velocity: str = "1[mm/s]",
        outlet_pressure: str = "0",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Setup Laminar Flow boundary conditions with specified boundaries.
        
        This tool configures inlet velocity and outlet pressure boundary conditions
        for a fluid flow simulation.
        
        Args:
            physics_name: Name of the Laminar Flow physics interface
            inlet_boundaries: List of boundary numbers for inlets
            outlet_boundaries: List of boundary numbers for outlets
            inlet_velocity: Inlet velocity expression (default: "1[mm/s]")
            outlet_pressure: Outlet pressure expression (default: "0")
            model_name: Model name (default: current model)
        
        Returns:
            Configuration confirmation
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            jm = model.java
            
            # Find physics in component
            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics '{physics_name}' not found. Available: {physics_interfaces}"}
            
            # Get component and physics
            comp = None
            for c in jm.component():
                for p in c.physics():
                    if physics_name in p.label() or p.tag() == 'spf':
                        comp = c
                        physics = p
                        break
                if comp:
                    break
            
            if comp is None:
                return {"success": False, "error": "Could not find physics interface"}
            
            results = {"inlets": [], "outlets": []}
            
            # Add inlet boundary conditions
            for i, boundary in enumerate(inlet_boundaries):
                inlet_tag = f'inl{i+1}'
                inlet = physics.create(inlet_tag, 'InletBoundary')
                inlet.selection().set([int(boundary)])
                inlet.set('U0', inlet_velocity)
                inlet.label(f'Inlet {i+1} (Boundary {boundary})')
                results["inlets"].append({
                    "tag": inlet_tag,
                    "boundary": boundary,
                    "velocity": inlet_velocity
                })
            
            # Add outlet boundary conditions
            for i, boundary in enumerate(outlet_boundaries):
                outlet_tag = f'out{i+1}'
                outlet = physics.create(outlet_tag, 'OutletBoundary')
                outlet.selection().set([int(boundary)])
                outlet.set('p0', outlet_pressure)
                outlet.label(f'Outlet {i+1} (Boundary {boundary})')
                results["outlets"].append({
                    "tag": outlet_tag,
                    "boundary": boundary,
                    "pressure": outlet_pressure
                })
            
            return {
                "success": True,
                "physics": physics_name,
                "configured_boundaries": results,
                "inlet_velocity": inlet_velocity,
                "outlet_pressure": outlet_pressure,
                "message": f"Configured {len(inlet_boundaries)} inlet(s) and {len(outlet_boundaries)} outlet(s)",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to setup boundaries: {str(e)}"}

    @mcp.tool()
    def physics_interactive_setup_heat(
        physics_name: str = "Heat Transfer in Solids",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Interactive setup wizard for Heat Transfer boundary conditions.
        
        This tool helps identify and configure thermal boundary conditions:
        1. Lists all available boundaries
        2. Shows typical boundary condition types for thermal analysis
        3. Provides setup instructions
        
        Args:
            physics_name: Name of the Heat Transfer physics interface
            model_name: Model name (default: current model)
        
        Returns:
            Boundary information and setup instructions
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            boundaries_info = geometry_get_boundaries(None, model_name)
            if not boundaries_info.get("success"):
                return boundaries_info
            
            return {
                "success": True,
                "message": "Interactive Heat Transfer Setup",
                "available_boundaries": boundaries_info["total_boundaries"],
                "boundaries": boundaries_info["boundaries"],
                "boundary_condition_types": {
                    "TemperatureBoundary": "Fixed temperature (heat sink/source)",
                    "HeatFluxBoundary": "Prescribed heat flux (heat source)",
                    "ConvectiveHeatFlux": "Convection cooling/heating",
                    "Symmetry": "Symmetry plane (adiabatic)",
                    "ThermalInsulation": "Thermal insulation (default)"
                },
                "typical_setup": {
                    "heat_source": "Use HeatFluxBoundary with q0 parameter (W/m^2)",
                    "heat_sink": "Use TemperatureBoundary with T0 parameter (K or degC)",
                    "convection": "Use ConvectiveHeatFlux with h and Text parameters"
                },
                "example_usage": {
                    "heat_source": "physics_setup_heat_boundaries(physics_name='Heat Transfer in Solids', heat_flux_boundaries=[1, 2], heat_flux_value='1e6[W/m^2]')",
                    "heat_sink": "physics_setup_heat_boundaries(physics_name='Heat Transfer in Solids', temperature_boundaries=[3], temperature_value='293.15[K]')"
                },
                "next_step": "Tell me which boundary numbers to use for heat source and heat sink",
            }
        except Exception as e:
            return {"success": False, "error": f"Interactive setup failed: {str(e)}"}

    @mcp.tool()
    def physics_setup_heat_boundaries(
        physics_name: str,
        heat_flux_boundaries: Sequence[int] = [],
        temperature_boundaries: Sequence[int] = [],
        convection_boundaries: Sequence[int] = [],
        radiation_boundaries: Sequence[int] = [],
        heat_flux_value: str = "1e6[W/m^2]",
        temperature_value: str = "293.15[K]",
        convection_coeff: str = "10[W/(m^2*K)]",
        ambient_temp: str = "293.15[K]",
        radiation_emissivity: str = "0.9",
        radiation_ambient_temp: str = "293.15[K]",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Setup Heat Transfer boundary conditions with specified boundaries.

        This tool configures thermal boundary conditions for heat transfer simulation:
        - Heat flux boundaries (heat sources)
        - Temperature boundaries (heat sinks)
        - Convective cooling/heating boundaries
        - Surface-to-ambient radiation boundaries (gray-body to ambient)

        Args:
            physics_name: Name of the Heat Transfer physics interface
            heat_flux_boundaries: List of boundary numbers for heat flux
            temperature_boundaries: List of boundary numbers for fixed temperature
            convection_boundaries: List of boundary numbers for convection
            radiation_boundaries: List of boundary numbers for surface-to-ambient
                radiation (Java feature class ``SurfaceToAmbientRadiation`` /
                short id ``sar``; KB scripting_completion_text/physics.md confirms
                this id under the ``ht`` interface).
            heat_flux_value: Heat flux value (default: "1e6[W/m^2]")
            temperature_value: Temperature value (default: "293.15[K]" = 20°C)
            convection_coeff: Convection coefficient (default: "10[W/(m^2*K)]")
            ambient_temp: Ambient temperature for convection (default: "293.15[K]")
            radiation_emissivity: Surface emissivity ε for radiation BC,
                dimensionless 0..1 (default: "0.9")
            radiation_ambient_temp: Ambient temperature T_amb for radiation BC
                (default: "293.15[K]")
            model_name: Model name (default: current model)

        Returns:
            Configuration confirmation. ``configured_boundaries`` includes a
            ``radiation`` list with per-feature {tag, boundary, emissivity,
            ambient_temp, silent_exception}. ``silent_exception`` (PR-C-fix v2
            pattern, commit 00330d0) captures any per-property setter failure
            without aborting the tool — so callers see the exact mph/Java
            error if e.g. a property name is unknown for this COMSOL version.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java

            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics '{physics_name}' not found. Available: {physics_interfaces}"}

            comp = None
            for c in jm.component():
                for p in c.physics():
                    if physics_name in p.label() or p.tag() == 'ht':
                        comp = c
                        physics = p
                        break
                if comp:
                    break

            if comp is None:
                return {"success": False, "error": "Could not find physics interface"}

            results = {"heat_flux": [], "temperature": [], "convection": [], "radiation": []}

            # Add heat flux boundaries (heat sources)
            for i, boundary in enumerate(heat_flux_boundaries):
                tag = f'hf{i+1}'
                bc = physics.create(tag, 'HeatFluxBoundary')
                bc.selection().set([int(boundary)])
                bc.set('q0', heat_flux_value)
                bc.label(f'Heat Flux {i+1} (Boundary {boundary})')
                results["heat_flux"].append({
                    "tag": tag,
                    "boundary": boundary,
                    "heat_flux": heat_flux_value
                })

            # Add temperature boundaries (heat sinks)
            for i, boundary in enumerate(temperature_boundaries):
                tag = f'temp{i+1}'
                bc = physics.create(tag, 'TemperatureBoundary')
                bc.selection().set([int(boundary)])
                bc.set('T0', temperature_value)
                bc.label(f'Temperature {i+1} (Boundary {boundary})')
                results["temperature"].append({
                    "tag": tag,
                    "boundary": boundary,
                    "temperature": temperature_value
                })

            # Add convection boundaries
            for i, boundary in enumerate(convection_boundaries):
                tag = f'conv{i+1}'
                bc = physics.create(tag, 'ConvectiveHeatFlux')
                bc.selection().set([int(boundary)])
                bc.set('h', convection_coeff)
                bc.set('Text', ambient_temp)
                bc.label(f'Convection {i+1} (Boundary {boundary})')
                results["convection"].append({
                    "tag": tag,
                    "boundary": boundary,
                    "h": convection_coeff,
                    "T_amb": ambient_temp
                })

            # Add surface-to-ambient radiation boundaries.
            # Property names verified against KB scripting_completion_text/
            # physics.md (data/completion/physics.xml lines 18608-18611,
            # token batrsace = sar feature) AND live probe (Pilot 07 v3
            # silent_exception trace + sar_probe_v3 model 2026-05-11):
            #   feature class = SurfaceToAmbientRadiation
            #   selection:        bc.selection().set(int[])
            #   ambient_temp:     bc.set('Tamb', value)             — confirmed live
            #   emissivity mode:  bc.set('epsilon_rad_mat','userdef') — KB-authoritative
            #   emissivity value: bc.set('epsilon_rad', value)       — confirmed live
            # The mode key was wrongly named `epsilon_mat` in commit 256740d
            # (Pilot 07 v3 measured silent_exception "Unknown parameter
            # X#epsilon mat"). The KB physics.xml token table maps the
            # surface emissivity selector for sar to `epsilon_rad_mat`
            # (values "from_mat | userdef"), not `epsilon_mat`. Without
            # the mode flip, sar defaults to "from material" and COMSOL
            # looks up the internal property `epsilon rad` (with a space)
            # on the boundary's material — solve raises "Undefined
            # material property 'epsilon rad'". Setting
            # epsilon_rad_mat='userdef' before epsilon_rad lets the
            # caller-supplied literal value take effect.
            # Tamb_src defaults to 'userdef' (KB shows it accepts only
            # that value), so no explicit flip is needed for ambient temp.
            for i, boundary in enumerate(radiation_boundaries):
                tag = f'sar{i+1}'
                silent_exception = None
                bc = physics.create(tag, 'SurfaceToAmbientRadiation')
                bc.selection().set([int(boundary)])
                try:
                    bc.set('Tamb', radiation_ambient_temp)
                except Exception as e:
                    silent_exception = (
                        f"set(Tamb) -> {type(e).__name__}: {e}"
                    )
                try:
                    bc.set('epsilon_rad_mat', 'userdef')
                except Exception as e:
                    msg = f"set(epsilon_rad_mat) -> {type(e).__name__}: {e}"
                    silent_exception = (
                        f"{silent_exception}; {msg}" if silent_exception else msg
                    )
                try:
                    bc.set('epsilon_rad', radiation_emissivity)
                except Exception as e:
                    msg = f"set(epsilon_rad) -> {type(e).__name__}: {e}"
                    silent_exception = (
                        f"{silent_exception}; {msg}" if silent_exception else msg
                    )
                bc.label(f'Surface-to-Ambient Radiation {i+1} (Boundary {boundary})')
                results["radiation"].append({
                    "tag": tag,
                    "boundary": boundary,
                    "emissivity": radiation_emissivity,
                    "ambient_temp": radiation_ambient_temp,
                    "silent_exception": silent_exception,
                })

            return {
                "success": True,
                "physics": physics_name,
                "configured_boundaries": results,
                "summary": {
                    "heat_flux_boundaries": len(heat_flux_boundaries),
                    "temperature_boundaries": len(temperature_boundaries),
                    "convection_boundaries": len(convection_boundaries),
                    "radiation_boundaries": len(radiation_boundaries),
                },
                "message": (
                    f"Configured {len(heat_flux_boundaries)} heat flux, "
                    f"{len(temperature_boundaries)} temperature, "
                    f"{len(convection_boundaries)} convection, and "
                    f"{len(radiation_boundaries)} radiation boundaries"
                ),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to setup heat boundaries: {str(e)}"}

    @mcp.tool()
    def physics_boundary_selection(
        physics_name: str,
        boundary_condition_type: str,
        boundary_numbers: Sequence[int],
        properties: dict = {},
        selection_dim: Optional[int] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Generic boundary/edge/point/domain condition setup with selection.

        Use this tool to configure any feature by specifying:
        1. The physics interface name
        2. The feature type (BC, point load, edge constraint, ...)
        3. The entity numbers to apply the condition to
        4. The geometric entity dimension of the selection
           (``selection_dim``: 0=points, 1=edges, 2=faces, 3=domains).
           When omitted, auto-inferred as ``geom.sdim - 1`` (the
           boundary dim — matches the natural dim of boundary BCs like
           Fixed / BoundaryLoad / HeatFlux).
        5. Properties specific to the feature

        Solid Mechanics PointLoad (Pilot 08 fix): pass
        ``selection_dim=0`` so ``physics.create(tag, type, dim)`` (3-arg
        form, KB ProgrammingReferenceManual chunk 77014) creates the
        feature at the point dim. The 2-arg ``physics.create(tag,
        type)`` defaults the dim to the geometry's boundary dim,
        which COMSOL then rejects when point indices are passed.

        Common feature types by physics:

        Heat Transfer (ht):
        - TemperatureBoundary: Set T0 (temperature)
        - HeatFluxBoundary: Set q0 (heat flux)
        - ConvectiveHeatFlux: Set h (coefficient), Text (ambient temp)

        Laminar Flow (spf):
        - InletBoundary: Set U0 (velocity)
        - OutletBoundary: Set p0 (pressure)
        - Wall: No-slip wall

        Solid Mechanics (solid):
        - Fixed: Fixed constraint (dim = boundary)
        - BoundaryLoad: ``LoadType`` selector + the matching vector
          (dim = boundary). For 3D ``solid``: ``LoadType``
          ∈ {"ForceArea", "TotalForce", "FollowerPressure",
          "Resultant"}, with ``FperArea`` (length = sdim StringArray)
          carrying the user-defined force-per-area vector.
        - PointLoad: Set ``Fp`` (length = sdim StringArray for the
          force vector) — ``selection_dim=0`` required.
        - EdgeLoad: ``LoadType`` ∈ {"ForceLength", "TotalForce"},
          paired with ``FperLength`` (length = sdim StringArray) —
          ``selection_dim=1`` required on 3D.

        Property value formats — KB scripting_completion_text/physics.md
        (data/completion/physics.xml) is authoritative. Use the
        ``silent_exceptions`` field in the response to confirm each
        property landed cleanly:

        - Scalar (``type="String"``): pass a Python ``str``. Examples:
          ``"LoadType": "ForceArea"``, ``"T0": "293.15[K]"``.
        - Vector (``type="StringArray"``): pass a Python ``list`` of
          strings, one per geometry component. Examples (3D):
          ``"FperArea": ["0", "0", "-1e6[N/m^2]"]``,
          ``"FperLength": ["0", "0", "-1e3[N/m]"]``,
          ``"Fp": ["100[N]", "0", "0"]``. The Java side expects a
          ``String[]`` of length = sdim; JPype routes a Python list
          through the typed setter automatically. Passing a scalar
          string where a StringArray is required silently no-ops on
          the Java node (this was the Pilot 08 v2 failure mode for
          BoundaryLoad before this PR added the per-property
          ``silent_exceptions`` diagnostic).

        Args:
            physics_name: Name of the physics interface
            boundary_condition_type: Type of feature (Java class name)
            boundary_numbers: Selected entity numbers
            properties: Dictionary of property names and values
                (see *Property value formats* above)
            selection_dim: Selection dimension override (0/1/2/3).
                Default ``None`` → auto-infer ``geom.sdim - 1``.
            model_name: Model name (default: current model)

        Returns:
            Configuration confirmation. The ``boundary_condition`` block
            includes the resolved ``selection_dim`` so callers can audit
            whether they got points/edges/faces/domains, plus a
            ``silent_exceptions`` dict mapping each property key to
            ``None`` (set succeeded) or ``"ExceptionName: message"``
            (set failed silently — feature still created, but the
            property did NOT land and the caller should treat the BC
            as misconfigured). This is the per-property analogue of
            the sar BC ``silent_exception`` field in
            ``physics_setup_heat_boundaries`` (commit 053f48a pattern).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java

            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics '{physics_name}' not found. Available: {physics_interfaces}"}

            comp = None
            physics = None
            for c in jm.component():
                for p in c.physics():
                    if physics_name in p.label():
                        comp = c
                        physics = p
                        break
                if comp:
                    break

            if comp is None:
                return {"success": False, "error": "Could not find physics interface"}

            # Resolve geometry tag for sdim auto-inference.
            geom_tag = None
            try:
                geoms = list(comp.geom())
                if geoms:
                    geom_tag = str(geoms[0].tag())
            except Exception:
                geom_tag = None

            resolved_dim = _resolve_selection_dim(
                comp, geom_tag, selection_dim
            )

            # Create feature. If we resolved a dim, use the canonical
            # 3-arg ``physics.create(tag, type, dim)`` form so the
            # selection lives at the requested dim from the start.
            # Otherwise fall back to 2-arg create (feature picks its
            # natural dim).
            import random
            tag = f'bc_{random.randint(1000, 9999)}'
            if resolved_dim is not None:
                bc = physics.create(
                    tag, boundary_condition_type, int(resolved_dim)
                )
            else:
                bc = physics.create(tag, boundary_condition_type)
            bc.selection().set([int(b) for b in boundary_numbers])

            # Set properties — capture per-key outcome so callers can
            # tell whether the typed Java setter actually accepted the
            # value, instead of silently no-op'ing (Pilot 08 v2 fix:
            # BoundaryLoad LoadType + FperLength looked applied because
            # `properties` echoed in the response, but at solve time
            # displacement=0 / stress=0 — the underlying COMSOL node
            # had never received the values because the inner except
            # swallowed every exception unconditionally).
            silent_exceptions: dict = {}
            for prop_name, prop_value in properties.items():
                try:
                    bc.set(prop_name, prop_value)
                    silent_exceptions[prop_name] = None
                except Exception as set_e:
                    silent_exceptions[prop_name] = (
                        f"{type(set_e).__name__}: {set_e}"
                    )

            bc.label(f'{boundary_condition_type} (Entities {list(boundary_numbers)})')

            return {
                "success": True,
                "physics": physics_name,
                "boundary_condition": {
                    "type": boundary_condition_type,
                    "tag": tag,
                    "boundaries": list(boundary_numbers),
                    "selection_dim": resolved_dim,
                    "properties": properties,
                    "silent_exceptions": silent_exceptions,
                },
                "message": (
                    f"Created {boundary_condition_type} on entities "
                    f"{list(boundary_numbers)} at dim={resolved_dim}"
                ),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create boundary condition: {str(e)}"}


