"""Study and solving tools for COMSOL MCP Server.

Naming convention (spec mcp_pr_c_fix_v2_spec.md §3.3, mirrors the mesh
tools): every ``study_*`` tool accepts a study by EITHER its Java tag
(e.g. ``std1``) OR its display label (e.g. ``Stationary Study``). The
shared ``_resolve_study`` helper prefers tag matches and falls back to
label matches; on miss it surfaces a unified error. Every response
also reports ``tag`` and ``label`` so callers do not have to guess
which form their input was.
"""

from typing import Any, Optional, Tuple
from mcp.server.fastmcp import FastMCP

from .session import session_manager
from ..async_handler.solver import async_solver


# Spec mcp_mesh_study_tools_spec.md §4.1 — alias to (step_tag, feature_type).
# Aliases are matched case-insensitively after stripping spaces/hyphens.
_STUDY_TYPE_TO_STEP = {
    "stationary":      ("stat",  "Stationary"),
    "transient":       ("time",  "Transient"),
    "time_dependent":  ("time",  "Transient"),
    "timedependent":   ("time",  "Transient"),
    "eigenfrequency":  ("eig",   "Eigenfrequency"),
    "frequency_domain": ("freq",  "Frequency"),
    "frequencydomain": ("freq",  "Frequency"),
    "frequency":       ("freq",  "Frequency"),
    "eigenvalue":      ("eigen", "Eigenvalue"),
}

# Reverse map: feature type string -> canonical alias key. Used when we
# read an existing step's type back from COMSOL.
_STEP_TYPE_TO_CANONICAL = {
    "Stationary":      "stationary",
    "Transient":       "transient",
    "Eigenfrequency":  "eigenfrequency",
    "Frequency":       "frequency_domain",
    "Eigenvalue":      "eigenvalue",
}


# Spec mcp_study_step_set_property_spec.md §3.3 — advisory hint placed on
# the ``step`` dict of every study_create / study_add_step response so
# agents discover the property setter immediately after step creation.
_STEP_SET_PROPERTY_HINT = (
    "Use study_step_set_property(<study>, <step_tag>, <prop>, <value>) "
    "to set tlist / atolglobal / rtol / etc. "
    "Reference: KB html_help_text/comsol/comsol_api_solver.50.48.md "
    "(Time properties) and comsol_api_solver.50.76.md "
    "(Time Dependent study step properties)."
)


def _coerce_step_property_value(value: Any) -> Any:
    """Coerce a Python value for ``study.feature(step).set(name, value)``.

    Spec §3.2 (ii): strings (including expression strings like
    ``range(12,-0.5,0)``) are passed through verbatim; tuples are
    normalized to lists (Java arrays are easier to construct from
    homogeneous Python lists under JPype); ints / floats / bools fall
    through to JPype's automatic boxing (``Boolean.TRUE``, etc.).

    Kept deliberately permissive — Java ``.set(String, Object)`` accepts
    just about anything, and the symmetric ``physics_set_property``
    passes its value straight through. Surfacing a Java exception
    (caught by the caller) is more useful than a Python-side
    pre-rejection when a property's expected type is undocumented.
    """
    if isinstance(value, tuple):
        return list(value)
    return value


def _safe_str(value, default: str = "") -> str:
    try:
        if value is None:
            return default
        return str(value)
    except Exception:
        return default


def _normalize_study_type(s: str) -> Optional[str]:
    if not isinstance(s, str) or not s.strip():
        return None
    norm = s.strip().lower().replace("-", "_").replace(" ", "_")
    return norm if norm in _STUDY_TYPE_TO_STEP else None


def _study_descriptors(jm) -> list:
    """Return ``[(study_obj, tag, label, type_alias, step_tags), ...]``
    for every study in the model. ``type_alias`` is one of the keys of
    ``_STUDY_TYPE_TO_STEP`` (best-effort) or ``None`` if unknown.
    """
    out: list = []
    try:
        for s in list(jm.study()):
            try:
                tag = _safe_str(s.tag())
            except Exception:
                tag = ""
            try:
                label = _safe_str(s.label())
            except Exception:
                label = ""
            step_tags: list = []
            primary_step_type = None
            try:
                for f in list(s.feature()):
                    try:
                        step_tags.append(_safe_str(f.tag()))
                    except Exception:
                        pass
                    if primary_step_type is None:
                        try:
                            primary_step_type = _safe_str(f.getType())
                        except Exception:
                            try:
                                primary_step_type = _safe_str(
                                    f.feature().info().type()
                                )
                            except Exception:
                                primary_step_type = None
            except Exception:
                pass
            type_alias = (
                _STEP_TYPE_TO_CANONICAL.get(primary_step_type)
                if primary_step_type else None
            )
            out.append((s, tag, label, type_alias, step_tags))
    except Exception:
        pass
    return out


