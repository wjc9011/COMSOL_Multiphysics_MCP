"""MIM (Metal-Insulator-Metal) patch metasurface tools for COMSOL MCP Server.

These tools support the Au-Al2O3-Au MIM thermal emitter workflow
(Chen et al. Int. J. Thermal Sciences 185, 2023, 108069):
  1. geometry_probe_domains  -- enhanced boundary probing with up/down domains
  2. mim_patch_build          -- build patch geometry + BCs + mesh from a baseline
  3. mim_evaluate_spectral    -- evaluate Rtotal/Ttotal/Atotal vs wavelength

Key discovery: the Floquet periodic condition in ewfd (curl elements) REQUIRES
compatible (identical) meshes on source/destination side faces. The "CopyFace"
mesh feature copies the 2-D mesh from source to destination, ensuring exact
conformity. Combined with FreeTet for the volume, this avoids the Sweep-mesh
topology-mismatch issue that occurs when a patch domain changes the cross-section.
"""

from typing import Optional, Sequence

from mcp.server.mcpserver import MCPServer

from .session import session_manager

MAX_PROBED_BOUNDARIES = 4096

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _get_geom_node(model, geometry_name: Optional[str], component_name: str = "comp1"):
    """Return (geom_node, error)."""
    jm = model.java
    try:
        comp = jm.component(component_name)
        if comp is None:
            return None, f"Component '{component_name}' not found."
        if geometry_name:
            geom = comp.geom(geometry_name)
            if geom is None:
                return None, f"Geometry '{geometry_name}' not found."
        else:
            geoms = comp.geom()
            if geoms.size() == 0:
                return None, "No geometry sequences found."
            tags = list(geoms.tags())
            geom = geoms.get(tags[0])
        return geom, None
    except Exception as e:
        return None, f"Failed to get geometry: {e}"


def _probe_boundaries(geom):
    """Return complete boundary data, domain count, boundary count, and dimension.

    Refuse geometries above 4096 boundaries before allocating adjacency data.
    Side classification requires complete data and must never use truncation.
    """
    import jpype as _jp

    n_bnd = geom.getNBoundaries()
    if n_bnd > MAX_PROBED_BOUNDARIES:
        raise ValueError(f"geometry exceeds boundary probe limit ({MAX_PROBED_BOUNDARIES})")
    n_dom = geom.getNDomains()
    sdim = int(geom.getSDim())

    ud = geom.getUpDown()
    ups = list(ud[0]) if n_bnd > 0 else []
    downs = list(ud[1]) if n_bnd > 0 else []

    PP = _jp.JArray(_jp.JArray(_jp.JDouble))(1)
    boundaries = []
    for i in range(1, n_bnd + 1):
        info = {"boundary_number": i, "up_domain": ups[i - 1], "down_domain": downs[i - 1]}
        try:
            pr = list(geom.faceParamRange(i))
            u_mid = (float(pr[0]) + float(pr[1])) / 2.0
            v_mid = (float(pr[2]) + float(pr[3])) / 2.0
            PP[0] = _jp.JArray(_jp.JDouble)([u_mid, v_mid])
            normal = list(geom.faceNormal(i, PP)[0])
            center = list(geom.faceX(i, PP)[0])
            info["normal"] = [float(n) for n in normal]
            info["center"] = [float(c) for c in center]
            info["interior"] = ups[i - 1] != 0 and downs[i - 1] != 0
        except Exception as e:
            info["error"] = f"probe failed: {str(e)[:60]}"
        boundaries.append(info)
    return boundaries, n_dom, n_bnd, sdim


