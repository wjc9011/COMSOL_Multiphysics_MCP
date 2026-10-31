"""Physics Builder tools: generate and register custom physics interfaces.

Built on the reverse-engineered .mphphb format (case comsol63-install-re,
E-007): a custom physics interface is a plain-XML PhysicsBuilder design.
These tools wrap the two-step workflow validated live on COMSOL 6.3:

1. ``physicsbuilder_generate`` - surgically derives a new interface from a
   bundled template (the SchrodingerEquation demo shipped with every COMSOL
   install): rename type/label/field variable, replace the weak-form
   expression, rename the template's three user inputs (mu -> diffusivity,
   V -> source, psi0 -> boundary value) with sane units/defaults.
2. ``physicsbuilder_register`` / ``physicsbuilder_status`` - manage the
   ``physicsbuilder.settings.developmentfiles`` preference (comma-separated
   paths) in BOTH comsol.prefs and comsolserver.prefs, with
   ``usebuilder=on`` (COMSOL booleans are on/off; ``true`` is silently
   ignored). Registration is read at session start: restart the COMSOL
   session, then ``physics().create(tag, typename, geom)`` works like any
   built-in interface.

Caveats (documented, by design of the thin v1):
- models using a custom physics do NOT embed its definition - the .mphphb
  must be registered on any machine that opens the model;
- the template's PhysicsArea (Model Wizard category) is kept unchanged;
- complex interfaces (multiple fields, separate BC features) need manual
  editing of the generated file - the format reference is
  doc/pdf/COMSOL_Multiphysics/COMSOL_PhysicsBuilderManual.pdf.
"""

import os
import time
import xml.etree.ElementTree as ET
from typing import Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "resources", "physicsbuilder")
_TEMPLATE = os.path.join(_DATA_DIR, "template.mphphb")

# ~/.comsol/v63 - both the desktop and the mphserver read their own file
_PREFS_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), ".comsol", "v63", "comsol.prefs"),
    os.path.join(os.path.expanduser("~"), ".comsol", "v63", "comsolserver.prefs"),
]
_DEV_FILES_KEY = "physicsbuilder.settings.developmentfiles"
_USEBUILDER_KEY = "physicsbuilder.settings.usebuilder"

_PARAM_RENAMES = {"mu": "diffusivity", "V": "source", "psi0": "boundary"}
_PARAM_DEFAULTS = {
    "mu": ("D", "Diffusivity"),
    "V": ("f", "Source term"),
    "psi0": ("u0", "Boundary value"),
}


def _pv_edit(entity, key, newval):
    """Set the value of a direct propertyValue child; True if found."""
    for p in entity.findall("propertyValue"):
        if p.get("name") == key:
            p.set("value", newval)
            return True
    return False


def generate_mphphb(filepath: str, typename: str, label: str, field_var: str,
                    weak_expression: str, diffusivity_param: str = "D",
                    source_param: str = "f", bnd_param: str = "u0",
                    template: Optional[str] = None,
                    extra_substitutions: Optional[dict] = None) -> dict:
    """Generate a custom-physics .mphphb from the bundled template."""
    template = template or _TEMPLATE
    if not os.path.exists(template):
        # fall back to the demo shipped with the local COMSOL install
        template = os.path.join(os.environ.get("COMSOL_ROOT", r"F:\comsol\COMSOL63\Multiphysics"),
                                "demo", "builder", "SchrodingerEquation.mphphb")
    tree = ET.parse(template)
    root = tree.getroot()
    now = str(int(time.time() * 1000))
    for d in root.iter("Design"):
        d.set("name", typename + ".mphphb")
        d.set("modified", now)

    # drop the template's Eigenvalue-only study restriction (valueMatrix attr);
    # absence = default allowed studies, like the ThermoelectricEffect demo
    for e in root.iter("DesignEntity"):
        for p in list(e.findall("propertyValue")):
            if p.get("name") == "p:studyTypeSelection":
                e.remove(p)

    renames = {"mu": diffusivity_param, "V": source_param, "psi0": bnd_param}
    for e in root.iter("DesignEntity"):
        op = e.get("op")
        if op == "PhysicsInterface":
            e.set("name", label)
            _pv_edit(e, "p:type", typename)
            _pv_edit(e, "p:description", label)
            _pv_edit(e, "p:shortName", typename[:6].lower())
        elif op == "PhysicsDomainFeature":
            e.set("name", label + " Model")
            _pv_edit(e, "p:type", typename + "Equ")
        elif op == "FieldVariable":
            _pv_edit(e, "p:name", field_var)
            _pv_edit(e, "p:symbol", field_var)
            _pv_edit(e, "p:description", "Dependent variable " + field_var)
        elif op == "WeakEquation":
            _pv_edit(e, "p:expression", weak_expression)
        elif op == "UserInput":
            nm = None
            for p in e.findall("propertyValue"):
                if p.get("name") == "p:name":
                    nm = p.get("value")
            if nm in renames:
                for p in e.findall("propertyValue"):
                    if p.get("name") in ("p:name", "p:symbol"):
                        p.set("value", renames[nm])
                    elif p.get("name") == "p:defaultScalarValue":
                        p.set("value", "1")
                    elif p.get("name") == "p:quantity_builtin":
                        p.set("value", "dimensionless")
                    elif p.get("name") == "p:description":
                        p.set("value", _PARAM_DEFAULTS[nm][1])
        elif op == "InputSubDefinition":
            for p in e.findall("propertyValue"):
                v, nm = p.get("value"), p.get("name")
                if nm == "p:name" and v in renames:
                    p.set("value", renames[v])
                elif nm == "p:expression" and v:
                    for old, new in (("par.mu", "par." + diffusivity_param),
                                     ("par.V", "par." + source_param),
                                     ("par.psi0", "par." + bnd_param)):
                        if old in v:
                            v = v.replace(old, new)
                    p.set("value", v)

    # generic field-variable rename inside every expression-like value
    for p in root.iter("propertyValue"):
        v = p.get("value")
        if v and "psi" in v:
            p.set("value", v.replace("psi", field_var))
    if extra_substitutions:
        for p in root.iter("propertyValue"):
            v = p.get("value")
            if not v:
                continue
            for old, new in extra_substitutions.items():
                if old in v:
                    v = v.replace(old, new)
            p.set("value", v)

    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    tree.write(filepath, encoding="utf-8", xml_declaration=True)
    return {
        "file": filepath,
        "typename": typename,
        "label": label,
        "field_variable": field_var,
        "weak_expression": weak_expression,
        "params": {renames[k]: _PARAM_DEFAULTS[k][1] for k in _PARAM_RENAMES},
        "note": "register with physicsbuilder_register, then RESTART the COMSOL "
                "session and create with physics().create(tag, '%s', geom)" % typename,
    }


