"""
The Python-side semantic memory writer for Jugnu.
Responsibility:
    - Load the e5-small-v2 model to generate 384-dim text vectors.
    - Receive text events from ipc_client.py.
    - Write a SHORT SNIPPET + file_path into jugnu.db (not the full file).
    - Provide semantic search for RAG context injection.
Architecture Optimizations:
    - Snippet-only storage: embed only first 300 chars, store filepath for full retrieval.
    - Quality gate: skip content with fewer than 5 words (junk filter).
    - In-memory dedup: skip if same content was embedded in last 60 seconds.
    - DB dedup: skip if exact text snippet already exists in episodic_memories.
    - INSERT OR IGNORE on vec_episodic: prevents UNIQUE constraint crash on retry.
    - Lazy search: skip KNN query entirely if DB is empty (day 1 protection).
"""
import sqlite3
import struct
import time
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

# How many characters of a file to embed as its "fingerprint"
SNIPPET_LEN = 300

# Minimum word count to consider content worth embedding
MIN_WORDS = 5

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
        # We must check if we're online, otherwise HF throws getaddrinfo failed.
        import socket
        try:
            socket.setdefaulttimeout(3)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
            is_online = True
        except Exception:
            is_online = False
            
        self._model = SentenceTransformer(self.MODEL_ID, local_files_only=not is_online)
        print(f"{_GREEN}[Embedder] Model ready.{_RESET}")

        # OPTIMIZATION: In-memory throttle
        # Maps app_name -> (snippet, timestamp) of the last embedded content.
        # If the same app sends the same snippet within 60 seconds, we skip it.
        self._last_embedded: dict[str, tuple[str, float]] = {}

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
        Convert text into a 384-dim float vector.
        IMPORTANT: e5-small-v2 is asymmetric.
          - Stored passages must use prefix: "passage: <text>"
          - Search queries must use prefix:  "query: <text>"
        """
        # SentenceTransformer returns a numpy float32 array
        embedding: np.ndarray = self._model.encode(f"passage: {text}", normalize_embeddings=True)
        return embedding.tolist()

    
    def _serialize_vector(self, vec: list[float]) -> bytes:
        """
        Convert a Python list of floats into a raw bytes blob.
        sqlite-vec expects IEEE 754 single-precision (4 bytes per float).
        struct.pack with format '<{n}f' means: little-endian, n floats.
        """

        return struct.pack(f"<{len(vec)}f", *vec)

    def save_memory(self, app_name: str, window_title: str, text_content: str, file_path: str | None = None) -> bool:
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

          TRAP FIX: We check for duplicate text before inserting.
          - If the same text was already stored, we skip it entirely.
          - This prevents both the episodic_memories duplication AND the
          - vec_episodic UNIQUE constraint failure that happens when rowids
          - get out of sync on a retry.
        """
        # --- GATE 1: Empty content ---
        if not text_content or not text_content.strip():
            return False

        # ---GATE 2: Quality filter - skip junk like "ok", single variable names ---
        if len(text_content.split()) < MIN_WORDS:
            print(f"{_YELLOW}[Embedder] Skipping low-quality content (<{MIN_WORDS} words).{_RESET}")
            return False

        # --- GATE 3: Snppet extraction ---
        # For files: we only embed the first SNIPPET_LEN chars as the "fingerprint".
        # The full content stays on disk at file_path - we re-read it at query time.
        snippet = text_content[:SNIPPET_LEN]

        # --- GATE 4: In-memory throttle ---
        # If same app sent snippet within 60 seconds, skip entirely.
        now = time.time()
        last_snippet, last_time = self._last_embedded.get(app_name, ("", 0.0))
        if snippet == last_snippet and (now - last_time) < 60.6:
            print(f"{_YELLOW}[Embedder] Throttled duplicate from {app_name}.{_RESET}")
            return False
        self._last_embedded[app_name] = (snippet, now)


        try:
            cursor = self._conn.cursor()

            # --- GATE 5: DB dedup - O(1) thanks to idx_episodic_text_index ---

            cursor.execute(
                "SELECT id FROM episodic_memories WHERE text_content = ? LIMIT 1;",
                (snippet,)
            )
            if cursor.fetchone():
                print(f"{_YELLOW}[Embedder] Duplicate snippet in DB, skipping.{_RESET}")
                return False
            
            # --- EMBED & STORE ---
            print(f"{_CYAN}[Embedder] Embedding memory for: {app_name}{_RESET}")
            vec = self._embed(snippet)
            blob = self._serialize_vector(vec)

            # Insert metadata row (snippet only, not full file content)
            cursor.execute(
                """INSERT INTO episodic_memories
                    (app_name, window_title, file_path, text_content)
                   VALUES (?, ?, ?, ?);""",
                   (app_name, window_title, file_path, snippet)
            )
            rowid = cursor.lastrowid

            # Insert vector linked by the same rowid.
            # INSERT OR IGNORE: safety not agaist UNIQUE constraint on retry.
            cursor.execute(
                "INSERT OR IGNORE INTO vec_episodic(rowid, embedding) VALUES (?, ?);",
                (rowid, blob)
            )
            self._conn.commit()
            print(f"{_GREEN}[Embedder] Saved memory #{rowid} for {app_name} ({len(snippet)} chars).{_RESET}")
            return True
        except sqlite3.Error as e:
            print(f"{_RED}[Embedder] DB error: {e}{_RESET}")
            self._conn.rollback()
            return False

        except Exception as e:
            print(f"{_RED}[Embedder] Unexpected error: {e}{_RESET}")
            return False

    def semantic_search(self, query_text: str, limit: int = 5) -> list[dict]:
        """
        Find the most semantically similar memories to a query.
        Returns a list of dicts with 'snippet' and 'file_path' keys.
        The caller can re-read the file from file_path for full context.

        OPTIMIZATION: Lazy guard — skip KNN entirely if DB is empty.
        """

        try:
            # Lazy guard: don't run KNN on empty table (day 1 protection)
            count = self._conn.execute(
                "SELECT COUNT(*) FROM episodic_memories"
            ).fetchone()[0]
            if count == 0:
                return []

            query_vec = self._model.encode(f"query: {query_text}", normalize_embeddings=True)
            query_blob = self._serialize_vector(query_vec.tolist())

            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT m.text_content, m.file_path
                FROM vec_episodic v
                INNER JOIN episodic_memories m ON v.rowid = m.id
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY distance ASC;
                """,
                (query_blob, limit)
            )
            rows = cursor.fetchall()
            results = [{"snippet": row[0], "file_path": row[1]} for row in rows]
            print(f"{_GREEN}[Embedder] Semantic search: {len(results)} results.{_RESET}")
            return results
        except sqlite3.Error as e:
            print(f"{_RED}[Embedder] DB error during search: {e}{_RESET}")
            return []
        except Exception as e:
            print(f"{_RED}[Embedder] Unexpected error: {e}{_RESET}")
            return []

    
    def close(self):
        """Cleanly close the SQLite connection."""
        if self._conn:
            self._conn.close()
            print(f"{_YELLOW}[Embedder] SQLite connection closed.{_RESET}")
