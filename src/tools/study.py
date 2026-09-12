"""Study and solving tools for COMSOL MCP Server."""

from typing import Optional
from mcp.server.fastmcp import FastMCP

from .session import session_manager
from ..async_handler.solver import async_solver


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
    def study_create(
        study_type: str = "Stationary",
        study_name: Optional[str] = None,
        model_name: Optional[str] = None,
        time_range: Optional[str] = None
    ) -> dict:
        """
        Create a new study in the model.

        Common study types:
        - "Stationary": Stationary study (most common for electrostatics, structural)
        - "TimeDependent": Time-dependent study
        - "Eigenfrequency": Eigenfrequency analysis
        - "Frequency": Frequency domain study
        - "Perturbation": Perturbation study

        Args:
            study_type: Type of study to create
            study_name: Optional name/tag for the study
            model_name: Model name (default: current model)
            time_range: Optional COMSOL range expression for a time-dependent
                step's output times, e.g. "range(0,1,60)". Stored in the step's
                tlist property.

        Returns:
            Created study info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            existing_studies = jm.study().size()
            study_tag = study_name or f"std{existing_studies + 1}"

            # COMSOL 6.3 spells study-step types out ("Stationary", "Transient",
            # ...); the short codes are not accepted as feature types.
            TYPE_MAP = {
                "Stationary": "Stationary",
                "TimeDependent": "Transient",
                "Transient": "Transient",
                "Eigenfrequency": "Eigenfrequency",
                "Frequency": "Frequency",
                "Perturbation": "Perturbation",
                "stat": "Stationary",
                "time": "Transient",
                "eig": "Eigenfrequency",
                "freq": "Frequency",
            }

            step_type = TYPE_MAP.get(study_type, study_type)

            study = jm.study().create(study_tag)
            step = study.create("step1", step_type)

            applied_time_range = None
            time_range_error = None
            if time_range:
                try:
                    step.set("tlist", time_range)
                    applied_time_range = time_range
                except Exception as e:
                    time_range_error = str(e)

            result = {
                "success": True,
                "study": study_tag,
                "type": study_type,
                "step_type": step_type,
                "model": model.name(),
            }
            if time_range:
                result["time_range"] = applied_time_range
                result["time_range_error"] = time_range_error
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to create study: {str(e)}"}
    
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
