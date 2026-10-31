"""Exhaustive model audit, diff and Java-export tools for COMSOL MCP Server.

Solves the "AI blind spot" problem: model_inspect returns name lists only and
physics_list_features is one level deep. These tools walk the entire model tree
by tag (never by localized label), dumping every property, selection and
nested sub-feature, with all access failures recorded explicitly.

Core logic lives in module-level functions so it can be tested/reused without
an MCP round trip. The CLI script comsol_audit.py wraps audit_model().
"""

import json
import re
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .session import session_manager

# All audit sections in canonical order
AUDIT_SECTIONS = (
    'parameters', 'functions', 'components', 'global_variables',
    'studies', 'solvers', 'batches', 'results',
)


# ---------------------------------------------------------------------------
# Java/jpype helpers
# ---------------------------------------------------------------------------

def _jtags(arr) -> list:
    """Java String[] -> list[str]"""
    return [str(arr[i]) for i in range(len(arr))]


def _jprops(java) -> tuple[dict, list]:
    """Read every property of a node. Single failures are collected, not raised."""
    props, failed = {}, []
    if not hasattr(java, 'properties'):
        return props, failed
    from mph.node import get as mph_get  # getValueType + typed getters
    try:
        names = _jtags(java.properties())
    except Exception as e:
        return props, [f'properties(): {e}']
    for name in names:
        try:
            val = mph_get(java, name)
            s = json.dumps(val, ensure_ascii=False, default=str)
            if len(s) > 400:  # truncate huge arrays (mesh coords etc.)
                val = f'<{len(s)} chars truncated>'
            props[name] = val
        except Exception as e:
            failed.append(f'{name}: {e}')
    return props, failed


def _selection_info(java):
    """Resolve node selection: named selection / manual entity numbers."""
    try:
        sel = java.selection()
    except Exception:
        return None
    try:
        named = str(sel.named())
        if named:
            return {'named': named}
    except Exception:
        pass
    for dim in (3, 2, 1, 0):
        try:
            e = sel.entities(dim)
            if e is not None and len(e):
                return {'dim': dim, 'entities': [int(x) for x in e]}
        except Exception:
            continue
    return None


def _dump_node(java, kind: str, with_selection: bool = True) -> dict:
    d = {'kind': kind}
    try:
        d['tag'] = str(java.tag())
    except Exception:
        d['tag'] = '?'
    try:
        d['label'] = str(java.label())
    except Exception:
        d['label'] = '?'
    try:
        d['type'] = str(java.getType())
    except Exception:
        pass
    try:
        d['active'] = bool(java.isActive())
    except Exception:
        pass
    props, pfail = _jprops(java)
    if props:
        d['props'] = props
    if pfail:
        d['prop_read_failures'] = pfail
    if with_selection:
        si = _selection_info(java)
        if si:
            d['selection'] = si
    return d


def _walk_features(container, errors: list) -> list:
    """Recursively walk a feature tree (physics/solver/plot features)."""
    out = []
    try:
        tags = _jtags(container.feature().tags())
    except Exception as e:
        errors.append(f'feature().tags(): {e}')
        return out
    for t in tags:
        try:
            feat = container.feature(t)
            d = _dump_node(feat, 'feature')
            subs = _walk_features(feat, errors)
            if subs:
                d['features'] = subs
            out.append(d)
        except Exception as e:
            errors.append(f'feature {t}: {e}')
    return out


# ---------------------------------------------------------------------------
# Core audit
# ---------------------------------------------------------------------------