def _identify_side_pairs(boundaries, P_val=None, bbox=None, tol=1e-12):
    """Identify periodic CELL side face pairs from boundary normals + centers.

    Returns dict: {x_src, x_dst, y_src, y_dst, bottom, top} lists of bnd numbers.

    IMPORTANT: This filters by BOTH normal AND center coordinate so that interior
    patch/air-interface side faces (which share the same ±x/±y normal but are at
    interior positions, e.g. cx=L/2 inside the cell) are NOT misclassified as the
    Floquet periodic cell side. Without coordinate filtering, CopyFace source
    would include patch side faces and break Floquet mesh compatibility.

    Args:
        boundaries: list of boundary dicts (each must have 'normal' and 'center').
        P_val: cell period (used as x_max/y_max if bbox not given). Backward compat.
        bbox: optional (xmin, xmax, ymin, ymax, zmin, zmax) for tighter filtering.
        tol: absolute tolerance for matching cell-edge coordinate (metres).
    """
    if bbox is None and P_val is not None:
        bbox = (0.0, P_val, 0.0, P_val, 0.0, P_val)
    if bbox is None:
        raise ValueError("cell bbox or period is required for side classification")
    xmin, xmax, ymin, ymax, zmin, zmax = list(bbox)

    def _on_edge(coord, edge, tol):
        if edge is None:
            return True  # no bbox filtering
        return abs(coord - edge) <= tol

    x_src, x_dst, y_src, y_dst = [], [], [], []
    bottom, top = [], []
    for b in boundaries:
        if "normal" not in b or "center" not in b:
            continue
        if b.get("interior"):
            continue
        nx, ny, nz = b["normal"]
        cx, cy, cz = b["center"]
        if nz < -0.5 and _on_edge(cz, zmin, tol):  # bottom face (normal -z, z=zmin)
            bottom.append(b["boundary_number"])
        elif nz > 0.5 and _on_edge(cz, zmax, tol) and zmax is not None:  # top, z=zmax
            top.append(b["boundary_number"])
        elif nz > 0.5 and zmax is None:  # top without bbox (legacy)
            top.append(b["boundary_number"])
        elif nx < -0.5 and _on_edge(cx, xmin, tol):  # x=xmin cell side
            x_src.append(b["boundary_number"])
        elif nx > 0.5 and _on_edge(cx, xmax, tol):  # x=xmax cell side
            x_dst.append(b["boundary_number"])
        elif ny < -0.5 and _on_edge(cy, ymin, tol):  # y=ymin cell side
            y_src.append(b["boundary_number"])
        elif ny > 0.5 and _on_edge(cy, ymax, tol):  # y=ymax cell side
            y_dst.append(b["boundary_number"])
    return {
        "x_src": x_src,
        "x_dst": x_dst,
        "y_src": y_src,
        "y_dst": y_dst,
        "bottom": bottom,
        "top": top,
    }


def _list_pair_metadata(comp) -> list[dict[str, str]]:
    """Return pair tags and labels with clientapi strings normalized for JSON."""
    try:
        raw_tags = list(comp.pair().tags())
    except Exception:
        return []

    pairs = []
    for raw_tag in raw_tags:
        tag = str(raw_tag)
        try:
            pair = comp.pair().get(raw_tag)
            pairs.append({"tag": tag, "label": str(pair.label())})
        except Exception:
            pairs.append({"tag": tag})
    return pairs


def _find_air_block_tag(geom) -> Optional[str]:
    """Find the unique largest tall Block-like feature and return its Python tag."""
    candidates: list[tuple[float, str]] = []
    for raw_tag in list(geom.feature().tags()):
        tag = str(raw_tag)
        if tag == "fin":
            continue
        feature = geom.feature().get(raw_tag)
        try:
            size_text = str(feature.getString("size"))
            size = [float(value) for value in size_text.replace(",", " ").split()]
            if len(size) == 3 and all(value > 0 for value in size) and size[2] > 1e-7:
                candidates.append((size[2], tag))
        except Exception:  # noqa: S110 - unsupported geometry features are not candidates
            pass
    if not candidates:
        return None
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]


