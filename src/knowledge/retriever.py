"""Vector retriever for COMSOL documentation using ChromaDB."""

import logging
import os
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from .pdf_processor import PDFProcessor, DocumentChunk, check_pdf_dependencies

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_PDF_DIR = Path(__file__).parent.parent.parent / "pdf"
DEFAULT_DB_DIR = Path(__file__).parent.parent.parent / "knowledge_base"

# Embedding model - use HF mirror for China if needed
HF_MIRROR = "https://hf-mirror.com"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # Fast, good quality

# Local model cache path for offline use
_LOCAL_MODEL_PATH = None


def _find_local_model_path() -> Optional[str]:
    """Find the local cache path for the embedding model."""
    import os
    import glob
    
    cache_base = os.path.expanduser("~/.cache/huggingface/hub")
    pattern = os.path.join(cache_base, f"models--sentence-transformers--{EMBEDDING_MODEL}", "snapshots", "*")
    matches = glob.glob(pattern)
    if matches:
        return matches[0]
    return None


def setup_hf_mirror():
    """Setup HuggingFace mirror for users in China."""
    global _LOCAL_MODEL_PATH
    
    # Try to find local model first
    _LOCAL_MODEL_PATH = _find_local_model_path()
    if _LOCAL_MODEL_PATH:
        return
    
    # Fall back to mirror if no local cache
    if "HF_ENDPOINT" not in os.environ:
        os.environ["HF_ENDPOINT"] = HF_MIRROR
        logger.info(f"Using HuggingFace mirror: {HF_MIRROR}")


@dataclass
class SearchResult:
    """A search result from the vector store."""
    text: str
    source: str
    module: str
    chapter: Optional[str]
    page: Optional[int]
    score: float
    
    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source": self.source,
            "module": self.module,
            "chapter": self.chapter,
            "page": self.page,
            "score": self.score,
        }


