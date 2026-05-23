"""PDF document processor for COMSOL documentation."""

import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Generator
import logging

logger = logging.getLogger(__name__)

# Default PDF directory
DEFAULT_PDF_DIR = Path(__file__).parent.parent.parent / "pdf"


@dataclass
class DocumentChunk:
    """A chunk of text from a PDF document."""
    text: str
    source: str
    module: str
    chapter: Optional[str] = None
    page: Optional[int] = None
    chunk_id: str = ""
    language: Optional[str] = None   # e.g. "en", "ja", "zh" (None = unspecified)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source": self.source,
            "module": self.module,
            "chapter": self.chapter,
            "page": self.page,
            "chunk_id": self.chunk_id,
            "language": self.language,
            "metadata": self.metadata,
        }


class PDFProcessor:
    """Process PDF documents into chunks for vector storage."""
    
    # Chapter patterns to identify section breaks
    CHAPTER_PATTERNS = [
        r"^Chapter\s+(\d+)[:\s]+(.+)$",
        r"^(\d+(?:\.\d+)*)\s+(.+)$",
        r"^([A-Z][A-Za-z\s]+)$",
    ]
    
    # Minimum chunk size (characters)
    MIN_CHUNK_SIZE = 200
    # Maximum chunk size (characters)
    MAX_CHUNK_SIZE = 2000
    # Overlap between chunks
    CHUNK_OVERLAP = 200
    
    def __init__(self, pdf_dir: str | Path, default_language: Optional[str] = None):
        """
        Args:
            pdf_dir: root directory for PDF discovery
            default_language: language tag applied to all chunks unless
                              overridden (e.g. "en", "ja"). Useful when
                              an entire directory is single-language.
                              None = unspecified (chunks have no lang tag).
        """
        self.pdf_dir = Path(pdf_dir)
        self.default_language = default_language

    def get_pdf_files(self) -> list[Path]:
        """Get all PDF files in the directory."""
        pdf_files = []
        for path in self.pdf_dir.rglob("*.pdf"):
            pdf_files.append(path)
        return sorted(pdf_files)
    
    def get_module_name(self, pdf_path: Path) -> str:
        """Extract module name from PDF path."""
        # Parent folder name is usually the module name
        parent = pdf_path.parent.name
        if parent and parent != "pdf":
            return parent
        # Fallback to filename
        return pdf_path.stem
    
    def extract_text_from_pdf(self, pdf_path: Path) -> Generator[tuple[int, str], None, None]:
        """Extract text from PDF, yielding (page_number, text) tuples."""
        try:
            import fitz  # pymupdf
        except ImportError:
            logger.error("pymupdf not installed. Run: pip install pymupdf")
            return
        
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                page = doc[page_num]
                text = page.get_text()
                if text.strip():
                    yield page_num + 1, text
            doc.close()
        except Exception as e:
            logger.error(f"Error extracting text from {pdf_path}: {e}")
    
    def clean_text(self, text: str) -> str:
        """Clean extracted text."""
        # Remove excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        # Remove page numbers and headers/footers
        text = re.sub(r"^\d+\s*$", "", text, flags=re.MULTILINE)
        return text.strip()
    
    def detect_chapters(self, text: str) -> list[tuple[str, int, int]]:
        """Detect chapter boundaries in text.
        
        Returns list of (chapter_title, start_pos, end_pos)
        """
        chapters = []
        lines = text.split("\n")
        
        current_chapter = "Introduction"
        chapter_start = 0
        
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            
            for pattern in self.CHAPTER_PATTERNS:
                match = re.match(pattern, line)
                if match:
                    # Save previous chapter
                    if chapter_start < i:
                        chapters.append((current_chapter, chapter_start, i))
                    
                    # Start new chapter
                    if len(match.groups()) >= 2:
                        current_chapter = match.group(2).strip()
                    else:
                        current_chapter = match.group(1).strip()
                    chapter_start = i
                    break
        
        # Add last chapter
        if chapter_start < len(lines):
            chapters.append((current_chapter, chapter_start, len(lines)))
        
        return chapters
    
    def split_into_chunks(self, text: str, source: str, module: str, 
                          page: Optional[int] = None) -> list[DocumentChunk]:
        """Split text into chunks for vector storage."""
        chunks = []
        text = self.clean_text(text)
        
        if len(text) < self.MIN_CHUNK_SIZE:
            return chunks
        
        # Simple sliding window chunking
        start = 0
        chunk_index = 0
        
        while start < len(text):
            end = start + self.MAX_CHUNK_SIZE
            
            # Try to break at sentence boundary
            if end < len(text):
                # Look for sentence end
                last_period = text.rfind(".", start, end)
                last_newline = text.rfind("\n", start, end)
                break_point = max(last_period, last_newline)
                
                if break_point > start + self.MIN_CHUNK_SIZE:
                    end = break_point + 1
            
            chunk_text = text[start:end].strip()
            
            if len(chunk_text) >= self.MIN_CHUNK_SIZE:
                # Use source file path for unique ID (handles multiple PDFs per module)
                safe_source = source.replace("/", "_").replace("\\", "_").replace(".pdf", "")
                chunk_id = f"{safe_source}_p{page or 0}_c{chunk_index}"
                chunks.append(DocumentChunk(
                    text=chunk_text,
                    source=source,
                    module=module,
                    page=page,
                    chunk_id=chunk_id,
                    language=self.default_language,
                ))
                chunk_index += 1
            
            start = end - self.CHUNK_OVERLAP
            if start < 0:
                start = 0
        
        return chunks
    
    def process_pdf(self, pdf_path: Path) -> list[DocumentChunk]:
        """Process a single PDF file into chunks."""
        module = self.get_module_name(pdf_path)
        source = str(pdf_path.relative_to(self.pdf_dir))
        all_chunks = []
        
        logger.info(f"Processing: {source}")
        
        for page_num, text in self.extract_text_from_pdf(pdf_path):
            chunks = self.split_into_chunks(text, source, module, page_num)
            all_chunks.extend(chunks)
        
        logger.info(f"  Generated {len(all_chunks)} chunks from {module}")
        return all_chunks
    
    def process_all_pdfs(self, limit: Optional[int] = None) -> list[DocumentChunk]:
        """Process all PDF files in the directory."""
        pdf_files = self.get_pdf_files()
        if limit:
            pdf_files = pdf_files[:limit]
        
        all_chunks = []
        for pdf_path in pdf_files:
            chunks = self.process_pdf(pdf_path)
            all_chunks.extend(chunks)
        
        logger.info(f"Total chunks generated: {len(all_chunks)}")
        return all_chunks
    
    def get_available_modules(self) -> list[dict]:
        """Get list of available documentation modules."""
        modules = {}
        
        for pdf_path in self.get_pdf_files():
            module = self.get_module_name(pdf_path)
            if module not in modules:
                modules[module] = {
                    "name": module,
                    "files": [],
                    "file_count": 0,
                }
            modules[module]["files"].append(pdf_path.name)
            modules[module]["file_count"] += 1
        
        return list(modules.values())


def check_pdf_dependencies() -> dict:
    """Check if PDF processing dependencies are installed."""
    deps = {
        "pymupdf": False,
        "chromadb": False,
        "sentence_transformers": False,
    }
    
    try:
        import fitz
        deps["pymupdf"] = True
    except ImportError:
        pass
    
    try:
        import chromadb
        deps["chromadb"] = True
    except ImportError:
        pass
    
    try:
        import sentence_transformers
        deps["sentence_transformers"] = True
    except ImportError:
        pass
    
    return deps
