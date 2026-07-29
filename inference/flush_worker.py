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
import json
from typing import TypedDict
import datetime

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
      - Lines shorter than 2 chars (isolated symbols, stray letters)
      - Known browser/LeetCode nav chrome (Submit, Premium, Editorial, etc.)
      - Collapses 3+ consecutive newlines into 2 (paragraph boundary)
    """
    # Known LeetCode / browser UI nav tokens that are NEVER technical content.
    # These appear as standalone lines at the top of the RootWebArea Document.
    NAV_LABELS = {
        "submit", "editorial", "solutions", "submissions", "description",
        "testcase", "test result", "premium", "daily question", "hint",
        "topics", "companies", "code", "run", "accepted", "solved",
        "easy", "medium", "hard", "discussion", "similar questions",
        "related topics", "next", "prev", "previous", "advertisement",
        "yes", "no", "acceptance rate", "staff", "comment", "choose a type",
        "all solutions", "saved", "you must run your code first", "auto"
    }
    
    # Text triggers that indicate the end of the actual problem description.
    # Everything after these is usually hints, discussions, or related questions.
    CUTOFF_TRIGGERS = [
        "seen this question in a real interview before?",
        "💡 discussion rules",
        "comments ("
    ]

    lines = text.splitlines()
    clean = []
    for line in lines:
        stripped = line.strip()
        lower_stripped = stripped.lower()
        
        # Hard cutoff: stop reading if we hit the comments or footer of a LeetCode problem
        if any(trigger in lower_stripped for trigger in CUTOFF_TRIGGERS):
            break

        # Skip pure-number lines (scrollbar artifacts, acceptance rates, fractions)
        if re.fullmatch(r'[\d\s/|%.,kkm]+', lower_stripped):
            continue
        # Skip very short lines (single chars, menu dots, etc.)
        if len(stripped) < 2:
            continue
        # Skip known nav chrome labels
        if lower_stripped in NAV_LABELS or lower_stripped.startswith("hint "):
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

# ── Code Detector ────────────────────────────────────────────────────────
def _detect_code_tags(code: str) -> list[str]:
    """Heuristic language detection from code. No LLM needed."""
    tags = []
    if any(k in code for k in ["#include", "vector<", "std::", "->", "endl", "::"]):
        tags.append("cpp")
    elif any(k in code for k in ["def ", "import ", "elif ", "print(", "lambda "]):
        tags.append("python")
    elif any(k in code for k in ["public class", "ArrayList", "System.out", "@Override"]):
        tags.append("java")
    elif any(k in code for k in ["func ", "package ", "fmt.Print", ":= "]):
        tags.append("go")
    elif any(k in code for k in ["fn ", "let mut ", "impl ", "use std"]):
        tags.append("rust")
    if "Solution" in code and any(k in code for k in ["nums", "target", "node", "root"]):
        tags.append("leetcode")
    if "dp[" in code or "memo" in code.lower():
        tags.append("dynamic programming")
    return tags or ["code"]

# Declarative registry: Add any competitive programming platform here without changing code logic
class CPPlatform(TypedDict):
    name: str
    domain_regex: str
    path_regex: str
    read_only_tabs: set[str]

CP_PLATFORMS: list[CPPlatform] = [
    {
        "name": "leetcode",
        "domain_regex": r"leetcode\.(?:com|cn)",
        "path_regex": r"/problems/([a-z0-9-]+)(?:/([a-z]+))?",
        "read_only_tabs": {"editorial", "solutions", "solution", "discussion", "comments"}
    },
    {
        "name": "codeforces",
        "domain_regex": r"codeforces\.com",
        "path_regex": r"/(?:contest|problemset/problem)/(\d+)/([a-z0-9]+)",
        "read_only_tabs": {"tutorial", "status", "standings"}
    },
    {
        "name": "atcoder",
        "domain_regex": r"atcoder\.jp",
        "path_regex": r"/contests/([a-z0-9-_]+)/tasks/([a-z0-9-_]+)",
        "read_only_tabs": {"editorial", "standings", "submissions"}
    },
    {
        "name": "codechef",
        "domain_regex": r"codechef\.com",
        "path_regex": r"/(?:problems|submit)/([a-z0-9-_]+)",
        "read_only_tabs": {"editorial", "solutions", "discuss"}
    }
]
def _parse_cp_url(url: str | None) -> dict | None:
    """
    Generalized regex parser for competitive programming platforms.
    Returns domain metadata and read-only tab status without hardcoding.
    """

    if not url:
        return None

    url_lower = url.lower()

    for plat in CP_PLATFORMS:
        if re.search(plat["domain_regex"], url_lower):
            match = re.search(plat["path_regex"], url_lower)
            if match:
                # Combine regex groups to form a unique problem slug (e.g., 'two-sum' or '1234-a')
                slug = "-".join([g for g in match.groups() if g]).rstrip("-")
                # Detect tab if present in path, otherwise default to coding workspace
                last_grp = match.group(len(match.groups())) if len(match.groups()) > 1 else None
                tab = last_grp or "workspace"
                
                is_read_only = any(ro in url_lower for ro in plat["read_only_tabs"])
                return {
                    "platform": plat["name"],
                    "slug": slug,
                    "tab": tab,
                    "is_read_only": is_read_only
                }
    return None
            
# ─────────────────────────────────────────────────────────────────────────────
# PRACTICE MODE SESSION MANAGEMENT
# Called from ipc_client.py when CP_STUCK fires on a solved problem.
# Each function opens and closes its own short-lived SQLite connection to avoid
# cross-thread lock conflicts with FlushWorker's own conn.
# ─────────────────────────────────────────────────────────────────────────────
def _get_or_create_practice_session(slug: str, platform: str) -> dict | None:
    """
    Returns the active practice session for this problem slug.
    Active = exists AND last_seen within 2h AND is_solved == 0.
    Creates a fresh session row if none found.
    Returns None on DB error.
    """
    TWO_HOURS_AGO = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    ).isoformat()

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, problem_slug, platform, hint_level, last_hint_text,
                   code_snapshot, detected_approach, stuck_count, is_solved
            FROM practice_sessions
            WHERE problem_slug = ?
              AND is_solved    = 0
              AND last_seen    > ?
            ORDER BY last_seen DESC
            LIMIT 1
            """,
            (slug, TWO_HOURS_AGO)
        )
        row = cur.fetchone()

        if row:
            session = dict(row)
            print(f"{_CYAN}[Practice] Resumed session for '{slug}' "
                  f"(hint_level={session['hint_level']}, "
                  f"stuck_count={session['stuck_count']}){_RESET}")

            conn.close()
            return session

        # No active session — create a fresh one
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur.execute(
            """
            INSERT INTO practice_sessions
                (problem_slug, platform, session_start, last_seen,
                 hint_level, stuck_count, is_solved)
            VALUES (?, ?, ?, ?, 0, 0, 0)
            """,
            (slug, platform, now, now)
        )
        conn.commit()
        new_id = cur.lastrowid
        print(f"{_GREEN}[Practice] New session #{new_id} created for '{slug}'{_RESET}")
        conn.close()
        return {
            "id": new_id, "problem_slug": slug, "platform": platform,
            "hint_level": 0, "last_hint_text": None,
            "code_snapshot": None, "detected_approach": None,
            "stuck_count": 0, "is_solved": 0,
        }
    except Exception as e:
        print(f"{_RED}[Practice] _get_or_create_practice_session error: {e}{_RESET}")
        return None

