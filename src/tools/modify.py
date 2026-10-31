"""Write-path tools for COMSOL MCP Server: nested physics features, solver
config, and postprocessing (results) nodes.

Read-side completeness is provided by audit.py (model_audit / model_diff /
physics_feature_info). This module closes the write-side gaps:

- physics_feature_*: create/set/toggle/delete features at ANY nesting depth
  (e.g. an ElectrodeReaction under an ElectrodeSurface), with selection and
  property application. The #1 gap for electrochemistry models.
- solver_feature_*: list/set/toggle solver-tree features (time stepping,
  tolerances, continuation chains) — needed by e.g. the 0-100s continuation
  workflow.
- results_node_*: create/set/delete/evaluate numerical (postprocessing) nodes
  such as Global evaluations or boundary averages.

All tools are tag-based and return explicit errors; nothing silent.
"""

import json
from pathlib import Path
from typing import Optional, Union

from mcp.server.fastmcp import FastMCP

from .session import session_manager
from .audit import _jtags, _dump_node, _selection_info, _walk_features, _jprops


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_model(model_name: Optional[str]):
    model = session_manager.get_model(model_name)
    if model is None:
        return None, {"success": False,
                      "error": f"Model not found: {model_name or 'no current model'}"}
    return model, None


def _find_physics(model, physics_name: str):
    """Locate a physics interface by tag or (localized) label."""
    m = model.java
    available = []
    for ct in _jtags(m.component().tags()):
        comp = m.component(ct)
        for pt in _jtags(comp.physics().tags()):
            p = comp.physics(pt)
            label = str(p.label())
            available.append(label)
            if pt == physics_name or label == physics_name:
                return ct, pt, p, None
    return None, None, None, {"success": False,
                              "error": f"Physics interface not found: {physics_name}. "
                                       f"Available: {available}"}


def _resolve_feature_path(container, path: str):
    """Walk a feature container down a 'a/b/c' tag path. Returns (node, parent_list, tag)."""
    parts = [p.strip() for p in path.split('/') if p.strip()]
    if not parts:
        return None, None, None, {"success": False, "error": "Empty feature path"}
    node = None
    listing = container.feature()
    for i, tag in enumerate(parts):
        try:
            node = listing.get(tag)
        except Exception:
            try:
                siblings = _jtags(listing.tags())
            except Exception:
                siblings = []
            return None, None, None, {
                "success": False,
                "error": f"Feature '{tag}' not found at path '{'/'.join(parts[:i+1])}'. "
                         f"Available here: {siblings}"}
        listing = node.feature()
    return node, container, parts[-1], None


def _apply_properties(java, properties: dict) -> dict:
    """Set properties on a java node; 'label' is special-cased. Returns report."""
    from mph.node import cast
    applied, failed = [], []
    for name, value in properties.items():
        try:
            if name == 'label':
                java.label(str(value))
            else:
                java.set(name, cast(value))
            applied.append(name)
        except Exception as e:
            failed.append({'property': name, 'error': str(e)})
    return {'applied': applied, 'failed': failed}


def _apply_selection(java, selection) -> dict:
    """Apply selection: list of ints, 'all', or named-selection tag string."""
    try:
        sel = java.selection()
        if selection == 'all':
            sel.all()
            return {'selection': 'all'}
        if isinstance(selection, str):
            sel.named(selection)
            return {'selection': f'named:{selection}'}
        sel.set([int(x) for x in selection])
        return {'selection': [int(x) for x in selection]}
    except Exception as e:
        return {'selection_error': str(e)}


# ---------------------------------------------------------------------------
# Physics feature tools
# ---------------------------------------------------------------------------

def physics_feature_info_core(model, physics_name: str, feature_path: str) -> dict:
    _, pt, p, err = _find_physics(model, physics_name)
    if err:
        return err
    node, _, _, err = _resolve_feature_path(p, feature_path)
    if err:
        return err
    d = _dump_node(node, 'feature')
    subs = _walk_features(node, [])
    if subs:
        d['features'] = subs
    d['path'] = f'{physics_name}/{feature_path}'
    return {'success': True, 'feature': d}


