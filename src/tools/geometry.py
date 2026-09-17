"""Geometry tools for COMSOL MCP Server."""

from typing import Optional, Sequence
from mcp.server.fastmcp import FastMCP

from .session import session_manager


def _get_geometry_node(model, geometry_name: Optional[str], component_name: str = "comp1"):
    """Helper to get geometry node via Java API.
    
    Returns:
        tuple: (geom_node, error_message) - geom_node is None if error
    """
    jm = model.java
    
    try:
        comp = jm.component(component_name)
        if comp is None:
            return None, f"Component '{component_name}' not found."
        
        if geometry_name:
            geom = comp.geom(geometry_name)
            if geom is None:
                return None, f"Geometry '{geometry_name}' not found in component '{component_name}'."
        else:
            geoms = list(comp.geom())
            if not geoms:
                return None, "No geometry sequences found. Create one first with geometry_create."
            geom = geoms[0]
        
        return geom, None
    except Exception as e:
        return None, f"Failed to get geometry: {str(e)}"


def register_geometry_tools(mcp: FastMCP) -> None:
    """Register geometry tools with the MCP server."""
    
    @mcp.tool()
    def geometry_list(model_name: Optional[str] = None) -> dict:
        """
        List all geometry sequences in a model.
        
        Args:
            model_name: Model name (default: current model)
        
        Returns:
            List of geometry sequence names
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geometries = model.geometries()
            return {
                "success": True,
                "geometries": geometries,
                "count": len(geometries),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list geometries: {str(e)}"}
    
    @mcp.tool()
    def geometry_create(
        geometry_name: Optional[str] = None,
        space_dimension: int = 3,
        component_name: str = "comp1",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Create a new geometry sequence in the model's component.
        
        IMPORTANT: A component must exist first. Use model_create_component if needed.
        
        Args:
            geometry_name: Name for the geometry sequence (default: 'geom1')
            space_dimension: Space dimension - 2 for 2D, 3 for 3D (default: 3)
            component_name: Component name (default: 'comp1')
            model_name: Model name (default: current model)
        
        Returns:
            Created geometry info
        """
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
                    "error": f"Component '{component_name}' not found. Create it first with model_create_component."
                }
            
            geom = comp.geom().create(geom_name, space_dimension)
            
            return {
                "success": True,
                "geometry": geom_name,
                "component": component_name,
                "space_dimension": space_dimension,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create geometry: {str(e)}"}
    
    @mcp.tool()
    def geometry_add_feature(
        feature_type: str,
        geometry_name: Optional[str] = None,
        feature_name: Optional[str] = None,
        model_name: Optional[str] = None,
        **kwargs
    ) -> dict:
        """
        Add a geometry feature to a geometry sequence.
        
        Common feature types:
        - Block: Rectangular block (3D)
        - Cylinder: Cylinder (3D)
        - Sphere: Sphere (3D)
        - Cone: Cone (3D)
        - WorkPlane: Working plane for 2D geometry
        - Rectangle: Rectangle (2D)
        - Circle: Circle (2D)
        - Polygon: Polygon from points
        - Import: Import CAD geometry
        - Union, Intersection, Difference: Boolean operations
        
        Args:
            feature_type: Type of geometry feature (Block, Cylinder, etc.)
            geometry_name: Geometry sequence name (default: first geometry)
            feature_name: Name for the feature (auto-generated if None)
            model_name: Model name (default: current model)
            **kwargs: Feature-specific properties (position, size, etc.)
        
        Returns:
            Created feature info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geometries = model.geometries()
            if not geometries:
                return {"success": False, "error": "No geometry sequences found. Create one first."}
            
            target_geom = geometry_name or geometries[0]
            if target_geom not in geometries:
                return {"success": False, "error": f"Geometry not found: {target_geom}"}
            
            geom_node = model / "geometries" / target_geom
            feature_node = geom_node.create(feature_type, feature_name)
            
            for prop_name, prop_value in kwargs.items():
                try:
                    feature_node.property(prop_name, prop_value)
                except Exception:
                    pass
            
            return {
                "success": True,
                "feature": {
                    "name": feature_node.name() if hasattr(feature_node, 'name') else feature_name,
                    "type": feature_type,
                    "geometry": target_geom,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add geometry feature: {str(e)}"}
    
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
        Add a block (rectangular cuboid) to the geometry.
        
        Args:
            position: Base position [x, y, z] in meters (default: origin)
            size: Dimensions [width, depth, height] in meters (default: 1m cube)
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature name (auto-generated if None)
            model_name: Model name (default: current model)
        
        Returns:
            Created block info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geom, error = _get_geometry_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}
            
            feat_name = feature_name or f"blk{geom.feature().size() + 1}"
            block = geom.feature().create(feat_name, "Block")
            
            block.set("pos", [str(p) for p in position])
            block.set("size", [str(s) for s in size])
            
            return {
                "success": True,
                "feature": {
                    "name": feat_name,
                    "type": "Block",
                    "position": list(position),
                    "size": list(size),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add block: {str(e)}"}
    
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
        Add a cylinder to the geometry.
        
        Args:
            position: Center of base [x, y, z] in meters
            radius: Radius in meters (default: 0.5)
            height: Height in meters (default: 1.0)
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature name (auto-generated if None)
            model_name: Model name (default: current model)
        
        Returns:
            Created cylinder info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geom, error = _get_geometry_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}
            
            feat_name = feature_name or f"cyl{geom.feature().size() + 1}"
            cyl = geom.feature().create(feat_name, "Cylinder")
            
            cyl.set("pos", [str(p) for p in position])
            cyl.set("r", str(radius))
            cyl.set("h", str(height))
            
            return {
                "success": True,
                "feature": {
                    "name": feat_name,
                    "type": "Cylinder",
                    "position": list(position),
                    "radius": radius,
                    "height": height,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add cylinder: {str(e)}"}
    
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
        Add a sphere to the geometry.
        
        Args:
            position: Center [x, y, z] in meters
            radius: Radius in meters (default: 0.5)
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature name (auto-generated if None)
            model_name: Model name (default: current model)
        
        Returns:
            Created sphere info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geom, error = _get_geometry_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}
            
            feat_name = feature_name or f"sph{geom.feature().size() + 1}"
            sphere = geom.feature().create(feat_name, "Sphere")
            
            sphere.set("pos", [str(p) for p in position])
            sphere.set("r", str(radius))
            
            return {
                "success": True,
                "feature": {
                    "name": feat_name,
                    "type": "Sphere",
                    "position": list(position),
                    "radius": radius,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add sphere: {str(e)}"}
    
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
            position: Base position [x, y] in meters
            size: Dimensions [width, height] in meters
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature name (auto-generated if None)
            model_name: Model name (default: current model)
        
        Returns:
            Created rectangle info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geom, error = _get_geometry_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}
            
            feat_name = feature_name or f"r{geom.feature().size() + 1}"
            rect = geom.feature().create(feat_name, "Rectangle")
            
            rect.set("pos", [str(p) for p in position])
            rect.set("size", [str(s) for s in size])
            
            return {
                "success": True,
                "feature": {
                    "name": feat_name,
                    "type": "Rectangle",
                    "position": list(position),
                    "size": list(size),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add rectangle: {str(e)}"}
    
    @mcp.tool()
    def geometry_add_circle(
        position: Sequence[float] = (0, 0),
        radius: float = 0.5,
        geometry_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Add a circle to a 2D geometry or work plane.
        
        Args:
            position: Center [x, y] in meters
            radius: Radius in meters (default: 0.5)
            geometry_name: Geometry sequence name
            model_name: Model name (default: current model)
        
        Returns:
            Created circle info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geometries = model.geometries()
            if not geometries:
                return {"success": False, "error": "No geometry sequences found."}
            
            target_geom = geometry_name or geometries[0]
            geom_node = model / "geometries" / target_geom
            circle_node = geom_node.create("Circle")
            
            if len(position) == 2:
                circle_node.property("pos", list(position))
            circle_node.property("r", radius)
            
            return {
                "success": True,
                "feature": {
                    "name": circle_node.name() if hasattr(circle_node, 'name') else "Circle",
                    "type": "Circle",
                    "geometry": target_geom,
                    "position": list(position),
                    "radius": radius,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to add circle: {str(e)}"}
    
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
            input_objects: Names of objects to unite
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature tag (auto-generated if None)
            model_name: Model name (default: current model)
        
        Returns:
            Created union operation info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geom, error = _get_geometry_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}

            feat_tag = feature_name or f"uni{geom.feature().size() + 1}"
            union_node = geom.feature().create(feat_tag, "Union")
            try:
                union_node.selection("input").set([str(o) for o in input_objects])
            except Exception:
                # a failed selection leaves an empty-input node that breaks all later builds
                geom.feature().remove(feat_tag)
                raise
            
            return {
                "success": True,
                "feature": {
                    "name": feat_tag,
                    "type": "Union",
                    "geometry": str(geom.tag()),
                    "input_objects": list(input_objects),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create union: {str(e)}"}
    
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
            input_object: Object to subtract from (e.g., 'blk1')
            objects_to_subtract: Objects to remove (e.g., ['cyl1'])
            geometry_name: Geometry sequence name (default: first geometry)
            component_name: Component name (default: 'comp1')
            feature_name: Feature name (auto-generated if None)
            model_name: Model name (default: current model)
        
        Returns:
            Created difference operation info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geom, error = _get_geometry_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}
            
            feat_name = feature_name or f"dif{geom.feature().size() + 1}"
            diff = geom.feature().create(feat_name, "Difference")
            try:
                diff.selection("input").set([input_object])
                diff.selection("input2").set(list(objects_to_subtract))
            except Exception:
                # a failed selection leaves an empty-input node that breaks all later builds
                geom.feature().remove(feat_name)
                raise
            
            return {
                "success": True,
                "feature": {
                    "name": feat_name,
                    "type": "Difference",
                    "input_object": input_object,
                    "subtracted": list(objects_to_subtract),
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create difference: {str(e)}"}
    
    @mcp.tool()
    def geometry_import(
        file_path: str,
        geometry_name: Optional[str] = None,
        import_type: str = "CAD",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Import geometry from a CAD file.
        
        Supported formats: STEP, IGES, STL, NASTRAN, etc.
        
        Args:
            file_path: Path to the CAD file
            geometry_name: Geometry sequence name
            import_type: Import type (CAD, mesh, etc.)
            model_name: Model name (default: current model)
        
        Returns:
            Import operation info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geometries = model.geometries()
            if not geometries:
                return {"success": False, "error": "No geometry sequences found."}
            
            target_geom = geometry_name or geometries[0]
            geom_node = model / "geometries" / target_geom
            import_node = geom_node.create("Import")
            
            model.import_(import_node, file_path)
            
            return {
                "success": True,
                "feature": {
                    "name": import_node.name() if hasattr(import_node, 'name') else "Import",
                    "type": "Import",
                    "geometry": target_geom,
                    "file": file_path,
                    "import_type": import_type,
                }
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to import geometry: {str(e)}"}
    
    @mcp.tool()
    def geometry_build(
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Build the geometry sequence to generate the actual geometry.
        
        This must be called after adding/modifying geometry features.
        
        Args:
            geometry_name: Geometry sequence name (default: build all)
            component_name: Component name (default: 'comp1')
            model_name: Model name (default: current model)
        
        Returns:
            Build status
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geom, error = _get_geometry_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}
            
            geom.run()
            
            return {
                "success": True,
                "geometry": geometry_name or "first",
                "message": "Geometry built successfully.",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to build geometry: {str(e)}"}
    
    @mcp.tool()
    def geometry_list_features(
        geometry_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        List all features in a geometry sequence.
        
        Args:
            geometry_name: Geometry sequence name (default: first geometry)
            model_name: Model name (default: current model)
        
        Returns:
            List of geometry features with their types
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            geometries = model.geometries()
            if not geometries:
                return {"success": False, "error": "No geometry sequences found."}
            
            target_geom = geometry_name or geometries[0]
            if target_geom not in geometries:
                return {"success": False, "error": f"Geometry not found: {target_geom}"}
            
            geom_node = model / "geometries" / target_geom
            features = []
            
            for child in geom_node.children():
                feat_info = {"name": child.name()}
                try:
                    feat_info["type"] = child.type() if hasattr(child, 'type') else "unknown"
                except Exception:
                    pass
                features.append(feat_info)
            
            return {
                "success": True,
                "geometry": target_geom,
                "features": features,
                "count": len(features),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list features: {str(e)}"}

    @mcp.tool()
    def geometry_create_box_selection(
        selection_name: str,
        x_min: str,
        x_max: str,
        y_min: str,
        y_max: str,
        z_min: Optional[str] = None,
        z_max: Optional[str] = None,
        entity_dimension: int = 1,
        condition: str = "intersects",
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Create a named Box selection for geometry entities by position.

        For a 2D model, use entity_dimension=1 to select boundaries. For a 3D
        model, use entity_dimension=2 to select boundary faces.

        Args:
            selection_name: Stable selection tag
            x_min: Minimum x-coordinate expression
            x_max: Maximum x-coordinate expression
            y_min: Minimum y-coordinate expression
            y_max: Maximum y-coordinate expression
            z_min: Optional minimum z-coordinate expression
            z_max: Optional maximum z-coordinate expression
            entity_dimension: Entity dimension (default: 1)
            condition: Box condition, usually "intersects" or "inside"
            geometry_name: Geometry sequence tag (default: first geometry)
            component_name: Component name (default: "comp1")
            model_name: Model name (default: current model)

        Returns:
            Created named selection and matched entity numbers when available
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
                return {
                    "success": False,
                    "error": f"Component not found: {component_name}"
                }

            if geometry_name:
                geom_tag = geometry_name
            else:
                geometries = comp.geom()
                if geometries.size() == 0:
                    return {
                        "success": False,
                        "error": "No geometry sequences found."
                    }
                geom_tag = str(geometries.tags()[0])

            selection = comp.selection().create(selection_name, "Box")
            selection.geom(geom_tag, int(entity_dimension))
            selection.set("xmin", x_min)
            selection.set("xmax", x_max)
            selection.set("ymin", y_min)
            selection.set("ymax", y_max)
            if z_min is not None:
                selection.set("zmin", z_min)
            if z_max is not None:
                selection.set("zmax", z_max)
            selection.set("condition", condition)

            result = {
                "success": True,
                "selection": {
                    "tag": selection_name,
                    "type": "Box",
                    "component": component_name,
                    "geometry": geom_tag,
                    "entity_dimension": int(entity_dimension),
                    "condition": condition,
                    "bounds": {
                        "x_min": x_min,
                        "x_max": x_max,
                        "y_min": y_min,
                        "y_max": y_max,
                    },
                },
            }
            if z_min is not None or z_max is not None:
                result["selection"]["bounds"].update({
                    "z_min": z_min,
                    "z_max": z_max,
                })

            try:
                result["selection"]["entities"] = [
                    int(entity) for entity in selection.entities()
                ]
            except Exception as e:
                result["selection"]["entities"] = []
                result["warning"] = (
                    "Selection created, but entities could not be evaluated. "
                    f"Build the geometry first. Details: {str(e)}"
                )

            return result
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to create box selection: {str(e)}"
            }

    @mcp.tool()
    def geometry_create_side_selections(
        x_min: str,
        x_max: str,
        y_min: str,
        y_max: str,
        prefix: str = "side",
        tolerance: str = "1e-9[m]",
        entity_dimension: int = 1,
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Create named left, right, bottom, and top Box selections.

        This is intended for rectangular 2D domains such as waveguides and
        water-wave tanks. The resulting tags are <prefix>_left,
        <prefix>_right, <prefix>_bottom, and <prefix>_top.
        """
        expanded_x_min = f"({x_min})-({tolerance})"
        expanded_x_max = f"({x_max})+({tolerance})"
        expanded_y_min = f"({y_min})-({tolerance})"
        expanded_y_max = f"({y_max})+({tolerance})"

        definitions = {
            "left": {
                "x_min": expanded_x_min,
                "x_max": f"({x_min})+({tolerance})",
                "y_min": expanded_y_min,
                "y_max": expanded_y_max,
            },
            "right": {
                "x_min": f"({x_max})-({tolerance})",
                "x_max": expanded_x_max,
                "y_min": expanded_y_min,
                "y_max": expanded_y_max,
            },
            "bottom": {
                "x_min": expanded_x_min,
                "x_max": expanded_x_max,
                "y_min": expanded_y_min,
                "y_max": f"({y_min})+({tolerance})",
            },
            "top": {
                "x_min": expanded_x_min,
                "x_max": expanded_x_max,
                "y_min": f"({y_max})-({tolerance})",
                "y_max": expanded_y_max,
            },
        }

        created = {}
        failed = {}
        for side, bounds in definitions.items():
            selection_tag = f"{prefix}_{side}"
            result = geometry_create_box_selection(
                selection_name=selection_tag,
                entity_dimension=entity_dimension,
                condition="intersects",
                geometry_name=geometry_name,
                component_name=component_name,
                model_name=model_name,
                **bounds,
            )
            if result.get("success"):
                created[side] = result["selection"]
            else:
                failed[side] = result.get("error", "Unknown error")

        return {
            "success": not failed,
            "prefix": prefix,
            "selections": created,
            "failed_selections": failed,
            "created_count": len(created),
            "failed_count": len(failed),
        }
