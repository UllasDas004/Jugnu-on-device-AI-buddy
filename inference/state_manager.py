import sqlite3
import os
import time
import json

class StateManager:
    def __init__(self):
        self.current_app = ""
        self.predicted_next = []
        self.clipboard_content = ""
        self.active_code_file = ""
        self.active_code_content = ""
        self.last_coding_app = ""
        self.last_coding_app_time = 0.0 # epoch timestamp
        # Per-app cache of the most recently processed UIA text.
        # Populated by FlushWorker after processing. Survives ocr_buffer deletion.
        self._last_screen_text: dict[str, str] = {}
    
    def update_switch(self, current_app, predicted_next):
        self.current_app = current_app
        self.predicted_next = predicted_next

    def set_last_coding_app(self, app_name):
        self.last_coding_app = app_name
        self.last_coding_app_time = time.time()

    def get_last_coding_app(self) -> str:
        """Returns the last meaningful coding app, or empty string if none."""
        return self.last_coding_app

    def update_screen_text(self, app_name: str, text: str):
        """Called by FlushWorker after processing a UIA row. Caches the text so
        generate_prompt_context can still read it after ocr_buffer is cleared."""
        if text and text.strip():
            self._last_screen_text[app_name] = text

    def was_recently_coding(self, within_minutes = 15):
        """Returns True if user was in a coding app within the last N minutes."""
        if not self.last_coding_app:
            return False
        elapsed = time.time() - self.last_coding_app_time
        return elapsed < (within_minutes * 60)

    def update_clipboard(self, content):
        self.clipboard_content = content

    def update_file(self, filepath):
        self.active_code_file = filepath

        # Try to read the file so the AI can read your code!
        try:
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    self.active_code_content = f.read()
        except Exception as e:
            print(f"[StateManager] failed to read file {filepath}: {e}")

    def get_context_summary(self):
        """Short human-readable summary shown to user in the confirmation dialog."""
        parts = []
        if self.last_coding_app:
            mins_ago = int((time.time() - self.last_coding_app_time) / 60)

            parts.append(f"💻 Last IDE: {self.last_coding_app} ({mins_ago} min ago)")
        
        if self.active_code_file:
            parts.append(f"📄 Last file: {os.path.basename(self.active_code_file)}")

        if self.clipboard_content:
            snippet = self.clipboard_content[:80].replace("\n", " ")
            parts.append(f"📋 Clipboard: \"{snippet}\"")
        
        return "\n".join(parts) if parts else "No recent coding context found."


    def generate_prompt_context(self, custom_problem = None, embedder = None, target_app: str = ""):
        """
        Packages all short-term memory + long-term RAG results into a string for the LLM.
        If embedder is provided, injects the top 3 semantically similar past memories.
        For file memories, re-reads the current file from disk for full context.
        """
        # FIX SM-2: Use the explicit target_app provided by the idle handler
        app = target_app or self.current_app

        # FIX SM-1: Detect if we are actually in an IDE
        IDE_APPS = {"code", "cursor", "devenv", "vim", "clion", "pycharm", "antigravity"}
        is_ide_context = any(ide in app.lower() for ide in IDE_APPS)

        context = f"The user is currently using: {app}.\n"

        if self.last_coding_app:
            mins_ago = int((time.time() - self.last_coding_app_time) /60)
            context += f"They were recently coding in {self.last_coding_app} ({mins_ago} min ago).\n"

        # FIX SM-1: ONLY inject code file content if we are idling in an IDE!
        if self.active_code_file and is_ide_context:
            context += f"The last file they worked on: {self.active_code_file}.\n"
        if self.active_code_content and is_ide_context:
            context += f"File content:\n```\n{self.active_code_content[:1500]}\n```\n"

        if self.clipboard_content:
            context += f"Clipboard: {self.clipboard_content[:300]}\n"

        # Smart UIA Formatter: Puts Code (Edit controls) at the TOP before truncation
        def _format_uia_payload(raw_payload: str) -> str:
            try:
                sections = json.loads(raw_payload)
                # P0-FIX: Preserve PageMeta URL and Title for deterministic anchors and platform detection!
                urls = [sec.get("url", "").strip() for sec in sections if sec.get("type") == "PageMeta" and sec.get("url")]
                titles = [sec.get("title", "").strip() for sec in sections if sec.get("type") == "PageMeta" and sec.get("title")]
                edits = [sec.get("text", "").strip() for sec in sections if sec.get("type") == "Edit" and sec.get("text", "").strip()]
                docs = [sec.get("text", "").strip() for sec in sections if sec.get("type") != "Edit" and sec.get("text", "").strip()]

                combined = ""
                if urls:
                    combined += f"[URL: {urls[0]}]\n"
                if titles:
                    combined += f"[TITLE: {titles[0]}]\n"
                if combined:
                    combined += "\n"

                # Prioritize Editor Content at the top!
                if edits:
                    combined += "--- Editor Content ---\n" + "\n\n".join(edits) + "\n\n"
                if docs:  
                    combined += "--- Page Content ---\n" + "\n\n".join(docs)
                return combined
            except:
                return raw_payload.replace("===SECTION===", "\n")
            
        cached = self._last_screen_text.get(app, "")
        uia_text = ""

        try:
            conn = sqlite3.connect("jugnu.db", timeout=0.5)
            # Try to get the absolute freshest from ocr_buffer first (C++ might have just dumped it)
            row = conn.execute("SELECT raw_text FROM ocr_buffer WHERE app_name = ? ORDER BY timestamp DESC LIMIT 1", (app,)).fetchone()
            
            if row and row[0]:
                uia_text = _format_uia_payload(row[0])
            elif cached:
                # Fallback to the last processed payload in memory
                uia_text = _format_uia_payload(cached)
            else:
                # Absolute last resort: episodic memories
                row = conn.execute("SELECT text_content FROM episodic_memories WHERE app_name = ? AND source_type = 'browser' ORDER BY timestamp DESC LIMIT 1", (app,)).fetchone()
                if row and row[0]:
                    uia_text = _format_uia_payload(row[0])
            conn.close()
        except Exception as e:
            print(f"[StateManager] DB read error: {e}")
            if cached:
                uia_text = _format_uia_payload(cached)
        
        if uia_text:
            display = uia_text[:4000] + ("\n...[truncated]" if len(uia_text) > 4000 else "")
            context += f"\nCurrent Screen Context (from {app}):\n```\n{display.strip()}\n```\n"

        if embedder:
            query = custom_problem or self.active_code_content[:200] or self.current_app
            # Try rich structured knowledge_docs first (OKF store)
            knowledge_results = embedder.search_knowledge_docs(query, limit = 3)
            if knowledge_results:
                context += "\n Relevant knowledge from past sessions:\n"
                for i, doc in enumerate(knowledge_results, 1):
                    context += (
                        f"[Knowledge {i}: {doc['topic']} "
                        f"(seen {doc['capture_count']}x)]\n"
                        f"{doc['content'][:3000]}\n\n"
                    )
            else:
                # Fallback to raw episodic memories if no structured docs yet
                memories = embedder.semantic_search(query, limit=3)
                if memories:
                    context += "\n Relevant past context (from long-term memory):\n"
                    for i, mem in enumerate(memories, 1):
                        snippet = mem["snippet"]
                        file_path = mem["file_path"]
                        if file_path and os.path.exists(file_path):
                            try:
                                with open(file_path, "r", encoding="utf-8") as f:
                                    full = f.read(800)
                                context += f"[Memory {i}] (from {os.path.basename(file_path)}):\n```\n{full}\n```\n"
                            except Exception:
                                context += f"[Memory {i}] {snippet}\n"
                        else:
                            context += f"[Memory {i}] {snippet}\n"

        if custom_problem:
            context += f"\nThe user described their specific problem as:\n\"{custom_problem}\"\n"

        return context