def physics_feature_create_core(
    model, physics_name: str, feature_type: str,
    tag: Optional[str] = None, parent: Optional[str] = None,
    dim: Optional[int] = None, selection: Optional[Union[list, str]] = None,
    properties: Optional[dict] = None,
) -> dict:
    ct, pt, p, err = _find_physics(model, physics_name)
    if err:
        return err
    if parent:
        holder, _, _, err = _resolve_feature_path(p, parent)
        if err:
            return err
        listing = holder.feature()
    else:
        holder = p
        listing = p.feature()
    if tag is None:
        try:
            tag = str(listing.uniqueTag(feature_type[:2].lower()))
        except Exception:
            existing = _jtags(listing.tags())
            n = 1
            tag = f'{feature_type.lower()}{n}'
            while tag in existing:
                n += 1
                tag = f'{feature_type.lower()}{n}'
    try:
        if dim is not None:
            listing.create(tag, feature_type, int(dim))
        else:
            listing.create(tag, feature_type)
    except Exception as e:
        return {'success': False,
                'error': f'Failed to create {feature_type} as {tag}: {e}. '
                         f'Check the feature type name (see .java export of a GUI-made '
                         f'example) and whether dim is required (boundary features need it).'}
    report = {'success': True, 'tag': tag, 'type': feature_type,
              'path': f'{physics_name}/{parent + "/" if parent else ""}{tag}'}
    try:
        node = listing.get(tag)
        if selection is not None:
            report.update(_apply_selection(node, selection))
        if properties:
            report['set'] = _apply_properties(node, properties)
        d = _dump_node(node, 'feature', with_selection=True)
        if properties:
            props, failed = _jprops(node)
            d['props'] = props
        report['feature'] = d
    except Exception as e:
        report['warning'] = f'Created, but post-read failed: {e}'
    return report


def physics_feature_set_core(model, physics_name: str, feature_path: str,
                             properties: dict) -> dict:
    _, pt, p, err = _find_physics(model, physics_name)
    if err:
        return err
    node, _, _, err = _resolve_feature_path(p, feature_path)
    if err:
        return err
    report = _apply_properties(node, properties)
    report['success'] = not report['failed']
    report['path'] = f'{physics_name}/{feature_path}'
    # read back the touched properties for verification
    readback = {}
    for name in report['applied']:
        if name == 'label':
            try:
                readback['label'] = str(node.label())
            except Exception:
                pass
        else:
            try:
                from mph.node import get as mph_get
                readback[name] = mph_get(node, name)
            except Exception:
                pass
    report['readback'] = readback
    return report


def physics_feature_toggle_core(model, physics_name: str, feature_path: str,
                                enabled: bool) -> dict:
    _, pt, p, err = _find_physics(model, physics_name)
    if err:
        return err
    node, _, _, err = _resolve_feature_path(p, feature_path)
    if err:
        return err
    try:
        node.active(bool(enabled))
        return {'success': True, 'path': f'{physics_name}/{feature_path}',
                'active': bool(node.isActive())}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def physics_feature_delete_core(model, physics_name: str, feature_path: str) -> dict:
    _, pt, p, err = _find_physics(model, physics_name)
    if err:
        return err
    parts = [x.strip() for x in feature_path.split('/') if x.strip()]
    if not parts:
        return {'success': False, 'error': 'Empty feature path'}
    if len(parts) == 1:
        listing = p.feature()
    else:
        holder, _, _, err = _resolve_feature_path(p, '/'.join(parts[:-1]))
        if err:
            return err
        listing = holder.feature()
    try:
        listing.remove(parts[-1])
        return {'success': True, 'removed': f'{physics_name}/{feature_path}'}
    except Exception as e:
        return {'success': False,
                'error': f'Failed to remove: {e}. Available: {_jtags(listing.tags())}'}


# ---------------------------------------------------------------------------
# Solver feature tools
# ---------------------------------------------------------------------------

def solver_feature_list_core(model, solution_tag: str) -> dict:
    m = model.java
    try:
        tags = _jtags(m.sol().tags())
    except Exception as e:
        return {'success': False, 'error': f'No solver configs: {e}'}
    if solution_tag not in tags:
        return {'success': False,
                'error': f'Solution not found: {solution_tag}. Available: {tags}'}
    sol = m.sol(solution_tag)
    d = _dump_node(sol, 'solution', with_selection=False)
    errors = []
    d['features'] = _walk_features(sol, errors)
    return {'success': True, 'solution': d, 'errors': errors or None}


def _resolve_solution(model, solution_tag: str):
    m = model.java
    tags = _jtags(m.sol().tags())
    if solution_tag not in tags:
        return None, {'success': False,
                      'error': f'Solution not found: {solution_tag}. Available: {tags}'}
    return m.sol(solution_tag), None


def solver_feature_set_core(model, solution_tag: str, feature_path: str,
                            properties: dict) -> dict:
    sol, err = _resolve_solution(model, solution_tag)
    if err:
        return err
    node, _, _, err = _resolve_feature_path(sol, feature_path)
    if err:
        return err
    report = _apply_properties(node, properties)
    report['success'] = not report['failed']
    report['path'] = f'{solution_tag}/{feature_path}'
    readback = {}
    from mph.node import get as mph_get
    for name in report['applied']:
        if name == 'label':
            continue
        try:
            readback[name] = mph_get(node, name)
        except Exception:
            pass
    report['readback'] = readback
    return report