def _require_air_block_contains_patch(
    geom,
    air_block_tag: str,
    patch_pos: Sequence[float],
    patch_size: Sequence[float],
) -> None:
    """Fail loudly when the chosen air block cannot contain the patch footprint."""
    feature = geom.feature().get(air_block_tag)
    try:
        block_pos = [float(v) for v in str(feature.getString("pos")).replace(",", " ").split()]
        block_size = [float(v) for v in str(feature.getString("size")).replace(",", " ").split()]
    except Exception as exc:
        raise ValueError(f"air block {air_block_tag!r} position/size is unreadable") from exc
    if len(block_pos) != 3 or len(block_size) != 3:
        raise ValueError(f"air block {air_block_tag!r} is not a three-dimensional block")
    tolerance = 1e-9
    for axis in range(3):
        low = float(patch_pos[axis])
        high = low + float(patch_size[axis])
        if (
            low < block_pos[axis] - tolerance
            or high > block_pos[axis] + block_size[axis] + tolerance
        ):
            raise ValueError(
                f"auto-detected air block {air_block_tag!r} does not contain the patch "
                "footprint; specify air_block_tag explicitly"
            )


def _set_copy_face_selections(feature, source: Sequence[int], destination: Sequence[int]) -> None:
    """Set an explicit directed CopyFace pair or fail before mesh execution."""
    failures = []
    for source_name, destination_name in (("source", "destination"), ("src", "dst")):
        try:
            feature.selection(source_name).set(source)
            feature.selection(destination_name).set(destination)
            return
        except Exception as exc:
            failures.append(type(exc).__name__)
    raise RuntimeError(
        "CopyFace does not expose a supported directed source/destination selection contract "
        f"({', '.join(failures)})"
    )


def _normalize_spectral_rows(results, expression_count: int) -> list[list[object]]:
    """Convert supported MPh spectral evaluation layouts into wavelength rows."""
    import numpy as np

    if isinstance(expression_count, bool) or not isinstance(expression_count, int):
        raise ValueError("expression_count must be a positive integer")
    if expression_count < 1:
        raise ValueError("expression_count must be a positive integer")
    array = np.asarray(results)
    if array.ndim > 2:
        array = np.squeeze(array)
    if array.ndim == 1:
        if expression_count == 1:
            return [[value] for value in array.tolist()]
        if array.size != expression_count:
            raise ValueError("spectral evaluation shape does not match the requested expressions")
        return [array.tolist()]
    if array.ndim != 2:
        raise ValueError("spectral evaluation must be a two-dimensional numeric matrix")
    if array.shape[0] == expression_count and array.shape[1] == expression_count:
        raise ValueError(
            "spectral evaluation matrix is square and its layout is ambiguous; "
            "the layout must be declared explicitly"
        )
    if array.shape[0] == expression_count:
        return array.T.tolist()
    if array.shape[1] == expression_count:
        return array.tolist()
    raise ValueError("spectral evaluation matrix does not match the requested expressions")


def _require_mim_selections(
    patch_footprint: Sequence[int],
    bottom: Sequence[int],
    top: Sequence[int],
    side_pairs: dict,
) -> None:
    """Require every selection needed by the patch boundary and mesh contract."""
    required = {
        "patch_footprint": patch_footprint,
        "bottom": bottom,
        "top": top,
        "x_src": side_pairs.get("x_src", []),
        "x_dst": side_pairs.get("x_dst", []),
        "y_src": side_pairs.get("y_src", []),
        "y_dst": side_pairs.get("y_dst", []),
    }
    missing = [name for name, values in required.items() if not values]
    if missing:
        raise ValueError("MIM patch build is missing required selections: " + ", ".join(missing))


def _restore_selection(selection, entities: Sequence[int]) -> None:
    """Restore one captured COMSOL selection through its original selection node."""
    selection.set(list(entities))