def audit_model(model, sections: Optional[list] = None) -> dict:
    """Exhaustively audit a loaded mph model object.

    Returns a dict with the full model tree plus audit_stats. Every access
    failure is recorded in audit_stats.errors — nothing is silently skipped.
    """
    want = (lambda s: True) if not sections else (lambda s: s in sections)
    m = model.java
    errors: list = []
    data: dict = {'model': model.name()}

    if want('parameters'):
        try:
            params = {}
            for name, value in model.parameters().items():
                entry = {'value': value}
                try:
                    desc = str(m.param().description(name))
                    if desc:
                        entry['description'] = desc
                except Exception:
                    pass
                params[name] = entry
            data['parameters'] = params
        except Exception as e:
            errors.append(f'parameters: {e}')

    if want('functions'):
        try:
            funcs = []
            for t in _jtags(m.func().tags()):
                f = m.func(t)
                d = _dump_node(f, 'function', with_selection=False)
                try:
                    d['expr'] = str(f.expr())
                except Exception:
                    pass
                funcs.append(d)
            data['functions'] = funcs
        except Exception as e:
            errors.append(f'functions: {e}')

    if want('components'):
        comps = []
        try:
            for ct in _jtags(m.component().tags()):
                comp = m.component(ct)
                cd = _dump_node(comp, 'component', with_selection=False)
                try:
                    geoms = []
                    for gt in _jtags(comp.geom().tags()):
                        g = comp.geom(gt)
                        gd = _dump_node(g, 'geometry', with_selection=False)
                        try:
                            gd['space_dim'] = int(g.getSDim())
                            gd['geom_dim'] = int(g.getDim())
                        except Exception:
                            pass
                        gd['features'] = _walk_features(g, errors)
                        geoms.append(gd)
                    cd['geometries'] = geoms
                except Exception as e:
                    errors.append(f'{ct} geometry: {e}')
                try:
                    sels = []
                    for st in _jtags(comp.selection().tags()):
                        sels.append(_dump_node(comp.selection(st), 'selection'))
                    cd['selections'] = sels
                except Exception as e:
                    errors.append(f'{ct} selections: {e}')
                try:
                    phys = []
                    for pt in _jtags(comp.physics().tags()):
                        p = comp.physics(pt)
                        pd = _dump_node(p, 'physics', with_selection=False)
                        try:
                            fields = []
                            for fl in _jtags(p.field().tags()):
                                fields.append({'field': fl,
                                               'components': _jtags(p.field(fl).component())})
                            if fields:
                                pd['fields'] = fields
                        except Exception:
                            pass
                        try:
                            pnodes = []
                            for pr in _jtags(p.prop().tags()):
                                pnodes.append(_dump_node(p.prop(pr), 'prop'))
                            if pnodes:
                                pd['prop_nodes'] = pnodes
                        except Exception as e:
                            errors.append(f'{pt} prop: {e}')
                        pd['features'] = _walk_features(p, errors)
                        phys.append(pd)
                    cd['physics'] = phys
                except Exception as e:
                    errors.append(f'{ct} physics: {e}')
                try:
                    mfs = []
                    for mt in _jtags(comp.multiphysics().tags()):
                        mfs.append(_dump_node(comp.multiphysics(mt), 'multiphysics'))
                    if mfs:
                        cd['multiphysics'] = mfs
                except Exception:
                    pass
                try:
                    mats = []
                    for mt in _jtags(comp.material().tags()):
                        mat = comp.material(mt)
                        md = _dump_node(mat, 'material')
                        try:
                            md['property_groups'] = _jtags(mat.propertyGroup().tags())
                        except Exception:
                            pass
                        mats.append(md)
                    if mats:
                        cd['materials'] = mats
                except Exception as e:
                    errors.append(f'{ct} materials: {e}')
                try:
                    meshes = []
                    for mt in _jtags(comp.mesh().tags()):
                        me = comp.mesh(mt)
                        md = _dump_node(me, 'mesh', with_selection=False)
                        md['features'] = _walk_features(me, errors)
                        try:
                            md['stats'] = {'elements': int(me.getNumElem())}
                        except Exception:
                            pass
                        meshes.append(md)
                    cd['meshes'] = meshes
                except Exception as e:
                    errors.append(f'{ct} mesh: {e}')
                try:
                    vs = []
                    for vt in _jtags(comp.variable().tags()):
                        v = comp.variable(vt)
                        vd = _dump_node(v, 'variables')
                        try:
                            vd['varlist'] = {str(v.varname(i)): str(v.varexpr(i))
                                             for i in range(int(v.nVarName()))}
                        except Exception:
                            pass
                        vs.append(vd)
                    if vs:
                        cd['variables'] = vs
                except Exception:
                    pass
                comps.append(cd)
            data['components'] = comps
        except Exception as e:
            errors.append(f'components: {e}')

    if want('global_variables'):
        try:
            gvs = []
            for vt in _jtags(m.variable().tags()):
                gvs.append(_dump_node(m.variable(vt), 'global_variables'))
            if gvs:
                data['global_variables'] = gvs
        except Exception:
            pass

    if want('studies'):
        try:
            studies = []
            for st in _jtags(m.study().tags()):
                s = m.study(st)
                sd = _dump_node(s, 'study', with_selection=False)
                sd['features'] = _walk_features(s, errors)
                studies.append(sd)
            data['studies'] = studies
        except Exception as e:
            errors.append(f'studies: {e}')

    if want('solvers'):
        try:
            sols = []
            for st in _jtags(m.sol().tags()):
                s = m.sol(st)
                sd = _dump_node(s, 'solution', with_selection=False)
                sd['features'] = _walk_features(s, errors)
                sols.append(sd)
            if sols:
                data['solvers'] = sols
        except Exception as e:
            errors.append(f'solvers: {e}')

    if want('batches'):
        try:
            bs = []
            for bt in _jtags(m.batch().tags()):
                b = m.batch(bt)
                bd = _dump_node(b, 'batch', with_selection=False)
                bd['features'] = _walk_features(b, errors)
                bs.append(bd)
            if bs:
                data['batches'] = bs
        except Exception:
            pass

    if want('results'):
        try:
            r = m.result()
            res = {}
            group_accessors = (
                ('datasets', lambda: r.dataset()),
                ('evaluations', lambda: r.numerical()),
                ('exports', lambda: r.export()),
                ('tables', lambda: r.table()),
            )
            for key, accessor in group_accessors:
                try:
                    items = []
                    listing = accessor()
                    for t in _jtags(listing.tags()):
                        items.append(_dump_node(listing.get(t), key.rstrip('s'),
                                                with_selection=False))
                    if items:
                        res[key] = items
                except Exception as e:
                    errors.append(f'result {key}: {e}')
            # Plot groups: client API may not expose a list accessor — record that
            # explicitly instead of failing silently.
            try:
                plots = []
                listing = None
                for acc in ('plotGroup', 'plot', 'pg'):
                    if hasattr(r, acc):
                        listing = getattr(r, acc)()
                        break
                if listing is not None:
                    for t in _jtags(listing.tags()):
                        pg = listing.get(t)
                        p = _dump_node(pg, 'plotgroup', with_selection=False)
                        p['features'] = _walk_features(pg, errors)
                        plots.append(p)
                else:
                    errors.append('result plots: no plot-group accessor on client API '
                                  '(plotGroup/plot/pg); see .java export for plot groups')
                if plots:
                    res['plots'] = plots
            except Exception as e:
                errors.append(f'result plots: {e}')
            try:
                probes = []
                for t in _jtags(r.probe().tags()):
                    probes.append(_dump_node(r.probe(t), 'probe'))
                if probes:
                    res['probes'] = probes
            except Exception:
                pass
            if res:
                data['results'] = res
        except Exception as e:
            errors.append(f'results: {e}')

    # ---- stats ----
    def _count(obj, key):
        n = 0
        if isinstance(obj, dict):
            if key in obj:
                n += 1
            for v in obj.values():
                n += _count(v, key)
        elif isinstance(obj, list):
            for v in obj:
                n += _count(v, key)
        return n

    def _count_props(obj):
        n = 0
        if isinstance(obj, dict):
            if 'props' in obj:
                n += len(obj['props'])
            for v in obj.values():
                n += _count_props(v)
        elif isinstance(obj, list):
            for v in obj:
                n += _count_props(v)
        return n

    data['audit_stats'] = {
        'total_nodes': _count(data, 'tag'),
        'total_properties': _count_props(data),
        'errors': errors,
        'error_count': len(errors),
        'sections': list(sections) if sections else list(AUDIT_SECTIONS),
    }
    return data


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def audit_to_markdown(data: dict) -> str:
    lines = [f"# 模型审计报告: {data['model']}", '']

    def val(v):
        s = json.dumps(v, ensure_ascii=False, default=str)
        return s if len(s) <= 120 else s[:117] + '...'

    def emit_node(d, indent):
        pad = '  ' * indent
        head = f"{pad}- **[{d.get('tag', '?')}]** {d.get('label', '?')}"
        if d.get('type'):
            head += f"  `{d['type']}`"
        if d.get('active') is False:
            head += '  ⛔**已禁用**'
        lines.append(head)
        sub = []
        if 'selection' in d:
            sel = d['selection']
            if 'named' in sel:
                sub.append(f"选择: 命名选择 `{sel['named']}`")
            elif 'dim' in sel:
                sub.append(f"选择: 维度{sel['dim']} 实体 {sel['entities']}")
            else:
                sub.append(f"选择: {val(sel)}")
        for k, v in d.get('props', {}).items():
            sub.append(f"{k} = {val(v)}")
        if 'varlist' in d:
            for vn, vx in d['varlist'].items():
                sub.append(f"变量 `{vn}` = {vx}")
        if 'expr' in d:
            sub.append(f"expr = {val(d['expr'])}")
        for s in sub:
            lines.append(f"{pad}  - {s}")
        for key in ('features', 'prop_nodes', 'geometries', 'selections', 'physics',
                    'multiphysics', 'materials', 'meshes', 'variables',
                    'property_groups'):
            for child in d.get(key, []) if isinstance(d.get(key), list) else []:
                if isinstance(child, dict):
                    emit_node(child, indent + 1)
                else:
                    lines.append(f"{'  ' * (indent + 1)}- {child}")
        for key in ('fields',):
            for child in d.get(key, []):
                lines.append(f"{'  ' * (indent + 1)}- 场变量 {val(child)}")

    if data.get('parameters'):
        lines += ['## 参数 (%d)' % len(data['parameters']), '']
        for name, info in data['parameters'].items():
            d = info.get('description', '')
            lines.append(f"- **{name}** = `{info['value']}`" + (f" — {d}" if d else ''))
        lines.append('')
    if data.get('functions'):
        lines += ['## 函数', '']
        for f in data['functions']:
            emit_node(f, 0)
        lines.append('')
    if data.get('global_variables'):
        lines += ['## 全局变量', '']
        for v in data['global_variables']:
            emit_node(v, 0)
        lines.append('')
    for comp in data.get('components', []):
        lines += [f"## 组件 [{comp.get('tag')}] {comp.get('label')}", '']
        for key, title in (('geometries', '### 几何'), ('selections', '### 命名选择集'),
                           ('physics', '### 物理场'), ('multiphysics', '### 多物理耦合'),
                           ('materials', '### 材料'), ('meshes', '### 网格'),
                           ('variables', '### 组件变量')):
            for it in comp.get(key, []):
                lines.append(title)
                emit_node(it, 0)
                lines.append('')
    for key, title in (('studies', '## 研究'), ('solvers', '## 求解器配置'),
                       ('batches', '## 批处理作业')):
        for it in data.get(key, []):
            lines.append(title)
            emit_node(it, 0)
            lines.append('')
    if data.get('results'):
        lines += ['## 结果节点', '']
        for key, items in data['results'].items():
            lines.append(f"### {key}")
            for it in items:
                emit_node(it, 0)
            lines.append('')

    st = data.get('audit_stats', {})
    lines += ['---',
              f"**审计完整性**: {st.get('total_nodes', '?')} 个节点, "
              f"{st.get('total_properties', '?')} 条属性, "
              f"{st.get('error_count', '?')} 个访问错误(全记录于 JSON errors 字段)", '']
    if st.get('errors'):
        lines += ['<details><summary>访问错误明细(点开)</summary>', '']
        for e in st['errors'][:50]:
            lines.append(f'- {e}')
        lines += ['', '</details>']
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Diff between two audits
# ---------------------------------------------------------------------------

