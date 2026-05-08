"""KB-side material lookup tools for COMSOL MCP Server.

Reads catalogs/materials_catalog.csv and materials/<source_lib>/*.json
from the COMSOL61_KB root. The catalog is produced by the KB-side
extraction script (see scripts/extract_basic_material_lib.py) and
need not exist for the server to start; missing-catalog responses
return a structured error so callers can guide the user.

Spec: plans/mcp_material_tools_spec.md §4.
"""

from __future__ import annotations

import csv
import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..tools.material import MATERIALS_CATALOG, MATERIALS_JSON_DIR


def _load_catalog_rows() -> tuple[list[dict], Optional[str]]:
    """Return (rows, error). rows is empty on missing/malformed catalog."""
    if not MATERIALS_CATALOG.exists():
        return [], (
            f"KB materials catalog not found at {MATERIALS_CATALOG}. "
            "Run the extraction script "
            "(scripts/extract_basic_material_lib.py) first."
        )

    rows: list[dict] = []
    try:
        with MATERIALS_CATALOG.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append({k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()})
    except Exception as e:
        return [], f"Failed to read catalog {MATERIALS_CATALOG}: {type(e).__name__}: {e}"
    return rows, None


def _summary_from_row(row: dict) -> dict:
    """Pick the headline properties from a catalog row."""
    summary_keys = (
        "thermalconductivity", "density", "heatcapacity",
        "youngsmodulus", "poissonsratio",
        "electricconductivity", "relpermittivity", "relpermeability",
        "refractiveindex", "ratioofspecificheat",
        "dynamicviscosity", "thermalexpansioncoefficient",
    )
    out = {}
    for k in summary_keys:
        v = row.get(k)
        if v not in (None, ""):
            out[k] = v
    return out


def _find_json_for(name: str, source_lib: Optional[str]) -> Optional[dict]:
    """Walk MATERIALS_JSON_DIR and return the matching record (or None)."""
    if not MATERIALS_JSON_DIR.exists():
        return None

    target_norm = name.replace(" ", "_").replace("/", "_")
    target_lower = name.lower()

    subdirs = []
    if source_lib:
        candidate = MATERIALS_JSON_DIR / source_lib
        if candidate.is_dir():
            subdirs.append(candidate)
    if not subdirs:
        subdirs = [d for d in MATERIALS_JSON_DIR.iterdir() if d.is_dir()]

    # Pass 1: exact filename match.
    for sub in subdirs:
        exact = sub / f"{target_norm}.json"
        if exact.exists():
            try:
                return json.loads(exact.read_text(encoding="utf-8"))
            except Exception:
                continue

    # Pass 2: case-insensitive name match across all candidate jsons.
    for sub in subdirs:
        for path in sub.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            nm = str(data.get("name", "")).lower()
            stem = path.stem.lower()
            stem_spaced = path.stem.replace("_", " ").lower()
            if target_lower in (nm, stem, stem_spaced):
                return data
    return None


def register_material_kb_tools(mcp: FastMCP) -> None:
    """Register KB-side material lookup tools (read-only)."""

    @mcp.tool()
    def kb_material_list(
        query: Optional[str] = None,
        source_lib: Optional[str] = None,
        has_property: Optional[str] = None,
        top_n: int = 50,
    ) -> dict:
        """
        List materials catalogued in the KB. Read-only — does not mutate
        any model.

        Args:
            query: Substring match on material name (case-insensitive).
            source_lib: Filter by source ('basic', etc.).
            has_property: Only return rows where this COMSOL key has a value.
            top_n: Max results (default 50).

        Returns:
            {success, materials: [...], catalog_path, total_in_catalog,
             returned}.
        """
        rows, err = _load_catalog_rows()
        if err and not rows:
            return {"success": False, "error": err}

        q_lower = query.lower() if query else None
        out: list[dict] = []
        for row in rows:
            name = row.get("name") or ""
            if q_lower and q_lower not in name.lower():
                continue
            if source_lib and row.get("source_lib") != source_lib:
                continue
            if has_property and not row.get(has_property):
                continue
            out.append({
                "name": name,
                "source_lib": row.get("source_lib"),
                "summary": _summary_from_row(row),
            })
            if len(out) >= top_n:
                break

        return {
            "success": True,
            "materials": out,
            "catalog_path": str(MATERIALS_CATALOG),
            "total_in_catalog": len(rows),
            "returned": len(out),
        }

    @mcp.tool()
    def kb_material_get(name: str) -> dict:
        """
        Get full property dump for a single material from the KB.

        Args:
            name: Material name as it appears in the catalog.

        Returns:
            {success, material: {name, source_lib, source_path,
             propertyGroups: {...}, extracted_at, ...}}.
        """
        rows, _ = _load_catalog_rows()
        catalog_row = None
        target_lower = name.lower()
        for row in rows:
            if (row.get("name") or "").lower() == target_lower:
                catalog_row = row
                break

        record = _find_json_for(name, catalog_row.get("source_lib") if catalog_row else None)
        if record is None:
            return {
                "success": False,
                "error": (
                    f"KB material not found: {name}. "
                    f"Looked under {MATERIALS_JSON_DIR}."
                ),
            }

        return {
            "success": True,
            "material": record,
            "catalog_row": catalog_row,
        }
