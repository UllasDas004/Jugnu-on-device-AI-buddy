"""
FlushWorker — Stage 2 of the OCR Data Cleaning Pipeline
Architecture:
    C++ screen_reader.cpp writes raw OCR blobs → ocr_buffer (SQLite)
    FlushWorker (this file) wakes every 60s → reads ocr_buffer
        → checks AC power (skip on battery)
        → deletes stale rows (> 10 min old) without processing
        → chunks each blob into 500-char pieces
        → feeds each chunk to Gemma's extract_ocr_chunk()
        → saves clean extracted text to episodic_memories via embedder
        → deletes all processed rows from ocr_buffer
Why chunking?
    A 3000-char OCR dump may have 500 chars of gold buried in noise.
    Gemma's extraction quality is best on focused 500-char windows.
    Feeding the whole blob at once confuses the extractor.
"""

import difflib
import sqlite3
import threading
import time
import ctypes
import re

_CYAN   = "\033[1;36m"
_GREEN  = "\033[1;32m"
_YELLOW = "\033[1;33m"
_RED    = "\033[1;31m"
_RESET  = "\033[0m"

DB_PATH          = "jugnu.db"
FLUSH_INTERVAL_S = 60   # seconds between flush cycles
STALE_MINUTES    = 10   # rows older than this are deleted without processing
CHUNK_SIZE       = 500  # characters per chunk sent to Gemma
MIN_CHUNK_WORDS  = 8    # gate: skip chunks with fewer than this many words

def _preprocess_ocr(text: str) -> str:
    """
    Clean raw OCR dump before chunking.
    Removes common garbage that ruins Gemma's extraction quality:
      - Lines that are only numbers (scrollbar positions like "123 / 456")
      - Lines shorter than 3 chars (isolated symbols, stray letters)
      - Collapses 3+ consecutive newlines into 2 (paragraph boundary)
    """
    lines = text.splitlines()
    clean = []
    for line in lines:
        stripped = line.strip()
        # Skip pure-number lines (scrollbar artifacts)
        if re.fullmatch(r'[\d\s/|%]+', stripped):
            continue
        # Skip very short lines (single chars,menu dots, etc...)
        if len(stripped) < 3:
            continue
        clean.append(stripped)

    # Collapse 3+ blank lines into 2 (preserve paragraph breaks)
    result = '\n'.join(clean)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()

# ── Power Check ─────────────────────────────────────────────────────────────