def _compute_hint_level(session: dict, current_code: str, new_approach: str) -> tuple[int, bool, float]:
    """
    Decides the hint level for this trigger based purely on code similarity:
      - Code barely changed (similarity >= 0.70) → advance level (genuinely stuck)
      - Code changed significantly → re-assess at same level (user is iterating)
    Approach changes are handled implicitly by the code diff.
    Returns: (final_hint_level, should_update_db)
    """
    stored_snapshot = session.get("code_snapshot") or ""
    current_level   = session.get("hint_level", 0)
    MAX_LEVEL       = 3

    if stored_snapshot and current_code:
        similarity = difflib.SequenceMatcher(
            None, stored_snapshot, current_code, autojunk=False
        ).ratio()
        print(f"\033[90m[Practice] Code similarity vs last snapshot: {similarity:.2f}\033[0m")

        if similarity >= 0.70:
            new_level = min(current_level + 1, MAX_LEVEL)
            print(f"{_YELLOW}[Practice] Code unchanged (sim={similarity:.2f}). "
                  f"Advancing: {current_level} → {new_level}{_RESET}")
            return new_level, True, similarity
        else:
            print(f"{_YELLOW}[Practice] Code changed (sim={similarity:.2f}). "
                  f"Re-assessing at level {current_level}.{_RESET}")
            return current_level, False, similarity

    return 0, True, 0.0  # First hint ever

