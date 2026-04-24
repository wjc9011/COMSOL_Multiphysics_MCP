"""Catalog loader + MD front-matter parser for COMSOL61_KB.

Stdlib-only (csv, yaml, pathlib, time). Lazy single-shot load: the three
CSVs are read on the first query and then held for the life of the
process. Matches wjc9011's ``SessionManager`` singleton pattern.

Public surface:
    KBCatalog                       — the loader/query class
    read_md_with_frontmatter(path)  — standalone MD utility
    _normalize_module_name(name)    — lower/underscore fuzzy form

Schemas (headers from the actual 2026-04-24 snapshot):
    manuals_catalog.csv   : filename,path,size_MB,module,doc_type
    examples_catalog.csv  : filename,path,file_type,module,subcategory,
                            has_pdf_doc,pdf_doc_path,has_m_script,
                            m_script_path,has_thumbnail,thumbnail_path,
                            size_MB,is_application_example
    scripts_catalog.csv   : filename,path,language,module,
                            associated_mph,associated_mph_path,size_KB

String booleans ("True"/"False"/"") are normalized to real bools on
load; sizes to floats when possible, leaving "" as None.
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

import yaml

from . import kb_paths


_BOOL_FIELDS_EXAMPLES = ("has_pdf_doc", "has_m_script", "has_thumbnail",
                         "is_application_example")
_FLOAT_FIELDS = ("size_MB", "size_KB")

# Non-module sentinels that appear in the `module` field of
# examples/scripts catalogs (CLAUDE.md §6). Filtered out of
# get_module_list when exclude_non_modules=True.
_NON_MODULE_NAMES = frozenset({"addins", "parts", "data", "demo"})


def _to_bool(value: str) -> Optional[bool]:
    """Parse CSV bool-ish strings.

    Returns True/False for "True"/"False" (case-insensitive), None for
    empty or anything else. None preserves "unknown" distinct from False.
    """
    if value is None:
        return None
    v = value.strip().lower()
    if v == "true":
        return True
    if v == "false":
        return False
    return None


def _to_float(value: str) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _normalize_module_name(name: Optional[str]) -> str:
    """Lower / underscore form for fuzzy module-name comparisons.

    Keeps the §6 convention tokens intact; only squashes case,
    whitespace, and dashes so that 'heat transfer module',
    'Heat-Transfer-Module', and 'Heat_Transfer_Module' all compare equal.
    The canonical stored form (Title_Case_With_Underscores) is *not*
    altered in the catalog rows — normalization is only for matching.
    """
    if not name:
        return ""
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def read_md_with_frontmatter(path: str | Path) -> tuple[dict, str]:
    """Parse a markdown file with optional YAML front matter.

    Returns ``(meta, body)``:
      - If the file starts with a ``---\\n`` line, everything up to the
        matching closing ``---`` is parsed as YAML into ``meta``.
      - Otherwise ``meta`` is ``{}`` and ``body`` is the whole file.
      - On YAML parse failure, prints a warning to stderr and returns
        ``({}, full_text)``. Never raises on malformed front matter.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")

    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].rstrip("\r\n") == "---":
        return {}, text

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end_idx = i
            break
    if end_idx == -1:
        return {}, text

    yaml_src = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1:])
    try:
        meta = yaml.safe_load(yaml_src) or {}
        if not isinstance(meta, dict):
            print(f"[kb_catalog] warning: front matter in {p} is not a "
                  f"dict; ignored", file=sys.stderr)
            meta = {}
    except yaml.YAMLError as e:
        print(f"[kb_catalog] warning: YAML parse failed for {p}: {e}",
              file=sys.stderr)
        meta = {}
        body = text
    return meta, body