def _read_prefs(path):
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    return lines


def _get_pref(lines, key):
    for l in lines:
        if l.startswith(key + "="):
            return l[len(key) + 1:]
    return None


def _set_pref(lines, key, value):
    for i, l in enumerate(lines):
        if l.startswith(key + "="):
            lines[i] = key + "=" + value
            return lines
    lines.append(key + "=" + value)
    return lines


def _normalize_entry(e):
    """Java-properties unescape the pieces COMSOL writes (mainly \\:)."""
    return e.replace("\\:", ":").replace("\\\\", "\\")


def register_mphphb(filepath: str) -> dict:
    """Append a .mphphb to physicsbuilder.settings.developmentfiles in both
    comsol.prefs and comsolserver.prefs; ensure usebuilder=on. Duplicate
    entries break interface loading (verified live) - existing entries are
    unescaped and deduplicated before writing."""
    filepath = os.path.abspath(filepath).replace("\\", "/")
    changed = {}
    for prefs in _PREFS_CANDIDATES:
        if not os.path.exists(prefs):
            changed[prefs] = "not found (skipped)"
            continue
        lines = _read_prefs(prefs)
        cur = _get_pref(lines, _DEV_FILES_KEY) or ""
        entries = [_normalize_entry(e) for e in cur.split(",") if e.strip()]
        deduped = []
        for e in entries:
            if e not in deduped:
                deduped.append(e)
        entries = deduped
        if filepath not in entries:
            entries.append(filepath)
        _set_pref(lines, _DEV_FILES_KEY, ",".join(entries))
        _set_pref(lines, _USEBUILDER_KEY, "on")
        with open(prefs, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        changed[prefs] = {"developmentfiles": ",".join(entries), "usebuilder": "on"}
    return {"registered": filepath, "prefs": changed,
            "note": "restart the COMSOL session for the new interface to load"}


def physicsbuilder_status() -> dict:
    out = {}
    for prefs in _PREFS_CANDIDATES:
        if not os.path.exists(prefs):
            out[prefs] = "not found"
            continue
        lines = _read_prefs(prefs)
        out[prefs] = {
            "usebuilder": _get_pref(lines, _USEBUILDER_KEY),
            "developmentfiles": [_normalize_entry(e) for e in
                                 (_get_pref(lines, _DEV_FILES_KEY) or "").split(",")
                                 if e.strip()],
        }
    return out


def register_physicsbuilder_tools(mcp) -> None:
    """Register physicsbuilder tools with the MCP server."""

    @mcp.tool()
    def physicsbuilder_generate(filepath: str, typename: str, label: str,
                                field_variable: str, weak_expression: str,
                                diffusivity_param: str = "D",
                                source_param: str = "f",
                                boundary_param: str = "u0",
                                extra_substitutions: Optional[dict] = None) -> dict:
        """Generate a custom physics interface (.mphphb) from the validated
        template - then call physicsbuilder_register and restart the session.

        The template carries one dependent variable, one weak-form domain
        equation, two domain inputs (diffusivity D, source f), one Dirichlet
        boundary group (value u0) and a zero-flux default. Example weak form
        for steady diffusion: "D*\\u2207u\\u00b7test(\\u2207u)-f*test(u)".

        Args:
            filepath: output .mphphb path.
            typename: interface type for physics().create (CamelCase, e.g. "MyDiffusion").
            label: display name (e.g. "My Steady Diffusion").
            field_variable: dependent variable name (e.g. "u").
            weak_expression: COMSOL weak-form string referencing the field
                variable, diffusivity/source params, and test(...).
            diffusivity_param/source_param/boundary_param: rename the
                template inputs if you prefer other symbols.
            extra_substitutions: optional {old: new} replacements applied to
                all property values (e.g. retitle leftovers).
        """
        return generate_mphphb(filepath, typename, label, field_variable,
                               weak_expression, diffusivity_param,
                               source_param, boundary_param,
                               extra_substitutions=extra_substitutions)

    @mcp.tool()
    def physicsbuilder_register(filepath: str) -> dict:
        """Register a .mphphb as a development file in BOTH comsol.prefs and
        comsolserver.prefs (physicsbuilder.settings.developmentfiles +
        usebuilder=on). Restart the COMSOL session afterwards - registration
        is only read at session start. Models using the custom physics need
        it registered on every machine that opens them."""
        return register_mphphb(filepath)

    @mcp.tool()
    def physicsbuilder_status() -> dict:
        """Show current Physics Builder registration state (development files
        and usebuilder flag in comsol.prefs / comsolserver.prefs)."""
        return physicsbuilder_status()