def _identify_patch_topology(
    boundaries: Sequence[dict],
    patch_size: Sequence[float],
    patch_pos: Sequence[float],
    preferred_footprint: Optional[Sequence[int]] = None,
) -> tuple[int, list[int]]:
    """Identify the patch domain and its footprint from box geometry and adjacency."""
    if len(patch_size) != 3 or len(patch_pos) != 3:
        raise ValueError("patch_size and patch_pos must contain exactly three values")
    size = [float(value) for value in patch_size]
    position = [float(value) for value in patch_pos]
    if any(value <= 0 for value in size):
        raise ValueError("patch_size values must be positive")
    low = position
    high = [position[index] + size[index] for index in range(3)]
    tolerance = max(1e-12, max(size) * 1e-6)

    def within(value: float, minimum: float, maximum: float) -> bool:
        return minimum - tolerance <= value <= maximum + tolerance

    patch_faces = []
    for boundary in boundaries:
        center = boundary.get("center")
        normal = boundary.get("normal")
        if not isinstance(center, list) or not isinstance(normal, list):
            continue
        if len(center) != 3 or len(normal) != 3:
            continue
        center_values = [float(value) for value in center]
        normal_values = [float(value) for value in normal]
        on_box = any(
            abs(abs(normal_values[axis]) - 1.0) <= 0.5
            and (
                abs(center_values[axis] - low[axis]) <= tolerance
                or abs(center_values[axis] - high[axis]) <= tolerance
            )
            and all(
                within(center_values[other], low[other], high[other])
                for other in range(3)
                if other != axis
            )
            for axis in range(3)
        )
        if on_box:
            patch_faces.append(boundary)
    if len(patch_faces) < 2:
        raise ValueError("patch topology could not be bound to the requested box")

    if preferred_footprint is not None:
        footprint = sorted({int(value) for value in preferred_footprint})
        by_number = {int(item["boundary_number"]): item for item in boundaries}
        if len(footprint) != 1 or any(value not in by_number for value in footprint):
            raise ValueError("trusted patch footprint selection is missing or ambiguous")
        footprint_face = by_number[footprint[0]]
        if footprint_face.get("interior") is not True:
            raise ValueError("trusted patch footprint must remain an interior boundary")

        def adjacent_domains(items: Sequence[dict]) -> set[int]:
            return {
                int(item[name])
                for item in items
                for name in ("up_domain", "down_domain")
                if isinstance(item.get(name), int)
                and not isinstance(item.get(name), bool)
                and int(item[name]) > 0
            }

        footprint_domains = adjacent_domains([footprint_face])
        top_faces = [
            item
            for item in patch_faces
            if abs(float(item["normal"][2])) > 0.5
            and abs(float(item["center"][2]) - high[2]) <= tolerance
        ]
        candidates = footprint_domains & adjacent_domains(top_faces)
        if len(candidates) != 1:
            raise ValueError("trusted footprint does not identify one patch domain")
        return next(iter(candidates)), footprint

    counts: dict[int, int] = {}
    bottom_domains: set[int] = set()
    top_domains: set[int] = set()
    for boundary in patch_faces:
        center = boundary["center"]
        normal = boundary["normal"]
        adjacent = set()
        for name in ("up_domain", "down_domain"):
            domain = boundary.get(name)
            if isinstance(domain, int) and not isinstance(domain, bool) and domain > 0:
                counts[domain] = counts.get(domain, 0) + 1
                adjacent.add(domain)
        if abs(float(normal[2])) > 0.5:
            if abs(float(center[2]) - low[2]) <= tolerance:
                bottom_domains.update(adjacent)
            if abs(float(center[2]) - high[2]) <= tolerance:
                top_domains.update(adjacent)
    if not counts:
        raise ValueError("patch topology has no readable adjacent domains")
    spanning_domains = bottom_domains & top_domains
    if len(spanning_domains) > 1:
        raise ValueError("patch topology has an ambiguous domain identity")
    if len(spanning_domains) == 1:
        patch_domain = next(iter(spanning_domains))
    else:
        maximum = max(counts.values())
        candidates = sorted(domain for domain, count in counts.items() if count == maximum)
        if len(candidates) != 1:
            raise ValueError("patch topology has an ambiguous domain identity")
        patch_domain = candidates[0]

    footprint = []
    for boundary in patch_faces:
        center = boundary["center"]
        normal = boundary["normal"]
        adjacent = {boundary.get("up_domain"), boundary.get("down_domain")}
        if (
            patch_domain in adjacent
            and boundary.get("interior") is True
            and abs(float(center[2]) - low[2]) <= tolerance
            and abs(float(normal[2])) > 0.5
        ):
            footprint.append(int(boundary["boundary_number"]))
    footprint = sorted(set(footprint))
    if len(footprint) != 1:
        raise ValueError("patch footprint interface is missing or ambiguous")
    return patch_domain, footprint


