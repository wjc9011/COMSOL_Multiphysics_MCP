"""KB-backed MCP tools for COMSOL61_KB.

Four tools, all catalog/MD-driven. No heavy deps — stdlib + PyYAML only.
Intentionally does not import from upstream modules (embedded.py /
retriever.py / pdf_processor.py): the knowledge layer we bring is
self-contained so upstream-main→comsol61-kb merges stay trivial.

Public entry point: ``register_kb_tools(mcp: FastMCP) -> None``.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

from mcp.server.fastmcp import FastMCP

from . import kb_paths
from .kb_catalog import (
    KBCatalog,
    _normalize_module_name,
    read_md_with_frontmatter,
)


# ---------- tokenization / scoring ----------

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Lowercased alnum tokens, len >= 2. Empty input -> []."""
    if not text:
        return []
    toks = [m.group(0).lower() for m in _WORD_RE.finditer(text)]
    return [t for t in toks if len(t) >= 2]


def _and_hit_score(haystack_lower: str, tokens: list[str]
                   ) -> tuple[bool, int, int]:
    """Return (all_tokens_present, total_hit_count, first_position).

    ``first_position`` is the earliest index of any token in the
    haystack, or -1 when not all tokens match. A plain ``str.count``
    sweep; good enough for snippet extraction.
    """
    total = 0
    first_pos = -1
    for tok in tokens:
        cnt = haystack_lower.count(tok)
        if cnt == 0:
            return False, 0, -1
        total += cnt
        idx = haystack_lower.find(tok)
        if first_pos == -1 or idx < first_pos:
            first_pos = idx
    return True, total, first_pos


def _extract_snippet(body: str, tokens: list[str],
                     radius_before: int = 80,
                     radius_after: int = 120) -> str:
    """Text excerpt around the first matching token, space-aligned."""
    if not tokens or not body:
        return ""
    lower = body.lower()
    first_pos = -1
    first_tok_len = 0
    for tok in tokens:
        idx = lower.find(tok)
        if idx == -1:
            continue
        if first_pos == -1 or idx < first_pos:
            first_pos = idx
            first_tok_len = len(tok)
    if first_pos == -1:
        return ""
    start = max(0, first_pos - radius_before)
    end = min(len(body), first_pos + first_tok_len + radius_after)
    # Expand to nearest whitespace boundaries so we don't cut words.
    while start > 0 and not body[start - 1].isspace():
        start -= 1
    while end < len(body) and not body[end].isspace():
        end += 1
    snippet = body[start:end].replace("\n", " ").replace("\r", " ")
    snippet = re.sub(r"\s{2,}", " ", snippet).strip()
    return "..." + snippet + "..."


# ---------- manuals MD index ----------

class _MDIndex:
    """Lazy path-only index of ``manuals_text/`` grouped by module.

    On first access, walks the tree once and reads just each file's YAML
    front matter (first ~4 KB) to capture module/doc_type. The full
    body is only read on demand during a search. This keeps memory low
    (about 1,523 small dicts) while enabling module-scoped scanning.
    """

    def __init__(self) -> None:
        self._by_module: Optional[dict[str, list[dict]]] = None
        self._build_seconds: float = 0.0

    @staticmethod
    def _quick_frontmatter(path: Path) -> dict:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            return {}
        if not head.startswith("---"):
            return {}
        lines = head.splitlines(keepends=True)
        if not lines or lines[0].rstrip("\r\n") != "---":
            return {}
        end_idx = -1
        for i in range(1, len(lines)):
            if lines[i].rstrip("\r\n") == "---":
                end_idx = i
                break
        if end_idx == -1:
            return {}
        try:
            meta = yaml.safe_load("".join(lines[1:end_idx])) or {}
        except yaml.YAMLError:
            meta = {}
        return meta if isinstance(meta, dict) else {}

    def _build(self) -> None:
        t0 = time.perf_counter()
        self._by_module = {}
        for p in kb_paths.MANUALS_TEXT_DIR.rglob("*.md"):
            meta = self._quick_frontmatter(p)
            module = meta.get("module") or ""
            rec = {
                "path": p,
                "module": module,
                "doc_type": meta.get("doc_type") or "",
                "source_filename": meta.get("source_filename") or p.name,
            }
            self._by_module.setdefault(module, []).append(rec)
        self._build_seconds = time.perf_counter() - t0

    @property
    def build_seconds(self) -> float:
        return self._build_seconds

    def get_records(self, module: Optional[str]) -> list[dict]:
        if self._by_module is None:
            self._build()
        assert self._by_module is not None
        if not module:
            return [r for rs in self._by_module.values() for r in rs]
        needle = _normalize_module_name(module)
        out: list[dict] = []
        for k, rs in self._by_module.items():
            if _normalize_module_name(k) == needle:
                out.extend(rs)
        return out

    def total_indexed(self) -> int:
        if self._by_module is None:
            self._build()
        assert self._by_module is not None
        return sum(len(v) for v in self._by_module.values())


