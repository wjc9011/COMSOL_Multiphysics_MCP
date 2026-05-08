"""Extract the COMSOL basic material library to the KB catalog + JSON dump.

Spec: plans/mcp_material_tools_spec.md §5, §6.

USAGE
-----
    # 1. Stop the comsol61-ops MCP server first (Ctrl+C the PowerShell
    #    that runs run_mcp_server.bat). mph holds the COMSOL license
    #    exclusively, so concurrent use will fail.
    # 2. Run this script:
    python scripts/extract_basic_material_lib.py
    # or with explicit destination:
    python scripts/extract_basic_material_lib.py --kb-root C:/path/to/COMSOL61_KB
    # 3. Restart the MCP server (run_mcp_server.bat).
    # 4. Toggle the comsol61-ops connector in the Claude client.

OUTPUTS
-------
    <KB_ROOT>/catalogs/materials_catalog.csv   (35 rows + header)
    <KB_ROOT>/materials/basic/<Material_Name>.json   (35 files)
    Updates <KB_ROOT>/agent_manifest.json (`materials` section).

This script is the only place in the repo that calls raw mph.Client();
all production tools go through src.tools.session.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Optional

import mph


DEFAULT_KB_ROOT = Path(__file__).resolve().parents[4]  # KB root
DEFAULT_LIB_PATH = Path(
    r"C:\Program Files\COMSOL\COMSOL61\Multiphysics\data\comsol_basic_material_lib.mph"
)

CATALOG_FIELDS = [
    "name", "source_lib", "source_path", "extracted_at",
    "thermalconductivity", "density", "heatcapacity",
    "youngsmodulus", "poissonsratio", "electricconductivity",
    "relpermittivity", "relpermeability", "refractiveindex",
    "ratioofspecificheat", "dynamicviscosity", "thermalexpansioncoefficient",
    "property_count_total",
]


def _safe_filename(name: str) -> str:
    """Mirror the resolution logic used by knowledge/material_kb_tools."""
    return name.replace(" ", "_").replace("/", "_").replace("\\", "_")


def _iter_property_groups(java_mat):
    """Yield (group_tag, props_dict) for every propertyGroup on a material.

    Falls back to Java introspection across mph versions.
    """
    try:
        for pg in list(java_mat.propertyGroup()):
            try:
                tag = str(pg.tag())
            except Exception:
                continue
            props: dict = {}
            try:
                prop_keys = list(pg.properties())
            except Exception:
                prop_keys = []
            for k in prop_keys:
                key = str(k)
                try:
                    v = pg.getString(key)
                except Exception:
                    try:
                        v = pg.get(key)
                    except Exception:
                        v = None
                if v is None:
                    continue
                props[key] = str(v)
            yield tag, props
    except Exception:
        return


def extract_one_material(java_mat) -> dict:
    """Return the per-material JSON record."""
    try:
        name = str(java_mat.label())
    except Exception:
        try:
            name = str(java_mat.tag())
        except Exception:
            name = "unknown"

    property_groups = {}
    for tag, props in _iter_property_groups(java_mat):
        property_groups[tag] = props

    return {
        "name": name,
        "tag": str(java_mat.tag()) if hasattr(java_mat, "tag") else None,
        "source_lib": "basic",
        "source_path": str(DEFAULT_LIB_PATH),
        "comsol_version": "6.1",
        "extracted_at": dt.datetime.now().isoformat(timespec="seconds"),
        "propertyGroups": property_groups,
    }


def collect_summary_for_catalog(record: dict) -> dict:
    """Pick headline scalar properties for the catalog CSV row."""
    pgs = record.get("propertyGroups") or {}
    flat = {}
    for grp, props in pgs.items():
        for k, v in props.items():
            flat.setdefault(k, v)

    summary_keys = [
        "thermalconductivity", "density", "heatcapacity",
        "youngsmodulus", "poissonsratio", "electricconductivity",
        "relpermittivity", "relpermeability", "refractiveindex",
        "ratioofspecificheat", "dynamicviscosity", "thermalexpansioncoefficient",
    ]
    return {k: flat.get(k, "") for k in summary_keys}


def update_agent_manifest(kb_root: Path, count: int, extracted_at: str) -> None:
    manifest_path = kb_root / "agent_manifest.json"
    if not manifest_path.exists():
        print(f"[warn] {manifest_path} not found; skipping manifest update.")
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] could not parse {manifest_path}: {e}; skipping update.")
        return

    manifest["materials"] = {
        "catalog_path": "catalogs/materials_catalog.csv",
        "json_dir": "materials/basic/",
        "source_lib": "comsol_basic_material_lib",
        "extracted_at": extracted_at,
        "count": count,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[ok] {manifest_path} updated (materials.count={count}).")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract COMSOL basic material library to KB catalog."
    )
    parser.add_argument(
        "--kb-root", type=Path, default=DEFAULT_KB_ROOT,
        help=f"KB root (default: {DEFAULT_KB_ROOT})",
    )
    parser.add_argument(
        "--library-path", type=Path, default=DEFAULT_LIB_PATH,
        help=f"comsol_basic_material_lib.mph (default: {DEFAULT_LIB_PATH})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing catalog/JSON files.",
    )
    parser.add_argument(
        "--no-manifest", action="store_true",
        help="Do not update agent_manifest.json.",
    )
    args = parser.parse_args()

    kb_root: Path = args.kb_root
    if not kb_root.exists():
        print(f"[err] KB root does not exist: {kb_root}")
        return 2
    catalog_path = kb_root / "catalogs" / "materials_catalog.csv"
    json_dir = kb_root / "materials" / "basic"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)

    if catalog_path.exists() and not args.force:
        print(
            f"[skip] {catalog_path} exists. Pass --force to overwrite."
        )
        return 0

    if not args.library_path.exists():
        print(f"[err] basic library not found: {args.library_path}")
        return 3

    print(f"[run] mph.start() — loading {args.library_path}")
    client = mph.start()
    try:
        model = client.load(str(args.library_path))
        try:
            java_mats = list(model.java.material())
        except Exception:
            # Some COMSOL builds expose materials only at the component
            # level inside library .mph files.
            java_mats = []
            for comp in list(model.java.component()):
                java_mats.extend(list(comp.material()))

        print(f"[info] discovered {len(java_mats)} materials.")
        records: list[dict] = []
        for jm in java_mats:
            try:
                rec = extract_one_material(jm)
            except Exception as e:
                print(f"[warn] failed to extract one material: {e}")
                continue
            records.append(rec)

        if not records:
            print("[err] no materials extracted; aborting.")
            return 4

        extracted_at = records[0]["extracted_at"]

        # Write per-material JSON
        for rec in records:
            fname = _safe_filename(rec["name"]) + ".json"
            out = json_dir / fname
            if out.exists() and not args.force:
                continue
            total = sum(len(g) for g in (rec.get("propertyGroups") or {}).values())
            rec["property_count_total"] = total
            out.write_text(
                json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8"
            )

        # Write catalog CSV
        with catalog_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CATALOG_FIELDS)
            writer.writeheader()
            for rec in records:
                summary = collect_summary_for_catalog(rec)
                row = {
                    "name": rec["name"],
                    "source_lib": rec["source_lib"],
                    "source_path": rec["source_path"],
                    "extracted_at": rec["extracted_at"],
                    "property_count_total": rec.get("property_count_total", 0),
                    **summary,
                }
                writer.writerow(row)

        print(f"[ok] wrote {len(records)} JSON files under {json_dir}")
        print(f"[ok] wrote catalog {catalog_path}")

        # Self-verification (spec §6.4)
        json_count = sum(1 for _ in json_dir.glob("*.json"))
        catalog_rows = 0
        with catalog_path.open(encoding="utf-8", newline="") as fh:
            for _ in csv.DictReader(fh):
                catalog_rows += 1
        ok = json_count == len(records) == catalog_rows
        print(
            f"[verify] json_files={json_count} records={len(records)} "
            f"catalog_rows={catalog_rows} ok={ok}"
        )

        if not args.no_manifest:
            update_agent_manifest(kb_root, len(records), extracted_at)

        return 0
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
