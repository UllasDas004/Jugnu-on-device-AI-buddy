import os
import time

class StateManager:
    def __init__(self):
        self.current_app = ""
        self.predicted_next = []
        self.clipboard_content = ""
        self.active_code_file = ""
        self.active_code_content = ""
        self.last_coding_app = ""
        self.last_coding_app_time = 0.0 # epoch timestamp
    
    def update_switch(self, current_app, predicted_next):
        self.current_app = current_app
        self.predicted_next = predicted_next

    def set_last_coding_app(self, app_name):
        self.last_coding_app = app_name
        self.last_coding_app_time = time.time()

    def was_recently_coding(self, within_minutes = 15):
        """Retruns True if user was in a coding app within the last N minutes."""
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

    
    def generate_prompt_context(self, custom_problem = None, embedder = None):
        """
        Packages all short-term memory + long-term RAG results into a string for the LLM.
        If embedder is provided, injects the top 3 semantically similar past memories.
        For file memories, re-reads the current file from disk for full context.
        """

        context = f"The user is currently using: {self.current_app}.\n"

        if self.last_coding_app:
            mins_ago = int((time.time() - self.last_coding_app_time) /60)
            context += f"They were recently coding in {self.last_coding_app} ({mins_ago} min ago).\n"
        
        if self.active_code_file:
            context += f"The last file they worked on: {self.active_code_file}.\n"

        if self.active_code_content:
            context += f"File content:\n```\n{self.active_code_content[:1500]}\n```\n"

        if self.clipboard_content:
            context += f"Clipboard: {self.clipboard_content[:300]}\n"

        if embedder:
            query = custom_problem or self.active_code_content[:200] or self.current_app
            memories = embedder.semantic_search(query, limit = 3)
            if memories:
                context += "\n Relevant past context (from long-term memory):\n"
                for i, mem in enumerate(memories, 1):
                    snippet = mem["snippet"]
                    file_path = mem["file_path"]
                    
                    # If we stored a file path, re-read the current version for full context
                    if file_path and os.path.exists(file_path):
                        try:
                            with open(file_path, "r", encoding="utf-8") as f:
                                full = f.read(800) # cap at 800 chars per past file
                            context += f"[{i}] (from {os.path.basename(file_path)}):\n```\n{full}\n```\n"
                        except Exception:
                            context += f"   [{i}] {snippet}\n"        
        if custom_problem:
            context += f"\nThe user described their specific problem as:\n\"{custom_problem}\"\n"

        return context