def _build_periodic_mesh(comp, side_pairs: dict):
    """Build a new periodic mesh without deleting pre-existing mesh sequences."""
    mesh_list = comp.mesh()
    preserved_tags = [str(value) for value in list(mesh_list.tags())]
    existing = set(preserved_tags)
    index = 1
    mesh_tag = f"mesh{index}"
    while mesh_tag in existing:
        index += 1
        mesh_tag = f"mesh{index}"

    mesh = mesh_list.create(mesh_tag)
    try:
        x_src = side_pairs["x_src"]
        y_src = side_pairs["y_src"]
        x_dst = side_pairs["x_dst"]
        y_dst = side_pairs["y_dst"]

        ftx = mesh.feature().create("ftri_x", "FreeTri")
        ftx.selection().set(x_src)
        fty = mesh.feature().create("ftri_y", "FreeTri")
        fty.selection().set(y_src)

        cpx = mesh.feature().create("cp_x", "CopyFace")
        _set_copy_face_selections(cpx, x_src, x_dst)

        cpy = mesh.feature().create("cp_y", "CopyFace")
        _set_copy_face_selections(cpy, y_src, y_dst)

        mesh.feature().create("ft1", "FreeTet")
        mesh.run()
    except Exception:
        try:
            mesh_list.remove(mesh_tag)
        except Exception as rollback_exc:
            raise RuntimeError(
                "MIM mesh setup failed and new-mesh rollback was incomplete"
            ) from rollback_exc
        raise
    return mesh, mesh_tag, preserved_tags


# ---------------------------------------------------------------------------
# tool registration
# ---------------------------------------------------------------------------


