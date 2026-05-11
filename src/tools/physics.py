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
        model_name: Optional[str] = None
    ) -> dict:
        """
        Configure a boundary condition for a physics interface.
        
        Common boundary conditions for Electrostatics:
        - "Ground": Zero potential boundary
        - "ElectricPotential": Specified voltage
        - "SurfaceChargeDensity": Surface charge
        - "ZeroCharge": Zero normal displacement field
        
        Common for Solid Mechanics:
        - "Fixed": Fixed constraint
        - "Roller": Roller constraint
        - "Symmetry": Symmetry plane
        - "BoundaryLoad": Applied force/pressure
        
        Common for Heat Transfer:
        - "Temperature": Fixed temperature
        - "HeatFlux": Heat flux boundary
        - "ConvectiveHeatFlux": Convection cooling
        - "Symmetry": Symmetry (adiabatic)
        
        Args:
            physics_name: Name of the physics interface
            boundary_condition: Type of boundary condition
            boundary_selection: Boundary/edge numbers to apply condition to
            properties: Dictionary of property names and values
            model_name: Model name (default: current model)
        
        Returns:
            Created boundary condition info
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
            bc_node = physics_node.create(boundary_condition)
            
            bc_node.property("selection", list(boundary_selection))
            
            if properties:
                for prop_name, prop_value in properties.items():
                    try:
                        bc_node.property(prop_name, prop_value)
                    except Exception:
                        pass
            
            return {
                "success": True,
                "boundary_condition": {
                    "name": bc_node.name() if hasattr(bc_node, 'name') else boundary_condition,
                    "type": boundary_condition,
                    "physics": physics_name,
                    "selection": list(boundary_selection),
                    "properties": properties,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to configure boundary: {str(e)}"}
    
    @mcp.tool()
    def physics_set_material(
        physics_name: str,
        material_name: str,
        domain_selection: Optional[Sequence[int]] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Assign a material to physics domains.
        
        Args:
            physics_name: Name of the physics interface
            material_name: Name of the material to assign
            domain_selection: Domain numbers (default: all domains for this physics)
            model_name: Model name (default: current model)
        
        Returns:
            Assignment confirmation
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            materials = model.materials()
            if material_name not in materials:
                return {"success": False, "error": f"Material not found: {material_name}"}
            
            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics interface not found: {physics_name}"}
            
            return {
                "success": True,
                "message": f"Material '{material_name}' should be configured to cover the required domains.",
                "note": "Use COMSOL GUI or low-level API for detailed material assignment.",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to set material: {str(e)}"}
    
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
            # Property names verified live (probe_sar.java export):
            #   feature class = SurfaceToAmbientRadiation
            #   selection: bc.selection().set(int[])  — NOT bc.set("selection", ...)
            #     (the latter is what physics_configure_boundary attempted
            #      and it raises "Unknown parameter X#selection")
            # Property names (Tamb / epsilon_rad) follow COMSOL ht feature
            # convention; if a future COMSOL version renames them, the per-
            # property silent_exception will surface the exact failure rather
            # than aborting the whole tool.
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
        model_name: Optional[str] = None
    ) -> dict:
        """
        Generic boundary condition setup with boundary selection.
        
        Use this tool to configure any boundary condition by specifying:
        1. The physics interface name
        2. The boundary condition type
        3. The boundary numbers to apply the condition to
        4. Properties specific to the boundary condition
        
        Common boundary condition types by physics:
        
        Heat Transfer (ht):
        - TemperatureBoundary: Set T0 (temperature)
        - HeatFluxBoundary: Set q0 (heat flux)
        - ConvectiveHeatFlux: Set h (coefficient), Text (ambient temp)
        
        Laminar Flow (spf):
        - InletBoundary: Set U0 (velocity)
        - OutletBoundary: Set p0 (pressure)
        - Wall: No-slip wall
        
        Solid Mechanics (solid):
        - Fixed: Fixed constraint
        - BoundaryLoad: Set Fx, Fy, Fz or FAx, FAy, FAz
        
        Args:
            physics_name: Name of the physics interface
            boundary_condition_type: Type of boundary condition
            boundary_numbers: List of boundary numbers
            properties: Dictionary of property names and values
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
            
            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics '{physics_name}' not found. Available: {physics_interfaces}"}
            
            comp = None
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
            
            # Create boundary condition
            import random
            tag = f'bc_{random.randint(1000, 9999)}'
            bc = physics.create(tag, boundary_condition_type)
            bc.selection().set([int(b) for b in boundary_numbers])
            
            # Set properties
            for prop_name, prop_value in properties.items():
                try:
                    bc.set(prop_name, prop_value)
                except Exception as e:
                    pass  # Property might not exist
            
            bc.label(f'{boundary_condition_type} (Boundaries {list(boundary_numbers)})')
            
            return {
                "success": True,
                "physics": physics_name,
                "boundary_condition": {
                    "type": boundary_condition_type,
                    "tag": tag,
                    "boundaries": list(boundary_numbers),
                    "properties": properties
                },
                "message": f"Created {boundary_condition_type} on boundaries {list(boundary_numbers)}",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create boundary condition: {str(e)}"}


