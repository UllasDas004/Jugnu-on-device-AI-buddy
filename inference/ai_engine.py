import ollama
import json

class AIEngine:
    def __init__(self):
        self.model_name = "gemma4:e2b"
        self._warmup()

    def _warmup(self):
        """
        Ollama's first cold-start always crashes with a CUDA error on this machine.
        The second call works perfectly. We fire a silent dummy request at startup
        so the crash happens here (invisible to user) and not during a real nudge.
        """
        print("\033[90m[AIEngine] Warming up model (first call may take a moment)...\033[0m")
        try:
            ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": "hi"}],
                options={"num_predict": 1, "num_ctx": 512, "flash_attn": False}
            )
            print("\033[1;32m[AIEngine] Model warm and ready!\033[0m")
        except Exception:
            # First call crashes — that's expected. Ollama recovers automatically.
            print("\033[90m[AIEngine] Cold-start done (first call reset is normal). Model ready for next query.\033[0m")

    def generate_insight(self, context_str):
        # Hard-truncate context to prevent KV-cache overrun (caused the 0xc0000409 crash)
        context_str = context_str[:1200]

        prompt = f"""You are Jugnu, a senior technical AI assistant.
        The developer might be stuck or pausing to think. Here is their current OS and editor context:
        {context_str}
        Provide a short, proactive suggestion to help them move forward based on the exact code or application they are looking at.
        Keep it under 3 sentences. Be direct, technical, and specific. Do NOT show your reasoning or thinking process."""

        print(f"\n\033[1;35m[AIEngine]\033[0m Querying local model {self.model_name}...")

        try:
            response = ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                think=False,         # Disable chain-of-thought — we want a direct answer
                options={
                    "num_ctx": 2048,      # Safe window that fits in VRAM without warmup crash
                    "flash_attn": False,  # Disable Flash Attention — fixes CUDA PDL crash on RTX 4050
                    "num_predict": 256,   # Short direct answer — no room for thinking dumps
                }
            )

            # TRAP FIX: newer ollama library returns a Pydantic ChatResponse object,
            # not a plain dict. We must use attribute access, not .get().
            # Use `or ''` everywhere — content/thinking can be None, not just missing.
            if hasattr(response, 'message') and response.message:
                content  = (response.message.content  or '').strip()
                thinking = (getattr(response.message, 'thinking', None) or '').strip()
            else:
                # Fallback for old dict-style response format
                msg      = (response or {}).get('message', {}) or {}
                content  = (msg.get('content')  or '').strip()
                thinking = (msg.get('thinking') or '').strip()

            if content:
                return content
            elif thinking:
                # Model returned empty content but has thinking — extract just the last
                # paragraph which typically contains the actual answer/conclusion.
                paragraphs = [p.strip() for p in thinking.split('\n\n') if p.strip()]
                return paragraphs[-1] if paragraphs else "I noticed you seem stuck — could you tell me what you're working on?"
            else:
                return "I noticed you seem stuck — could you tell me what you're working on?"

        except Exception as e:
            return f"Ollama Connection Failed! {e}"
    

    def extract_ocr_chunk(self, chunk: str, prev_context: str = "") -> str:
        """
        Uses Gemma as an extraction engine, not a classifier.
        Given a noisy OCR chunk, e Extracts technical knowledge from an OCR chunk.
        prev_context: the last thing Gemma extracted (so it can continue coherently).
        Returns structured "TOPIC: ...\nCONTENT: ..." or empty string if nothing useful.
        
        Returns the cleaned text string, or empty string if nothing useful found.
        
        Key settings:
        - num_predict: 300 — enough for a full code snippet or explanation
        - temperature: 0.1 — near-deterministic, we don't want creative extraction
        - think: False — no reasoning monologue, just the extracted text
        """

        context_hint = ""
        if prev_context:
            context_hint = f"\nFor context, you were just extracting this:\n{prev_context[:200]}\n"

        prompt = f"""You are a technical knowledge extractor for a developer AI assistant.
        You are given raw text from a developer's screen (OCR). Extract ONLY genuinely useful technical information.
        KEEP: code snippets, error messages, algorithm explanations, problem statements, technical concepts, API docs.
        DISCARD: window chrome, browser UI, tab titles, menu items, button labels.
        {context_hint}
        If there is NO useful technical content, return exactly: NONE
        OUTPUT FORMAT (always use this exact format when you find something):
        TOPIC: <one line — what is this about>
        CONTENT: <the extracted technical text, preserving any code formatting>
        Text to extract from:
        {chunk}"""

        try:
            response = ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                think=False,
                options={
                    "num_predict": 300,
                    "temperature": 0.1,   # near-deterministic extraction
                    "flash_attn": False,
                }
            )

            if hasattr(response, 'message') and response.message:
                result = (response.message.content or '').strip()
            else:
                result = ((response or {}).get('message', {}) or {}).get('content', '').strip()

            # If gemma returned "NONE" or empty, signal no useful content
            if not result or result.upper() == "NONE":
                return ""
            return result
        except Exception as e:
            print(f"\033[1;31m[AIEngine] extract_ocr_chunk error: {e}\033[0m")
            return ""

    def generate_search_query(self, screen_context: str) -> str:
        """
        Gemma reads the current screen context and generates a focused
        search query to find relevant past memories in the vector DB.
        Called when the user hits 'need help'.
        """
        prompt = f"""You are a search query generator for a developer's knowledge base.
        Here is the developer's current screen context:
        {screen_context[:800]}
        Generate a single, highly focused semantic search query (max 15 words) that captures the core technical problem or technology they are working on right now.
        Return ONLY the raw query string, nothing else. No quotes, no prefix."""

        try:
            response = ollama.chat(
                model = self.model_name,
                messages = [{"role": "user", "content": prompt}],
                think = False,
                options = {"num_predict": 30, "temperature": 0.1, "flash_attn": False}
            )
            if hasattr(response, 'message') and response.message:
                return (response.message.content or '').strip()
            return screen_context[:100] # fallback: use raw context as query
        except Exception:
            return screen_context[:100]
    
    def answer_with_context(self, user_query: str, context_chunks: list[str], sources: list[str] = []) ->str:
        """
        RAG answer function. Feeds the user query + past memories into Gemma
        and returns a context-aware answer.
        """
        context_block = "\n\n---\n\n".join(context_chunks)
        # Hard cap context to avoid KV overrun
        context_block = context_block[:1500]

        source_hint = ""
        if sources:
            source_hint = f"You retrieved this context from the developer's own past sessions on: {', '.join(sources)}. Reference the most relevant source by name in your answer."

        prompt = f"""You are Jugnu, a personal coding assistant with access to this developer's own learning history.
        Context from the developer's past sessions:
        {context_block}
        Developer's current question / situation:
        {user_query}
        {source_hint}
        Answer using the context above where relevant. Be direct, specific, and technical.
        Start your answer with: "Based on your past work on [topic], ..." when the context is relevant.
        If the context doesn't help, answer from general knowledge but say so.
        Keep the answer under 5 sentences."""

        try:
            response = ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                think=False,
                options={
                    "num_ctx": 2048,
                    "num_predict": 300,
                    "flash_attn": False,
                }
            )
            if hasattr(response, 'message') and response.message:
                content = (response.message.content or '').strip()
                return content if content else "I couldn't find relevant context. Try asking more specifically."
            return "Error generating answer."
        except Exception as e:
            return f"Ollama error: {e}"

    def synthesize_ocr_extractions(self,extractions: list[str]) -> str:
        """
        Final pass: synthesizes all chunk extractions from one OCR into one
        rich, coherent JSON knowledge document.
        Returns a JSON string on success, or empty string on failure.
        """

        combined_raw = "\n\n---\n\n".join(extractions)
        combined_raw = combined_raw[:2000]  # Hard cap for context window
        prompt = f"""You are a technical knowledge synthesizer for a developer AI.
        You have these extracted fragments from a single screen capture:
        {combined_raw}
        Synthesize them into ONE rich, coherent technical knowledge document.
        Remove repetition. Preserve ALL code snippets, algorithms, and technical details.
        Output ONLY a valid JSON object with exactly these fields:
        {{
        "topic": "one-line topic title",
        "tags": ["tag1", "tag2", "tag3"],
        "content": "full markdown synthesis with code blocks if present"
        }}
        Output ONLY the JSON. No explanation. No markdown fences around the JSON."""
        try:
            response = ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                think=False,
                options={
                    "num_ctx": 2048,
                    "num_predict": 600,
                    "temperature": 0.1,
                    "flash_attn": False,
                }
            )
            raw = ""
            if hasattr(response, 'message') and response.message:
                raw = (response.message.content or '').strip()

            # Strict JSON validation — if Gemma fails, return empty (caller falls back to raw text)
            if not raw:
                print("\033[91m[AIEngine] Gemma returned empty synthesis\033[0m")
                return ""
            
            # Strip accidental markdown fences if model wraps output
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            doc = json.loads(raw.strip())
            # Validate required fields
            if "topic" in doc and "content" in doc:
                if "tags" not in doc:
                    doc["tags"] = []
                return json.dumps(doc)
            return ""
        except (json.JSONDecodeError, Exception) as e:
            print(f"\033[1;31m[AIEngine] synthesize_to_okf_doc failed: {e}\033[0m")
            return ""
    
    def merge_knowledge_docs(self, existing_json: str, new_json: str) -> str:
        """
        Merges an existing knowledge doc with a new synthesis from the same topic.
        Returns the merged JSON string, or empty string on failure.
        """
        prompt = f"""You are merging two knowledge documents about the same topic.
        Existing document:
        {existing_json[:1000]}
        New information captured:
        {new_json[:800]}
        Produce ONE merged JSON document. Keep all unique code snippets and facts from both.
        Remove redundancy. Output ONLY valid JSON with fields: topic, tags, content.
        No explanation. No markdown fences."""

        try:
            response = ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                think=False,
                options={
                    "num_ctx": 2048,
                    "num_predict": 700,
                    "temperature": 0.1,
                    "flash_attn": False,
                }
            )
            raw = ""
            if hasattr(response, 'message') and response.message:
                raw = (response.message.content or '').strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            doc = json.loads(raw.strip())
            if "topic" in doc and "content" in doc:
                return json.dumps(doc)
            return ""
        except Exception as e:
            print(f"\033[1;31m[AIEngine] merge_knowledge_docs failed: {e}\033[0m")
            return ""