def register_mim_patch_tools(mcp: MCPServer) -> None:
    """Register MIM patch metasurface tools."""

    @mcp.tool()
    def geometry_probe_domains(
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Probe geometry boundaries with up/down domain information.

        Enhanced version of geometry_get_boundaries that also returns:
        - up_domain / down_domain for each boundary (identifies interior vs exterior)
        - interior flag (True if both up and down domains are non-zero)
        - Pair info (identity/assembly pairs) if any
        - Auto-identified periodic side face pairs (x_src/dst, y_src/dst, bottom, top)

        Use this to identify which boundaries are interior interfaces, which are
        periodic side pairs, and which is the bottom/top before setting BCs.

        Args:
            geometry_name: Geometry sequence name (default: first)
            component_name: Component name (default: 'comp1')
            model_name: Model name (default: current)

        Returns:
            Boundary info with up/down domains, pair info, side pair identification.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            jm = model.java
            geom, error = _get_geom_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}

            geom.run()
            boundaries, n_dom, n_bnd, sdim = _probe_boundaries(geom)

            # bbox
            try:
                bbox = [float(x) for x in geom.getBoundingBox()]
            except Exception:
                bbox = None

            # pairs
            comp = jm.component(component_name)
            pairs_info = _list_pair_metadata(comp)

            # identify side pairs (filter by coordinate so interior faces with
            # ±x/±y normals — e.g. patch side faces at x=L/2 — are NOT misread as
            # the Floquet periodic cell sides).
            bbox6 = tuple(bbox) if bbox is not None else None
            side_pairs = _identify_side_pairs(boundaries, bbox=bbox6)

            # identify interior boundaries (up!=0 and down!=0 → FormUnion interior)
            interior_bnds = [b["boundary_number"] for b in boundaries if b.get("interior")]

            result = {
                "success": True,
                "geometry": geometry_name or "first",
                "space_dimension": sdim,
                "total_domains": n_dom,
                "total_boundaries": n_bnd,
                "boundaries": boundaries,
                "interior_boundaries": interior_bnds,
                "pairs": pairs_info,
                "side_pairs": side_pairs,
            }
            if bbox is not None:
                result["bounding_box"] = bbox
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to probe: {e}"}

    @mcp.tool()
    def mim_patch_build(
        patch_size: Sequence[float],
        patch_pos: Sequence[float],
        air_block_tag: Optional[str] = None,
        patch_tag: str = "b_pat",
        diff_tag: str = "dif1",
        layered_transition_tag: str = "ltr1",
        layered_impedance_tag: str = "lib1",
        air_material_tag: str = "mat_air",
        ewfd_tag: str = "ewfd",
        geometry_name: Optional[str] = None,
        component_name: str = "comp1",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Build a MIM patch metasurface model from a 2-domain baseline.

        Adds a patch Block + Difference (keepsubtract=True) to the geometry,
        rebuilds with FormUnion (automatic continuity), then:
        - Updates LayeredTransition BC to the patch-footprint interior boundary
        - Updates LayeredImpedance BC to the bottom face
        - Assigns air material to the new patch domain
        - Creates a periodic-compatible mesh (FreeTri + CopyFace + FreeTet)

        The patch domain is AIR (not metal). The Au thin film is modeled by the
        LayeredTransition BC on the Al2O3/patch interior boundary (patch footprint).

        Args:
            patch_size: [width, depth, height] of the Au patch in meters.
            patch_pos: [x, y, z] base position of the patch in meters.
            air_block_tag: Tag of the existing air block to subtract from
                (auto-detected if None — picks the block with largest z-size).
            patch_tag: Tag for the new patch block feature (default 'b_pat').
            diff_tag: Tag for the Difference operation (default 'dif1').
            layered_transition_tag: Tag of existing LayeredTransition BC (default 'ltr1').
            layered_impedance_tag: Tag of existing LayeredImpedance BC (default 'lib1').
            air_material_tag: Tag of existing air material (default 'mat_air').
            ewfd_tag: Tag of the ewfd physics interface (default 'ewfd').
            geometry_name: Geometry sequence name (default: first).
            component_name: Component name (default: 'comp1').
            model_name: Model name (default: current).

        Returns:
            New domain/boundary counts, key boundary numbers, mesh stats.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        geom = None
        created_features: list[str] = []
        rollback_actions = []
        try:
            jm = model.java
            comp = jm.component(component_name)
            if comp is None:
                return {"success": False, "error": f"Component '{component_name}' not found."}

            geom, error = _get_geom_node(model, geometry_name, component_name)
            if error:
                return {"success": False, "error": error}

            report = {"success": True, "steps": []}

            # ---- Step 1: identify air block (auto-detect if not given) ----
            auto_detected_air = not air_block_tag
            if auto_detected_air:
                air_block_tag = _find_air_block_tag(geom)
            if not air_block_tag:
                return {
                    "success": False,
                    "error": "Could not auto-detect air block tag. Please specify air_block_tag.",
                }
            if auto_detected_air:
                _require_air_block_contains_patch(geom, air_block_tag, patch_pos, patch_size)
            report["air_block_tag"] = air_block_tag

            ewfd = comp.physics().get(ewfd_tag)
            if ewfd is None:
                raise ValueError(f"EWFD physics not found: {ewfd_tag}")
            for tag in (layered_transition_tag, layered_impedance_tag, "ps1"):
                feature = ewfd.feature().get(tag)
                if feature is None:
                    raise ValueError(f"Required physics feature not found: {tag}")
                selection = (
                    feature.selection("excitedPortSelection")
                    if tag == "ps1"
                    else feature.selection()
                )
                rollback_actions.append((selection, "selection", list(selection.entities())))
            air_mat = comp.material().get(air_material_tag)
            if air_mat is None:
                raise ValueError(f"Air material not found: {air_material_tag}")
            rollback_actions.append(
                (air_mat.selection(), "selection", list(air_mat.selection().entities()))
            )

            # ---- Step 2: add patch block ----
            b_pat = geom.feature().create(patch_tag, "Block")
            created_features.append(str(patch_tag))
            b_pat.set("size", [str(s) for s in patch_size])
            b_pat.set("pos", [str(p) for p in patch_pos])
            report["steps"].append(
                f"Added patch block {patch_tag}: size={list(patch_size)}, pos={list(patch_pos)}"
            )

            # ---- Step 3: add Difference (keepsubtract=True) ----
            dif = geom.feature().create(diff_tag, "Difference")
            created_features.append(str(diff_tag))
            dif.selection("input").set([air_block_tag])
            dif.selection("input2").set([patch_tag])
            try:
                dif.set("keepsubtract", True)
            except Exception:
                dif.set("keep", True)
            report["steps"].append(
                f"Added Difference {diff_tag}: input={air_block_tag}, "
                f"subtract={patch_tag}, keep=True"
            )

            # ---- Step 4: build geometry (FormUnion = default) ----
            try:
                fin = geom.feature().get("fin")
                action = fin.getString("action")
                if action and action != "union":
                    rollback_actions.append((fin, "action", str(action)))
                    fin.set("action", "union")
            except Exception:  # noqa: S110 - optional legacy fin action
                pass
            geom.run()

            boundaries, n_dom, n_bnd, sdim = _probe_boundaries(geom)
            report["n_domains"] = n_dom
            report["n_boundaries"] = n_bnd

            # ---- Step 5: identify key boundaries ----
            patch_dom, patch_footprint = _identify_patch_topology(
                boundaries,
                patch_size,
                patch_pos,
            )

            # Filter side/top/bottom by BOTH normal AND coordinate. Without the
            # coordinate filter, the patch side/top faces (interior interfaces with
            # ±x/±y/+z normals) would be misclassified as the cell exterior and
            # break Floquet CopyFace mesh compatibility.
            try:
                bbox_vals = [float(x) for x in geom.getBoundingBox()]
                bbox6 = (
                    bbox_vals[0],
                    bbox_vals[1],
                    bbox_vals[2],
                    bbox_vals[3],
                    bbox_vals[4],
                    bbox_vals[5],
                )
            except Exception:
                bbox6 = None
            side_pairs = _identify_side_pairs(boundaries, bbox=bbox6)
            bottom = side_pairs.get("bottom", [])
            top = side_pairs.get("top", [])

            report["patch_footprint_interface"] = patch_footprint
            report["bottom"] = bottom
            report["top"] = top
            report["side_pairs"] = side_pairs

            _require_mim_selections(
                patch_footprint,
                bottom,
                top,
                side_pairs,
            )

            # ---- Step 6: update BCs ----
            phys = comp.physics()
            ewfd = phys.get(ewfd_tag)
            if ewfd is None:
                raise ValueError(f"EWFD physics not found: {ewfd_tag}")

            ltr = ewfd.feature().get(layered_transition_tag)
            if ltr is None:
                raise ValueError(f"LayeredTransition feature not found: {layered_transition_tag}")
            ltr_selection = ltr.selection()
            ltr_selection.set(patch_footprint)
            report["steps"].append(
                f"LayeredTransition {layered_transition_tag} → boundaries {patch_footprint}"
            )

            lib = ewfd.feature().get(layered_impedance_tag)
            if lib is None:
                raise ValueError(f"LayeredImpedance feature not found: {layered_impedance_tag}")
            lib_selection = lib.selection()
            lib_selection.set(bottom)
            report["steps"].append(
                f"LayeredImpedance {layered_impedance_tag} → boundaries {bottom}"
            )

            ps = ewfd.feature().get("ps1")
            if ps is None:
                raise ValueError("PeriodicStructure feature not found: ps1")
            ps_selection = ps.selection("excitedPortSelection")
            ps_selection.set(top)
            report["steps"].append(f"PeriodicStructure excitedPort → boundaries {top}")

            # ---- Step 7: assign air material to patch domain ----
            mat_list = comp.material()
            air_mat = mat_list.get(air_material_tag)
            if air_mat is None:
                raise ValueError(f"Air material not found: {air_material_tag}")
            cur = list(air_mat.selection().entities())
            if patch_dom not in cur:
                air_mat.selection().set(cur + [patch_dom])
            report["steps"].append(f"Air material {air_material_tag} → domains {cur + [patch_dom]}")

            # ---- Step 8: create periodic-compatible mesh ----
            mesh, mesh_tag, preserved_mesh_tags = _build_periodic_mesh(
                comp,
                side_pairs,
            )
            report["mesh_tag"] = mesh_tag
            report["preserved_mesh_tags"] = preserved_mesh_tags
            try:
                report["mesh_elements"] = int(mesh.getNumElem())
                report["mesh_vertices"] = int(mesh.getNumVertex())
            except Exception:  # noqa: S110 - mesh counts are optional diagnostics
                pass
            report["steps"].append(
                f"Mesh: FreeTri+CopyFace+FreeTet → {report.get('mesh_elements', '?')} elements"
            )

            return report

        except Exception as e:
            rollback_errors = []
            for tag in reversed(created_features):
                try:
                    geom.feature().remove(tag)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{tag}: {type(rollback_exc).__name__}")
            for node, kind, previous in reversed(rollback_actions):
                if kind == "selection":
                    continue
                try:
                    node.set(kind, previous)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{kind}: {type(rollback_exc).__name__}")
            if created_features and geom is not None and not rollback_errors:
                try:
                    geom.run()
                except Exception:
                    rollback_errors.append("geometry rerun after feature removal failed")
            for node, kind, previous in reversed(rollback_actions):
                if kind != "selection":
                    continue
                try:
                    _restore_selection(node, previous)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{kind}: {type(rollback_exc).__name__}")
            result = {"success": False, "error": f"mim_patch_build failed: {e}"}
            if created_features:
                result["rolled_back"] = not rollback_errors
                if rollback_errors:
                    result["rollback_errors"] = rollback_errors[:8]
            return result

    @mcp.tool()
    def mim_evaluate_spectral(
        expressions: Optional[Sequence[str]] = None,
        wl_parameter: str = "wl",
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Evaluate spectral quantities (Rtotal, Ttotal, Atotal) vs wavelength.

        Convenience wrapper around results_evaluate that returns a clean
        wavelength-indexed table of reflection / transmission / absorption.

        Args:
            expressions: List of expressions to evaluate (default:
                ['ewfd.Rtotal', 'ewfd.Ttotal', 'ewfd.Atotal', 'wl']).
            wl_parameter: Name of the wavelength parameter (default 'wl').
            model_name: Model name (default: current).

        Returns:
            List of {wl, R, T, A} dicts (wl in µm).
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        if expressions is None:
            expressions = ["ewfd.Rtotal", "ewfd.Ttotal", "ewfd.Atotal", wl_parameter]

        try:
            # Ensure wl is included
            expr_list = list(expressions)
            if wl_parameter not in expr_list:
                expr_list.append(wl_parameter)

            results = model.evaluate(expr_list)

            # Build clean output
            spectral = []
            for row in _normalize_spectral_rows(results, len(expr_list)):
                vals = [float(v) for v in row]
                entry = {}
                for i, expr in enumerate(expr_list):
                    if expr == wl_parameter:
                        entry["wl_um"] = vals[i] * 1e6
                    else:
                        entry[expr] = vals[i]
                # Use exact absorptivity when available; otherwise preserve
                # transmission in the energy closure before applying the
                # opaque fallback.
                if "ewfd.Rtotal" in entry:
                    if "ewfd.Atotal" in entry:
                        entry["emissivity"] = entry["ewfd.Atotal"]
                        entry["emissivity_basis"] = "evaluated_absorptivity"
                    elif "ewfd.Ttotal" in entry:
                        entry["emissivity"] = 1.0 - entry["ewfd.Rtotal"] - entry["ewfd.Ttotal"]
                        entry["emissivity_basis"] = "one_minus_reflectance_transmittance"
                    else:
                        entry["emissivity"] = 1.0 - entry["ewfd.Rtotal"]
                        entry["emissivity_basis"] = "opaque_one_minus_reflectance"
                spectral.append(entry)

            return {
                "success": True,
                "n_wavelengths": len(spectral),
                "expressions": expr_list,
                "spectral_data": spectral,
            }
        except Exception as e:
            return {"success": False, "error": f"Evaluation failed: {e}"}