def solver_feature_toggle_core(model, solution_tag: str, feature_path: str,
                               enabled: bool) -> dict:
    sol, err = _resolve_solution(model, solution_tag)
    if err:
        return err
    node, _, _, err = _resolve_feature_path(sol, feature_path)
    if err:
        return err
    try:
        node.active(bool(enabled))
        return {'success': True, 'path': f'{solution_tag}/{feature_path}',
                'active': bool(node.isActive())}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ---------------------------------------------------------------------------
# Results node tools
# ---------------------------------------------------------------------------

_RESULT_GROUPS = {
    'numerical': lambda r: r.numerical(),
    'dataset': lambda r: r.dataset(),
    'export': lambda r: r.export(),
    'table': lambda r: r.table(),
}


def _resolve_result_group(model, group: str):
    if group not in _RESULT_GROUPS:
        return None, {'success': False,
                      'error': f"group must be one of {list(_RESULT_GROUPS)}, got '{group}'"}
    return _RESULT_GROUPS[group](model.java.result()), None


def results_node_create_core(model, group: str, node_type: str,
                             tag: Optional[str] = None,
                             properties: Optional[dict] = None,
                             selection: Optional[Union[list, str]] = None) -> dict:
    listing, err = _resolve_result_group(model, group)
    if err:
        return err
    if tag is None:
        try:
            tag = str(listing.uniqueTag(node_type[:3].lower()))
        except Exception:
            tag = f'{node_type.lower()}1'
    try:
        listing.create(tag, node_type)
    except Exception as e:
        return {'success': False,
                'error': f'Failed to create {group}/{tag} of type {node_type}: {e}'}
    report = {'success': True, 'group': group, 'tag': tag, 'type': node_type}
    try:
        node = listing.get(tag)
        if selection is not None:
            report.update(_apply_selection(node, selection))
        if properties:
            report['set'] = _apply_properties(node, properties)
        report['node'] = _dump_node(node, group, with_selection=True)
    except Exception as e:
        report['warning'] = f'Created, but post-read failed: {e}'
    return report


def results_node_set_core(model, group: str, tag: str, properties: dict) -> dict:
    listing, err = _resolve_result_group(model, group)
    if err:
        return err
    try:
        node = listing.get(tag)
    except Exception:
        return {'success': False,
                'error': f'Node not found: {group}/{tag}. '
                         f'Available: {_jtags(listing.tags())}'}
    report = _apply_properties(node, properties)
    report['success'] = not report['failed']
    report['path'] = f'{group}/{tag}'
    return report


def results_node_delete_core(model, group: str, tag: str) -> dict:
    listing, err = _resolve_result_group(model, group)
    if err:
        return err
    try:
        listing.remove(tag)
        return {'success': True, 'removed': f'{group}/{tag}'}
    except Exception as e:
        return {'success': False,
                'error': f'Failed to remove: {e}. Available: {_jtags(listing.tags())}'}


def results_numerical_evaluate_core(model, numerical_tag: str,
                                    unit: Optional[str] = None) -> dict:
    """Evaluate a numerical node. Requires a computed solution for the referenced dataset."""
    try:
        node = model.java.result().numerical(numerical_tag)
    except Exception:
        return {'success': False,
                'error': f'Numerical node not found: {numerical_tag}. '
                         f'Available: {_jtags(model.java.result().numerical().tags())}'}
    try:
        if unit:
            node.set('unit', unit)
        real = node.getReal()
        values = [float(real[i]) for i in range(len(real))] if hasattr(real, '__len__') else [float(real)]
        result = {'success': True, 'tag': numerical_tag, 'values': values,
                  'unit': unit or None}
        if not values:
            result['note'] = ('Empty result — the referenced dataset likely has no '
                              'computed solution data. Solve a study first.')
        return result
    except Exception as e:
        return {'success': False,
                'error': f'Evaluation failed: {e}. If the model has no solution, '
                         f'solve a study first.'}


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------