class VectorRetriever:
    """Manage vector storage and retrieval for COMSOL documentation."""
    
    def __init__(self, pdf_dir: str | Path = DEFAULT_PDF_DIR, 
                 db_dir: str | Path = DEFAULT_DB_DIR,
                 embedding_model: str = EMBEDDING_MODEL):
        self.pdf_dir = Path(pdf_dir)
        self.db_dir = Path(db_dir)
        self.embedding_model = embedding_model
        self._client = None
        self._collection = None
        self._embedding_fn = None
        
    @property
    def is_initialized(self) -> bool:
        """Check if the vector store is initialized."""
        return self._collection is not None
    
    def _get_embedding_function(self):
        """Get the embedding function."""
        if self._embedding_fn is None:
            try:
                setup_hf_mirror()
                
                from chromadb.utils import embedding_functions
                
                # Use local model path if available (offline mode)
                model_name = _LOCAL_MODEL_PATH if _LOCAL_MODEL_PATH else self.embedding_model
                if _LOCAL_MODEL_PATH:
                    logger.info(f"Using local cached model: {_LOCAL_MODEL_PATH}")
                
                self._embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=model_name
                )
            except Exception as e:
                logger.error(f"Failed to initialize embedding function: {e}")
                raise
        return self._embedding_fn
    
    def initialize(self) -> bool:
        """Initialize the vector store."""
        try:
            import chromadb
            
            self.db_dir.mkdir(parents=True, exist_ok=True)
            
            self._client = chromadb.PersistentClient(path=str(self.db_dir))
            self._collection = self._client.get_or_create_collection(
                name="comsol_docs",
                embedding_function=self._get_embedding_function(),
                metadata={"description": "COMSOL Multiphysics documentation"}
            )
            
            logger.info(f"Vector store initialized with {self._collection.count()} documents")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize vector store: {e}")
            return False
    
    def add_chunks(self, chunks: list[DocumentChunk]) -> int:
        """Add document chunks to the vector store."""
        if not self._collection:
            if not self.initialize():
                return 0
        
        if not chunks:
            return 0
        
        ids = [chunk.chunk_id for chunk in chunks]
        documents = [chunk.text for chunk in chunks]
        
        # Create flat metadata (ChromaDB only accepts simple types)
        metadatas = []
        for chunk in chunks:
            meta = {
                "source": chunk.source,
                "module": chunk.module,
            }
            if chunk.chapter:
                meta["chapter"] = chunk.chapter
            if chunk.page is not None:
                meta["page"] = chunk.page
            if chunk.language:
                meta["language"] = chunk.language
            metadatas.append(meta)
        
        try:
            # Add in batches
            batch_size = 100
            added = 0
            
            for i in range(0, len(chunks), batch_size):
                batch_ids = ids[i:i+batch_size]
                batch_docs = documents[i:i+batch_size]
                batch_metas = metadatas[i:i+batch_size]
                
                self._collection.add(
                    ids=batch_ids,
                    documents=batch_docs,
                    metadatas=batch_metas
                )
                added += len(batch_ids)
            
            logger.info(f"Added {added} chunks to vector store")
            return added
            
        except Exception as e:
            logger.error(f"Failed to add chunks: {e}")
            return 0
    
    def search(self, query: str, n_results: int = 5,
               module_filter: Optional[str] = None,
               language_filter: Optional[str] = None) -> list[SearchResult]:
        """Search for relevant documents.

        Args:
            query: free-text query (matched against chunk embeddings)
            n_results: top N hits to return
            module_filter: optional COMSOL module name (e.g. "ACDC_Module")
            language_filter: optional language code (e.g. "en", "ja")
                              to restrict hits to chunks tagged with that
                              language by build_knowledge_base.py --language
        """
        if not self._collection:
            if not self.initialize():
                return []

        try:
            # Build where filter: support both single-key and multi-key
            filters: list[dict] = []
            if module_filter:
                filters.append({"module": module_filter})
            if language_filter:
                filters.append({"language": language_filter})
            if len(filters) == 0:
                where_filter = None
            elif len(filters) == 1:
                where_filter = filters[0]
            else:
                where_filter = {"$and": filters}
            
            results = self._collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where_filter,
                include=["documents", "metadatas", "distances"]
            )
            
            search_results = []
            if results and results["documents"]:
                for i, doc in enumerate(results["documents"][0]):
                    meta = results["metadatas"][0][i] if results["metadatas"] else {}
                    distance = results["distances"][0][i] if results["distances"] else 0
                    
                    # Convert distance to similarity score (0-1)
                    score = 1.0 - min(distance, 1.0)
                    
                    search_results.append(SearchResult(
                        text=doc,
                        source=meta.get("source", ""),
                        module=meta.get("module", ""),
                        chapter=meta.get("chapter"),
                        page=meta.get("page"),
                        score=score,
                    ))
            
            return search_results
            
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []
    
    def get_stats(self) -> dict:
        """Get statistics about the vector store."""
        if not self._collection:
            return {"initialized": False, "count": 0}
        
        count = self._collection.count()
        
        # Get unique modules
        modules = set()
        try:
            results = self._collection.get(include=["metadatas"])
            for meta in results.get("metadatas", []):
                if meta and "module" in meta:
                    modules.add(meta["module"])
        except Exception:
            pass
        
        return {
            "initialized": True,
            "count": count,
            "modules": list(modules),
            "module_count": len(modules),
        }
    
    def clear(self) -> bool:
        """Clear all documents from the vector store."""
        if not self._client or not self._collection:
            return False
        
        try:
            self._client.delete_collection("comsol_docs")
            self._collection = None
            return True
        except Exception as e:
            logger.error(f"Failed to clear vector store: {e}")
            return False
    
    def rebuild_from_pdfs(self, limit: Optional[int] = None) -> dict:
        """Rebuild the vector store from PDF files."""
        processor = PDFProcessor(self.pdf_dir)
        
        # Get stats before
        stats = {"pdf_files_found": len(processor.get_pdf_files())}
        
        # Process PDFs
        chunks = processor.process_all_pdfs(limit=limit)
        stats["chunks_generated"] = len(chunks)
        
        # Clear and rebuild
        self.clear()
        self.initialize()
        
        added = self.add_chunks(chunks)
        stats["chunks_added"] = added
        
        # Get final stats
        stats["final_count"] = self._collection.count() if self._collection else 0
        
        return stats


# Global retriever instance
_retriever: Optional[VectorRetriever] = None


def get_retriever() -> VectorRetriever:
    """Get the global retriever instance."""
    global _retriever
    if _retriever is None:
        _retriever = VectorRetriever()
    return _retriever


def search_docs(query: str, n_results: int = 5, 
                module_filter: Optional[str] = None) -> list[dict]:
    """Search documentation (convenience function)."""
    retriever = get_retriever()
    results = retriever.search(query, n_results, module_filter)
    return [r.to_dict() for r in results]