def _flatten(obj, prefix: str, nodes: dict, props: dict):
    """Flatten audit tree into {node_path: label} and {prop_path: value}."""
    if isinstance(obj, dict):
        if 'tag' in obj and 'kind' in obj:
            path = f"{prefix}/{obj['tag']}" if prefix else str(obj['tag'])
            nodes[path] = obj.get('label', '?')
            for k, v in obj.get('props', {}).items():
                props[f'{path}:{k}'] = v
            if 'selection' in obj:
                props[f'{path}:selection'] = obj['selection']
            if obj.get('active') is False:
                props[f'{path}:active'] = False
            for key in ('features', 'prop_nodes', 'geometries', 'selections',
                        'physics', 'multiphysics', 'materials', 'meshes',
                        'variables'):
                for child in obj.get(key, []) or []:
                    _flatten(child, path, nodes, props)
        else:
            for k, v in obj.items():
                if k in ('audit_stats',):
                    continue
                if isinstance(v, (dict, list)) and k not in ('parameters',):
                    _flatten(v, prefix, nodes, props)
    elif isinstance(obj, list):
        for v in obj:
            _flatten(v, prefix, nodes, props)


def diff_audits(a: dict, b: dict, max_items: int = 100) -> dict:
    """Structural diff of two audit dicts (old=a, new=b)."""
    na, pa, nb, pb = {}, {}, {}, {}
    # parameters handled separately (simple dicts)
    param_a = a.get('parameters', {})
    param_b = b.get('parameters', {})
    for name in param_a:
        pa[f'parameters:{name}:value'] = param_a[name].get('value')
    for name in param_b:
        pb[f'parameters:{name}:value'] = param_b[name].get('value')
    _flatten({k: v for k, v in a.items() if k != 'parameters'}, '', na, pa)
    _flatten({k: v for k, v in b.items() if k != 'parameters'}, '', nb, pb)

    added_nodes = sorted(set(nb) - set(na))
    removed_nodes = sorted(set(na) - set(nb))
    added_props = sorted(set(pb) - set(pa))
    removed_props = sorted(set(pa) - set(pb))

    def _sig(v):
        # numpy arrays etc. don't support == comparison; compare via JSON form
        return json.dumps(v, ensure_ascii=False, default=str)

    changed_props = [
        {'path': p, 'old': pa[p], 'new': pb[p]}
        for p in sorted(set(pa) & set(pb))
        if _sig(pa[p]) != _sig(pb[p])
    ]

    def cap(lst):
        return {'count': len(lst), 'items': lst[:max_items]}

    return {
        'added_nodes': cap(added_nodes),
        'removed_nodes': cap(removed_nodes),
        'added_props': cap(added_props),
        'removed_props': cap(removed_props),
        'changed_props': cap(changed_props),
        'summary': (
            f"+{len(added_nodes)}/-{len(removed_nodes)} nodes, "
            f"+{len(added_props)}/-{len(removed_props)} props, "
            f"{len(changed_props)} changed"
        ),
    }