def _update_practice_session(
    session_id:        int,
    hint_level:        int,
    hint_text:         str,
    current_code:      str,
    detected_approach: str,
    is_solved:         bool = False,
) -> None:
    """
    Persists session state back to DB after a hint trigger.
    Increments stuck_count. Called AFTER generate_practice_hint() returns.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.execute(
            """
            UPDATE practice_sessions
            SET hint_level        = ?,
                last_hint_text    = ?,
                code_snapshot     = ?,
                detected_approach = ?,
                last_seen         = ?,
                is_solved         = ?,
                stuck_count       = stuck_count + 1
            WHERE id = ?
            """,
            (hint_level, (hint_text or "")[:2000], (current_code or "")[:3000],
             detected_approach, now, 1 if is_solved else 0, session_id)
        )
        conn.commit()
        conn.close()
        print(f"{_GREEN}[Practice] Session #{session_id} updated "
              f"(level={hint_level}, solved={is_solved}){_RESET}")
    except Exception as e:
        print(f"{_RED}[Practice] _update_practice_session error: {e}{_RESET}")

# ── FlushWorker Class ────────────────────────────────────────────────────────

class FlushWorker:
    """
    Daemon background thread that drains ocr_buffer, filters with Gemma, 
    and saves clean knowledge to the vector database.
    """

    def __init__(self, embedder, engine, state=None):
        self._embedder = embedder
        self._engine   = engine
        self._state    = state  # StateManager ref — used to cache latest screen text
        # Per-app dedup caches — track last seen code/page text to skip unchanged screens
        self._last_raw_by_app:  dict[str, str] = {}
        self._last_code_by_app: dict[str, str] = {}
        self._last_page_by_app: dict[str, str] = {}
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
            "SELECT id, app_name, window_title, raw_text FROM ocr_buffer "
            "WHERE datetime(timestamp) < datetime('now', '-30 seconds') "
            "ORDER BY id ASC;"
        ).fetchall()

        if not rows:
            conn.close()
            return

        print(f"{_CYAN}[FlushWorker] Processing {len(rows)} buffered OCR rows...{_RESET}")

        ids_to_delete   = []
        ids_failed      = []
        useful_saved    = 0
        total_chunks    = 0
        for row_id, app_name, window_title, raw_text in rows:
            # NOTE: We do NOT pre-queue for deletion here.
            # A row is only deleted if it is successfully synthesized.
            # Failed rows stay in ocr_buffer and are retried next cycle.
            try:
                # Strip null bytes and Unicode replacement chars that crash the C++ LLM tokenizer
                raw_text = raw_text.replace('\ufffc', '').replace('\x00', '')

                # P1-FIX: Cap cache size to prevent unbounded memory growth.
                # OCR blobs are ~2000 chars each; 20 entries = ~40KB max.
                MAX_CACHE = 20
                if len(self._last_raw_by_app) > MAX_CACHE:
                    self._last_raw_by_app.pop(next(iter(self._last_raw_by_app)))
                    self._last_code_by_app.pop(next(iter(self._last_code_by_app)), None)
                    self._last_page_by_app.pop(next(iter(self._last_page_by_app)), None)
                    
                last_raw = self._last_raw_by_app.get(app_name, "")
                last_code = self._last_code_by_app.get(app_name, "")
                last_page = self._last_page_by_app.get(app_name, "")
                
                skip_extraction = False
                current_code = ""
                current_page = ""
                code_sim = 0.0
                page_sim = 0.0

                try:
                    parsed = json.loads(raw_text)
                    if isinstance(parsed, list):
                        # Separate Code (Edit controls) and Page (Document/Text controls)
                        current_code = "\n".join([sec.get("text", "") for sec in parsed if sec.get("type") == "Edit"])
                        current_page = "\n".join([sec.get("text", "") for sec in parsed if sec.get("type") != "Edit"])

                        code_sim = difflib.SequenceMatcher(None, current_code, last_code).quick_ratio() if last_code or current_code else 1.0
                        page_sim = difflib.SequenceMatcher(None, current_page, last_page).quick_ratio() if last_page or current_page else 1.0

                        # Only skip if BOTH the code and the page are >85% unchanged
                        if code_sim > 0.95 and page_sim > 0.95:
                            skip_extraction = True
                            print(f"{_YELLOW}[FlushWorker] Area Match: Code={code_sim*100:.1f}%, Page={page_sim*100:.1f}%. Skipping AI extraction.{_RESET}")
                        else:
                            print(f"{_CYAN}[FlushWorker] Area Change: Code={code_sim*100:.1f}%, Page={page_sim*100:.1f}%. Processing screen.{_RESET}")
                    
                    else:
                        raise ValueError("Not a list")
                except Exception:
                    # Fallback to standard full text matching if not JSON
                    similarity = difflib.SequenceMatcher(None, raw_text, last_raw).quick_ratio()

                    if similarity > 0.95:
                        skip_extraction = True
                        print(f"{_YELLOW}[FlushWorker] Screen is {similarity*100:.1f}% unchanged. Skipping AI extraction.{_RESET}")
                
                if skip_extraction:
                    self._last_raw_by_app[app_name] = raw_text
                    self._last_code_by_app[app_name] = current_code
                    self._last_page_by_app[app_name] = current_page

                    if self._state:
                        self._state.update_screen_text(app_name, raw_text)
                    ids_to_delete.append(row_id)
                    continue

                # Update caches for normal processing
                self._last_raw_by_app[app_name] = raw_text
                self._last_code_by_app[app_name] = current_code
                self._last_page_by_app[app_name] = current_page

                if self._state:
                    self._state.update_screen_text(app_name, raw_text)
                # --------------------------------------------

                # ── DEBUG: Show raw UIA text in terminal ──────────────────────
                print(f"\033[90m{'─'*60}\033[0m")
                print(f"\033[90m[DEBUG] Raw UIA for {app_name} ({len(raw_text)} chars):\033[0m")
                print(f"\033[90m{raw_text[:600]}{'...(truncated)' if len(raw_text) > 600 else ''}\033[0m")
                print(f"\033[90m{'─'*60}\033[0m")
                # ─────────────────────────────────────────────────────────────

                # --- PHASE 2: Parse and route by UIA format ---

                # Detect IDE source by checking app_name and the raw_text for a file path hint
                # screen_reader.cpp prepends "FILE_PATH: <path>\n" for IDE captures
                file_path = None
                source_url = None
                ide_apps = {"code.exe", "devenv.exe", "clion64.exe", "idea64.exe", "pycharm64.exe",
                "rider64.exe", "webstorm64.exe", "sublime_text.exe", "notepad++.exe"}

                if app_name.lower() in ide_apps:
                    fp_match = re.search(r'FILE_PATH:\s*(.+)', raw_text)
                    if fp_match:
                        file_path = fp_match.group(1).strip()
                    else:
                        file_path = app_name  # Placeholder so source_type becomes 'ide'

                # Try new structured JSON format (post-UIA-boundary fix)
                structured_sections = None
                try:
                    parsed = json.loads(raw_text)
                    if isinstance(parsed, list) and parsed:
                        structured_sections = parsed
                except (ValueError, TypeError):
                    pass

                VERBATIM_TYPES = {"Edit"}
                CONTENT_TYPES  = {"Document", "Text", "MainContent"}


                if structured_sections is not None:
                    # ── NEW: Structured JSON from C++ with control type metadata ─────
                    print(f"{_CYAN}[FlushWorker] Structured UIA: {len(structured_sections)} sections{_RESET}")
                    for i, sec in enumerate(structured_sections):
                        stype = sec.get('type', '?')
                        sname = sec.get('name', '')
                        stext = sec.get('text', '')
                        print(f"\033[90m  [{i+1}] type={stype} | name='{sname[:40]}' | {len(stext)} chars | preview: {repr(stext[:80])}\033[0m")
                    section_extractions = []
                    
                    for sec in structured_sections:
                        ctrl_type = sec.get("type", "Unknown")
                        sec_text = sec.get("text", "").strip()
                        sec_name = sec.get("name", "")

                        total_chunks += 1

                        if ctrl_type == "PageMeta":
                            # Dedicated section from C++ RootWebArea extraction
                            raw_url   = sec.get("url", "").strip()
                            raw_title = sec.get("title", "").strip()
                            if raw_url:
                                source_url = raw_url.split("?")[0]  # strip query params
                            if raw_title:
                                window_title = raw_title  # override OS title with correct per-tab title
                            print(f"\033[90m[FlushWorker] PageMeta: title='{window_title}' url='{source_url}'\033[0m")
                            continue  # not a content section

                        if not sec_text:
                            continue

                        if ctrl_type in VERBATIM_TYPES:
                            # Always treat code editor sections as verbatim code
                            lang_tags = _detect_code_tags(sec_text)
                            section_extractions.append({
                                "content":      sec_text,
                                "tags":         lang_tags,
                                "mini_summary": f"Code from {sec_name or 'editor'}",
                                "verbatim":     True,
                                "full_buffer":  sec.get("full_buffer", False)
                            })
                        
                            print(f"{_CYAN}[FlushWorker] Edit ({len(sec_text)} chars) → verbatim [{', '.join(lang_tags)}] (full_buffer: {sec.get('full_buffer', False)}){_RESET}")

                        elif ctrl_type in CONTENT_TYPES:
                            # If page text is unchanged (>95% similarity), skip spinning up Gemma for notes/tags!
                            if page_sim > 0.95 and last_page:
                                print(f"{_YELLOW}[FlushWorker] Page text unchanged ({page_sim*100:.1f}%). Skipping Gemma metadata synthesis.{_RESET}")
                                continue

                            # Clean the full text — no LLM needed for content, just strip UI chrome
                            clean_sec_text = _preprocess_ocr(sec_text[:10000])
                            if not clean_sec_text:
                                continue
                            print(f"{_CYAN}[FlushWorker] {ctrl_type} ({len(clean_sec_text)} chars) → Gemma (metadata only){_RESET}")
                            # Send a 3000-char window to Gemma for context, but store the FULL cleaned text
                            # Gemma only extracts TOPIC + TAGS + NOTES (not CONTENT) → tiny output, always fits
                            result = self._engine.extract_section(clean_sec_text[:3000], ctrl_type, cleaned_content = clean_sec_text)

                            if result:
                                section_extractions.append(result)
                            else:
                                print(f"\033[33m[FlushWorker] Gemma returned NONE for {ctrl_type} ({len(clean_sec_text)} chars) — all UI chrome or empty content.\033[0m")
                    
                    if section_extractions:
                        combined_raw = "\n\n".join(e["content"] for e in section_extractions)

                        self._embedder.save_memory(
                            app_name = app_name, window_title = window_title,
                            text_content = combined_raw, file_path = None
                        )

                        doc = self._engine.combine_sections(section_extractions, file_path=file_path)
                        doc_dicts = [doc] if doc else []
                    else:
                        doc_dicts = []
                    
                else:
                    # ── OCR FALLBACK: noisy text, must extract with AI first ───────────
                    chunks = _chunk_text(raw_text, CHUNK_SIZE)
                    all_ocr_extractions = []
                    prev_extracted = ""

                    for chunk in chunks:
                        total_chunks += 1
                        # GATE: Skip obviously short UI noise before touching Gemma
                        if len(chunk.split()) < MIN_CHUNK_WORDS:
                            continue

                        print(f"  [Gemma] Extracting chunk {len(all_ocr_extractions)+1}/{len(chunks)}...")
                        extracted = self._engine.extract_ocr_chunk(chunk, prev_context=prev_extracted)

                        if extracted and len(extracted.split()) >= MIN_CHUNK_WORDS:
                            print(f"\n{_YELLOW}--- GEMMA EXTRACTED KNOWLEDGE ---{_RESET}")
                            print(f"{_GREEN}{extracted}{_RESET}")
                            print(f"{_YELLOW}---------------------------------{_RESET}\n")
                            all_ocr_extractions.append(extracted)
                            prev_extracted = extracted
                        else:
                            prev_extracted = chunk[:100] if chunk else ""
                    if all_ocr_extractions:
                        print(f"\n{_CYAN}  [Gemma] Synthesizing {len(all_ocr_extractions)} extractions into knowledge doc...{_RESET}")

                        # Always save raw joined text to episodic_memories (the raw log)
                        combined = "\n\n".join(all_ocr_extractions)
                        self._embedder.save_memory(
                            app_name=app_name,
                            window_title=window_title,
                            text_content=combined,
                            file_path=None
                        )

                        pseudo_ext = {"content": combined, "tags": ["ocr"], "notes": "", "topic": "OCR Capture", "verbatim": False}
                        doc_dicts = [self._engine.combine_sections([pseudo_ext], file_path=file_path)]
                    else:
                        doc_dicts = []

                # --- PHASE 4: Save all docs ─────────────────────────────────────────
                if doc_dicts:
                    for doc_dict in doc_dicts:
                        # Pass the window_title forward to the embedder!
                        doc_dict["window_title"] = window_title 
                        
                        print(f"\n{_YELLOW}━━━ SYNTHESIZED KNOWLEDGE DOC ━━━{_RESET}")
                        print(f"{_GREEN}TOPIC: {doc_dict.get('topic','')}{_RESET}")
                        print(f"{_CYAN}TAGS:  {', '.join(doc_dict.get('tags', []))}{_RESET}")
                        print(f"{_CYAN}SOURCE: {doc_dict.get('source_type','')} | PATH: {doc_dict.get('file_path') or 'none'} | URL: {doc_dict.get('source_url') or 'none'}{_RESET}")
                        print(f"{_GREEN}SUMMARY: {doc_dict.get('summary','')[:200]}{_RESET}")
                        print(f"{_GREEN}{doc_dict.get('content','')[:300]}...{_RESET}")
                        print(f"{_YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{_RESET}\n")

                        # Stamp the captured URL onto the doc before saving
                        if source_url and not doc_dict.get("source_url"):
                            doc_dict["source_url"] = source_url

                        # ── CP PARTNER STEP 1: Anti-Pollution & Domain Tagging ──
                        cp_info = _parse_cp_url(source_url)
                        if cp_info:
                            slug = cp_info["slug"]
                            print(f"{_CYAN}[FlushWorker] Detected {cp_info['platform']} problem: '{slug}' (tab: {cp_info['tab']}){_RESET}")
                            
                            # Anti-pollution rule: If viewing editorial/solutions, strip code to protect user attempt history
                            if cp_info["is_read_only"]:
                                print(f"{_YELLOW}[FlushWorker] Read-only tab detected ({cp_info['tab']}) — stripping code snippet to protect user attempt history.{_RESET}")
                                doc_dict["code_snippet"] = ""
                                doc_dict["full_buffer"] = False

                            # Inject domain tags for isolated RAG retrieval in Step 2
                            existing_tags = doc_dict.get("tags", [])
                            cp_tags = [cp_info["platform"], "cp", slug, f"tab-{cp_info['tab']}"]

                            # Detect Accepted submission state or Solved badge from raw screen text / URL
                            raw_lower = raw_text.lower()
                            is_sub_url = cp_info["tab"] in ("submissions", "submission", "status", "detail") or "/submissions/" in (source_url or "")
                            has_lc_accepted = "accepted" in raw_lower and any(w in raw_lower for w in ("beats", "runtime", "memory", "testcases", "submitted", "submission result"))
                            has_cc_accepted = "correct answer" in raw_lower or "verdict: accepted" in raw_lower

                            # Check for LeetCode's green Solved badge on Description tab (e.g. standalone "Solved" UI item or next to Easy/Medium/Hard)
                            has_solved_badge = False
                            try:
                                parsed_json = json.loads(raw_text)
                                if isinstance(parsed_json, list):
                                    for sec in parsed_json:
                                        t = sec.get("text", "").strip().lower()
                                        if t in ("solved", "solved ✓", "status: solved", "verdict: accepted", "accepted"):
                                            has_solved_badge = True
                                            break
                            except Exception:
                                pass
                            if not has_solved_badge:
                                has_solved_badge = bool(re.search(r'\b(?:solved|accepted)\b\s*(?:\r?\n|\s){1,5}\s*\b(?:easy|medium|hard)\b|\b(?:easy|medium|hard)\b\s*(?:\r?\n|\s){1,5}\s*\b(?:solved|accepted)\b', raw_lower))

                            if is_sub_url or has_lc_accepted or has_cc_accepted:
                                print(f"{_GREEN}[FlushWorker] FRESH ACCEPTED submission detected! Tagging '{slug}' as SOLVED & FRESH_SUBMISSION.{_RESET}")
                                cp_tags.extend(["solved", "fresh_submission"])
                                # Mark active practice session as solved so hint_level resets next open
                                try:
                                    active_session = _get_or_create_practice_session(slug, cp_info["platform"])
                                    if active_session and not active_session.get("is_solved"):
                                        _update_practice_session(
                                            session_id        = active_session["id"],
                                            hint_level        = active_session.get("hint_level", 0),
                                            hint_text         = active_session.get("last_hint_text") or "",
                                            current_code      = active_session.get("code_snapshot") or "",
                                            detected_approach = active_session.get("detected_approach") or "unknown",
                                            is_solved         = True,
                                        )
                                        print(f"{_GREEN}[Practice] Session marked SOLVED for '{slug}'{_RESET}")
                                except Exception as _pe:
                                    print(f"{_RED}[Practice] Could not mark session solved: {_pe}{_RESET}")
                            elif has_solved_badge:
                                print(f"{_GREEN}[FlushWorker] SOLVED badge detected! Tagging problem '{slug}' as SOLVED.{_RESET}")
                                cp_tags.append("solved")

                            doc_dict["tags"] = list(dict.fromkeys(cp_tags + existing_tags))
                            # Standardize topic name so all attempts anchor to the exact same problem
                            doc_dict["topic"] = f"{cp_info['platform'].capitalize()}: {slug}"
                        # ────────────────────────────────────────────────────────


                        saved = self._embedder.save_knowledge_doc(app_name, doc_dict, self._engine)
                        if saved:
                            useful_saved += 1
                            print(f"{_GREEN}[FlushWorker] Saved knowledge doc: '{doc_dict.get('topic','')}'.{_RESET}")
                        else:
                            # Embedder returned False = semantic duplicate already in DB.
                            # The knowledge is already there — still safe to delete the raw row.
                            print(f"{_YELLOW}[FlushWorker] Doc '{doc_dict.get('topic','')}' already in DB (duplicate). Row will be cleared.{_RESET}")
                    
                    # Always delete the row after synthesis ran — even if all were duplicates.
                    # Leaving it in means infinite reprocessing of the same screen capture.
                    if row_id not in ids_to_delete:
                        ids_to_delete.append(row_id)
                else:
                    # All sections failed synthesis — raw text already in episodic_memories
                    print(f"{_YELLOW}  [FlushWorker] All sections failed synthesis — raw text saved to episodic_memories only.{_RESET}")
                    ids_to_delete.append(row_id)
    
            except Exception as e:
                # Gemma crashed, OOM, Ollama timeout, etc.
                # Do NOT delete this row — leave it in ocr_buffer to be retried next cycle.
                print(f"{_RED}[FlushWorker] Row #{row_id} ({app_name}) failed with exception: {e}{_RESET}")
                ids_failed.append(row_id)
                
        # DELETE: Clean up all rows just processed
        if ids_to_delete:
            placeholders = ",".join("?" * len(ids_to_delete))
            conn.execute(
                f"DELETE FROM ocr_buffer WHERE id IN ({placeholders});",
                ids_to_delete
            )
            conn.commit()

        conn.close()

        # Summary report
        print(f"{_GREEN}[FlushWorker] Done. "
              f"{useful_saved} docs saved from {total_chunks} chunks "
              f"({len(rows)} raw rows processed, "
              f"{len(ids_to_delete)} deleted, "
              f"{len(ids_failed)} failed/retrying).{_RESET}")
        if ids_failed:
            print(f"{_RED}[FlushWorker] {len(ids_failed)} row(s) will be retried next cycle: {ids_failed}{_RESET}")