# ---------- module-level singletons (populated by register_kb_tools) ----

_CATALOG: Optional[KBCatalog] = None
_MD_INDEX: Optional[_MDIndex] = None


def _ensure_state() -> None:
    global _CATALOG, _MD_INDEX
    if _CATALOG is None:
        _CATALOG = KBCatalog()
    if _MD_INDEX is None:
        _MD_INDEX = _MDIndex()


def _rel_to_kb(path: Path) -> str:
    try:
        return str(path.relative_to(kb_paths.KB_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _strip_frontmatter_inline(text: str) -> str:
    """Cheap variant of read_md_with_frontmatter — body only, no dict."""
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return "".join(lines[i + 1:])
    return text


# ---------- tool implementations (plain functions, testable) ----------

def _impl_search_manuals(query: str,
                         module_filter: Optional[str],
                         top_n: int) -> dict:
    tokens = _tokenize(query)
    if not tokens:
        return {"success": False,
                "error": "empty or too-short query (need tokens of len >= 2)",
                "query": query, "results": []}
    _ensure_state()
    assert _MD_INDEX is not None
    records = _MD_INDEX.get_records(module_filter)

    scored: list[tuple[int, dict, str]] = []
    for rec in records:
        try:
            text = rec["path"].read_text(encoding="utf-8",
                                         errors="replace")
        except OSError:
            continue
        body = _strip_frontmatter_inline(text)
        ok, total, _first = _and_hit_score(body.lower(), tokens)
        if not ok:
            continue
        scored.append((total, rec, body))

    scored.sort(key=lambda t: (
        -t[0],
        (t[1].get("module") or "").lower(),
        (t[1].get("source_filename") or "").lower(),
    ))

    results = []
    for total, rec, body in scored[: max(0, int(top_n))]:
        results.append({
            "path": _rel_to_kb(rec["path"]),
            "module": rec.get("module", ""),
            "doc_type": rec.get("doc_type", ""),
            "source_filename": rec.get("source_filename", ""),
            "score": total,
            "snippet": _extract_snippet(body, tokens),
        })

    return {
        "success": True,
        "query": query,
        "module_filter": module_filter,
        "tokens": tokens,
        "candidates_scanned": len(records),
        "hit_count": len(scored),
        "results": results,
    }


def _impl_search_examples(query: str,
                          module_filter: Optional[str],
                          top_n: int) -> dict:
    tokens = _tokenize(query)
    if not tokens:
        return {"success": False,
                "error": "empty or too-short query (need tokens of len >= 2)",
                "query": query, "results": []}
    _ensure_state()
    assert _CATALOG is not None
    rows = _CATALOG.filter_examples(module=module_filter)

    scored: list[tuple[int, dict]] = []
    for row in rows:
        stem = Path(row.get("filename") or "").stem
        haystack = " ".join([
            row.get("filename") or "",
            stem,
            stem.replace("_", " "),
            row.get("subcategory") or "",
            row.get("module") or "",
        ]).lower()
        ok, total, _first = _and_hit_score(haystack, tokens)
        if ok:
            scored.append((total, row))

    scored.sort(key=lambda t: (
        -t[0],
        (t[1].get("module") or "").lower(),
        (t[1].get("filename") or "").lower(),
    ))

    results = []
    for total, row in scored[: max(0, int(top_n))]:
        results.append({
            "mph_path": row.get("path") or "",
            "filename": row.get("filename") or "",
            "module": row.get("module") or "",
            "subcategory": row.get("subcategory") or "",
            "has_pdf_doc": bool(row.get("has_pdf_doc")),
            "pdf_doc_path": row.get("pdf_doc_path") or "",
            "size_MB": row.get("size_MB"),
            "score": total,
        })

    return {
        "success": True,
        "query": query,
        "module_filter": module_filter,
        "tokens": tokens,
        "candidates_scanned": len(rows),
        "hit_count": len(scored),
        "results": results,
    }


def _resolve_example_md(row: dict) -> Optional[Path]:
    """Locate the companion MD in knowledge/manuals_text/examples/<Module>/.

    Tries the pdf_doc_path basename first (replacing .pdf -> .md), then
    falls back to the .mph basename. Returns None if neither exists.
    """
    module = row.get("module") or ""
    if not module:
        return None
    base_dir = kb_paths.MANUALS_TEXT_DIR / "examples" / module

    pdf_raw = row.get("pdf_doc_path") or ""
    if pdf_raw:
        md_name = Path(pdf_raw).stem + ".md"
        cand = base_dir / md_name
        if cand.is_file():
            return cand

    mph_name = row.get("filename") or ""
    if mph_name:
        md_name = Path(mph_name).stem + ".md"
        cand = base_dir / md_name
        if cand.is_file():
            return cand
    return None


def _impl_get_example_detail(mph_path: str,
                             offset: int,
                             length: int) -> dict:
    _ensure_state()
    assert _CATALOG is not None
    row = _CATALOG.get_example_by_mph(mph_path)
    if row is None:
        return {
            "success": False,
            "error": f"no example matches: {mph_path}",
            "mph_path": mph_path,
            "module": "",
            "subcategory": "",
            "has_doc": False,
            "doc_total_chars": 0,
            "doc_excerpt": "",
            "truncated": False,
        }

    module = row.get("module") or ""
    subcategory = row.get("subcategory") or ""
    has_doc_flag = bool(row.get("has_pdf_doc"))
    result: dict = {
        "success": True,
        "mph_path": row.get("path") or mph_path,
        "mph_filename": row.get("filename") or Path(mph_path).name,
        "module": module,
        "subcategory": subcategory,
        "has_doc": False,
        "doc_total_chars": 0,
        "doc_excerpt": "",
        "truncated": False,
        "offset": int(offset),
        "length": int(length),
    }

    if not has_doc_flag:
        result["note"] = "catalog marks has_pdf_doc=False"
        return result

    md_path = _resolve_example_md(row)
    if md_path is None:
        result["note"] = ("catalog marks has_pdf_doc=True but companion "
                          "MD not found under manuals_text/examples/")
        return result

    _meta, body = read_md_with_frontmatter(md_path)
    doc_total = len(body)
    off = max(0, int(offset))
    ln = max(0, int(length))
    end = min(doc_total, off + ln)
    excerpt = body[off:end]
    truncated = end < doc_total
    if truncated:
        excerpt = (excerpt
                   + f"... (truncated, total {doc_total} chars, "
                     "use offset/length to continue)")

    result.update({
        "has_doc": True,
        "doc_total_chars": doc_total,
        "doc_excerpt": excerpt,
        "doc_md_path": _rel_to_kb(md_path),
        "truncated": truncated,
    })
    return result


_OVERVIEW_CACHE: dict = {"mtime": None, "text": None}


def _read_overview_text() -> str:
    p = kb_paths.MODULES_OVERVIEW_MD
    mtime = p.stat().st_mtime
    if _OVERVIEW_CACHE["mtime"] != mtime:
        _OVERVIEW_CACHE["text"] = p.read_text(encoding="utf-8")
        _OVERVIEW_CACHE["mtime"] = mtime
    return _OVERVIEW_CACHE["text"] or ""


def _impl_get_module_overview(module: str) -> dict:
    _ensure_state()
    assert _CATALOG is not None
    try:
        text = _read_overview_text()
    except OSError as e:
        return {"success": False,
                "error": f"cannot read modules_overview.md: {e}",
                "module": module}

    lines = text.splitlines()
    needle = _normalize_module_name(module)
    start = -1
    header_text = ""
    for i, line in enumerate(lines):
        if line.startswith("## "):
            head = line[3:].strip()
            if _normalize_module_name(head) == needle:
                start = i
                header_text = head
                break

    if start == -1:
        full_avail = _CATALOG.get_module_list(exclude_non_modules=True)
        return {
            "success": False,
            "error": f"module not found: {module}",
            "module": module,
            "available": full_avail[:60],
            "available_count_total": len(full_avail),
        }

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    block = "\n".join(lines[start:end]).rstrip()

    return {
        "success": True,
        "module": header_text,
        "overview_md": block,
        "char_length": len(block),
    }


# ---------- MCP registration ----------

def register_kb_tools(mcp: FastMCP) -> None:
    """Register the 4 catalog-backed knowledge tools on an MCP server.

    Safe to call more than once per process, but only the most recent
    registration wins (FastMCP's own semantics). Path-validation
    problems are logged to stderr but do not abort registration — that
    way the server can still boot when the KB dir is temporarily
    unreachable and surface the error at first tool call.
    """
    problems = kb_paths.validate_kb_paths()
    if problems:
        print(f"[kb_tools] WARN: {len(problems)} KB path issue(s) — "
              f"knowledge tools may fail until fixed.",
              file=sys.stderr)
        for p in problems:
            print(f"[kb_tools]   - {p}", file=sys.stderr)

    _ensure_state()

    @mcp.tool()
    def kb_search_manuals(
        query: str,
        module_filter: Optional[str] = None,
        top_n: int = 5,
    ) -> dict:
        """Search the extracted COMSOL manual text (UG/RM/Tutorial/etc.).

        Use when the user needs a *conceptual explanation* or
        *configuration recipe* from the official manuals — anything
        of the form "how do I set up X?", "what does Y mean?",
        "which boundary condition matches Z?".

        Examples:
          - "how do I set up conjugate heat transfer walls?"
          - "explain the meaning of ht.qx in the Heat Transfer module"
          - "RANS turbulence model options in CFD Module"

        Do NOT use this when the user wants a ready-to-run example —
        call ``kb_search_examples`` instead.

        Prefer this over ``pdf_search``, which requires optional
        vector DB dependencies (chromadb, sentence-transformers)
        that are not installed in this environment.
        """
        return _impl_search_manuals(query, module_filter, int(top_n))

    @mcp.tool()
    def kb_search_examples(
        query: str,
        module_filter: Optional[str] = None,
        top_n: int = 10,
    ) -> dict:
        """Find ready-to-run .mph example models by keyword + module.

        Use when the user wants a *starting template* or a *case
        study* they can open directly in COMSOL. Searches the
        ``examples_catalog.csv`` by filename, subcategory and module.

        Examples:
          - "recommend a piezoelectric transducer example"
          - "cantilever beam stress example under Structural Mechanics"
          - "micromixer demo in CFD Module"

        Do NOT use this when the user needs a conceptual answer from a
        manual — call ``kb_search_manuals`` instead.

        This is the preferred example search; upstream ``pdf_search``
        only indexes PDFs, not .mph catalog metadata.
        """
        return _impl_search_examples(query, module_filter, int(top_n))

    @mcp.tool()
    def kb_get_example_detail(
        mph_path: str,
        offset: int = 0,
        length: int = 3000,
    ) -> dict:
        """Return the companion documentation text for a specific .mph.

        Use after ``kb_search_examples`` has returned a candidate and
        you want to know what the model actually demonstrates. Pages
        through the doc with ``offset``/``length`` so large PDFs stay
        within the token budget.

        Examples:
          - "give me the overview of li_ion_battery_impedance.mph"
          - "continue reading from offset 3000 of the chip_thermal tutorial"
          - "does micromixer_3d.mph cover mixing efficiency?"

        Do NOT use this when you only know a keyword and haven't
        picked a specific .mph — run ``kb_search_examples`` first.

        Preferred for reading example documentation; does not depend
        on vector DB (``pdf_search``) which is unavailable in this
        environment.
        """
        return _impl_get_example_detail(mph_path, int(offset),
                                        int(length))

    @mcp.tool()
    def kb_get_module_overview(module: str) -> dict:
        """Return the per-module summary block from modules_overview.md.

        Use when the user asks "what is in <Module>?" or wants a
        high-level count of manuals/examples/scripts for comparison.
        Returns the exact markdown block for that module header from
        the curated overview file.

        Examples:
          - "summary of Heat_Transfer_Module"
          - "what does the Ray_Optics_Module cover?"
          - "how many examples ship with CFD_Module?"

        Do NOT use this when the user wants a specific example or
        manual passage — use ``kb_search_examples`` or
        ``kb_search_manuals``.

        Preferred over ``pdf_list_modules`` for module-level
        summaries; reads directly from modules_overview.md.
        """
        return _impl_get_module_overview(module)


# ---------- __main__ smoke tests ----------

def _main() -> int:
    print(f"[kb_tools smoke] KB_ROOT = {kb_paths.KB_ROOT}")
    problems = kb_paths.validate_kb_paths()
    if problems:
        print(f"[kb_tools smoke] KB path problems: {problems}")
        return 2

    print()
    q1 = "conjugate heat transfer wall"
    print(f"=== test 1: kb_search_manuals({q1!r}, None, 5) ===")
    t0 = time.perf_counter()
    r1 = _impl_search_manuals(q1, None, 5)
    dt = time.perf_counter() - t0
    print(f"  elapsed: {dt:.2f}s")
    assert _MD_INDEX is not None
    print(f"  md_index build: {_MD_INDEX.build_seconds*1000:.1f} ms "
          f"(first call included)")
    print(f"  total_indexed: {_MD_INDEX.total_indexed()}")
    print(f"  keys: {sorted(r1.keys())}")
    print(f"  candidates_scanned: {r1.get('candidates_scanned')}")
    print(f"  hit_count: {r1.get('hit_count')}")
    for i, res in enumerate(r1.get("results", [])[:3]):
        print(f"  [{i}] score={res['score']} module={res['module']} "
              f"doc_type={res['doc_type']}")
        print(f"      path={res['path']}")
        print(f"      snippet={res['snippet'][:240]}")

    print()
    q2 = "cantilever beam stress"
    mf2 = "Structural_Mechanics_Module"
    print(f"=== test 2: kb_search_examples({q2!r}, {mf2!r}, 10) ===")
    t0 = time.perf_counter()
    r2 = _impl_search_examples(q2, mf2, 10)
    dt = time.perf_counter() - t0
    print(f"  elapsed: {dt*1000:.1f} ms")
    print(f"  keys: {sorted(r2.keys())}")
    print(f"  candidates_scanned: {r2.get('candidates_scanned')}")
    print(f"  hit_count: {r2.get('hit_count')}")
    for i, res in enumerate(r2.get("results", [])[:5]):
        print(f"  [{i}] score={res['score']} filename={res['filename']} "
              f"subcat={res['subcategory']} has_pdf={res['has_pdf_doc']}")

    first_mph = None
    if r2.get("results"):
        first_mph = r2["results"][0]["mph_path"]

    # test 2b/2c (supplementary): SM examples have 'beam' in many
    # filenames but neither 'cantilever' nor 'stress' appear in the
    # surface text (filename+subcat+module). The strict-AND result of 0
    # for 2/2b is correct; 2c with single 'beam' proves the algorithm.
    print()
    q2b = "beam stress"
    print(f"=== test 2b (supplementary): kb_search_examples({q2b!r}, "
          f"{mf2!r}, 5) ===")
    r2b = _impl_search_examples(q2b, mf2, 5)
    print(f"  hit_count: {r2b.get('hit_count')} "
          f"(candidates={r2b.get('candidates_scanned')})")
    for i, res in enumerate(r2b.get("results", [])[:3]):
        print(f"  [{i}] {res['filename']} | subcat={res['subcategory']} "
              f"| score={res['score']}")
    print()
    q2c = "beam"
    print(f"=== test 2c (algorithm liveness): "
          f"kb_search_examples({q2c!r}, {mf2!r}, 5) ===")
    r2c = _impl_search_examples(q2c, mf2, 5)
    print(f"  hit_count: {r2c.get('hit_count')} "
          f"(candidates={r2c.get('candidates_scanned')})")
    for i, res in enumerate(r2c.get("results", [])[:5]):
        print(f"  [{i}] {res['filename']} | subcat={res['subcategory']} "
              f"| score={res['score']}")

    if first_mph is None:
        print()
        print("  fallback: picking first Heat_Transfer example with PDF "
              "for tests 3/4 (test 2 returned 0 hits on exact AND match)")
        assert _CATALOG is not None
        alt = _CATALOG.filter_examples(module="Heat_Transfer_Module",
                                       has_pdf_doc=True)
        if alt:
            first_mph = alt[0]["path"]

    print()
    print(f"=== test 3: kb_get_example_detail({Path(first_mph).name!r}, "
          f"0, 3000) ===")
    r3 = _impl_get_example_detail(first_mph, 0, 3000)
    print(f"  keys: {sorted(r3.keys())}")
    print(f"  has_doc={r3.get('has_doc')} "
          f"doc_total_chars={r3.get('doc_total_chars')} "
          f"truncated={r3.get('truncated')}")
    ex1 = r3.get("doc_excerpt") or ""
    print(f"  excerpt len: {len(ex1)}, first 200 chars:")
    print(f"    {ex1[:200]!r}")
    if r3.get("doc_md_path"):
        print(f"  doc_md_path: {r3['doc_md_path']}")

    print()
    print(f"=== test 4: kb_get_example_detail({Path(first_mph).name!r}, "
          f"3000, 3000) ===")
    r4 = _impl_get_example_detail(first_mph, 3000, 3000)
    print(f"  has_doc={r4.get('has_doc')} "
          f"truncated={r4.get('truncated')}")
    ex2 = r4.get("doc_excerpt") or ""
    print(f"  excerpt len: {len(ex2)}, first 200 chars:")
    print(f"    {ex2[:200]!r}")

    print()
    print("=== test 5: kb_get_module_overview('Heat_Transfer_Module') ===")
    r5 = _impl_get_module_overview("Heat_Transfer_Module")
    print(f"  keys: {sorted(r5.keys())}")
    print(f"  success={r5.get('success')} module={r5.get('module')} "
          f"char_length={r5.get('char_length')}")
    print(f"  first 240 chars of overview_md:")
    print(f"    {(r5.get('overview_md') or '')[:240]!r}")

    print()
    print("=== test 6: kb_get_module_overview('nonexistent_module') ===")
    r6 = _impl_get_module_overview("nonexistent_module")
    print(f"  keys: {sorted(r6.keys())}")
    print(f"  success={r6.get('success')} error={r6.get('error')}")
    avail = r6.get("available", [])
    print(f"  available list size: {len(avail)} "
          f"(total={r6.get('available_count_total')})")
    print(f"  first 5 available: {avail[:5]}")
    # Sanity: non-module sentinels must NOT leak.
    leak = set(avail) & {"addins", "parts", "data", "demo"}
    print(f"  non-module leak check: {leak or 'OK (none)'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