# ---------------------------------------------------------------------------
# Java export with unicode decoding
# ---------------------------------------------------------------------------

def export_java(model, output_path: Optional[str] = None, decode_unicode: bool = True) -> dict:
    """Export model as .java source; optionally write a \\uXXXX-decoded copy."""
    try:
        if output_path is None:
            base = model.file()
            output_path = str(Path(base).with_suffix('.java')) if base else f'{model.name()}.java'
        path = Path(output_path)
        if path.suffix.lower() != '.java':
            path = path.with_suffix('.java')
        # 2026-10-16 hardening: (1) auto-create parent dirs; (2) echo the raw
        # received path — observed in the field: underscores in output_path can
        # arrive as spaces (client/transport-layer mangling), and without the
        # raw echo this is indistinguishable from a user typo.
        result_raw_arg = str(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        model.save(path=str(path), format='Java')
        result = {'java_file': str(path), 'output_path_raw_received': result_raw_arg}
        if '_' in result_raw_arg and '_' not in str(path):
            result['warning'] = ('underscores missing in resolved path - check '
                                 'client/transport layer argument mangling')
        if decode_unicode:
            try:
                src = path.read_text(encoding='utf-8', errors='replace')
                decoded = re.sub(r'\\u([0-9a-fA-F]{4})',
                                 lambda m: chr(int(m.group(1), 16)), src)
                dpath = path.with_name(path.stem + '_readable.java')
                dpath.write_text(decoded, encoding='utf-8')
                result['readable_file'] = str(dpath)
                result['lines'] = decoded.count('\n') + 1
            except Exception as e:
                result['decode_error'] = str(e)
        return {'success': True, **result}
    except Exception as e:
        return {'success': False, 'error': f'Failed to export Java: {e}'}


# ---------------------------------------------------------------------------
# Recursive physics feature listing (upgrades physics_list_features)
# ---------------------------------------------------------------------------

def list_physics_features_deep(model, physics_name: str, include_details: bool = False) -> dict:
    """Recursive feature listing for one physics interface, tag-based.

    Returns features with tag/label/type/active, nested sub-features, and
    selection entity numbers; optionally full property values.
    """
    m = model.java
    tags = _jtags(m.component().tags())
    found = None
    for ct in tags:
        for pt in _jtags(m.component(ct).physics().tags()):
            p = m.component(ct).physics(pt)
            if str(p.label()) == physics_name or pt == physics_name:
                found = (ct, pt, p)
                break
        if found:
            break
    if found is None:
        return {'success': False,
                'error': f'Physics interface not found: {physics_name} '
                         f'(available: {[str(m.component(ct).physics(pt).label()) for ct in tags for pt in _jtags(m.component(ct).physics().tags())]})'}

    ct, pt, p = found

    def describe(feat, path_prefix):
        d = {'tag': str(feat.tag()), 'label': str(feat.label())}
        try:
            d['type'] = str(feat.getType())
        except Exception:
            pass
        try:
            d['active'] = bool(feat.isActive())
        except Exception:
            pass
        sel = _selection_info(feat)
        if sel:
            d['selection'] = sel
        path = f'{path_prefix}/{feat.tag()}'
        if include_details:
            props, failed = _jprops(feat)
            if props:
                d['props'] = props
            if failed:
                d['prop_read_failures'] = failed
        subs = []
        try:
            for st in _jtags(feat.feature().tags()):
                subs.append(describe(feat.feature(st), path))
        except Exception:
            pass
        if subs:
            d['features'] = subs
        return d

    features = []
    try:
        prop_nodes = []
        for pr in _jtags(p.prop().tags()):
            node = p.prop(pr)
            pd = {'tag': str(node.tag()), 'label': str(node.label())}
            try:
                pd['type'] = str(node.getType())
            except Exception:
                pass
            if include_details:
                props, failed = _jprops(node)
                if props:
                    pd['props'] = props
            prop_nodes.append(pd)
    except Exception:
        prop_nodes = []
    for ft in _jtags(p.feature().tags()):
        features.append(describe(p.feature(ft), f'{ct}/physics/{pt}'))

    return {
        'success': True,
        'component': ct,
        'tag': pt,
        'physics': str(p.label()),
        'features': features,
        'prop_nodes': prop_nodes,
        'count': len(features),
    }


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

def register_audit_tools(mcp: FastMCP) -> None:
    """Register audit/diff/export tools with the MCP server."""

    @mcp.tool()
    def model_audit(
        model_name: Optional[str] = None,
        output_dir: Optional[str] = None,
        sections: Optional[list] = None,
    ) -> dict:
        """
        Exhaustively audit a model: every node, property, nested sub-feature,
        boundary selection and disabled-feature flag, walking the full model
        tree by tag. Unlike model_inspect (name lists only), this captures
        everything — designed to eliminate AI blind spots.

        Writes <model>_audit.json (machine-readable) and <model>_audit.md
        (human/AI-readable) next to the model file (or into output_dir).
        Returns summary statistics and file paths, NOT the full content —
        open the files for details.

        Args:
            model_name: Model to audit (default: current model)
            output_dir: Directory for report files (default: alongside model file)
            sections: Optional filter, e.g. ['physics','studies','solvers'].
                      Choices: parameters, functions, components, global_variables,
                      studies, solvers, batches, results. Default: all.

        Returns:
            Audit stats (node/property/error counts), file paths, error list
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False,
                    "error": f"Model not found: {model_name or 'no current model'}"}

        try:
            data = audit_model(model, sections=sections)
            outdir = Path(output_dir) if output_dir else None
            if outdir is None:
                base = model.file()
                outdir = (Path(base).parent if base else Path('.')) / f"{model.name()}_audit"
            outdir.mkdir(parents=True, exist_ok=True)
            jpath = outdir / f"{model.name()}_audit.json"
            mpath = outdir / f"{model.name()}_audit.md"
            jpath.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str),
                             encoding='utf-8')
            mpath.write_text(audit_to_markdown(data), encoding='utf-8')
            st = data['audit_stats']
            return {
                'success': True,
                'stats': {k: v for k, v in st.items() if k != 'errors'},
                'errors': st['errors'],
                'json_file': str(jpath),
                'markdown_file': str(mpath),
                'note': 'Full content in the two files above; read them for details.',
            }
        except Exception as e:
            return {'success': False, 'error': f'Audit failed: {e}'}

    @mcp.tool()
    def model_diff(
        audit_file_old: str,
        audit_file_new: str,
        max_items: int = 100,
    ) -> dict:
        """
        Structurally diff two audit JSON files (from model_audit): added/removed
        nodes and properties, and changed property values (old vs new).

        Standard workflow: run model_audit before modifying a model, modify it
        (via MCP tools or GUI), run model_audit again, then model_diff the two
        files to verify exactly what changed — nothing slips through unnoticed.

        Args:
            audit_file_old: Path to the earlier *_audit.json
            audit_file_new: Path to the later *_audit.json
            max_items: Max items per change category in the response (default 100)

        Returns:
            Change lists: added_nodes, removed_nodes, added_props,
            removed_props, changed_props (with old/new values), summary line
        """
        try:
            a = json.loads(Path(audit_file_old).read_text(encoding='utf-8'))
            b = json.loads(Path(audit_file_new).read_text(encoding='utf-8'))
            return {'success': True, 'old': a.get('model'), 'new': b.get('model'),
                    **diff_audits(a, b, max_items=max_items)}
        except Exception as e:
            return {'success': False, 'error': f'Diff failed: {e}'}

    @mcp.tool()
    def model_export_java(
        model_name: Optional[str] = None,
        output_path: Optional[str] = None,
        decode_unicode: bool = True,
    ) -> dict:
        """
        Export the model as Java API source code (the equivalent of the GUI's
        File > Export > Model Java-file). Every GUI setting maps to one API
        call, so this shows exactly how to reproduce/modify the model in code.

        With decode_unicode=True (default) also writes *_readable.java where
        \\uXXXX escapes are converted back to readable characters (COMSOL
        exports non-ASCII labels as escapes).

        Note: prefer model_audit for reading current state — .java exports can
        contain historical set() replay (same property set several times, last
        one wins), whereas model_audit reads final effective values.

        Args:
            model_name: Model to export (default: current model)
            output_path: Output .java path (default: next to the model file)
            decode_unicode: Also write a decoded *_readable.java copy (default True)

        Returns:
            Paths to exported files and line count
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False,
                    "error": f"Model not found: {model_name or 'no current model'}"}
        return export_java(model, output_path=output_path, decode_unicode=decode_unicode)