def register_modify_tools(mcp: FastMCP) -> None:
    """Register write-path tools with the MCP server."""

    @mcp.tool()
    def physics_feature_info(
        physics_name: str,
        feature_path: str,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Read one physics feature at any nesting depth (e.g. 'es2' or 'es2/er1'),
        including all properties, selection entity numbers, active state and
        its own sub-features. Cheaper than a full model_audit when you only
        need one feature.

        Args:
            physics_name: Physics tag ('tcd') or label ('三次电流分布，Nernst-Planck')
            feature_path: Feature tag path, '/'-separated for nesting ('es2/er1')
            model_name: Model name (default: current model)

        Returns:
            Feature details (tag/label/type/active/selection/props/sub-features)
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return physics_feature_info_core(model, physics_name, feature_path)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def physics_feature_create(
        physics_name: str,
        feature_type: str,
        tag: Optional[str] = None,
        parent: Optional[str] = None,
        dim: Optional[int] = None,
        selection: Optional[list] = None,
        selection_all: bool = False,
        properties: Optional[dict] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a physics feature, optionally NESTED under a parent feature
        (e.g. an ElectrodeReaction under an ElectrodeSurface), with selection
        and initial properties applied. Use model_export_java on a GUI-built
        example to find the exact feature_type string and property names.

        Args:
            physics_name: Physics tag or label
            feature_type: Feature type, e.g. 'ElectrodeReaction', 'Wall', 'Inflow'
            tag: Optional tag for the new feature (auto-generated if omitted)
            parent: Optional parent feature path for nesting, e.g. 'es2'
            dim: Geometric entity level (e.g. 1=boundary in 2D, 2=domain).
                 Required for boundary-level features.
            selection: Entity numbers to select, e.g. [4, 7]
            selection_all: Select all entities (default False)
            properties: Optional {property: value} dict applied after creation
            model_name: Model name (default: current model)

        Returns:
            Created feature info including readback of applied properties
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            sel = 'all' if selection_all else selection
            return physics_feature_create_core(
                model, physics_name, feature_type, tag=tag, parent=parent,
                dim=dim, selection=sel, properties=properties)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def physics_feature_set(
        physics_name: str,
        feature_path: str,
        properties: dict,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Set properties on an existing physics feature at any nesting depth
        (e.g. 'es2/er1' sets the copper-deposition reaction kinetics).
        Values may be strings, numbers, or lists (for array properties).
        The special property 'label' renames the feature.
        Every applied value is read back and returned for verification.

        Args:
            physics_name: Physics tag or label
            feature_path: Feature tag path ('es2' or nested 'es2/er1')
            properties: {property: value} dict
            model_name: Model name (default: current model)

        Returns:
            applied/failed lists plus readback values for verification
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return physics_feature_set_core(model, physics_name, feature_path, properties)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def physics_feature_toggle(
        physics_name: str,
        feature_path: str,
        enabled: bool,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Enable or disable a physics feature (nested paths supported).
        Disabled features stay in the model but are excluded from solving —
        the classic source of 'why is this BC not active' confusion; use
        model_audit to spot ⛔ disabled features.

        Args:
            physics_name: Physics tag or label
            feature_path: Feature tag path ('es2' or 'es2/er1')
            enabled: True to enable, False to disable
            model_name: Model name (default: current model)

        Returns:
            New active state of the feature
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return physics_feature_toggle_core(model, physics_name, feature_path, enabled)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def physics_feature_delete(
        physics_name: str,
        feature_path: str,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Delete a physics feature (nested paths supported). Destructive to the
        in-memory model — save a version first (model_save_version) if unsure.

        Args:
            physics_name: Physics tag or label
            feature_path: Feature tag path to delete ('es2/er1' deletes only er1)
            model_name: Model name (default: current model)

        Returns:
            Confirmation of removal
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return physics_feature_delete_core(model, physics_name, feature_path)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def solver_feature_list(
        solution_tag: str,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        List the full solver configuration tree of a solution (time stepping,
        tolerance, linear solvers, continuation chains) with all properties.
        Read-only; the same data appears under 'solvers' in model_audit.

        Args:
            solution_tag: Solution/solver tag, e.g. 'sol3' (see model_audit)
            model_name: Model name (default: current model)

        Returns:
            Recursive solver feature tree
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return solver_feature_list_core(model, solution_tag)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def solver_feature_set(
        solution_tag: str,
        feature_path: str,
        properties: dict,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Set solver configuration properties on a solver-tree feature
        (e.g. sol3 feature 't1': time step list, max step, tolerances).
        Path may nest, e.g. 't1/direct1' for a linear solver. Applied values
        are read back and returned. Change solver settings with care; keep the
        model saved at a known-good version.

        Args:
            solution_tag: Solution tag, e.g. 'sol3'
            feature_path: Solver feature path ('t1' or nested 't1/direct1')
            properties: {property: value} dict
            model_name: Model name (default: current model)

        Returns:
            applied/failed lists plus readback values
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return solver_feature_set_core(model, solution_tag, feature_path, properties)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def solver_feature_toggle(
        solution_tag: str,
        feature_path: str,
        enabled: bool,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Enable/disable a solver-tree feature (e.g. turn off a dependent
        variable or an inner solver block).

        Args:
            solution_tag: Solution tag, e.g. 'sol3'
            feature_path: Solver feature path
            enabled: True to enable, False to disable
            model_name: Model name (default: current model)

        Returns:
            New active state
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return solver_feature_toggle_core(model, solution_tag, feature_path, enabled)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def results_node_create(
        group: str,
        node_type: str,
        tag: Optional[str] = None,
        properties: Optional[dict] = None,
        selection: Optional[list] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a postprocessing/results node. Numerical node types include
        'Global' (global evaluation), 'IntSurface', 'AveBoundary', 'MaxLine',
        'IntLine'... — set 'expr' in properties, and a 'selection' for
        boundary/surface types. Dataset types: 'Surface', 'Mirror', 'Clip3D',
        'Revolution2D'... Export types: 'Data', 'Image', 'Animation'.

        Args:
            group: 'numerical', 'dataset', 'export', or 'table'
            node_type: Node type, e.g. 'Global', 'AveBoundary'
            tag: Optional tag (auto-generated if omitted)
            properties: Optional {property: value} dict (e.g. {'expr': 'Itot'})
            selection: Entity numbers for boundary/surface selections
            model_name: Model name (default: current model)

        Returns:
            Created node info
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return results_node_create_core(model, group, node_type, tag=tag,
                                            properties=properties, selection=selection)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def results_node_set(
        group: str,
        tag: str,
        properties: dict,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Set properties on an existing results node (numerical/dataset/export/table).

        Args:
            group: 'numerical', 'dataset', 'export', or 'table'
            tag: Node tag
            properties: {property: value} dict
            model_name: Model name (default: current model)

        Returns:
            applied/failed lists
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return results_node_set_core(model, group, tag, properties)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def results_node_delete(
        group: str,
        tag: str,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Delete a results node (numerical/dataset/export/table).

        Args:
            group: 'numerical', 'dataset', 'export', or 'table'
            tag: Node tag
            model_name: Model name (default: current model)

        Returns:
            Confirmation of removal
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return results_node_delete_core(model, group, tag)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def results_numerical_evaluate(
        numerical_tag: str,
        unit: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Evaluate a numerical node (created via results_node_create or from the
        GUI) and return its value(s). Requires the referenced dataset to have
        a computed solution.

        Args:
            numerical_tag: Numerical node tag, e.g. 'gev1'
            unit: Optional output unit, e.g. 'A/m^2'
            model_name: Model name (default: current model)

        Returns:
            Evaluated values
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return results_numerical_evaluate_core(model, numerical_tag, unit=unit)
        except Exception as e:
            return {'success': False, 'error': str(e)}


# ---------------------------------------------------------------------------
# Coordinate interpolation (mphinterp equivalent)
# ---------------------------------------------------------------------------

def results_interpolate_core(model, expressions, coords, dataset: Optional[str] = None,
                             unit: Optional[str] = None,
                             inner: Optional[Union[int, str]] = None) -> dict:
    """Evaluate expressions at arbitrary coordinates (mphinterp equivalent).

    Robust cross-model recipe: pull the full field via model.evaluate (mesh
    nodes, reliable) and interpolate client-side with scipy LinearNDInterpolator.
    Works regardless of COMSOL frame quirks that break server-side at()/Interp
    node approaches. Coordinates are given in SI METERS and converted with the
    geometry's length unit automatically.
    """
    import numpy as np
    from scipy.interpolate import LinearNDInterpolator
    if isinstance(expressions, str):
        expressions = [expressions]
    try:
        sdim = 2
        comp0 = str(model.java.component().tags()[0])
        gt = str(model.java.component(comp0).geom().tags()[0])
        g = model.java.component(comp0).geom(gt)
        sdim = int(g.getSDim())
        try:
            lu = str(g.lengthUnit())
        except Exception:
            lu = 'm'
        factor = {'mm': 1e-3, 'cm': 1e-2, 'm': 1.0, 'um': 1e-6,
                  'dm': 1e-1, 'nm': 1e-9, 'in': 0.0254}.get(lu, 1.0)
    except Exception:
        lu, factor = 'm', 1.0
    if coords and len(coords[0]) != sdim:
        return {'success': False,
                'error': f'Each point needs {sdim} coordinates (space dim), got {len(coords[0])}'}

    # resolve dataset tag -> mph label (mph resolves dataset by label)
    ds_label = None
    if dataset is not None:
        try:
            ds_label = str(model.java.result().dataset(dataset).label())
        except Exception:
            return {'success': False, 'error': f'Dataset not found: {dataset}'}

    kw = {}
    if inner is not None:
        kw['inner'] = inner

    try:
        dskw = {'dataset': ds_label} if ds_label else {}
        axes = []
        for ax in ('x', 'y', 'z')[:sdim]:
            arr = np.asarray(model.evaluate(ax, **dskw, **kw)).ravel()
            axes.append(arr / factor)  # geometry-unit numbers -> SI meters
        pts_mesh = np.column_stack(axes)
        values = {}
        for expr in expressions:
            arr = np.asarray(model.evaluate(expr, **dskw, **kw)).ravel()
            if unit:
                try:
                    arr = np.asarray(model.evaluate(expr, unit, **dskw, **kw)).ravel()
                except Exception:
                    pass
            values[expr] = arr
        pts_query = np.asarray(coords, dtype=float)
        out = {}
        for expr, arr in values.items():
            interp = LinearNDInterpolator(pts_mesh, arr)
            res = interp(pts_query)
            out[expr] = [None if (isinstance(v, float) and np.isnan(v)) else float(v)
                         for v in res]
        return {'success': True, 'dataset': dataset or 'default',
                'length_unit': lu, 'space_dim': sdim,
                'n_points': len(coords), 'values': out,
                'note': 'coordinates in SI meters; NaN = outside mesh'}
    except Exception as e:
        return {'success': False,
                'error': f'Interpolation failed: {e}. Check that the dataset has '
                         f'computed solution data (solve first) and expression names.'}


def register_interpolate_tool(mcp: FastMCP) -> None:
    """Register the coordinate-interpolation tool."""

    @mcp.tool()
    def results_interpolate(
        expressions: list,
        coords: list,
        dataset: Optional[str] = None,
        unit: Optional[str] = None,
        inner: Optional[Union[int, str]] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Evaluate expressions at arbitrary coordinates — the MCP equivalent of
        LiveLink for MATLAB's mphinterp (implemented as full-field evaluation
        plus client-side scipy interpolation, which is robust against COMSOL
        frame quirks that break server-side at()/Interp approaches). Use for
        point probes and profile sampling (e.g. boundary-layer concentration
        profiles), which results_evaluate (whole-dataset) cannot do.

        Args:
            expressions: Expression or list of expressions, e.g. ['cCu','phil']
            coords: Points in SI METERS (not geometry mm!), each a list matching
                    the space dim, e.g. [[0.001, 0], [0.002, 0]] for 2D; a ±1361 mm
                    geometry spans [-1.361, 1.361]
            dataset: Dataset tag (default: first dataset)
            unit: Optional output unit (applied to all expressions)
            inner: Inner solution for time-dependent data: 'first', 'last', or index
            model_name: Model name (default: current model)

        Returns:
            Values per expression, aligned with the input point order
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return results_interpolate_core(model, expressions, coords,
                                            dataset=dataset, unit=unit, inner=inner)
        except Exception as e:
            return {'success': False, 'error': str(e)}


# ---------------------------------------------------------------------------
# Model-level functions (interpolation/analytic) — OCV curves etc.
# ---------------------------------------------------------------------------

def _apply_table(java, table: list):
    """Write a 2D table (list of rows) into an Interpolation function."""
    for row, vals in enumerate(table):
        for col, v in enumerate(vals):
            java.setIndex('table', str(v), row, col)
    return {'table_rows': len(table), 'table_cols': len(table[0]) if table else 0}


def function_create_core(model, func_type: str = 'Interpolation',
                         tag: Optional[str] = None,
                         funcname: Optional[str] = None,
                         table: Optional[list] = None,
                         properties: Optional[dict] = None) -> dict:
    """Create a model-level function. Battery OCV curves: func_type='Interpolation',
    funcname='U_neg', table=[[x0,y0],[x1,y1],...], properties={'fununit':'1[V]',
    'interp':'cubicspline'} (C2-continuous, per the no-constant-OCV rule)."""
    m = model.java
    listing = m.func()
    if tag is None:
        try:
            tag = str(listing.uniqueTag('func'))
        except Exception:
            tag = 'func1'
    try:
        if func_type == 'Interpolation':
            listing.create(tag, func_type)
        else:
            listing.create(tag, func_type)
        node = listing.get(tag)
    except Exception as e:
        return {'success': False,
                'error': f'Failed to create function {func_type} as {tag}: {e}. '
                         f'Valid types include Interpolation, Analytic, Step, '
                         f'Piecewise, Waveform, Image.'}
    report = {'success': True, 'tag': tag, 'type': func_type}
    try:
        if funcname:
            node.set('funcname', funcname)
            report['funcname'] = funcname
        if func_type == 'Interpolation' and table:
            node.set('source', 'table')
            report.update(_apply_table(node, table))
        if properties:
            report['set'] = _apply_properties(node, properties)
    except Exception as e:
        report['warning'] = f'Created, but setup failed: {e}'
    try:
        props, failed = _jprops(node)
        report['function'] = {'tag': str(node.tag()),
                              'props': {k: v for k, v in list(props.items())[:30]}}
    except Exception:
        pass
    return report


def function_set_core(model, tag: str, properties: Optional[dict] = None,
                      table: Optional[list] = None,
                      funcname: Optional[str] = None) -> dict:
    """Update an existing model-level function (properties and/or table data)."""
    m = model.java
    try:
        node = m.func(tag)
    except Exception:
        return {'success': False,
                'error': f'Function not found: {tag}. Available: '
                         f'{_jtags(m.func().tags())}'}
    report = {'path': tag}
    if funcname:
        node.set('funcname', funcname)
    if table:
        node.set('source', 'table')
        report.update(_apply_table(node, table))
    if properties:
        report['set'] = _apply_properties(node, properties)
        report['success'] = not report['set']['failed']
    else:
        report['success'] = True
    from mph.node import get as mph_get
    readback = {}
    for name in (report.get('set', {}).get('applied', []) or []):
        try:
            readback[name] = mph_get(node, name)
        except Exception:
            pass
    if readback:
        report['readback'] = readback
    return report


# ---------------------------------------------------------------------------
# Materials with property groups (from-scratch battery/electrolyte materials)
# ---------------------------------------------------------------------------

def _resolve_component(model, component_tag: Optional[str]):
    m = model.java
    ct = component_tag or _jtags(m.component().tags())[0]
    return m.component(ct), None


def _apply_property_groups(mat, property_groups: dict) -> dict:
    """{group_tag: {prop: value}}; group 'def' exists by default, others are
    created as 'User defined' groups unless the value carries an explicit type."""
    applied, failed = [], []
    for group, props in property_groups.items():
        try:
            try:
                pg = mat.propertyGroup(group)
            except Exception:
                pg = mat.propertyGroup().create(group, 'User defined')
            for prop, value in props.items():
                try:
                    from mph.node import cast
                    pg.set(prop, cast(value if not isinstance(value, str) else value))
                    applied.append(f'{group}:{prop}')
                except Exception as e:
                    failed.append(f'{group}:{prop}: {e}')
        except Exception as e:
            failed.append(f'group {group}: {e}')
    return {'applied': applied, 'failed': failed}


def material_create_core(model, tag: Optional[str] = None, label: Optional[str] = None,
                         component_tag: Optional[str] = None,
                         selection: Optional[list] = None,
                         selection_all: bool = True,
                         property_groups: Optional[dict] = None) -> dict:
    """Create a material with property groups on a component. This closes the
    'cannot build liion materials from scratch' gap: create the material, then
    fill property groups whose values may reference interpolation functions
    (e.g. OCV U(x), concentration-dependent Ds(c))."""
    comp, err = _resolve_component(model, component_tag)
    if err:
        return err
    listing = comp.material()
    if tag is None:
        try:
            tag = str(listing.uniqueTag('mat'))
        except Exception:
            tag = 'mat1'
    try:
        listing.create(tag, 'Common')
        mat = listing.get(tag)
    except Exception as e:
        return {'success': False,
                'error': f'Failed to create material {tag}: {e}. Note: on the client '
                         f'API use the two-argument create(tag, "Common"); the '
                         f'three-argument form is not accepted.'}
    report = {'success': True, 'tag': tag}
    if label:
        mat.label(label)
    if selection_all:
        try:
            mat.selection().all()
            report['selection'] = 'all'
        except Exception:
            pass
    elif selection:
        report.update(_apply_selection(mat, selection))
    if property_groups:
        report['set'] = _apply_property_groups(mat, property_groups)
    try:
        groups = _jtags(mat.propertyGroup().tags())
        report['property_groups'] = groups
        for g in groups:
            try:
                report.setdefault('readback', {})[g] = {
                    p: str(mat.propertyGroup(g).getString(p))
                    for p in _jtags(mat.propertyGroup(g).properties())
                    if p not in ('info',)}
            except Exception:
                pass
    except Exception:
        pass
    return report



def material_set_core(model, material_tag: str,
                      label: Optional[str] = None,
                      component_tag: Optional[str] = None,
                      selection: Optional[Union[list, str]] = None,
                      property_groups: Optional[dict] = None) -> dict:
    """Set label/selection/property groups on an existing material. Missing
    property groups are created as 'User defined' automatically."""
    comp, err = _resolve_component(model, component_tag)
    if err:
        return err
    try:
        mat = comp.material(material_tag)
    except Exception:
        return {'success': False,
                'error': f'Material not found: {material_tag}. Available: '
                         f'{_jtags(comp.material().tags())}'}
    report = {'path': material_tag}
    if label:
        mat.label(label)
    if selection is not None:
        report.update(_apply_selection(mat, selection))
    if property_groups:
        report['set'] = _apply_property_groups(mat, property_groups)
        report['success'] = not report['set']['failed']
    else:
        report['success'] = True
    return report


def material_delete_core(model, material_tag: str,
                         component_tag: Optional[str] = None) -> dict:
    comp, err = _resolve_component(model, component_tag)
    if err:
        return err
    try:
        comp.material().remove(material_tag)
        return {'success': True, 'removed': material_tag}
    except Exception as e:
        return {'success': False,
                'error': f'Failed to remove: {e}. Available: '
                         f'{_jtags(comp.material().tags())}'}


def register_material_function_tools(mcp: FastMCP) -> None:
    """Register function/material creation tools."""

    @mcp.tool()
    def function_create(
        func_type: str = "Interpolation",
        tag: Optional[str] = None,
        funcname: Optional[str] = None,
        table: Optional[list] = None,
        properties: Optional[dict] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a model-level function — the missing piece for from-scratch
        battery models: OCV curves are Interpolation functions
        (table=[[x,y],...] in ascending x, properties={'fununit':'1[V]',
        'interp':'cubicspline'} for C2 continuity — never piecewise-constant).
        Also supports Analytic (properties={'expr':..., 'args':...}), Step,
        Piecewise, Waveform.

        Args:
            func_type: 'Interpolation' (default), 'Analytic', 'Step', ...
            tag: Optional tag (auto if omitted)
            funcname: Name callable in expressions, e.g. 'U_neg' -> U_neg(x)
            table: Data rows [[x0,y0],[x1,y1],...] for Interpolation
            properties: Extra properties (fununit, interp, expr, args...)
            model_name: Model name (default: current)

        Returns:
            Created function info with property readback
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return function_create_core(model, func_type, tag=tag, funcname=funcname,
                                        table=table, properties=properties)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def function_set(
        tag: str,
        properties: Optional[dict] = None,
        table: Optional[list] = None,
        funcname: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Update an existing model-level function (properties and/or table).

        Args:
            tag: Function tag, e.g. 'int1'
            properties: {property: value} dict
            table: New data rows for Interpolation functions
            funcname: New callable name
            model_name: Model name (default: current)

        Returns:
            applied/failed plus readback
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return function_set_core(model, tag, properties=properties,
                                     table=table, funcname=funcname)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def material_create(
        tag: Optional[str] = None,
        label: Optional[str] = None,
        component_tag: Optional[str] = None,
        selection: Optional[list] = None,
        selection_all: bool = True,
        property_groups: Optional[dict] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a material with property groups — closes the gap that forced
        battery models to start from Application Library skeletons. Property
        group values are expressions and may reference interpolation functions
        (e.g. {'def': {'density':'2260[kg/m^3]'}, 'pgliion': {'Ds':
        'Ds_neg(cse)'}}). Group 'def' exists by default; others are created
        automatically. For liion interface groups, mirror the group/property
        names from a GUI model's model_export_java output.

        Args:
            tag: Optional material tag (auto if omitted)
            label: Display label, e.g. 'NMC111 (Chen2020)'
            component_tag: Component (default: first)
            selection: Domain numbers; default selects ALL domains
            selection_all: Explicit all-domain selection flag (default True)
            property_groups: {group_tag: {property: value}}
            model_name: Model name (default: current)

        Returns:
            Created material info incl. groups and value readback
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return material_create_core(model, tag=tag, label=label,
                                        component_tag=component_tag,
                                        selection=selection,
                                        selection_all=selection_all,
                                        property_groups=property_groups)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def material_set(
        material_tag: str,
        label: Optional[str] = None,
        component_tag: Optional[str] = None,
        selection: Optional[list] = None,
        property_groups: Optional[dict] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Set label/selection/property groups on an existing material. Missing
        groups are created automatically. Use model_export_java on a GUI
        material to find exact liion group/property names.

        Args:
            material_tag: Material tag, e.g. 'mat2'
            label: New label
            component_tag: Component (default: first)
            selection: Domain numbers
            property_groups: {group_tag: {property: value}}
            model_name: Model name (default: current)

        Returns:
            applied/failed lists
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return material_set_core(model, material_tag, label=label,
                                     component_tag=component_tag, selection=selection,
                                     property_groups=property_groups)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @mcp.tool()
    def material_delete(
        material_tag: str,
        component_tag: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Delete a material from a component.

        Args:
            material_tag: Material tag
            component_tag: Component (default: first)
            model_name: Model name (default: current)

        Returns:
            Confirmation
        """
        model, err = _get_model(model_name)
        if err:
            return err
        try:
            return material_delete_core(model, material_tag,
                                        component_tag=component_tag)
        except Exception as e:
            return {'success': False, 'error': str(e)}
