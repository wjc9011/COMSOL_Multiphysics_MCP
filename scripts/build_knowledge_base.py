#!/usr/bin/env python
"""
Build the PDF documentation knowledge base for COMSOL MCP Server.

This script:
1. Scans the pdf/ directory for PDF files
2. Extracts and chunks text from each PDF
3. Creates embeddings and stores in ChromaDB

Usage:
    python scripts/build_knowledge_base.py [--limit N] [--rebuild]

Options:
    --limit N     Only process first N PDF files (for testing)
    --rebuild     Clear existing database and rebuild from scratch
    --no-mirror   Don't use HuggingFace mirror (for users outside China)
"""

import argparse
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def setup_mirror():
    """Setup HuggingFace mirror for users in China."""
    hf_mirror = "https://hf-mirror.com"
    if "HF_ENDPOINT" not in os.environ:
        os.environ["HF_ENDPOINT"] = hf_mirror
        print(f"[INFO] Using HuggingFace mirror: {hf_mirror}")


from src.knowledge.pdf_processor import PDFProcessor, DEFAULT_PDF_DIR, check_pdf_dependencies
from src.knowledge.retriever import VectorRetriever, DEFAULT_DB_DIR


def main():
    parser = argparse.ArgumentParser(
        description="Build COMSOL documentation knowledge base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    # Default: index pdf/ with the bundled COMSOL manuals
    python scripts/build_knowledge_base.py

    # Add a directory of user-supplied PDFs (e.g. textbooks)
    python scripts/build_knowledge_base.py \\
        --extra-pdfs /path/to/textbooks_ja \\
        --extra-language ja

    # Use a multilingual embedding model (better for non-English content)
    python scripts/build_knowledge_base.py \\
        --embedding-model intfloat/multilingual-e5-base
"""
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit number of PDFs to process")
    parser.add_argument("--rebuild", action="store_true", help="Clear and rebuild database")
    parser.add_argument("--pdf-dir", type=str, default=None, help="PDF directory path")
    parser.add_argument("--db-dir", type=str, default=None, help="Database directory path")
    parser.add_argument("--status", action="store_true", help="Show status only")
    parser.add_argument("--no-mirror", action="store_true", help="Don't use HuggingFace mirror")
    # Multilingual support
    parser.add_argument("--embedding-model", type=str, default=None,
                        help="Sentence-transformers model name "
                             "(default: all-MiniLM-L6-v2). Try "
                             "intfloat/multilingual-e5-base for non-English content.")
    parser.add_argument("--language", type=str, default=None,
                        help="Language tag for chunks from --pdf-dir "
                             "(e.g. 'en', 'ja', 'zh'). Stored in chunk "
                             "metadata; queryable via search(language_filter=...)")
    # Extra PDF directories (third-party textbooks, user notes, etc.)
    parser.add_argument("--extra-pdfs", action="append", default=[],
                        metavar="DIR",
                        help="Extra directory of PDFs to ingest "
                             "(in addition to --pdf-dir). Can be passed "
                             "multiple times. NOTE: 3rd-party copyrighted "
                             "textbooks should NOT be committed to this repo; "
                             "supply your own path here.")
    parser.add_argument("--extra-language", action="append", default=[],
                        metavar="LANG",
                        help="Language tag for the corresponding --extra-pdfs "
                             "directory. If supplied, must match the number of "
                             "--extra-pdfs flags.")
    args = parser.parse_args()
    
    # Setup HuggingFace mirror for China
    if not args.no_mirror:
        setup_mirror()
    
    print("=" * 60)
    print("COMSOL Documentation Knowledge Base Builder")
    print("=" * 60)
    
    # Check dependencies
    print("\n[1] Checking dependencies...")
    deps = check_pdf_dependencies()
    for dep, installed in deps.items():
        status = "[OK]" if installed else "[X]"
        print(f"    {status} {dep}")
    
    if not all(deps.values()):
        print("\n[ERROR] Missing dependencies. Install with:")
        print("    pip install pymupdf chromadb sentence-transformers")
        return 1
    
    # Set paths
    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else DEFAULT_PDF_DIR
    db_dir = Path(args.db_dir) if args.db_dir else DEFAULT_DB_DIR
    
    print(f"\n[2] Configuration:")
    print(f"    PDF directory: {pdf_dir}")
    print(f"    Database directory: {db_dir}")
    
    if not pdf_dir.exists():
        print(f"\n[ERROR] PDF directory not found: {pdf_dir}")
        return 1
    
    # Initialize processor (with optional language tag for the main pdf_dir)
    processor = PDFProcessor(pdf_dir, default_language=args.language)

    # Validate --extra-pdfs / --extra-language pairing
    if args.extra_language and len(args.extra_language) != len(args.extra_pdfs):
        print(f"\n[ERROR] --extra-language was given {len(args.extra_language)} times "
              f"but --extra-pdfs was given {len(args.extra_pdfs)} times. "
              f"They must match (or omit --extra-language entirely).")
        return 1
    
    # Show status only
    if args.status:
        modules = processor.get_available_modules()
        print(f"\n[3] Available modules ({len(modules)}):")
        for m in modules[:20]:
            print(f"    - {m['name']} ({m['file_count']} files)")
        if len(modules) > 20:
            print(f"    ... and {len(modules) - 20} more")
        
        # Check existing database
        retriever = VectorRetriever(pdf_dir, db_dir)
        stats = retriever.get_stats()
        print(f"\n[4] Existing knowledge base:")
        print(f"    Initialized: {stats.get('initialized', False)}")
        print(f"    Documents: {stats.get('count', 0)}")
        print(f"    Modules indexed: {stats.get('module_count', 0)}")
        return 0
    
    # List available PDFs
    pdf_files = processor.get_pdf_files()
    print(f"\n[3] Found {len(pdf_files)} PDF files")
    
    modules = processor.get_available_modules()
    print(f"    Modules: {len(modules)}")
    for m in modules[:10]:
        print(f"    - {m['name']} ({m['file_count']} files)")
    if len(modules) > 10:
        print(f"    ... and {len(modules) - 10} more")
    
    # Initialize retriever (with optional custom embedding model)
    print(f"\n[4] Initializing vector store...")
    retriever_kwargs = {}
    if args.embedding_model:
        retriever_kwargs["embedding_model"] = args.embedding_model
        print(f"    Embedding model: {args.embedding_model}")
    retriever = VectorRetriever(pdf_dir, db_dir, **retriever_kwargs)

    if args.rebuild:
        print("    Clearing existing database...")
        retriever.clear()

    retriever.initialize()

    # Process PDFs
    print(f"\n[5] Processing PDFs...")
    if args.limit:
        print(f"    Limit: {args.limit} files")
    if args.language:
        print(f"    Language tag for {pdf_dir.name}: {args.language}")

    stats = retriever.rebuild_from_pdfs(limit=args.limit)

    # Process --extra-pdfs directories
    extra_stats: list[dict] = []
    for i, extra_dir_str in enumerate(args.extra_pdfs):
        extra_dir = Path(extra_dir_str)
        if not extra_dir.exists():
            print(f"\n[WARN] Skipping non-existent --extra-pdfs dir: {extra_dir}")
            continue
        extra_lang = (args.extra_language[i]
                      if args.extra_language else None)
        print(f"\n[5+{i+1}] Processing extra PDFs from {extra_dir}"
              f" (language={extra_lang or 'none'})")
        extra_processor = PDFProcessor(extra_dir,
                                         default_language=extra_lang)
        extra_chunks = extra_processor.process_all_pdfs()
        added = retriever.add_chunks(extra_chunks)
        extra_stats.append({
            "dir": str(extra_dir),
            "language": extra_lang,
            "chunks": len(extra_chunks),
            "added": added,
        })

    print(f"\n[6] Results:")
    print(f"    PDF files found: {stats.get('pdf_files_found', 0)}")
    print(f"    Chunks generated: {stats.get('chunks_generated', 0)}")
    print(f"    Chunks added: {stats.get('chunks_added', 0)}")
    for es in extra_stats:
        print(f"    Extra ({es['language']}) from {es['dir']}: "
              f"{es['added']} added")
    final_count = retriever._collection.count() if retriever._collection else 0
    print(f"    Total in database: {final_count}")
    
    # Test search
    print(f"\n[7] Testing search...")
    results = retriever.search("electrostatic field", n_results=2)
    if results:
        print(f"    Query: 'electrostatic field'")
        print(f"    Results: {len(results)}")
        for i, r in enumerate(results):
            print(f"    [{i+1}] Module: {r.module}, Score: {r.score:.3f}")
            # Filter non-ASCII chars for Windows console compatibility
            preview = r.text[:100].encode('ascii', 'replace').decode('ascii')
            print(f"        Preview: {preview}...")
    else:
        print("    No results (this may indicate an issue)")
    
    print("\n" + "=" * 60)
    print("Knowledge base build complete!")
    print("=" * 60)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
