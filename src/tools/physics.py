"""Physics tools for COMSOL MCP Server."""

from typing import Optional, Sequence
from mcp.server.fastmcp import FastMCP

from .session import session_manager

_tag_counter = {}


def _get_component_java(java_model, component_name=None):
    """Return an explicit component or the model's first component by tag."""
    if component_name:
        return java_model.component(component_name)
    components = java_model.component()
    if components.size() == 0:
        raise ValueError("No components defined in the model.")
    return java_model.component(str(components.tags()[0]))


def _find_physics_java(jm, physics_name):
    """Look up a physics node by label or tag across all components."""
    context = _find_physics_context(jm, physics_name)
    return context[1] if context else None


def _find_physics_context(jm, physics_name):
    """Look up the component and physics node by label or tag."""
    for component_tag in jm.component().tags():
        comp = jm.component(str(component_tag))
        for physics_tag in comp.physics().tags():
            p = comp.physics(str(physics_tag))
            if p.label() == physics_name or p.tag() == physics_name:
                return comp, p
    return None


def _make_tag(prefix="bc"):
    """Generate a unique tag using a monotonic counter."""
    _tag_counter[prefix] = _tag_counter.get(prefix, 0) + 1
    return f"{prefix}_{_tag_counter[prefix]}"


def _set_feature_properties(feature, properties):
    """Set feature properties and return any failures without hiding them."""
    failures = {}
    for prop_name, prop_value in properties.items():
        try:
            feature.set(prop_name, prop_value)
        except Exception as e:
            failures[prop_name] = str(e)
    return failures


def _set_domain_selection(physics_java, domain_selection):
    """Apply an optional explicit domain selection to a physics interface."""
    if domain_selection:
        physics_java.selection().set([int(d) for d in domain_selection])


def _get_geometry_tag(component, geometry_name=None):
    """Resolve an explicit geometry tag or the component's first geometry."""
    if geometry_name:
        return geometry_name
    geometries = component.geom()
    if geometries.size() == 0:
        raise ValueError("No geometry found in component.")
    return str(geometries.tags()[0])


def _get_boundary_dimension(component, boundary_dimension=None):
    """Resolve a boundary dimension from input or the first component geometry."""
    if boundary_dimension is not None:
        return int(boundary_dimension)
    geometries = component.geom()
    if geometries.size() == 0:
        raise ValueError("No geometry found in component.")
    geom_tag = str(geometries.tags()[0])
    return int(component.geom(geom_tag).getSDim()) - 1


