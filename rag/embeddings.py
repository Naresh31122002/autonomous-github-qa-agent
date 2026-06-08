"""
Code Embeddings Module
======================
Handles code chunking and embedding generation for RAG.
"""

from typing import List, Dict
import hashlib
import numpy as np


class CodeEmbedder:
    """
    Generates embeddings for code chunks using sentence transformers.
    
    For demo purposes, this uses a simple approach. In production,
    you would use models like CodeBERT or sentence-transformers.
    """
    
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """
        Initialize code embedder.
        
        Args:
            model_name: Name of the embedding model to use
        """
        self.model_name = model_name
        self.model = None
        self.dimension = 384  # Default for all-MiniLM-L6-v2
        
        # Try to load sentence-transformers
        try:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(model_name)
            self.dimension = self.model.get_sentence_embedding_dimension()
            print(f"[RAG] Loaded embedding model: {model_name} (dim={self.dimension})")
        except ImportError:
            print("[RAG] sentence-transformers not installed. Using dummy embeddings for demo.")
        except Exception as e:
            print(f"[RAG] Error loading model: {e}. Using dummy embeddings for demo.")
    
    def chunk_code(self, code: str, filename: str, chunk_size: int = 500) -> List[Dict]:
        """
        Split code into chunks for embedding.
        
        Args:
            code: Source code string
            filename: Name of the file
            chunk_size: Maximum characters per chunk
        
        Returns:
            List of chunk dicts with metadata
        """
        chunks = []
        
        # Split by functions/classes (simple approach)
        # In production, use AST parsing for better chunking
        lines = code.split('\n')
        
        current_chunk = []
        current_size = 0
        chunk_id = 0
        
        for i, line in enumerate(lines):
            line_size = len(line) + 1  # +1 for newline
            
            # Start new chunk if size exceeded or at function/class definition
            if (current_size + line_size > chunk_size and current_chunk) or \
               (line.strip().startswith(('def ', 'class ', 'async def '))):
                
                if current_chunk:
                    chunk_text = '\n'.join(current_chunk)
                    chunks.append({
                        'text': chunk_text,
                        'filename': filename,
                        'chunk_id': chunk_id,
                        'start_line': i - len(current_chunk) + 1,
                        'end_line': i,
                        'size': current_size
                    })
                    chunk_id += 1
                
                current_chunk = [line]
                current_size = line_size
            else:
                current_chunk.append(line)
                current_size += line_size
        
        # Add final chunk
        if current_chunk:
            chunk_text = '\n'.join(current_chunk)
            chunks.append({
                'text': chunk_text,
                'filename': filename,
                'chunk_id': chunk_id,
                'start_line': len(lines) - len(current_chunk) + 1,
                'end_line': len(lines),
                'size': current_size
            })
        
        print(f"[RAG] Chunked {filename} into {len(chunks)} chunks")
        return chunks
    
    def embed_chunks(self, chunks: List[Dict]) -> np.ndarray:
        """
        Generate embeddings for code chunks.
        
        Args:
            chunks: List of chunk dicts with 'text' field
        
        Returns:
            Numpy array of embeddings (n_chunks, dimension)
        """
        if self.model is not None:
            # Use actual sentence-transformers model
            texts = [chunk['text'] for chunk in chunks]
            embeddings = self.model.encode(texts, show_progress_bar=False)
            return np.array(embeddings)
        else:
            return np.array([self._hash_embedding(chunk['text']) for chunk in chunks])
    
    def embed_query(self, query: str) -> np.ndarray:
        """
        Generate embedding for a query string.
        
        Args:
            query: Query text
        
        Returns:
            Numpy array of shape (1, dimension)
        """
        if self.model is not None:
            embedding = self.model.encode([query], show_progress_bar=False)
            return np.array(embedding)
        else:
            return np.array([self._hash_embedding(query)])

    def _hash_embedding(self, text: str) -> np.ndarray:
        """Create a deterministic lexical embedding when model loading is unavailable."""
        vector = np.zeros(self.dimension, dtype='float32')
        tokens = [
            token.lower()
            for token in text.replace("_", " ").split()
            if token.strip()
        ]
        for token in tokens:
            digest = hashlib.sha256(token.encode('utf-8')).digest()
            idx = int.from_bytes(digest[:4], 'big') % self.dimension
            vector[idx] += 1.0
        norm = np.linalg.norm(vector)
        if norm == 0:
            return vector
        return vector / norm
    
    def process_repository(self, python_files: List[Dict]) -> tuple:
        """
        Process all Python files in a repository.
        
        Args:
            python_files: List of dicts with 'filename' and 'content'
        
        Returns:
            Tuple of (embeddings, metadata)
        """
        all_chunks = []
        
        # Chunk all files
        for file_info in python_files:
            chunks = self.chunk_code(
                file_info['content'],
                file_info['filename']
            )
            all_chunks.extend(chunks)
        
        if not all_chunks:
            return np.array([]), []
        
        # Generate embeddings
        embeddings = self.embed_chunks(all_chunks)
        
        print(f"[RAG] Generated {len(embeddings)} embeddings for repository")
        return embeddings, all_chunks
