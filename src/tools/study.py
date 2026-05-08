"""Study and solving tools for COMSOL MCP Server."""

from typing import Optional
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


def _normalize_study_type(s: str) -> Optional[str]:
    if not isinstance(s, str) or not s.strip():
        return None
    norm = s.strip().lower().replace("-", "_").replace(" ", "_")
    return norm if norm in _STUDY_TYPE_TO_STEP else None


def _study_tags(jm) -> list:
    """Return tag strings for all studies in the model."""
    tags = []
    try:
        for s in list(jm.study()):
            try:
                tags.append(str(s.tag()))
            except Exception:
                continue
    except Exception:
        pass
    return tags


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
        
        Args:
            model_name: Model name (default: current model)
        
        Returns:
            List of study names with their types
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            studies = model.studies()
            
            study_info = []
            for study_name in studies:
                info = {"name": study_name}
                try:
                    study_node = model / "studies" / study_name
                    children = [child.name() for child in study_node.children()]
                    info["steps"] = children
                except Exception:
                    pass
                study_info.append(info)
            
            return {
                "success": True,
                "studies": study_info,
                "count": len(study_info),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list studies: {str(e)}"}
    
    @mcp.tool()
    def study_solve(
        study_name: Optional[str] = None,
        model_name: Optional[str] = None,
        wait: bool = True,
        timeout: Optional[float] = None
    ) -> dict:
        """
        Solve a study (synchronous by default).
        
        Args:
            study_name: Study to solve (None for all studies)
            model_name: Model name (default: current model)
            wait: If True, wait for completion; if False, return immediately
            timeout: Maximum wait time in seconds (only used if wait=True)
        
        Returns:
            Solution status, or error message
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
            if wait:
                model.solve(study_name)
                return {
                    "success": True,
                    "study": study_name,
                    "message": "Solving completed.",
                }
            else:
                started = async_solver.start_solve(model, study_name)
                if started:
                    return {
                        "success": True,
                        "study": study_name,
                        "message": "Solving started in background. Use study_get_progress to monitor.",
                        "async": True,
                    }
                else:
                    return {
                        "success": False,
                        "error": "Failed to start async solver."
                    }
        except Exception as e:
            return {"success": False, "error": f"Failed to solve: {str(e)}"}
    
    @mcp.tool()
    def study_solve_async(
        study_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Start solving a study in the background (asynchronous).
        
        Use study_get_progress to monitor progress and study_cancel to stop.
        
        Args:
            study_name: Study to solve (None for all studies)
            model_name: Model name (default: current model)
        
        Returns:
            Confirmation that solving started, or error message
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
            started = async_solver.start_solve(model, study_name)
            if started:
                return {
                    "success": True,
                    "study": study_name,
                    "model": model.name(),
                    "message": "Solving started in background.",
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to start async solver."
                }
        except Exception as e:
            return {"success": False, "error": f"Failed to start solving: {str(e)}"}
    
    @mcp.tool()
    def study_get_progress() -> dict:
        """
        Get the progress of the current solving operation.
        
        Returns:
            Progress information including status, percentage, and elapsed time
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
            Cancellation status
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
            timeout: Maximum time to wait in seconds (None for indefinite)
        
        Returns:
            Final progress status
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
            List of solution configurations
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
            {success, study: {tag, label, type, step: {tag, type}}} on
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
                    "step": {"tag": actual_step_tag, "type": step_type},
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

        Args:
            study_name: Tag of the existing study (e.g. 'std1').
            step_type: Step type alias (same vocabulary as study_create:
                "stationary", "transient", "eigenfrequency", etc.).
            step_tag: Tag for the new step (auto if None).
            model_name: Model name (default: current).

        Returns:
            {success, study, step: {tag, type}} on success.
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
            study = jm.study(study_name)
            if study is None:
                return {
                    "success": False,
                    "error": f"Study not found: {study_name}",
                }

            actual_tag = step_tag or _auto_step_tag(study, base_tag)
            study.create(actual_tag, feature_type)

            return {
                "success": True,
                "study": study_name,
                "step": {"tag": actual_tag, "type": feature_type},
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

        Args:
            study_name: Tag of the study to remove.
            model_name: Model name (default: current).

        Returns:
            {success, removed} on success.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            if study_name not in set(_study_tags(jm)):
                return {
                    "success": False,
                    "error": f"Study not found: {study_name}",
                }
            jm.study().remove(study_name)
            return {"success": True, "removed": study_name}
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to remove study: {type(e).__name__}: {e}",
            }

    @mcp.tool()
    def datasets_list(model_name: Optional[str] = None) -> dict:
        """
        List all datasets in a model.
        
        Datasets represent solution data that can be evaluated or visualized.
        
        Args:
            model_name: Model name (default: current model)
        
        Returns:
            List of dataset names
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
