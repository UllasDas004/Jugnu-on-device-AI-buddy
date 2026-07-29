import difflib
import sqlite3
import struct
import time
import sqlite_vec
from sentence_transformers import SentenceTransformer
import json
import datetime
import math


# ANSI colors for terminal logging
_CYAN   = "\033[1;36m"
_GREEN  = "\033[1;32m"
_RED    = "\033[1;31m"
_YELLOW = "\033[1;33m"
_RESET  = "\033[0m"

# How many characters of a file to embed as its "fingerprint"
SNIPPET_LEN = 800

# Minimum word count to consider content worth embedding
MIN_WORDS = 5

def _merge_code_diff(ext_code: str, new_code: str) -> tuple[str, bool]:
    """
    Union merge via difflib opcodes. Policy: NEVER delete from knowledge, only ADD.
    A 'delete' opcode does NOT mean the user deleted code — it means the code
    scrolled off the UIA window. With Ghost Clipboard (C++ SetFocus+Ctrl+A+Ctrl+C),
    new_code IS the full buffer, so the diff is a proper full-file diff like git.
    Same policy works correctly for both cases.
    opcodes:
      equal   → keep
      delete  → keep (scrolled away, code still exists)
      insert  → add  (user wrote new code)
      replace → keep old + add new unique lines (accumulate, never lose)
    """
    if not ext_code:
        return new_code, bool(new_code)

    if not new_code:
        return ext_code, False

    ext_lines = ext_code.splitlines()
    new_lines = new_code.splitlines()
    ext_set = set(ext_lines)

    matcher = difflib.SequenceMatcher(None, ext_lines, new_lines, autojunk=False)
    merged = []
    changed = False

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            merged.extend(ext_lines[i1 : i2])
        elif tag == 'delete':
            merged.extend(ext_lines[i1 : i2])        # scrolled away — keep
        elif tag == 'insert':
            merged.extend(new_lines[j1 : j2])         # new code — add
            changed = True
        elif tag == 'replace':
            merged.extend(ext_lines[i1 : i2])       # keep old version
            for line in new_lines[j1 : j2]:         # add new unique lines
                if line not in ext_set:
                    merged.extend(line)
                    changed = True
    
    return '\n'.join(merged), changed


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
            # P2-FIX: Use context manager so the socket FD is always closed.
            # Previously leaked an OS file descriptor on every startup.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
                _s.settimeout(3)
                _s.connect(("8.8.8.8", 53))
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
        # SentenceTransformer returns a numpy float32 array or a Tensor
        embedding = self._model.encode(f"passage: {text}", normalize_embeddings=True)
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

        # P2-FIX: Cap cache size — evict oldest entry if over limit.
        MAX_EMBED_CACHE = 20
        if len(self._last_embedded) >= MAX_EMBED_CACHE:
            self._last_embedded.pop(next(iter(self._last_embedded)))

        self._last_embedded[app_name] = (snippet, now)


        try:
            cursor = self._conn.cursor()

            # --- EMBED ---
            # We embed first so we can do semantic deduplication
            print(f"{_CYAN}[Embedder] Embedding memory for: {app_name}{_RESET}")
            vec = self._embed(snippet)
            blob = self._serialize_vector(vec)
            # --- PHASE 2: DB Semantic Dedup ---
            # Catch duplicates that fell out of the RAM cache (e.g. after restart)
            # L2 distance < 0.14 means cos_sim > 0.99 (matches strict RAM cache)
            cursor.execute("SELECT COUNT(*) FROM episodic_memories")
            if cursor.fetchone()[0] > 0:
                cursor.execute(
                    """
                    SELECT distance FROM vec_episodic 
                    WHERE embedding MATCH ? AND k = 1
                    """,
                    (blob,)
                )
                row = cursor.fetchone()
                if row and row[0] < 0.14:
                    print(f"{_YELLOW}[Embedder] DB Semantic duplicate found (dist={row[0]:.3f}), skipping.{_RESET}")
                    return False

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

    def _should_update_summary(self, capture_count: int) -> bool:
        return capture_count > 1 and (capture_count % 3 == 0)

    def save_knowledge_doc(self, app_name: str, doc: dict | str, engine) -> bool:
        """
        Saves or merges a structured knowledge document using a high-fidelity
        fuzzy Multi-Signal Conflict Resolution Protocol to prevent data corruption.
        """
        try:
            if isinstance(doc, str):
                doc = json.loads(doc)
        except Exception:
            print(f"{_RED}[Embedder] Invalid doc in save_knowledge_doc, skipping.{_RESET}")
            return False
        
        topic = doc.get("topic", "")
        # Gate 6: Handle empty topic validation
        if not topic:
            print(f"{_YELLOW}[Embedder] Skipping save: Empty topic element detected.{_RESET}")
            return False
        
        summary     = doc.get("summary", "") or ""
        content     = doc.get("content", "") or doc.get("blocks", "") or ""
        code_snippet= doc.get("code_snippet", "") or ""
        notes       = doc.get("notes", "") or ""
        tags_str    = json.dumps(doc.get("tags", []))
        file_path   = doc.get("file_path")
        source_url  = doc.get("source_url")
        source_type = doc.get("source_type", "browser")
        window_title= doc.get("window_title", "")

        # Embedding anchor = window_title + stable content text (NOT the AI-generated summary).
        # Summary changes every Gemma run (temperature > 0) → different vector each capture → merge fails.
        # Content and window_title are verbatim-extracted from UIA, 100% stable across captures.
        anchor_prefix = window_title if window_title else topic
        embed_text = f"{anchor_prefix}. {content[:500]}" if content else anchor_prefix

        try:
            cursor = self._conn.cursor()

            # Embed the topic + content snippet for semantic search
            vec = self._model.encode(f"passage: {embed_text}", normalize_embeddings=True)
            blob = self._serialize_vector(vec.tolist())

            # Check if vec_knowledge has anything to search
            cursor.execute("SELECT COUNT(*) FROM knowledge_docs")
            count = cursor.fetchone()[0]

            if count > 0:
                existing_id = None
                ext_code = ext_content = ext_notes = ext_tags = ext_summary = ext_topic = ""
                ext_count = 1

                # ── PRIMARY: Hard identity lookup by Title, URL, or File path ─────────────
                # Vector distance is the wrong tool for identity. Two captures of the
                # same LeetCode page can diverge in topic text and fail the threshold.
                # window title, URL and file path are exact, deterministic, always-correct keys.
                if window_title and source_type == "browser":
                    cursor.execute(
                        "SELECT id, code_snippet, content, notes, tags, capture_count, summary, topic FROM knowledge_docs WHERE window_title = ? LIMIT 1",
                        (window_title,)
                    )
                    match = cursor.fetchone()
                    if not match and source_url:
                        # Fallback to URL if title check didn't match (for older entries)
                        cursor.execute(
                            "SELECT id, code_snippet, content, notes, tags, capture_count, summary, topic FROM knowledge_docs WHERE source_url = ? LIMIT 1",
                            (source_url,)
                        )
                        match = cursor.fetchone()
                    if match:
                        existing_id = match[0]
                        ext_code, ext_content, ext_notes, ext_tags, ext_count, ext_summary, ext_topic = match[1:]
                        print(f"{_CYAN}[Embedder] Anchor match (id={existing_id}). Merging...{_RESET}")

                elif file_path:
                    cursor.execute(
                        "SELECT id, code_snippet, content, notes, tags, capture_count, summary, topic FROM knowledge_docs WHERE file_path = ? LIMIT 1",
                        (file_path,)
                    )
                    match = cursor.fetchone()
                    if match:
                        existing_id = match[0]
                        ext_code, ext_content, ext_notes, ext_tags, ext_count, ext_summary, ext_topic = match[1:]
                        print(f"{_CYAN}[Embedder] File-path match (id={existing_id}). Merging by path...{_RESET}")

                # ── FALLBACK: Semantic vector search (no URL/path available) ──────
                if existing_id is None:
                    cursor.execute(
                        "SELECT distance, rowid FROM vec_knowledge WHERE embedding MATCH ? AND k = 1",
                        (blob,)
                    )
                    row = cursor.fetchone()
                    if row and row[0] < 0.30:
                        existing_id = row[1]
                        cursor.execute(
                            "SELECT code_snippet, content, notes, tags, capture_count, summary, topic FROM knowledge_docs WHERE id = ?",
                            (existing_id,)
                        )
                        match = cursor.fetchone()
                        if match:
                            ext_code, ext_content, ext_notes, ext_tags, ext_count, ext_summary, ext_topic = match
                            print(f"{_CYAN}[Embedder] Semantic match (id={existing_id}, dist={row[0]:.3f}). Merging...{_RESET}")
                        else:
                            existing_id = None  # vec_knowledge out of sync — treat as new

                # ── MERGE: Update existing doc ──────────────────────────────────
                if existing_id is not None:
                    try:
                        old_tags = json.loads(ext_tags) if ext_tags else []
                    except Exception:
                        old_tags = []
                    
                    new_tags = doc.get("tags", [])
                    is_old_ocr = "ocr" in old_tags
                    is_new_ocr = "ocr" in new_tags

                    if not is_old_ocr and is_new_ocr:
                        print(f"{_YELLOW}[Embedder] Ignoring messy OCR capture because a clean UIA capture already exists!{_RESET}")
                        return True
                    
                    changed = False
                    final_topic = topic

                    if is_old_ocr and not is_new_ocr:
                        print(f"{_CYAN}[Embedder] Upgrading knowledge doc from OCR to UIA. Overwriting content entirely.{_RESET}")
                        final_content = content
                        final_notes = notes
                        final_code = code_snippet
                        # BUGFIX: Also overwrite the summary during the upgrade!
                        ext_summary = summary if summary else ext_summary
                        changed = True
                        if "ocr" in old_tags:
                            old_tags.remove("ocr")
                    else:
                        # Use old topic if new one is just "OCR Capture"
                        if final_topic == "OCR Capture" and ext_topic and ext_topic != "OCR Capture":
                            final_topic = ext_topic

                        is_full_buffer = doc.get("full_buffer", False)
                        is_already_solved = any(t in old_tags for t in ("solved", "accepted", "fresh_submission"))
                        is_fresh_submission = "fresh_submission" in doc.get("tags", [])

                        # ── Code merge: union-merge via diff opcodes ─────────────────
                        if is_already_solved and not is_fresh_submission:
                            print(f"{_CYAN}[Embedder] Problem is already SOLVED in DB! Locking reference code_snippet (no overwrite by practice attempt).{_RESET}")
                            final_code = ext_code
                            changed = False
                        elif is_full_buffer:
                            # We have the 100% accurate full file from Ghost Clipboard.
                            # No diff needed — overwrite with truth, allowing deletions.
                            final_code = code_snippet
                            changed = (final_code != ext_code)
                        else:
                            # UIA partial window — use union-merge to prevent scroll-loss.
                            final_code, merged_changed = _merge_code_diff(ext_code or "", code_snippet)
                            changed = changed or merged_changed

                        # Fuzzy Paragraph Dedup (0.55 threshold catches re-extractions of the same text)
                        old_paras = [p.strip() for p in (ext_content or "").split("\n\n") if p.strip()]
                        new_paras = [p.strip() for p in content.split("\n\n") if p.strip()]
                        for np in new_paras:
                            best_ratio, best_idx = 0.0, -1
                            for i, op in enumerate(old_paras):
                                r = difflib.SequenceMatcher(None, np, op).ratio()
                                if r > best_ratio:
                                    best_ratio, best_idx = r, i
                            if best_ratio > 0.55:
                                if len(np) > len(old_paras[best_idx]):
                                    old_paras[best_idx] = np
                                    changed = True
                            else:
                                old_paras.append(np)
                                changed = True
                        final_content = "\n\n".join(old_paras)

                        # Notes Fuzzy Paragraph Dedup (0.55 threshold prevents 3x duplication of algorithmic notes)
                        old_notes = [p.strip() for p in (ext_notes or "").split("\n\n") if p.strip()]
                        new_notes_paras = [p.strip() for p in notes.split("\n\n") if p.strip()]
                        for nnp in new_notes_paras:
                            best_ratio, best_idx = 0.0, -1
                            for i, onp in enumerate(old_notes):
                                r = difflib.SequenceMatcher(None, nnp, onp).ratio()
                                if r > best_ratio:
                                    best_ratio, best_idx = r, i
                            if best_ratio > 0.55:
                                # Replace with longer/richer note instead of appending duplicates
                                if len(nnp) > len(old_notes[best_idx]):
                                    old_notes[best_idx] = nnp
                                    changed = True
                            else:
                                old_notes.append(nnp)
                                changed = True
                        final_notes = "\n\n".join(old_notes)

                    merged_tags = list(dict.fromkeys(old_tags + doc.get("tags", [])))

                    new_count = ext_count + 1 if (changed or len(final_content) > len(ext_content or "") + 100) else ext_count

                    # Safe summary regen: only if content grew significantly (>200 chars) or summary is missing
                    final_summary = ext_summary
                    content_grew = len(final_content) > len(ext_content or "") + 200
                    if (content_grew or not ext_summary) and self._should_update_summary(new_count) and engine is not None:
                        print(f"{_CYAN}[Embedder] Re-calculating semantic summary anchor...{_RESET}")
                        final_summary = engine.generate_summary(topic, final_content, final_code)

                    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    cursor.execute(
                        """UPDATE knowledge_docs
                        SET topic=?, summary=?, content=?, code_snippet=?, notes=?, tags=?,
                            source_url=COALESCE(NULLIF(source_url,''), ?),
                            window_title=COALESCE(NULLIF(window_title,''), ?),
                            last_updated=?, capture_count=?
                        WHERE id=?;""",
                        (final_topic, final_summary, final_content, final_code, final_notes,
                         json.dumps(merged_tags), source_url or "", window_title or "",
                         now, new_count, existing_id)
                    )

                    # Re-embed with the stable content anchor if content changed
                    if changed:
                        anchor_prefix = window_title if window_title else final_topic
                        merged_vec = self._model.encode(f"passage: {anchor_prefix}. {final_content[:500]}", normalize_embeddings=True)
                        cursor.execute("UPDATE vec_knowledge SET embedding=? WHERE rowid=?;", (self._serialize_vector(merged_vec.tolist()), existing_id))

                    self._conn.commit()
                    return True

            # ── FRESH INSERT ──────────────────────────────────────────────────────
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            cursor.execute(
                """INSERT INTO knowledge_docs
                    (topic, source_app, source_type, file_path, source_url, window_title, summary, tags, notes, content, code_snippet, first_seen, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                (topic, app_name, source_type, file_path, source_url, window_title, summary, tags_str, notes, content, code_snippet, now, now)
            )
            rowid = cursor.lastrowid
            cursor.execute("INSERT OR IGNORE INTO vec_knowledge(rowid, embedding) VALUES (?, ?);", (rowid, blob))
            self._conn.commit()
            print(f"{_GREEN}[Embedder] New knowledge doc #{rowid} saved: '{topic}'{_RESET}")
            return True

        except Exception as e:
            print(f"{_RED}[Embedder] save_knowledge_doc error: {e}{_RESET}")
            self._conn.rollback()
            return False
            
    
    def _blended_rerank(self, docs: list[dict]) -> list[dict]:
        """
        Re-ranks KNN results using exponential decay recency + log frequency.
        Keeps scores bounded and smooth — no scale divergence.
        """
        current_time = time.time()
        seconds_in_a_day = 86400.0
        for doc in docs:
            # Exponential decay: half-life ~3 days, bounded [0, 1]
            days_old = (current_time - doc.get('last_updated_ts', current_time)) / seconds_in_a_day
            recency_bonus = math.exp(-0.23 * max(0.0, days_old))
            # Log frequency: capture 1 → 0.0, 3 → 0.47, 10+ → ~1.0
            frequency_bonus = min(1.0, math.log1p(doc.get('capture_count', 1)) / math.log1p(10))
            raw_distance = doc.get('distance', 1.0)
            doc['final_score'] = raw_distance - (0.12 * recency_bonus) - (0.08 * frequency_bonus)
        
        return sorted(docs, key=lambda x: x['final_score'])

    def search_knowledge_docs(self, query_text: str, limit: int = 3, required_tag: str | None = None) -> list[dict]:
        """
        Semantic search over the OKF knowledge_docs table.
        Returns full structured dicts — no flattening.
        Applies: Layer-3 topic deduplication + blended re-ranking.
        """

        try:
            cursor = self._conn.cursor()

            # Lazy guard
            cursor.execute("SELECT COUNT(*) FROM knowledge_docs")
            if cursor.fetchone()[0] == 0:
                return []
            
            query_vec = self._model.encode(f"query: {query_text}", normalize_embeddings=True)
            query_blob = self._serialize_vector(query_vec.tolist())

            # Fetch +1 extra to absorb dedup losses
            fetch_limit = limit + 2

            sql = """
                SELECT kd.topic, kd.tags, kd.content, kd.code_snippet, kd.notes,
                   kd.capture_count, kd.last_updated, kd.source_type, distance
                FROM vec_knowledge vk
                INNER JOIN knowledge_docs kd ON vk.rowid = kd.id
                WHERE vk.embedding MATCH ? AND k = ?
            """
            params = [query_blob, fetch_limit]

            # Domain-isolation filter: push tag check directly into SQL query
            if required_tag:
                sql += " AND kd.tags LIKE ?"
                params.append(f'%"{required_tag}"%')
                # Increase candidate pool so HNSW finds enough tagged items without missing any
                params[1] = max(fetch_limit, 25)

            sql += " ORDER BY distance ASC;"
            cursor.execute(sql, params)
            rows = cursor.fetchall()

            # Build raw result dicts — all fields separate, no flattening
            raw_results = []
            
            for topic,tags_str,content,code_snippet,notes,capture_count,last_updated, source_type, distance in rows:
                try:
                    tags = json.loads(tags_str) if tags_str else []
                except Exception:
                    tags = []

                # Convert last_updated ISO string to epoch for re-ranking
                last_updated_ts = time.time()
                if last_updated:
                    try:
                        dt = datetime.datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                        last_updated_ts = dt.timestamp()
                    except Exception:
                        pass

                raw_results.append({
                    "topic":          topic,
                    "tags":           tags,
                    "content":        content or "",
                    "code_snippet":   code_snippet or "",
                    "notes":          notes or "",
                    "capture_count":  capture_count or 1,
                    "last_updated":   last_updated or "",
                    "last_updated_ts": last_updated_ts,
                    "source_type":    source_type or "browser",
                    "distance":       distance or 1.0,
                })

            # Layer 3 Hard Deduplication — skip docs whose topic is too similar
            # to an already-accepted one (prevents 3 captures of same problem)
            deduped = []
            for doc in raw_results:
                is_dup = False
                for accepted in deduped:
                    if accepted['topic'] == doc['topic']:
                        is_dup = True
                        break

                    sim = difflib.SequenceMatcher(None, accepted['topic'].lower(), doc['topic'].lower()).ratio()

                    if sim > 0.85:
                        is_dup = True
                        break
                
                if not is_dup:
                    deduped.append(doc)
                if len(deduped) >= limit:
                    break

            # Blended re-rank: recency + frequency on top of cosine distance
            reranked = self._blended_rerank(deduped)
            print(f"{_GREEN}[Embedder] Knowledge search: {len(reranked)} docs (reranked).{_RESET}")
            return reranked
            
        except Exception as e:
            print(f"{_RED}[Embedder] search_knowledge_docs error: {e}{_RESET}")
        return []
                

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