def _study_tags(jm) -> list:
    """Return tag strings for all studies in the model."""
    return [d[1] for d in _study_descriptors(jm) if d[1]]


def _resolve_study(
    jm,
    study_name_or_label: Optional[str],
) -> Tuple:
    """Find a study by tag (preferred) or label.

    Spec mcp_pr_c_fix_v2_spec.md §3.3.

    Returns ``(study_obj, tag, label, type_alias, step_tags,
    error_str)``. On success ``error_str`` is None; on miss the other
    fields are None and ``error_str`` carries a human-readable message.
    Passing ``None`` / ``""`` returns an "all studies" sentinel
    (study_obj is None, error_str is None) so the caller can choose to
    iterate.
    """
    if study_name_or_label in (None, ""):
        return None, None, None, None, None, None

    descriptors = _study_descriptors(jm)
    if not descriptors:
        return None, None, None, None, None, (
            "No studies in the model. Create one with study_create first."
        )

    # Tag-first.
    for s, tag, label, type_alias, step_tags in descriptors:
        if tag == study_name_or_label:
            return s, tag, label, type_alias, step_tags, None

    # Label fallback.
    for s, tag, label, type_alias, step_tags in descriptors:
        if label == study_name_or_label:
            return s, tag, label, type_alias, step_tags, None

    return None, None, None, None, None, (
        f"Study not found: '{study_name_or_label}'. "
        "Pass the tag (e.g. 'std1') or the exact GUI label "
        "(e.g. 'Stationary Study')."
    )


def _auto_study_tag(jm, prefix: str = "std") -> str:
    """Generate a non-colliding study tag."""
    existing = set(_study_tags(jm))
    i = len(existing) + 1
    while f"{prefix}{i}" in existing:
        i += 1
    return f"{prefix}{i}"


def _step_tags(study_obj) -> list:
    """Return tag strings for all steps in a study."""
    tags = []
    try:
        for f in list(study_obj.feature()):
            try:
                tags.append(str(f.tag()))
            except Exception:
                continue
    except Exception:
        pass
    return tags


def _auto_step_tag(study_obj, base_tag: str) -> str:
    """Pick a non-colliding step tag based on a desired base."""
    existing = set(_step_tags(study_obj))
    if base_tag not in existing:
        return base_tag
    i = 2
    while f"{base_tag}{i}" in existing:
        i += 1
    return f"{base_tag}{i}"