class KBCatalog:
    """Lazy in-memory view of the three catalog CSVs.

    Thread-unsafe by design — one instance per process, mirrors the
    upstream ``SessionManager`` singleton pattern. First query triggers
    disk I/O; subsequent queries are pure filter over Python lists.
    """

    def __init__(self) -> None:
        self._manuals: Optional[list[dict]] = None
        self._examples: Optional[list[dict]] = None
        self._scripts: Optional[list[dict]] = None
        self._load_wall_seconds: dict[str, float] = {}

    def _load_csv(self, path: Path, bool_fields: Iterable[str] = ()
                  ) -> list[dict]:
        rows: list[dict] = []
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                for f in bool_fields:
                    if f in raw:
                        raw[f] = _to_bool(raw[f])
                for f in _FLOAT_FIELDS:
                    if f in raw:
                        raw[f] = _to_float(raw[f])
                rows.append(raw)
        return rows

    def _ensure_loaded(self) -> None:
        if self._manuals is None:
            t0 = time.perf_counter()
            self._manuals = self._load_csv(kb_paths.MANUALS_CATALOG_CSV)
            self._load_wall_seconds["manuals"] = time.perf_counter() - t0
        if self._examples is None:
            t0 = time.perf_counter()
            self._examples = self._load_csv(
                kb_paths.EXAMPLES_CATALOG_CSV,
                bool_fields=_BOOL_FIELDS_EXAMPLES,
            )
            self._load_wall_seconds["examples"] = time.perf_counter() - t0
        if self._scripts is None:
            t0 = time.perf_counter()
            self._scripts = self._load_csv(kb_paths.SCRIPTS_CATALOG_CSV)
            self._load_wall_seconds["scripts"] = time.perf_counter() - t0

    @property
    def load_wall_seconds(self) -> dict[str, float]:
        return dict(self._load_wall_seconds)

    def _module_matches(self, row_module: str,
                        needle: Optional[str]) -> bool:
        if not needle:
            return True
        return _normalize_module_name(row_module) == \
            _normalize_module_name(needle)

    def filter_manuals(self, module: Optional[str] = None,
                       doc_type: Optional[str] = None) -> list[dict]:
        """Return manuals rows matching the optional filters."""
        self._ensure_loaded()
        assert self._manuals is not None
        out = []
        for row in self._manuals:
            if not self._module_matches(row.get("module", ""), module):
                continue
            if doc_type and row.get("doc_type") != doc_type:
                continue
            out.append(row)
        return out

    def filter_examples(self, module: Optional[str] = None,
                        subcategory: Optional[str] = None,
                        has_pdf_doc: Optional[bool] = None
                        ) -> list[dict]:
        """Return examples rows matching the optional filters."""
        self._ensure_loaded()
        assert self._examples is not None
        out = []
        for row in self._examples:
            if not self._module_matches(row.get("module", ""), module):
                continue
            if subcategory and row.get("subcategory") != subcategory:
                continue
            if has_pdf_doc is not None and \
                    row.get("has_pdf_doc") != has_pdf_doc:
                continue
            out.append(row)
        return out

    def get_example_by_mph(self, mph_path: str | Path
                           ) -> Optional[dict]:
        """2-pass lookup. First pass: exact full path match.
        Second pass: basename match (fallback).
        Reason: 48 cross-module duplicates exist; exact path required
        to disambiguate.

        Accepts either the full ``path`` from the catalog or just the
        filename (e.g. ``li_ion_battery_impedance.mph``). Returns None
        when neither pass resolves. See CLAUDE.md §7.3.
        """
        self._ensure_loaded()
        assert self._examples is not None
        key = str(mph_path).strip()
        if not key:
            return None
        key_norm = key.replace("/", "\\").lower()
        key_base = Path(key).name.lower()

        # Pass 1: exact full-path match (preferred for cross-module dupes)
        for row in self._examples:
            if (row.get("path") or "").lower() == key_norm:
                return row
        # Pass 2: basename fallback
        for row in self._examples:
            if (row.get("filename") or "").lower() == key_base:
                return row
        return None

    def get_module_list(self, exclude_non_modules: bool = True
                        ) -> list[str]:
        """Unique sorted list of modules seen across all three catalogs.

        When ``exclude_non_modules`` is True (default), the four
        sentinel buckets from CLAUDE.md §6 (``addins``, ``parts``,
        ``data``, ``demo``) are filtered out so callers get a clean
        module-only roster suitable for `module_filter` arguments.
        """
        self._ensure_loaded()
        assert self._manuals is not None
        assert self._examples is not None
        assert self._scripts is not None
        names: set[str] = set()
        for row in self._manuals:
            m = row.get("module")
            if m:
                names.add(m)
        for row in self._examples:
            m = row.get("module")
            if m:
                names.add(m)
        for row in self._scripts:
            m = row.get("module")
            if m:
                names.add(m)
        if exclude_non_modules:
            names -= _NON_MODULE_NAMES
        return sorted(names)


