# Multilingual Search + Extra PDFs

This fork adds three small, backwards-compatible features for users who
want to:

1. Index third-party / proprietary PDFs alongside the bundled COMSOL
   manuals (e.g. textbooks in their native language)
2. Query the knowledge base in a non-English language
3. Filter search results to a single language

## 1. `--extra-pdfs <DIR>` (build_knowledge_base.py)

Add one or more directories of additional PDFs to the same vector
store. Repeatable.

```bash
python scripts/build_knowledge_base.py \
    --extra-pdfs /home/me/comsol_textbooks_ja
```

The default `pdf/` directory (bundled COMSOL official manuals) is
still ingested. `--extra-pdfs` adds to it.

**Copyright reminder**: do not commit 3rd-party textbooks to a public
fork. Keep them on your local filesystem (or in a `.gitignore`'d
directory) and use `--extra-pdfs` to ingest them at index-build time.

## 2. `--language <CODE>` and `--extra-language <CODE>`

Tag chunks with an ISO 639-1 language code. Stored in chunk metadata
and queryable via `search(language_filter=...)`.

```bash
# Tag bundled COMSOL manuals as English
python scripts/build_knowledge_base.py --language en

# Tag bundled manuals as English AND add Japanese textbooks
python scripts/build_knowledge_base.py \
    --language en \
    --extra-pdfs /path/to/textbooks_ja --extra-language ja
```

`--extra-language` is positional with `--extra-pdfs`: each
`--extra-pdfs` flag pairs with the corresponding `--extra-language`
flag in command-line order. If you omit `--extra-language`, those
chunks have no language tag.

## 3. `--embedding-model <NAME>`

Override the default `all-MiniLM-L6-v2` embedding model. For
multilingual content, try:

```bash
python scripts/build_knowledge_base.py \
    --embedding-model intfloat/multilingual-e5-base \
    --rebuild
```

| Model | Size | Multilingual | Recommended for |
|-------|------|--------------|-----------------|
| `all-MiniLM-L6-v2` (default) | ~80 MB | partial | English-only corpora |
| `intfloat/multilingual-e5-base` | ~280 MB | ★ excellent | Mixed-language (en + ja + zh + …) |
| `intfloat/multilingual-e5-large` | ~1.1 GB | ★★ best | Production multilingual |

Switching models requires `--rebuild` because the embedding dimension
changes between models.

## 4. Querying by language (Python API)

```python
from src.knowledge.retriever import VectorRetriever
r = VectorRetriever()
r.initialize()
# Japanese textbook hits only
hits = r.search("有限要素法 マクスウェル", n_results=5, language_filter="ja")
# COMSOL official manual hits only
hits = r.search("electrostatic boundary", n_results=5, language_filter="en")
# Combined: filter to ACDC module AND English
hits = r.search("magnetostatic", n_results=5,
                module_filter="ACDC_Module", language_filter="en")
```

## Compatibility

- All flags are **optional**. Without them, behaviour is identical
  to upstream wjc9011.
- Existing knowledge bases (built before this fork) work without
  rebuild. The `language` field is simply `None` on legacy chunks
  and `language_filter` excludes them only when explicitly given.
- The fork is a strict superset of upstream wjc9011; switching
  back-and-forth between branches is safe.

## Use case: Sugahara lab (Kindai University) COMSOL textbooks

Lab has 5 Japanese COMSOL textbooks (~300 MB total) covering:
- Engineering simulation introduction
- Science / tech multiphysics
- Solid mechanics
- **Electromagnetic FEM** (224 pages — primary lab interest)
- Multiphysics for next-generation engineers

These are commercial publications and NOT committed to the fork.
Lab users install the fork + run:

```bash
python scripts/build_knowledge_base.py \
    --language en \
    --extra-pdfs S:/COMSOL/02_教科書 --extra-language ja \
    --embedding-model intfloat/multilingual-e5-base \
    --rebuild
```

Then in Claude Desktop / Claude Code, semantic queries in either
language hit the appropriate side of the corpus.
