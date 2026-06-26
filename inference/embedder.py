"""
The Python-side semantic memory writer for Jugnu
Responsibility:
    - Load the e5-small-v2 ONNX model to generate 384-dim text vectors.
    - Receive text events from ipc_client.py.
    - Write the text + its vector directly into jugnu.db using sqlite-vec Python bindings.
Architecture note:
    - Python writes vectors directly to SQLite. C++ reads them for RAG.
    - This avoids reverse IPC (sending binary blob back through the Named Pipe)
    - and correctly respects our boundary: C++ = OS hooks, Python = ML math.
"""
import struct
import sqlite3
import sqlite_vec
import numpy as np
from sentence_transformers import SentenceTransformer


# ANSI colors for terminal logging
_CYAN   = "\033[1;36m"
_GREEN  = "\033[1;32m"
_RED    = "\033[1;31m"
_YELLOW = "\033[1;33m"
_RESET  = "\033[0m"

class Embedder:
    """
    Manages e5-small-v2 text embedding and vector DB writes.
    This class is intentionally lightweight — it is instantiated ONCE
    at startup and reused for all embedding calls.
    """

    DB_PATH     = "jugnu.db"
    MODEL_ID    = "intfloat/e5-small-v2"
    DIM         = 384

    def __init__(self):
        print(f"{_CYAN}[Embedder] Loading e5-small-v2...{_RESET}")

        # SentenceTransformaer caches the model locally after first download.
        # On subsequent runs it loads from the HuggingFace cache (~133MB).
        self._model = SentenceTransformer(self.MODEL_ID)
        print(f"{_GREEN}[Embedder] Model ready.{_RESET}")

        # Open our own SQLite connection.
        # TRAP FIX: Use timeout=5.0 so that if C++ is in the middle of a
        # BEGIN TRANSCATION flush, Python waits up to 5 seconds rather than
        # Crashing immediately with SQLITE_BUSY.
        self._conn = sqlite3.connect(self.DB_PATH, timeout=5.0, check_same_thread=False)

        # Load the sqlite-vec extension into this connection.
        # This gives us access to the vec_episodic VIRTUAL TABLE that C++ created.
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)

        print(f"{_GREEN}[Embedder] Connected to {self.DB_PATH} with sqlite-vec.{_RESET}")

    
    def _embed(self, text: str) -> list[float]:
        """
        Convert a raw string into a 384-dimensional float vector.
        e5-small-v2 expects an 'instruction prefix' for best results:
          - Passage (things being stored):  "passage: <text>"
          - Query  (things being searched): "query: <text>"
        We use "passage:" for everything we store in episodic_memory.
        """
        prefixed = f"passage: {text}"

        # SentenceTransformer returns a numpy float32 array
        embedding: np.ndarray = self._model.encode(prefixed, normalize_embeddings=True)
        return embedding.tolist()

    
    def _serialize_vector(self, vec: list[float]) -> bytes:
        """
        Convert a Python list of floats into a raw bytes blob.
        sqlite-vec expects IEEE 754 single-precision (4 bytes per float).
        struct.pack with format '<{n}f' means: little-endian, n floats.
        """

        return struct.pack(f"<{len(vec)}f", *vec)

    def save_memory(self, app_name: str, window_title: str, text_content: str) -> bool:
        """
        Embed text_content and write it into the jugnu.db vector store.
        This function is the core of the Semantic RAG pipeline.
        Flow:
          1. Run e5-small inference → float[384] vector
          2. INSERT metadata row into episodic_memories (regular SQL table)
          3. INSERT blob into vec_episodic virtual table, mapped to the same rowid
          4. COMMIT the transaction atomically
        The two-table design is deliberate:
          - episodic_memories stores human-readable text (for nightly extraction).
          - vec_episodic stores the binary vector (for cosine similarity search).
          They share a rowid so a JOIN can reunite the vector result with its text.
        """
        if not text_content or not text_content.strip():
            return False

        try:
            print(f"{_CYAN}[Embedder] Embedding memory for: {app_name}{_RESET}")
            vec = self._embed(text_content)
            blob = self._serialize_vector(vec)

            cursor = self._conn.cursor()

            # Step 1: Insert metadata
            cursor.execute(
                "INSERT INTO episodic_memories (app_name, window_title, text_content) VALUES (?, ?, ?);",
                (app_name, window_title, text_content)
            )

            rowid = cursor.lastrowid # sqlite3 gives us the last inserted rowid

            # Step 2: Insert vector, linking it via the SAME rowid
            cursor.execute(
                "INSERT INTO vec_episodic(rowid, embedding) VALUES (?, ?);",
                (rowid, blob)
            )

            self._conn.commit()
            print(f"{_GREEN}[Embedder] Saved memory #{rowid} ({len(text_content)} chars).{_RESET}")
            return True
        except sqlite3.Error as e:
            print(f"{_RED}[Embedder] DB error saving memory: {e}{_RESET}")
            self._conn.rollback()
            return False
        except Exception as e:
            print(f"{_RED}[Embedder] Unexpected error: {e}{_RESET}")
            return False

    def semantic_search(self, query_text: str, limit: int = 5) -> list[str]:
        """
        Find the most semantically similar memories to a query string.
        Uses sqlite-vec's KNN (K-Nearest Neighbour) syntax with MATCH.
        The query uses the "query:" prefix (not "passage:") — this is the
        correct e5-small-v2 asymmetric search pattern.
        Returns: list of text_content strings, ordered by similarity (closest first).
        """

        query_prefixed = f"query: {query_text}"
        query_vec = self._model.encode(query_prefixed, normalize_embeddings=True)
        query_blob = self._serialize_vector(query_vec.tolist())

        try:
            cursor = self._conn.cursor()
            # sqlite-vec KNN syntax: WHERE embedding MATCH ? AND k = ?
            # It automatically orders by distance ASC (closest first).
            cursor.execute(
                """
                SELECT m.text_content
                FROM vec_episodic v
                INNER JOIN episodic_memories m ON v.rowid = m.id
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY distance ASC;
                """,
                (query_blob, limit)
            )
            rows = cursor.fetchall()
            results = [row[0] for row in rows]
            print(f"{_GREEN}[Embedder] Semantic search returned {len(results)} results.{_RESET}")
            return results
        except sqlite3.Error as e:
            print(f"{_RED}[Embedder] DB error during search: {e}{_RESET}")
            return []
    
    def close(self):
        """Cleanly close the SQLite connection."""
        if self._conn:
            self._conn.close()
            print(f"{_YELLOW}[Embedder] SQLite connection closed.{_RESET}")