# ----- __main__ smoke tests (no pytest dependency) -----

def _main() -> int:
    print(f"[smoke] KB_ROOT = {kb_paths.KB_ROOT}")
    problems = kb_paths.validate_kb_paths()
    if problems:
        print(f"[smoke] KB path problems: {problems}")
        return 2

    cat = KBCatalog()

    # Trigger lazy load via one query that needs everything.
    t0 = time.perf_counter()
    module_list = cat.get_module_list()
    total = time.perf_counter() - t0
    print(f"[smoke] total wall to first query: {total*1000:.1f} ms")
    for name, seconds in cat.load_wall_seconds.items():
        print(f"[smoke]   load {name:10s}: {seconds*1000:.1f} ms")
    print(f"[smoke] unique modules across catalogs: {len(module_list)}")
    print(f"[smoke] first 5: {module_list[:5]}")

    heat_manuals = cat.filter_manuals(module="Heat_Transfer_Module")
    print(f"[smoke] Heat_Transfer_Module manuals: {len(heat_manuals)}")
    if heat_manuals:
        by_type: dict[str, int] = {}
        for r in heat_manuals:
            by_type[r.get("doc_type", "")] = \
                by_type.get(r.get("doc_type", ""), 0) + 1
        print(f"[smoke]   doc_type breakdown: {by_type}")

    heat_examples_doc = cat.filter_examples(
        module="Heat_Transfer_Module", has_pdf_doc=True)
    heat_examples_all = cat.filter_examples(module="Heat_Transfer_Module")
    print(f"[smoke] Heat_Transfer_Module examples total:   "
          f"{len(heat_examples_all)}")
    print(f"[smoke] Heat_Transfer_Module examples w/ PDF:  "
          f"{len(heat_examples_doc)}")

    fuzzy = cat.filter_manuals(module="heat-transfer-module")
    print(f"[smoke] fuzzy module 'heat-transfer-module' hits: "
          f"{len(fuzzy)}  (expected equal to exact-case: "
          f"{len(heat_manuals)})")

    # Pick any manual MD that exists for a front-matter parse demo.
    sample_md = None
    for m in heat_manuals:
        guess = kb_paths.MANUALS_TEXT_DIR / "modules" / \
            "Heat_Transfer_Module" / m["filename"].replace(".pdf", ".md")
        if guess.exists():
            sample_md = guess
            break
    if sample_md is None:
        # Fallback: walk the dir and grab first .md
        for p in (kb_paths.MANUALS_TEXT_DIR / "modules" /
                  "Heat_Transfer_Module").glob("*.md"):
            sample_md = p
            break
    if sample_md is not None:
        meta, body = read_md_with_frontmatter(sample_md)
        print(f"[smoke] parsed front matter of: {sample_md.name}")
        print(f"[smoke]   meta keys: {sorted(meta.keys())}")
        print(f"[smoke]   body length: {len(body)} chars "
              f"(first 60: {body[:60]!r})")
    else:
        print("[smoke] no Heat_Transfer MD found to parse — skipped")

    by_mph = cat.get_example_by_mph("lithium_battery_pouch_3d.mph")
    print(f"[smoke] get_example_by_mph('lithium_battery_pouch_3d.mph') "
          f"-> {'hit' if by_mph else 'miss'}")
    if by_mph:
        print(f"[smoke]   module={by_mph.get('module')} "
              f"has_pdf_doc={by_mph.get('has_pdf_doc')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