# ---------------------------------------------------------------------------
# Geometry boundary location info
# ---------------------------------------------------------------------------

def geometry_boundary_info_core(model, geom_tag: Optional[str] = None,
                                component_tag: Optional[str] = None) -> dict:
    """Per-boundary measure + adjacency + geometry bounding box.

    Gives the AI spatial context for boundary numbers: how long each boundary
    is, which domains it separates, and the overall extent of the geometry —
    enough to reason about 'bottom edge / inlet side' without guessing.
    """
    import jpype
    m = model.java
    ct = component_tag or _jtags(m.component().tags())[0]
    comp = m.component(ct)
    gt = geom_tag or _jtags(comp.geom().tags())[0]
    g = comp.geom(gt)
    JInt = jpype.JArray(jpype.JInt)

    info = {'component': ct, 'geometry': gt}
    try:
        info['space_dim'] = int(g.getSDim())
    except Exception:
        pass
    try:
        box = [float(x) for x in g.getBoundingBox()]
        names = ['xmin', 'xmax', 'ymin', 'ymax', 'zmin', 'zmax'][:len(box)]
        info['bounding_box'] = dict(zip(names, box))
    except Exception as e:
        info['bounding_box_error'] = str(e)
    try:
        info['length_unit'] = str(g.lengthUnit())
    except Exception:
        pass

    boundaries = []
    try:
        nb = int(g.getNBoundaries())
        lengths = None
        try:
            gm = g.measure()
            arr = gm.getLength(JInt(list(range(1, nb + 1))))
            lengths = [float(arr[i]) for i in range(len(arr))]
        except Exception:
            pass
        adjacency = None
        try:
            adj = g.getAdj(1, 2)  # boundary -> adjacent domains, all at once
            adjacency = [[int(x) for x in adj[i]] for i in range(len(adj))]
        except Exception:
            pass
        for i in range(1, nb + 1):
            b = {'boundary': i}
            if lengths:
                b['measure'] = lengths[i - 1]
            if adjacency and i <= len(adjacency):
                b['adjacent_domains'] = adjacency[i - 1]
            boundaries.append(b)
        info['boundaries'] = boundaries
        info['boundary_count'] = nb
        if not lengths:
            info['measure_note'] = ('per-boundary measure not exposed by the client API; '
                                    'derive sizes from bounding_box and geometry features '
                                    'in model_audit output')
    except Exception as e:
        info['boundary_error'] = str(e)

    try:
        info['domain_count'] = int(g.getNDomains())
    except Exception:
        pass
    return {'success': True, **info}


def register_boundary_info_tool(mcp: FastMCP) -> None:
    """Register the geometry boundary location tool."""

    @mcp.tool()
    def geometry_boundary_info(
        geom_tag: Optional[str] = None,
        component_tag: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Spatial context for geometry boundary numbers: overall bounding box
        (xmin/xmax/ymin/ymax), per-boundary measure (length in 2D), and which
        domains each boundary is adjacent to. Use this to reason about which
        boundary is which physical surface (e.g. long bottom edge = cathode
        roll) instead of guessing boundary numbers.

        Args:
            geom_tag: Geometry tag (default: first geometry)
            component_tag: Component tag (default: first component)
            model_name: Model name (default: current model)

        Returns:
            Bounding box, length unit, per-boundary measure + adjacent domains
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False,
                    "error": f"Model not found: {model_name or 'no current model'}"}
        try:
            return geometry_boundary_info_core(model, geom_tag=geom_tag,
                                               component_tag=component_tag)
        except Exception as e:
            return {'success': False, 'error': str(e)}
