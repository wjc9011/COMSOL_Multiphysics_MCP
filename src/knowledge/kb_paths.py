"""COMSOL61_KB path resolution.

Single source of truth for every path that points into the read-only KB
snapshot. Everything downstream (kb_catalog, kb_tools) must go through
these constants so that relocation only requires an env var flip.

Resolution order for the KB root:
    1. Env var ``COMSOL61_KB_PATH`` (if set and exists).
    2. Hard-coded default: ``C:\\Users\\LimLAB\\Documents\\COMSOL61_KB``.

No fallback to "walk up from __file__" — the spec pins the default.
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_KB_ROOT = Path(r"C:\Users\LimLAB\Documents\COMSOL61_KB")


def _resolve_kb_root() -> Path:
    env = os.environ.get("COMSOL61_KB_PATH", "").strip()
    if env:
        return Path(env)
    return _DEFAULT_KB_ROOT


KB_ROOT: Path = _resolve_kb_root()

CATALOGS_DIR: Path = KB_ROOT / "catalogs"
KNOWLEDGE_DIR: Path = KB_ROOT / "knowledge"
MANUALS_TEXT_DIR: Path = KNOWLEDGE_DIR / "manuals_text"

EXAMPLES_INDEX_MD: Path = KNOWLEDGE_DIR / "examples_index.md"
MODULES_OVERVIEW_MD: Path = KNOWLEDGE_DIR / "modules_overview.md"
MANIFEST_JSON: Path = KB_ROOT / "agent_manifest.json"

MANUALS_CATALOG_CSV: Path = CATALOGS_DIR / "manuals_catalog.csv"
EXAMPLES_CATALOG_CSV: Path = CATALOGS_DIR / "examples_catalog.csv"
SCRIPTS_CATALOG_CSV: Path = CATALOGS_DIR / "scripts_catalog.csv"


_REQUIRED_PATHS = [
    ("KB_ROOT", KB_ROOT, "dir"),
    ("CATALOGS_DIR", CATALOGS_DIR, "dir"),
    ("KNOWLEDGE_DIR", KNOWLEDGE_DIR, "dir"),
    ("MANUALS_TEXT_DIR", MANUALS_TEXT_DIR, "dir"),
    ("EXAMPLES_INDEX_MD", EXAMPLES_INDEX_MD, "file"),
    ("MODULES_OVERVIEW_MD", MODULES_OVERVIEW_MD, "file"),
    ("MANIFEST_JSON", MANIFEST_JSON, "file"),
    ("MANUALS_CATALOG_CSV", MANUALS_CATALOG_CSV, "file"),
    ("EXAMPLES_CATALOG_CSV", EXAMPLES_CATALOG_CSV, "file"),
    ("SCRIPTS_CATALOG_CSV", SCRIPTS_CATALOG_CSV, "file"),
]


def validate_kb_paths() -> list[str]:
    """Return a list of missing-or-wrong-type path names.

    Empty list means all required paths exist and have the expected type.
    The list items are human-readable strings, each naming one problem.
    """
    problems: list[str] = []
    for name, path, kind in _REQUIRED_PATHS:
        if not path.exists():
            problems.append(f"{name}: missing -> {path}")
            continue
        if kind == "dir" and not path.is_dir():
            problems.append(f"{name}: expected directory -> {path}")
        elif kind == "file" and not path.is_file():
            problems.append(f"{name}: expected file -> {path}")
    return problems


if __name__ == "__main__":
    print(f"KB_ROOT            = {KB_ROOT}")
    print(f"CATALOGS_DIR       = {CATALOGS_DIR}")
    print(f"KNOWLEDGE_DIR      = {KNOWLEDGE_DIR}")
    print(f"MANUALS_TEXT_DIR   = {MANUALS_TEXT_DIR}")
    print(f"EXAMPLES_INDEX_MD  = {EXAMPLES_INDEX_MD}")
    print(f"MODULES_OVERVIEW_MD= {MODULES_OVERVIEW_MD}")
    print(f"MANIFEST_JSON      = {MANIFEST_JSON}")
    print(f"MANUALS_CATALOG    = {MANUALS_CATALOG_CSV}")
    print(f"EXAMPLES_CATALOG   = {EXAMPLES_CATALOG_CSV}")
    print(f"SCRIPTS_CATALOG    = {SCRIPTS_CATALOG_CSV}")
    print()
    problems = validate_kb_paths()
    if not problems:
        print("validate_kb_paths: OK (all 10 paths present)")
    else:
        print(f"validate_kb_paths: {len(problems)} problem(s)")
        for p in problems:
            print(f"  - {p}")