class _SYSTEM_POWER_STATUS(ctypes.Structure):
    """Win32 SYSTEM_POWER_STATUS struct for checking AC vs battery."""
    _fields_ = [
        ("ACLineStatus",        ctypes.c_byte),   # 0=battery, 1=AC, 255=unknown
        ("BatteryFlag",         ctypes.c_byte),
        ("BatteryLifePercent",  ctypes.c_byte),
        ("SystemStatusFlag",    ctypes.c_byte),
        ("BatteryLifeTime",     ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]

def _is_on_ac_power() -> bool:
    """Returns True if the laptop is plugged into AC power."""
    status = _SYSTEM_POWER_STATUS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
    return status.ACLineStatus == 1

# ── Text Chunker ─────────────────────────────────────────────────────────────

def _chunk_text(text: str, size: int) -> list[str]:
    """
    Context-aware chunker. Tries to split at natural boundaries in priority order:
      Priority 1: blank line (\n\n) — paragraph/code block boundary
      Priority 2: newline (\n)      — single line boundary
      Priority 3: sentence end '. ' — prose boundary
      Priority 4: hard cut          — absolute last resort
    This prevents cutting mid-function or mid-sentence.
    """
    chunks = []
    text = text.strip()
    while len(text) > size:
        # Try each boundary in priority order
        split_at = -1
        for separator in ['\n\n', '\n', '. ']:
            pos = text.rfind(separator, 0, size)
            if pos != -1:
                split_at = pos + len(separator)
                break
        
        if split_at == -1:
            split_at = size     # Hard cut as last resort

        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()

    if text:
        chunks.append(text)

    return [c for c in chunks if c] # filter empty


# ── FlushWorker Class ────────────────────────────────────────────────────────

class FlushWorker:
    """
    Daemon background thread that drains ocr_buffer, filters with Gemma, 
    and saves clean knowledge to the vector database.
    """

    def __init__(self, embedder, engine):
        self._embedder = embedder
        self._engine   = engine
        self._thread   = threading.Thread(
            target=self._run,
            daemon=True,       # dies automatically when main thread exits
            name="FlushWorker"
        )

    def start(self):
        self._thread.start()
        print(f"{_CYAN}[FlushWorker] OCR cleaning pipeline started. "
              f"Flushing every {FLUSH_INTERVAL_S}s on AC power.{_RESET}")

    def _run(self):
        """Main loop: sleep → check power → flush."""
        while True:
            time.sleep(FLUSH_INTERVAL_S)
            try:
                self._flush_cycle()
            except Exception as e:
                print(f"{_RED}[FlushWorker] Unhandled cycle error: {e}{_RESET}")
        
    def _flush_cycle(self):
        """One complete drain-and-clean cycle."""

        # GATE: Only run when plugged in to protect battery
        if not _is_on_ac_power():
            print(f"{_YELLOW}[FlushWorker] On battery — skipping flush cycle.{_RESET}")
            return

        conn = sqlite3.connect(DB_PATH, timeout=0.5)

        # STALENESS PURGE: Delete rows older than STALE_MINUTES without processing.
        # These are from a session the user has long moved on from.

        deleted = conn.execute(
            f"DELETE FROM ocr_buffer "
            f"WHERE datetime(timestamp) < datetime('now', '-{STALE_MINUTES}minutes');"
        ).rowcount

        if deleted > 0:
            print(f"{_YELLOW}[FlushWorker] Purged {deleted} stale rows (>{STALE_MINUTES} min old).{_RESET}")
        conn.commit()
        
        # READ: Only process rows that have settled for at least 30s
        # (prevents reading mid-burst while C++ is still capturing the same app)

        rows = conn.execute(
            "SELECT id, app_name, raw_text FROM ocr_buffer "
            "WHERE datetime(timestamp) < datetime('now', '-30 seconds') "
            "ORDER BY id ASC;"
        ).fetchall()

        if not rows:
            conn.close()
            return

        print(f"{_CYAN}[FlushWorker] Processing {len(rows)} buffered OCR rows...{_RESET}")

        ids_to_delete   = []
        useful_saved    = 0
        total_chunks    = 0
        for row_id, app_name, raw_text in rows:
            ids_to_delete.append(row_id)

            # --- PHASE 1: Pre-Gemma OCR Deduplication ---
            if not hasattr(self, '_last_raw_by_app'):
                self._last_raw_by_app = {}
            # P1-FIX: Cap cache size to prevent unbounded memory growth.
            # OCR blobs are ~2000 chars each; 20 entries = ~40KB max.
            MAX_CACHE = 20
            if len(self._last_raw_by_app) > MAX_CACHE:
                self._last_raw_by_app.pop(next(iter(self._last_raw_by_app)))
                
            last_raw = self._last_raw_by_app.get(app_name, "")
            # Calculate how similar this OCR dump is to the last one we saw
            similarity = difflib.SequenceMatcher(None, raw_text, last_raw).ratio()

            if similarity > 0.85:
                print(f"{_YELLOW}[FlushWorker] Screen for {app_name} is {similarity*100:.1f}% unchanged. Skipping AI extraction.{_RESET}")
                continue # Skip the Gemma extraction entirely!

            # Update cache with the new screen state
            self._last_raw_by_app[app_name] = raw_text
            # --------------------------------------------

            # Chunk and extract — collect ALL extractions from this ONE OCR into a list
            chunks = _chunk_text(raw_text, CHUNK_SIZE)
            all_extractions = []
            prev_extracted = ""

            for chunk in chunks:
                total_chunks += 1

                # GATE: Skip obviously short UI noise before touching Gemma
                if len(chunk.split()) < MIN_CHUNK_WORDS:
                    continue

                # Pass prev_extracted so Gemma knows what it was extracting before
                extracted = self._engine.extract_ocr_chunk(chunk, prev_context = prev_extracted)

                if extracted and len(extracted.split()) >= MIN_CHUNK_WORDS:
                    print(f"\n{_YELLOW}--- GEMMA EXTRACTED KNOWLEDGE ---{_RESET}")
                    print(f"{_GREEN}{extracted}{_RESET}")
                    print(f"{_YELLOW}---------------------------------{_RESET}\n")
                    
                    all_extractions.append(extracted)
                    prev_extracted = extracted
                else:
                    prev_extracted = chunk[:100] if chunk else ""

            # --- Final Synthesis Pass (ONE call per OCR) ---
            if all_extractions:
                print(f"\n{_CYAN}  [Gemma] Synthesizing {len(all_extractions)} extractions into knowledge doc...{_RESET}")
                
                # Always save raw joined text to episodic_memories (the log)
                combined = "\n\n".join(all_extractions)
                saved = self._embedder.save_memory(
                    app_name=app_name,
                    window_title=app_name,
                    text_content=combined,
                    file_path=None
                )

                # Try to produce a structured OKF JSON knowledge doc
                doc_json = self._engine.synthesize_ocr_extractions(all_extractions)

                if doc_json:
                    print(f"\n{_YELLOW}━━━ SYNTHESIZED KNOWLEDGE DOC ━━━{_RESET}")
                    import json
                    doc = json.loads(doc_json)
                    print(f"{_GREEN}TOPIC: {doc.get('topic','')}{_RESET}")
                    print(f"{_CYAN}TAGS:  {', '.join(doc.get('tags', []))}{_RESET}")
                    print(f"{_GREEN}{doc.get('content','')[:300]}...{_RESET}")
                    print(f"{_YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{_RESET}\n")
                    saved = self._embedder.save_knowledge_doc(app_name, doc_json, self._engine)

                    if saved:
                        useful_saved += 1
                        print(f"{_GREEN}[FlushWorker] Saved 1 combined memory from {len(all_extractions)} chunks.{_RESET}")
                else:
                    # JSON synthesis failed — raw text already saved to episodic_memories above
                    print(f"{_YELLOW}  [FlushWorker] Synthesis failed — raw text saved to episodic_memories only.{_RESET}")
                    useful_saved += 1
            else:
                print(f"{_YELLOW}  No useful content found in this OCR.{_RESET}")
                
        # DELETE: Clean up all rows just processed
        if ids_to_delete:
            placeholders = ",".join("?" * len(ids_to_delete))
            conn.execute(
                f"DELETE FROM ocr_buffer WHERE id IN ({placeholders});",
                ids_to_delete
            )
            conn.commit()

        conn.close()
        print(f"{_GREEN}[FlushWorker] Done. "
          f"{useful_saved} memories saved from {total_chunks} chunks "
          f"({len(rows)} raw rows processed).{_RESET}")