def _create_boundary_features(
    physics_java,
    physics_name,
    boundary_conditions,
    boundary_dimension,
):
    """Create a batch of boundary features from a common configuration format."""
    created = []
    failed = []

    for index, condition in enumerate(boundary_conditions):
        condition_type = condition.get("type") or condition.get("boundary_condition")
        boundaries = condition.get("boundaries") or condition.get("boundary_selection")
        selection_name = condition.get("selection_name")
        properties = condition.get("properties") or {}

        if not condition_type:
            failed.append({"index": index, "error": "Missing boundary condition type."})
            continue
        if not boundaries and not selection_name:
            failed.append({
                "index": index,
                "type": condition_type,
                "error": "Missing boundary selection or selection name.",
            })
            continue

        tag = condition.get("tag") or _make_tag(condition_type.lower())
        dimension = condition.get("dimension", boundary_dimension)
        try:
            feature = physics_java.create(tag, condition_type, int(dimension))
            if selection_name:
                feature.selection().named(selection_name)
            else:
                feature.selection().set([int(b) for b in boundaries])
            property_failures = _set_feature_properties(feature, properties)
            label = condition.get("label")
            if label:
                feature.label(label)
            elif selection_name:
                feature.label(f'{condition_type} (Selection {selection_name})')
            else:
                feature.label(f'{condition_type} (Boundaries {list(boundaries)})')

            result = {
                "tag": tag,
                "type": condition_type,
                "dimension": int(dimension),
                "properties": properties,
            }
            if selection_name:
                result["selection_name"] = selection_name
            else:
                result["boundaries"] = list(boundaries)
            if property_failures:
                result["property_errors"] = property_failures
                failed.append(result)
            else:
                created.append(result)
        except Exception as e:
            failed.append({
                "index": index,
                "tag": tag,
                "type": condition_type,
                "dimension": int(dimension),
                "error": str(e),
            })
            if selection_name:
                failed[-1]["selection_name"] = selection_name
            elif boundaries:
                failed[-1]["boundaries"] = list(boundaries)

    return {
        "success": not failed,
        "physics": physics_name,
        "configured_boundaries": created,
        "failed_boundaries": failed,
        "configured_count": len(created),
        "failed_count": len(failed),
    }


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
    "Mathematics": {
        "coefficient_form_pde": "Coefficient Form PDE (c)",
        "general_form_pde": "General Form PDE (g)",
        "weak_form_pde": "Weak Form PDE (w)",
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

ACOUSTIC_BOUNDARY_CONDITIONS = {
    "SoundHard": {
        "description": "Sound hard wall; no user property is normally required.",
        "properties": [],
    },
    "SoundSoft": {
        "description": "Sound soft boundary with zero acoustic pressure.",
        "properties": [],
    },
    "Pressure": {
        "description": "Prescribed acoustic pressure.",
        "properties": ["p0"],
    },
    "Impedance": {
        "description": "Specific acoustic impedance boundary.",
        "properties": ["Zn"],
    },
    "NormalAcceleration": {
        "description": "Prescribed normal acceleration source.",
        "properties": ["nacc"],
    },
    "NormalVelocity": {
        "description": "Prescribed normal velocity source.",
        "properties": ["nvel"],
    },
    "PlaneWaveRadiation": {
        "description": "Nonreflecting plane-wave radiation boundary.",
        "properties": [],
    },
    "SphericalWaveRadiation": {
        "description": "Nonreflecting spherical-wave radiation boundary.",
        "properties": [],
    },
}

PDE_BOUNDARY_CONDITIONS = {
    "DirichletBoundary": {
        "description": "Prescribed dependent-variable value.",
        "properties": ["r"],
    },
    "FluxBoundary": {
        "description": "Prescribed generalized inward flux/source.",
        "properties": ["g", "q"],
    },
    "ZeroFluxBoundary": {
        "description": "Zero inward flux boundary condition.",
        "properties": [],
    },
    "WeakContribution": {
        "description": "Weak contribution on selected boundaries.",
        "properties": ["weak"],
    },
    "PeriodicCondition": {
        "description": "Periodic boundary condition.",
        "properties": [],
    },
}

PDE_BOUNDARY_ALIASES = {
    "dirichlet": "DirichletBoundary",
    "flux": "FluxBoundary",
    "neumann": "FluxBoundary",
    "zero_flux": "ZeroFluxBoundary",
    "no_flux": "ZeroFluxBoundary",
    "wall": "ZeroFluxBoundary",
    "weak": "WeakContribution",
    "periodic": "PeriodicCondition",
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
    def physics_get_acoustic_boundary_conditions() -> dict:
        """
        Get common Pressure Acoustics boundary feature types and properties.

        Returns:
            Acoustic boundary condition reference
        """
        return {
            "success": True,
            "boundary_conditions": ACOUSTIC_BOUNDARY_CONDITIONS,
            "note": (
                "Feature types and properties can vary by acoustic interface and "
                "COMSOL version. Custom feature types remain available through "
                "physics_configure_acoustic_boundary."
            ),
        }

    @mcp.tool()
    def physics_get_pde_boundary_conditions() -> dict:
        """
        Get common PDE boundary feature types and properties.

        Returns:
            PDE boundary condition reference
        """
        return {
            "success": True,
            "boundary_conditions": PDE_BOUNDARY_CONDITIONS,
            "aliases": PDE_BOUNDARY_ALIASES,
            "note": (
                "PDE property values can be scalars, vectors, or matrices depending "
                "on the number of dependent variables. Custom feature types remain "
                "available through physics_configure_pde_boundary."
            ),
        }
    
    @mcp.tool()
    def physics_add(
        physics_type: str,
        component_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a physics interface to the model.

        Common physics types:
        - "Electrostatics" or "es": Electrostatic field analysis
        - "ElectricCurrents" or "ec": Electric current conduction
        - "SolidMechanics" or "solid": Structural stress analysis
        - "HeatTransfer" or "ht": Heat transfer in solids
        - "LaminarFlow" or "spf": Fluid dynamics

        Args:
            physics_type: Type identifier (e.g., "Electrostatics", "es")
            component_name: Component to add physics to (default: first component)
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

        try:
            jm = model.java

            if component_name:
                comp = jm.component(component_name)
            else:
                comp = _get_component_java(jm)

            if comp is None:
                return {"success": False, "error": f"Component not found: {component_name}"}

            tag = physics_type.replace(" ", "_").lower()
            # COMSOL 6.3 must be given the geometry sequence on creation,
            # otherwise the interface is created detached (0-dimensional).
            try:
                geom_tag = _get_geometry_tag(comp)
                physics_java = comp.physics().create(tag, physics_type, geom_tag)
            except Exception:
                physics_java = comp.physics().create(tag, physics_type)

            return {
                "success": True,
                "physics": {
                    "name": physics_java.label() if hasattr(physics_java, 'label') else physics_type,
                    "type": physics_type,
                    "tag": tag,
                    "component": comp.tag(),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add physics: {str(e)}"}
    
    @mcp.tool()
    def physics_add_electrostatics(
        domain_selection: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Electrostatics physics interface for electric field analysis.

        Args:
            domain_selection: Selection name for domains (default: all domains)
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

        try:
            jm = model.java
            comp = _get_component_java(jm)
            # COMSOL 6.3 must be given the geometry sequence on creation,
            # otherwise the interface is created detached (0-dimensional).
            try:
                geom_tag = _get_geometry_tag(comp)
                physics_java = comp.physics().create("es", "Electrostatics", geom_tag)
            except Exception:
                physics_java = comp.physics().create("es", "Electrostatics")

            if domain_selection:
                try:
                    physics_java.selection().set(domain_selection)
                except Exception:
                    pass

            return {
                "success": True,
                "physics": {
                    "name": physics_java.label() if hasattr(physics_java, 'label') else "Electrostatics",
                    "type": "Electrostatics",
                    "tag": "es",
                    "domain_selection": domain_selection,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add Electrostatics: {str(e)}"}
    
    @mcp.tool()
    def physics_add_solid_mechanics(
        domain_selection: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Solid Mechanics physics for structural analysis.

        Args:
            domain_selection: Selection name for domains (default: all domains)
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

        try:
            jm = model.java
            comp = _get_component_java(jm)
            # COMSOL 6.3 must be given the geometry sequence on creation,
            # otherwise the interface is created detached (0-dimensional).
            try:
                geom_tag = _get_geometry_tag(comp)
                physics_java = comp.physics().create("solid", "SolidMechanics", geom_tag)
            except Exception:
                physics_java = comp.physics().create("solid", "SolidMechanics")

            if domain_selection:
                try:
                    physics_java.selection().set(domain_selection)
                except Exception:
                    pass

            return {
                "success": True,
                "physics": {
                    "name": physics_java.label() if hasattr(physics_java, 'label') else "Solid Mechanics",
                    "type": "SolidMechanics",
                    "tag": "solid",
                    "domain_selection": domain_selection,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add Solid Mechanics: {str(e)}"}
    
    @mcp.tool()
    def physics_add_heat_transfer(
        domain_selection: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Heat Transfer physics for thermal analysis.

        Args:
            domain_selection: Selection name for domains (default: all domains)
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

        try:
            jm = model.java
            comp = _get_component_java(jm)
            # COMSOL 6.3 must be given the geometry sequence on creation,
            # otherwise the interface is created detached (0-dimensional).
            try:
                geom_tag = _get_geometry_tag(comp)
                physics_java = comp.physics().create("ht", "HeatTransfer", geom_tag)
            except Exception:
                geom_tag = None
                physics_java = comp.physics().create("ht", "HeatTransfer")

            if domain_selection:
                try:
                    physics_java.selection().set(domain_selection)
                except Exception:
                    pass

            return {
                "success": True,
                "physics": {
                    "name": physics_java.label() if hasattr(physics_java, 'label') else "Heat Transfer",
                    "type": "HeatTransfer",
                    "tag": "ht",
                    "domain_selection": domain_selection,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add Heat Transfer: {str(e)}"}
    
    @mcp.tool()
    def physics_add_laminar_flow(
        domain_selection: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Laminar Flow physics for fluid dynamics.

        Args:
            domain_selection: Selection name for domains (default: all domains)
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

        try:
            jm = model.java
            comp = _get_component_java(jm)
            # COMSOL 6.3 must be given the geometry sequence on creation,
            # otherwise the interface is created detached (0-dimensional).
            try:
                geom_tag = _get_geometry_tag(comp)
                physics_java = comp.physics().create("spf", "LaminarFlow", geom_tag)
            except Exception:
                physics_java = comp.physics().create("spf", "LaminarFlow")

            if domain_selection:
                try:
                    physics_java.selection().set(domain_selection)
                except Exception:
                    pass

            return {
                "success": True,
                "physics": {
                    "name": physics_java.label() if hasattr(physics_java, 'label') else "Laminar Flow",
                    "type": "LaminarFlow",
                    "tag": "spf",
                    "domain_selection": domain_selection,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add Laminar Flow: {str(e)}"}

    @mcp.tool()
    def physics_add_pressure_acoustics(
        domain_selection: Optional[Sequence[int]] = None,
        component_name: Optional[str] = None,
        geometry_name: Optional[str] = None,
        physics_tag: str = "acpr",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add Pressure Acoustics physics for frequency-domain acoustic analysis.

        Args:
            domain_selection: Domain numbers (default: all domains)
            component_name: Component to add physics to (default: first component)
            geometry_name: Geometry sequence tag (default: first geometry)
            physics_tag: Physics interface tag (default: "acpr")
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

        try:
            jm = model.java
            comp = _get_component_java(jm, component_name)
            geom_tag = _get_geometry_tag(comp, geometry_name)
            physics_java = comp.physics().create(
                physics_tag,
                "PressureAcoustics",
                geom_tag,
            )
            _set_domain_selection(physics_java, domain_selection)

            return {
                "success": True,
                "physics": {
                    "name": (
                        physics_java.label()
                        if hasattr(physics_java, 'label')
                        else "Pressure Acoustics"
                    ),
                    "type": "PressureAcoustics",
                    "tag": physics_tag,
                    "component": comp.tag(),
                    "geometry": geom_tag,
                    "domain_selection": (
                        list(domain_selection) if domain_selection else "all"
                    ),
                }
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to add Pressure Acoustics: {str(e)}"
            }

    @mcp.tool()
    def physics_add_acoustics(
        physics_type: str,
        physics_tag: Optional[str] = None,
        domain_selection: Optional[Sequence[int]] = None,
        component_name: Optional[str] = None,
        geometry_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a geometry-based Acoustics Module physics interface.

        Use the exact COMSOL physics type for the installed version. Confirmed
        COMSOL 6.3 examples include PressureAcoustics and
        ThermoacousticsSinglePhysics. This tool also supports other acoustic
        interface types without maintaining a hard-coded allowlist.

        Args:
            physics_type: Exact COMSOL acoustic physics type
            physics_tag: Physics interface tag (default: generated from type)
            domain_selection: Domain numbers (default: all domains)
            component_name: Component to add physics to (default: first component)
            geometry_name: Geometry sequence tag (default: first geometry)
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

        tag = physics_tag or physics_type.replace(" ", "_").lower()

        try:
            jm = model.java
            comp = _get_component_java(jm, component_name)
            geom_tag = _get_geometry_tag(comp, geometry_name)
            physics_java = comp.physics().create(tag, physics_type, geom_tag)
            _set_domain_selection(physics_java, domain_selection)

            return {
                "success": True,
                "physics": {
                    "name": (
                        physics_java.label()
                        if hasattr(physics_java, 'label')
                        else physics_type
                    ),
                    "type": physics_type,
                    "tag": tag,
                    "component": comp.tag(),
                    "geometry": geom_tag,
                    "domain_selection": (
                        list(domain_selection) if domain_selection else "all"
                    ),
                },
                "note": (
                    "The physics type is passed directly to the COMSOL Java API; "
                    "availability depends on installed products and licenses."
                ),
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to add acoustic physics '{physics_type}': {str(e)}"
            }

    def _add_pde_interface(
        physics_type,
        default_tag,
        equation_feature,
        dependent_variables,
        equation_properties,
        domain_selection,
        component_name,
        physics_tag,
        model_name,
    ):
        """Create and optionally configure a PDE interface."""
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        variables = list(dependent_variables) if dependent_variables else ["u"]
        tag = physics_tag or default_tag

        try:
            jm = model.java
            comp = _get_component_java(jm, component_name)
            physics_java = comp.physics().create(tag, physics_type, variables)
            _set_domain_selection(physics_java, domain_selection)

            property_failures = {}
            if equation_properties:
                equation = physics_java.feature(equation_feature)
                property_failures = _set_feature_properties(
                    equation,
                    equation_properties,
                )

            result = {
                "success": not property_failures,
                "physics": {
                    "name": (
                        physics_java.label()
                        if hasattr(physics_java, 'label')
                        else physics_type
                    ),
                    "type": physics_type,
                    "tag": tag,
                    "component": comp.tag(),
                    "dependent_variables": variables,
                    "domain_selection": (
                        list(domain_selection) if domain_selection else "all"
                    ),
                    "equation_properties": equation_properties or {},
                }
            }
            if property_failures:
                result["error"] = "One or more PDE equation properties were rejected."
                result["property_errors"] = property_failures
            return result
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to add {physics_type}: {str(e)}"
            }

    @mcp.tool()
    def physics_add_coefficient_form_pde(
        dependent_variables: Sequence[str] = ("u",),
        equation_properties: Optional[dict] = None,
        domain_selection: Optional[Sequence[int]] = None,
        component_name: Optional[str] = None,
        physics_tag: str = "c",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a Coefficient Form PDE interface.

        Common equation properties include c, a, f, da, ea, al, be, and
        ga. Values can be scalar, vector, or matrix expressions.
        Rejected properties are returned explicitly.
        """
        return _add_pde_interface(
            "CoefficientFormPDE",
            "c",
            "cfeq1",
            dependent_variables,
            equation_properties,
            domain_selection,
            component_name,
            physics_tag,
            model_name,
        )

    @mcp.tool()
    def physics_add_general_form_pde(
        dependent_variables: Sequence[str] = ("u",),
        equation_properties: Optional[dict] = None,
        domain_selection: Optional[Sequence[int]] = None,
        component_name: Optional[str] = None,
        physics_tag: str = "g",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a General Form PDE interface.

        Common equation properties include Ga, f, da, and ea. Values can be
        scalar, vector, or matrix expressions. Rejected properties are returned
        explicitly.
        """
        return _add_pde_interface(
            "GeneralFormPDE",
            "g",
            "gfeq1",
            dependent_variables,
            equation_properties,
            domain_selection,
            component_name,
            physics_tag,
            model_name,
        )

    @mcp.tool()
    def physics_add_weak_form_pde(
        dependent_variables: Sequence[str] = ("u",),
        equation_properties: Optional[dict] = None,
        domain_selection: Optional[Sequence[int]] = None,
        component_name: Optional[str] = None,
        physics_tag: str = "w",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a Weak Form PDE interface.

        Set the weak expression with equation_properties={"weak": "..."}.
        Rejected properties are returned explicitly.
        """
        return _add_pde_interface(
            "WeakFormPDE",
            "w",
            "wfeq1",
            dependent_variables,
            equation_properties,
            domain_selection,
            component_name,
            physics_tag,
            model_name,
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

        Common boundary conditions for Heat Transfer:
        - "Temperature": Fixed temperature
        - "HeatFlux": Heat flux boundary
        - "ConvectiveHeatFlux": Convection cooling
        - "ThermalInsulation": Thermal insulation (adiabatic)

        Common for Solid Mechanics:
        - "Fixed": Fixed constraint
        - "Roller": Roller constraint
        - "Symmetry": Symmetry plane
        - "BoundaryLoad": Applied force/pressure

        Common for Electrostatics:
        - "Ground": Zero potential boundary
        - "ElectricPotential": Specified voltage
        - "SurfaceChargeDensity": Surface charge
        - "ZeroCharge": Zero normal displacement field

        Args:
            physics_name: Name or label of the physics interface
            boundary_condition: Type of boundary condition (e.g. "Temperature", "HeatFlux")
            boundary_selection: Boundary/edge numbers to apply condition to
            properties: Dictionary of property names and values (e.g. {"T0": "293.15[K]"})
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

        properties = properties or {}

        try:
            jm = model.java

            physics_java = _find_physics_java(jm, physics_name)

            if physics_java is None:
                return {"success": False, "error": f"Physics interface not found: {physics_name}"}

            tag = _make_tag(boundary_condition.lower())
            bc = physics_java.create(tag, boundary_condition)
            bc.selection().set([int(b) for b in boundary_selection])

            if properties:
                for prop_name, prop_value in properties.items():
                    try:
                        bc.set(prop_name, prop_value)
                    except Exception:
                        pass

            bc.label(f'{boundary_condition} (Boundaries {list(boundary_selection)})')

            return {
                "success": True,
                "boundary_condition": {
                    "name": tag,
                    "type": boundary_condition,
                    "physics": physics_name,
                    "selection": list(boundary_selection),
                    "properties": properties,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to configure boundary: {str(e)}"}

    def _setup_specialized_boundaries(
        physics_name,
        boundary_conditions,
        condition_reference,
        boundary_dimension,
        model_name,
    ):
        """Find a physics interface and create specialized boundary features."""
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            context = _find_physics_context(model.java, physics_name)
            if context is None:
                return {
                    "success": False,
                    "error": f"Physics interface not found: {physics_name}"
                }
            comp, physics_java = context
            dimension = _get_boundary_dimension(comp, boundary_dimension)

            result = _create_boundary_features(
                physics_java,
                physics_name,
                boundary_conditions,
                dimension,
            )
            custom_types = sorted({
                condition_type
                for condition in boundary_conditions
                for condition_type in [
                    condition.get("type") or condition.get("boundary_condition")
                ]
                if condition_type and condition_type not in condition_reference
            })
            if custom_types:
                result["custom_condition_types"] = custom_types
                result["note"] = (
                    "Custom condition types were passed directly to the COMSOL "
                    "Java API. Their availability depends on the selected physics "
                    "interface, installed products, and COMSOL version."
                )
            return result
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to setup boundaries: {str(e)}"
            }

    @mcp.tool()
    def physics_configure_acoustic_boundary(
        physics_name: str,
        boundary_condition: str,
        boundary_selection: Optional[Sequence[int]] = None,
        properties: Optional[dict] = None,
        selection_name: Optional[str] = None,
        boundary_dimension: Optional[int] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Configure one Pressure Acoustics boundary condition.

        Common types are SoundHard, SoundSoft, Pressure, Impedance,
        NormalAcceleration, NormalVelocity, PlaneWaveRadiation, and
        SphericalWaveRadiation. Unknown types are passed through for other
        acoustic interfaces and versions. Property-setting errors are returned
        instead of being ignored.
        """
        return _setup_specialized_boundaries(
            physics_name,
            [{
                "type": boundary_condition,
                "boundaries": (
                    list(boundary_selection) if boundary_selection else None
                ),
                "selection_name": selection_name,
                "properties": properties or {},
            }],
            ACOUSTIC_BOUNDARY_CONDITIONS,
            boundary_dimension,
            model_name,
        )

    @mcp.tool()
    def physics_setup_acoustic_boundaries(
        physics_name: str,
        boundary_conditions: Sequence[dict],
        boundary_dimension: Optional[int] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Configure multiple acoustic boundary conditions in one call.

        Each item accepts:
        - type: COMSOL boundary feature type
        - boundaries: List of boundary numbers
        - selection_name: Optional named COMSOL selection instead of numbers
        - properties: Optional COMSOL property dictionary
        - dimension: Optional geometric entity dimension override
        - tag: Optional explicit feature tag
        - label: Optional display label

        Example:
            [
                {"type": "SoundHard", "boundaries": [1, 2]},
                {
                    "type": "Impedance",
                    "boundaries": [3],
                    "properties": {"Zn": "rho0*c0"}
                }
            ]
        """
        return _setup_specialized_boundaries(
            physics_name,
            boundary_conditions,
            ACOUSTIC_BOUNDARY_CONDITIONS,
            boundary_dimension,
            model_name,
        )

    @mcp.tool()
    def physics_configure_pde_boundary(
        physics_name: str,
        boundary_condition: str,
        boundary_selection: Optional[Sequence[int]] = None,
        properties: Optional[dict] = None,
        selection_name: Optional[str] = None,
        boundary_dimension: Optional[int] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Configure one Coefficient, General, or Weak Form PDE boundary condition.

        Common types are DirichletBoundary, FluxBoundary, ZeroFluxBoundary,
        WeakContribution, and PeriodicCondition. Unknown types are passed
        through for version-specific PDE features. Use selection_name to bind
        the condition to a named geometric selection instead of entity numbers.
        Property-setting errors are returned instead of being ignored.
        """
        condition_type = PDE_BOUNDARY_ALIASES.get(
            boundary_condition.lower(),
            boundary_condition,
        )
        return _setup_specialized_boundaries(
            physics_name,
            [{
                "type": condition_type,
                "boundaries": (
                    list(boundary_selection) if boundary_selection else None
                ),
                "selection_name": selection_name,
                "properties": properties or {},
            }],
            PDE_BOUNDARY_CONDITIONS,
            boundary_dimension,
            model_name,
        )

    @mcp.tool()
    def physics_setup_pde_boundaries(
        physics_name: str,
        boundary_conditions: Sequence[dict],
        boundary_dimension: Optional[int] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Configure multiple PDE boundary conditions in one call.

        Each item accepts:
        - type: COMSOL boundary feature type
        - boundaries: List of boundary numbers
        - selection_name: Optional named COMSOL selection instead of numbers
        - properties: Optional scalar, vector, or matrix property dictionary
        - dimension: Optional geometric entity dimension override
        - tag: Optional explicit feature tag
        - label: Optional display label
        """
        normalized_conditions = []
        for condition in boundary_conditions:
            normalized = dict(condition)
            condition_type = (
                normalized.get("type")
                or normalized.get("boundary_condition")
            )
            if condition_type:
                normalized["type"] = PDE_BOUNDARY_ALIASES.get(
                    condition_type.lower(),
                    condition_type,
                )
            normalized_conditions.append(normalized)

        return _setup_specialized_boundaries(
            physics_name,
            normalized_conditions,
            PDE_BOUNDARY_CONDITIONS,
            boundary_dimension,
            model_name,
        )
    
    @mcp.tool()
    def physics_set_material(
        physics_name: str,
        material_name: str,
        domain_selection: Optional[Sequence[int]] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Assign a material to physics domains.

        This tool tries to add the material from COMSOL's built-in library
        if it's not already in the model.

        Args:
            physics_name: Name of the physics interface
            material_name: Name of the material (e.g. "Silicon", "Steel AISI 4340", "Copper")
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
            jm = model.java
            materials = model.materials()
            tag = material_name.replace(" ", "_").replace("-", "_")

            if material_name not in materials:
                comp = _get_component_java(jm)
                try:
                    mat = comp.material().create(tag, "Common")
                    mat.label(material_name)
                except Exception as e:
                    return {"success": False, "error": f"Could not create material node: {str(e)}"}

            physics_java = _find_physics_java(jm, physics_name)

            if physics_java is None:
                return {"success": False, "error": f"Physics interface not found: {physics_name}"}

            mat_node = comp.material(tag)
            if domain_selection:
                mat_node.selection().set([int(d) for d in domain_selection])

            return {
                "success": True,
                "material": material_name,
                "physics": physics_name,
                "domain_selection": list(domain_selection) if domain_selection else "all",
                "message": f"Material '{material_name}' assigned to physics '{physics_name}'",
                "warning": "Material node has no physical properties. Set properties manually in COMSOL GUI.",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to set material: {str(e)}"}
    
    @mcp.tool()
    def multiphysics_add(
        coupling_type: str,
        physics_list: Sequence[str],
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
            physics_list: Names of physics interfaces to couple
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
        
        try:
            coupling_node = model.create("multiphysics", coupling_type)
            
            return {
                "success": True,
                "coupling": {
                    "name": coupling_node.name() if hasattr(coupling_node, 'name') else coupling_type,
                    "type": coupling_type,
                    "physics": list(physics_list),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add multiphysics: {str(e)}"}
    
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
    def geometry_get_boundaries(
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Get all boundaries from a geometry with their properties.

        Use this to identify which boundary numbers correspond to which faces
        before setting boundary conditions.

        Args:
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            model_name: Model name (default: current model)

        Returns:
            List of boundaries with their numbers and areas
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java

            comp = jm.component(component_name)
            if comp is None:
                return {"success": False, "error": f"Component '{component_name}' not found."}

            geom_tag = geometry_name
            if not geom_tag:
                geoms = comp.geom()
                if geoms.size() == 0:
                    return {"success": False, "error": "No geometries in component."}
                geom_tag = geoms[0].tag()

            geom = comp.geom(geom_tag)
            geom.run()

            nboundary = geom.getNboundary()
            ndomain = geom.getNdomain()

            boundaries = []
            for i in range(1, nboundary + 1):
                try:
                    bd_info = {"boundary_number": i}
                    boundaries.append(bd_info)
                except Exception:
                    boundaries.append({"boundary_number": i, "error": "Could not get info"})

            return {
                "success": True,
                "geometry": geom_tag,
                "total_boundaries": nboundary,
                "total_domains": ndomain,
                "boundaries": boundaries,
                "hint": "Use boundary_number to set boundary conditions with physics_configure_boundary",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to get boundaries: {str(e)}"}
    
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
            physics_java = _find_physics_java(jm, physics_name)

            if physics_java is None:
                return {"success": False, "error": f"Could not find physics interface: {physics_name}"}

            results = {"inlets": [], "outlets": []}

            for i, boundary in enumerate(inlet_boundaries):
                inlet_tag = _make_tag("inl")
                inlet = physics_java.create(inlet_tag, 'InletBoundary')
                inlet.selection().set([int(boundary)])
                inlet.set('U0', inlet_velocity)
                inlet.label(f'Inlet {i+1} (Boundary {boundary})')
                results["inlets"].append({
                    "tag": inlet_tag,
                    "boundary": boundary,
                    "velocity": inlet_velocity
                })
            
            for i, boundary in enumerate(outlet_boundaries):
                outlet_tag = _make_tag("out")
                outlet = physics_java.create(outlet_tag, 'OutletBoundary')
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
        heat_flux_boundaries: Optional[Sequence[int]] = None,
        temperature_boundaries: Optional[Sequence[int]] = None,
        convection_boundaries: Optional[Sequence[int]] = None,
        heat_flux_value: str = "1e6[W/m^2]",
        temperature_value: str = "293.15[K]",
        convection_coeff: str = "10[W/(m^2*K)]",
        ambient_temp: str = "293.15[K]",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Setup Heat Transfer boundary conditions with specified boundaries.
        
        This tool configures thermal boundary conditions for heat transfer simulation:
        - Heat flux boundaries (heat sources)
        - Temperature boundaries (heat sinks)
        - Convective cooling/heating boundaries
        
        Args:
            physics_name: Name of the Heat Transfer physics interface
            heat_flux_boundaries: List of boundary numbers for heat flux
            temperature_boundaries: List of boundary numbers for fixed temperature
            convection_boundaries: List of boundary numbers for convection
            heat_flux_value: Heat flux value (default: "1e6[W/m^2]")
            temperature_value: Temperature value (default: "293.15[K]" = 20°C)
            convection_coeff: Convection coefficient (default: "10[W/(m^2*K)]")
            ambient_temp: Ambient temperature for convection (default: "293.15[K]")
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

        heat_flux_boundaries = heat_flux_boundaries or []
        temperature_boundaries = temperature_boundaries or []
        convection_boundaries = convection_boundaries or []

        try:
            jm = model.java

            physics_interfaces = model.physics()
            if physics_name not in physics_interfaces:
                return {"success": False, "error": f"Physics '{physics_name}' not found. Available: {physics_interfaces}"}

            physics_java = _find_physics_java(jm, physics_name)

            if physics_java is None:
                return {"success": False, "error": f"Could not find physics interface: {physics_name}"}

            results = {"heat_flux": [], "temperature": [], "convection": []}

            for i, boundary in enumerate(heat_flux_boundaries):
                tag = _make_tag("hf")
                bc = physics_java.create(tag, 'HeatFluxBoundary')
                bc.selection().set([int(boundary)])
                bc.set('q0', heat_flux_value)
                bc.label(f'Heat Flux {i+1} (Boundary {boundary})')
                results["heat_flux"].append({
                    "tag": tag,
                    "boundary": boundary,
                    "heat_flux": heat_flux_value
                })
            
            for i, boundary in enumerate(temperature_boundaries):
                tag = _make_tag("temp")
                bc = physics_java.create(tag, 'TemperatureBoundary')
                bc.selection().set([int(boundary)])
                bc.set('T0', temperature_value)
                bc.label(f'Temperature {i+1} (Boundary {boundary})')
                results["temperature"].append({
                    "tag": tag,
                    "boundary": boundary,
                    "temperature": temperature_value
                })
            
            for i, boundary in enumerate(convection_boundaries):
                tag = _make_tag("conv")
                bc = physics_java.create(tag, 'ConvectiveHeatFlux')
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
            
            return {
                "success": True,
                "physics": physics_name,
                "configured_boundaries": results,
                "summary": {
                    "heat_flux_boundaries": len(heat_flux_boundaries),
                    "temperature_boundaries": len(temperature_boundaries),
                    "convection_boundaries": len(convection_boundaries)
                },
                "message": f"Configured {len(heat_flux_boundaries)} heat flux, {len(temperature_boundaries)} temperature, and {len(convection_boundaries)} convection boundaries",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to setup heat boundaries: {str(e)}"}

    @mcp.tool()
    def physics_boundary_selection(
        physics_name: str,
        boundary_condition_type: str,
        boundary_numbers: Sequence[int],
        properties: Optional[dict] = None,
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
        - Temperature: Set T0 (temperature)
        - HeatFlux: Set q0 (heat flux)
        - ConvectiveHeatFlux: Set h (coefficient), Text (ambient temp)

        Laminar Flow (spf):
        - InletBoundary: Set U0 (velocity)
        - OutletBoundary: Set p0 (pressure)
        - Wall: No-slip wall

        Solid Mechanics (solid):
        - Fixed: Fixed constraint
        - BoundaryLoad: Set Fx, Fy, Fz or FAx, FAy, FAz

        Args:
            physics_name: Name or label of the physics interface
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

        properties = properties or {}

        try:
            jm = model.java

            physics_java = _find_physics_java(jm, physics_name)

            if physics_java is None:
                return {"success": False, "error": f"Physics interface not found: {physics_name}"}

            tag = _make_tag("bc")
            bc = physics_java.create(tag, boundary_condition_type)
            bc.selection().set([int(b) for b in boundary_numbers])

            for prop_name, prop_value in properties.items():
                try:
                    bc.set(prop_name, prop_value)
                except Exception:
                    pass

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