def register_study_tools(mcp: FastMCP) -> None:
    """Register study and solving tools with the MCP server."""

    @mcp.tool()
    def study_list(model_name: Optional[str] = None) -> dict:
        """
        List all studies in a model.

        Spec mcp_pr_c_fix_v2_spec.md §3.3: response is a list of dicts
        with ``tag``, ``label``, ``type`` (canonical alias), and
        ``steps`` (step tags). The previous list-of-strings shape was
        too lossy — callers had to inspect studies separately to learn
        the tag.

        Args:
            model_name: Model name (default: current model).

        Returns:
            ``{success, studies: [{tag, label, type, steps}, ...],
                count}``.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            descriptors = _study_descriptors(jm)
            studies = [
                {
                    "tag": tag,
                    "label": label,
                    "type": type_alias,
                    "steps": step_tags,
                }
                for _s, tag, label, type_alias, step_tags in descriptors
            ]
            return {
                "success": True,
                "studies": studies,
                "count": len(studies),
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to list studies: {type(e).__name__}: {e}",
            }

    @mcp.tool()
    def study_solve(
        study_name: Optional[str] = None,
        model_name: Optional[str] = None,
        wait: bool = True,
        timeout: Optional[float] = None
    ) -> dict:
        """
        Solve a study (synchronous by default).

        Accepts a study tag (e.g. ``std1``) or label
        (e.g. ``Stationary Study``); see ``_resolve_study``.

        Args:
            study_name: Study tag or label (None for all studies).
            model_name: Model name (default: current model).
            wait: If True, wait for completion; if False, return
                immediately.
            timeout: Maximum wait time in seconds (only used if
                wait=True).

        Returns:
            ``{success, study: {tag, label, type} | None, message}``,
            or error.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        if async_solver.is_running:
            return {
                "success": False,
                "error": "Another solving operation is in progress. Use study_get_progress to check status."
            }

        try:
            jm = model.java
            _s, tag, label, type_alias, _steps, err = _resolve_study(jm, study_name)
            if err:
                return {"success": False, "error": err}

            # mph's model.solve(name) keys studies by node *name* (label).
            # When the caller passed a tag we resolve it here and feed
            # the label down. ``None`` flows through unchanged ("all").
            target_for_mph = label if tag else None
            study_payload = (
                {"tag": tag, "label": label, "type": type_alias}
                if tag else None
            )

            if wait:
                model.solve(target_for_mph)
                return {
                    "success": True,
                    "study": study_payload,
                    "message": "Solving completed.",
                }
            else:
                started = async_solver.start_solve(model, target_for_mph)
                if started:
                    return {
                        "success": True,
                        "study": study_payload,
                        "message": "Solving started in background. Use study_get_progress to monitor.",
                        "async": True,
                    }
                else:
                    return {
                        "success": False,
                        "error": "Failed to start async solver."
                    }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to solve: {type(e).__name__}: {e}",
            }

    @mcp.tool()
    def study_solve_async(
        study_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Start solving a study in the background (asynchronous).

        Use ``study_get_progress`` to monitor progress and
        ``study_cancel`` to stop. Accepts study tag or label.

        Args:
            study_name: Study tag or label (None for all studies).
            model_name: Model name (default: current model).

        Returns:
            Confirmation that solving started, or error message.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        if async_solver.is_running:
            progress = async_solver.get_progress()
            return {
                "success": False,
                "error": "Another solving operation is already in progress.",
                "current_progress": progress,
            }

        try:
            jm = model.java
            _s, tag, label, type_alias, _steps, err = _resolve_study(jm, study_name)
            if err:
                return {"success": False, "error": err}

            target_for_mph = label if tag else None
            study_payload = (
                {"tag": tag, "label": label, "type": type_alias}
                if tag else None
            )

            started = async_solver.start_solve(model, target_for_mph)
            if started:
                return {
                    "success": True,
                    "study": study_payload,
                    "model": model.name(),
                    "message": "Solving started in background.",
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to start async solver."
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to start solving: {type(e).__name__}: {e}",
            }

    @mcp.tool()
    def study_get_progress() -> dict:
        """
        Get the progress of the current solving operation.

        Returns:
            Progress information including status, percentage, and
            elapsed time.
        """
        progress = async_solver.get_progress()
        return {
            "success": True,
            "progress": progress,
        }

    @mcp.tool()
    def study_cancel() -> dict:
        """
        Cancel the current solving operation.

        Note: The solver may take a moment to respond to cancellation.

        Returns:
            Cancellation status.
        """
        if async_solver.cancel():
            return {
                "success": True,
                "message": "Cancellation requested. Solver will stop at next checkpoint.",
            }
        return {
            "success": False,
            "message": "No solving operation in progress.",
        }

    @mcp.tool()
    def study_wait(timeout: Optional[float] = None) -> dict:
        """
        Wait for the current solving operation to complete.

        Args:
            timeout: Maximum time to wait in seconds (None for indefinite).

        Returns:
            Final progress status.
        """
        completed = async_solver.wait(timeout=timeout)
        progress = async_solver.get_progress()

        return {
            "success": True,
            "completed": completed,
            "progress": progress,
        }

    @mcp.tool()
    def solutions_list(model_name: Optional[str] = None) -> dict:
        """
        List all solutions in a model.

        Args:
            model_name: Model name (default: current model)

        Returns:
            List of solution configurations.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            solutions = model.solutions()
            return {
                "success": True,
                "solutions": solutions,
                "count": len(solutions),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list solutions: {str(e)}"}

    @mcp.tool()
    def study_create(
        study_type: str = "stationary",
        name: Optional[str] = None,
        label: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Create a study and add a single solver step matching the given type.

        Supported study_type (case-insensitive, alias-tolerant):
          "stationary"       → Stationary
          "transient"        → Time Dependent
          "time_dependent"   → Time Dependent
          "eigenfrequency"   → Eigenfrequency
          "frequency_domain" → Frequency Domain
          "eigenvalue"       → Eigenvalue

        Args:
            study_type: Study type alias (see above).
            name: COMSOL tag for the study (auto-generated if None,
                e.g. 'std1').
            label: Display label (default: derived from study_type).
            model_name: Model name (default: current).

        Returns:
            ``{success, study: {tag, label, type, step: {tag, type}}}`` on
            success.
        """
        canonical = _normalize_study_type(study_type)
        if canonical is None:
            return {
                "success": False,
                "error": (
                    f"Unsupported study_type: '{study_type}'. "
                    f"Supported: {sorted(set(_STUDY_TYPE_TO_STEP.keys()))}"
                ),
            }

        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        step_tag, step_type = _STUDY_TYPE_TO_STEP[canonical]

        try:
            jm = model.java
            tag = name or _auto_study_tag(jm, "std")
            existing = set(_study_tags(jm))
            if tag in existing:
                return {
                    "success": False,
                    "error": f"Study tag '{tag}' already exists",
                }

            study = jm.study().create(tag)
            display_label = label or f"{step_type} Study"
            try:
                study.label(display_label)
            except Exception:
                pass

            actual_step_tag = _auto_step_tag(study, step_tag)
            study.create(actual_step_tag, step_type)

            return {
                "success": True,
                "study": {
                    "tag": tag,
                    "label": display_label,
                    "type": canonical,
                    "step": {
                        "tag": actual_step_tag,
                        "type": step_type,
                        # Spec mcp_study_step_set_property_spec.md §3.3:
                        # advisory hint so agents discover the setter.
                        "set_property_hint": _STEP_SET_PROPERTY_HINT,
                    },
                },
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to create study: {type(e).__name__}: {e}",
            }

    @mcp.tool()
    def study_add_step(
        study_name: str,
        step_type: str,
        step_tag: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Append an additional solver step to an existing study.

        Accepts study tag or label.

        Args:
            study_name: Tag or label of the existing study.
            step_type: Step type alias (same vocabulary as study_create:
                "stationary", "transient", "eigenfrequency", etc.).
            step_tag: Tag for the new step (auto if None).
            model_name: Model name (default: current).

        Returns:
            ``{success, study: {tag, label}, step: {tag, type}}`` on
            success.
        """
        canonical = _normalize_study_type(step_type)
        if canonical is None:
            return {
                "success": False,
                "error": (
                    f"Unsupported step_type: '{step_type}'. "
                    f"Supported: {sorted(set(_STUDY_TYPE_TO_STEP.keys()))}"
                ),
            }

        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        base_tag, feature_type = _STUDY_TYPE_TO_STEP[canonical]

        try:
            jm = model.java
            study, tag, label, _type, _steps, err = _resolve_study(jm, study_name)
            if err:
                return {"success": False, "error": err}

            actual_tag = step_tag or _auto_step_tag(study, base_tag)
            study.create(actual_tag, feature_type)

            return {
                "success": True,
                "study": {"tag": tag, "label": label},
                "step": {
                    "tag": actual_tag,
                    "type": feature_type,
                    # Spec mcp_study_step_set_property_spec.md §3.3.
                    "set_property_hint": _STEP_SET_PROPERTY_HINT,
                },
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to add study step: {type(e).__name__}: {e}",
            }

    @mcp.tool()
    def study_remove(
        study_name: str,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Remove a study from the model.

        Accepts study tag or label.

        Args:
            study_name: Tag or label of the study to remove.
            model_name: Model name (default: current).

        Returns:
            ``{success, removed: {tag, label}}`` on success.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            _s, tag, label, _type, _steps, err = _resolve_study(jm, study_name)
            if err:
                return {"success": False, "error": err}
            jm.study().remove(tag)
            return {
                "success": True,
                "removed": {"tag": tag, "label": label},
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to remove study: {type(e).__name__}: {e}",
            }

    @mcp.tool()
    def study_step_set_property(
        study_name: str,
        step_tag: str,
        property_name: str,
        value: Any,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Set a property on a study step's feature (canonical Java API path).

        Spec ``plans/mcp_study_step_set_property_spec.md`` §3.1, §3.2.

        Canonical Java path::

            model.study(<study_tag>).feature(<step_tag>)
                 .set(<property_name>, <value>)

        This is the symmetric counterpart of ``physics_set_property``, but
        uses the ``.feature()`` API path (vs ``.prop()`` for physics
        interface-level scalars). The most common use case is setting
        ``tlist`` on a time-dependent study step, but any of the ~30
        study step properties documented in the COMSOL Programming
        Reference can be set this way (atolglobal, rtol, useinitsol,
        useparam, plist, disabledphysics, mesh, ...).

        Common use cases:
        - Time-dependent ``tlist``::
            study_step_set_property("std1", "time", "tlist",
                                    "range(0,0.1,1)")
        - Backward-time integration (Black-Scholes, comsol_82)::
            study_step_set_property("std1", "time", "tlist",
                                    "range(12,-0.5,0)")
        - Absolute tolerance override::
            study_step_set_property("std1", "time", "atolglobal", "1e-6")
        - Output time control::
            study_step_set_property("std1", "time", "tout", "tsteps")
        - Stationary continuation parameter::
            study_step_set_property("std1", "stat", "useparam", True)

        Args:
            study_name: Study tag (e.g. ``"std1"``) or display label
                (e.g. ``"Transient Study"``). Resolved by
                ``_resolve_study`` — tag is preferred but label fallback
                works.
            step_tag: Study step tag — typically returned by
                ``study_create`` or ``study_add_step`` as
                ``response["study"]["step"]["tag"]`` (e.g. ``"time"`` for
                transient, ``"stat"`` for stationary). Pass it directly;
                no label fallback (study steps don't expose user-visible
                labels in the same way studies do).
            property_name: Property key as defined by COMSOL's Java API
                (e.g. ``"tlist"``, ``"atolglobal"``, ``"rtol"``,
                ``"initstep"``). See KB
                ``html_help_text/comsol/comsol_api_solver.50.48.md`` and
                ``comsol_api_solver.50.76.md`` for the full property table.
            value: Property value. Strings are passed through to Java
                verbatim (allowing expression syntax like
                ``"range(12,-0.5,0)"`` or ``"[0, 0.5, 1.0, 2.0]"``).
                Ints / floats / bools rely on JPype auto-boxing. Tuples
                are normalized to lists for predictable array conversion.
            model_name: Model name (default: current model).

        Returns:
            On success::

                {"success": True,
                 "study": {"tag": <resolved_tag>, "label": <study_label>},
                 "step": {"tag": <step_tag>},
                 "property": {"name": <property_name>, "value": <value>},
                 "java_path":
                     "model.study('<tag>').feature('<step>').set('<prop>', <value>)"}

            On failure (spec §3.2 (iii) — diagnostic ``attempted_java_path``)::

                {"success": False,
                 "error": <message>,
                 "attempted_java_path":
                     "model.study('<tag>').feature('<step>').set('<prop>', <value>)"}
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}",
            }

        try:
            jm = model.java
            study_obj, tag, label, _type, _steps, err = _resolve_study(
                jm, study_name,
            )
            if err:
                return {"success": False, "error": err}

            attempted_path = (
                f"model.study('{tag}').feature('{step_tag}')"
                f".set('{property_name}', <value>)"
            )

            try:
                feature = study_obj.feature(step_tag)
            except Exception as e:
                return {
                    "success": False,
                    "error": (
                        f"Step '{step_tag}' not found in study "
                        f"'{tag}': {type(e).__name__}: {e}"
                    ),
                    "attempted_java_path": (
                        f"model.study('{tag}').feature('{step_tag}')"
                    ),
                }

            if feature is None:
                return {
                    "success": False,
                    "error": (
                        f"Step '{step_tag}' not found in study '{tag}'."
                    ),
                    "attempted_java_path": (
                        f"model.study('{tag}').feature('{step_tag}')"
                    ),
                }

            java_value = _coerce_step_property_value(value)

            try:
                feature.set(property_name, java_value)
            except Exception as e:
                return {
                    "success": False,
                    "error": (
                        f"{attempted_path} failed: "
                        f"{type(e).__name__}: {e}"
                    ),
                    "attempted_java_path": attempted_path,
                }

            return {
                "success": True,
                "study": {"tag": tag, "label": label},
                "step": {"tag": step_tag},
                "property": {"name": property_name, "value": value},
                "java_path": attempted_path,
            }
        except Exception as e:
            return {
                "success": False,
                "error": (
                    f"Failed to set study step property: "
                    f"{type(e).__name__}: {e}"
                ),
            }

    @mcp.tool()
    def datasets_list(model_name: Optional[str] = None) -> dict:
        """
        List all datasets in a model.

        Datasets represent solution data that can be evaluated or visualized.

        Args:
            model_name: Model name (default: current model)

        Returns:
            List of dataset names.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            datasets = model.datasets()
            return {
                "success": True,
                "datasets": datasets,
                "count": len(datasets),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list datasets: {str(e)}"}
