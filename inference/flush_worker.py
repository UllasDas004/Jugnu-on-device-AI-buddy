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

from numpy import extract
import sqlite3
from transformers.models.esm.openfold_utils import chunk_layer
from pydantic.fields import _fields
import ctypes
import sqlite3
import threading
import time
import ctypes

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
    Split text into chunks of ~`size` characters.
    Always breaks at a space boundary to avoid cutting words in half.
    """
    chunks = []
    text = text.strip()
    while len(text) > size:
        # Find the last space within the size limit
        split_at = text.rfind(' ', 0, size)
        if split_at == -1:
            split_at = size # no space found, hard cut
        
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

            # Split the raw OCR blob into focused 500-character windows
            chunks = _chunk_text(raw_text, CHUNK_SIZE)

            for chunk in chunks:
                total_chunks += 1

                # GATE: Skip obviously short UI noise before touching Gemma
                if len(chunk.split()) < MIN_CHUNK_WORDS:
                    continue

                # EXTRACT: Ask Gemma to pull out only useful technical content
                extracted = self._engine.extract_ocr_chunk(chunk)

                # SAVE: Only embed if Gemma found something worth keeping
                if extracted and len(extracted.split()) >= MIN_CHUNK_WORDS:
                    # Print the extracted text to the terminal for testing/demo purposes
                    print(f"\n{_YELLOW}--- GEMMA EXTRACTED KNOWLEDGE ---{_RESET}")
                    print(f"{_GREEN}{extracted}{_RESET}")
                    print(f"{_YELLOW}---------------------------------{_RESET}\n")

                    saved = self._embedder.save_memory(
                        app_name        = app_name,
                        window_title    = app_name,
                        text_content    = extracted,
                        file_path       = None     # OCR memories have no file path
                    )
                    if saved:
                        useful_saved += 1
                
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