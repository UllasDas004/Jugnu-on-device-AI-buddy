import ollama
import json

class AIEngine:
    def __init__(self):
        self.model_name = "gemma4:e2b"
        self._warmup()

    def _warmup(self):
        """
        Ollama's first cold-start can crash with a CUDA error (0xc0000409) on RTX 4050
        if Flash Attention is auto-enabled by the server. We fire a dummy request so the
        crash happens here (invisible to user) and the server recovers before real queries.
        Retries up to 3 times in case the server needs a moment to recover.

        IMPORTANT: ollama serve must be started with OLLAMA_FLASH_ATTENTION=0.
        In PowerShell: $env:OLLAMA_FLASH_ATTENTION=0; ollama serve
        """
        import os
        if not os.environ.get("OLLAMA_FLASH_ATTENTION"):
            print("\033[1;33m[AIEngine] WARNING: OLLAMA_FLASH_ATTENTION env var not set!\033[0m")
            print("\033[1;33m[AIEngine] Start Ollama with: $env:OLLAMA_FLASH_ATTENTION=0; ollama serve\033[0m")

        print("\033[90m[AIEngine] Warming up model (first call may take a moment)...\033[0m")
        for attempt in range(3):
            try:
                ollama.chat(
                    model=self.model_name,
                    messages=[{"role": "user", "content": "hi"}],
                    options={"num_predict": 1, "num_ctx": 2048, "flash_attn": False}
                )
                print("\033[1;32m[AIEngine] Model warm and ready!\033[0m")
                return  # Success — exit immediately
            except Exception:
                if attempt < 2:
                    print(f"\033[90m[AIEngine] Cold-start attempt {attempt+1}/3 failed (normal). Retrying...\033[0m")
                    import time; time.sleep(3)  # Give Ollama time to recover
                else:
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
                    "num_ctx": 4096,
                    "num_predict": 1500,
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
                options = {"num_ctx": 2048, "num_predict": 30, "temperature": 0.1, "flash_attn": False}
            )
            if hasattr(response, 'message') and response.message:
                return (response.message.content or '').strip()
            return screen_context[:100] # fallback: use raw context as query
        except Exception:
            return screen_context[:100]
    
    def answer_with_context(self, user_query: str, context_chunks: list[str], sources: list[str] | None = None) ->str:
        """
        RAG answer function. Feeds the user query + past memories into Gemma
        and returns a context-aware answer.
        """

        # P2-FIX: Never use mutable default argument. The same [] would be shared
        # across all calls if a caller appended to it — a classic Python footgun.
        if sources is None:
            sources = []

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

    def synthesize_ocr_extractions(self, extractions: list[str]) -> list[str]:
        """
        Section-wise synthesis: process each UIA section independently with
        a safe character cap, then return ALL valid results as a list.
        Each rich section becomes its own knowledge doc — nothing gets thrown away.
        Prevents llama-server stack/context overflow when UIA captures large buffers.
        """
        MAX_SECTION_CHARS = 3000  # Safe for 4096 token ctx with system prompt overhead

        all_docs: list[str] = []  # Collect ALL valid sections

        for i, section in enumerate(extractions):
            section = section[:MAX_SECTION_CHARS]

            prompt = f"""You are a strict technical knowledge organizer.
You are given raw screen text from a developer's screen.
Your ONLY job is to organize this text into the structured sections below.

CRITICAL RULES:
1. DO NOT summarize, paraphrase, or pass judgement on the code.
2. Copy the ENTIRE code block EXACTLY character-for-character into the CODE section.
3. Copy the problem statement directly into the CONTEXT section.

RAW TEXT TO ORGANIZE:
{section}

Output EXACTLY in this format (use NONE for any section with no content):
TOPIC: <one-line title — if LeetCode problem, prefix with "LeetCode: ">
TAGS: <tag1, tag2, tag3>
CONTEXT: <The problem statement, OR context of the codebase>
IMPLEMENTATION: <The technical approach, algorithm, or architecture>
CODE:
<exact code block if present, else NONE>
NOTES: <Constraints, edge cases, hints, or UI notes>

Do NOT output anything before TOPIC:"""

            try:
                print(f"\033[35m[Gemma]\033[0m Synthesizing section {i+1}/{len(extractions)}...")
                response = ollama.chat(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                    think=False,
                    options={
                        "num_ctx": 4096,
                        "num_predict": 1024,   # Synthesis output doesn't need 2048 tokens
                        "temperature": 0.1,
                        "flash_attn": False,
                    }
                )
                raw = ""
                if hasattr(response, 'message') and response.message:
                    raw = (response.message.content or '').strip()

                if not raw:
                    print(f"\033[91m[AIEngine] Gemma returned empty for section {i+1}\033[0m")
                    continue

                # Parse the structured response
                topic = ""
                tags = []
                sections = {"CONTEXT": [], "IMPLEMENTATION": [], "CODE": [], "NOTES": []}
                current_section = None

                for line in raw.split('\n'):
                    if line.startswith("TOPIC:"):
                        current_section = None
                        topic = line.replace("TOPIC:", "").strip()
                    elif line.startswith("TAGS:"):
                        current_section = None
                        tags = [t.strip() for t in line.replace("TAGS:", "").split(',') if t.strip()]
                    elif line.startswith("CONTEXT:"):        current_section = "CONTEXT"
                    elif line.startswith("IMPLEMENTATION:"): current_section = "IMPLEMENTATION"
                    elif line.startswith("CODE:"):           current_section = "CODE"
                    elif line.startswith("NOTES:"):          current_section = "NOTES"
                    elif current_section:
                        sections[current_section].append(line)

                content_parts = []
                ctx_text = "\n".join(sections["CONTEXT"]).strip()
                if ctx_text and ctx_text != "NONE":
                    content_parts.append(f"CONTEXT:\n{ctx_text}")

                imp_text = "\n".join(sections["IMPLEMENTATION"]).strip()
                if imp_text and imp_text != "NONE":
                    content_parts.append(f"IMPLEMENTATION:\n{imp_text}")

                code_text = "\n".join(sections["CODE"]).strip()
                if code_text and code_text != "NONE":
                    content_parts.append(f"CODE:\n```\n{code_text}\n```")

                notes_text = "\n".join(sections["NOTES"]).strip()
                if notes_text and notes_text != "NONE":
                    content_parts.append(f"NOTES:\n{notes_text}")

                content = "\n\n".join(content_parts)

                # Collect ALL sections that have real content — not just the best one
                if topic and content:
                    all_docs.append(json.dumps({"topic": topic, "tags": tags, "content": content}))
                    print(f"\033[32m[Gemma] Successfully synthesized section {i+1} ('{topic}')\033[0m")


            except Exception as e:
                print(f"\033[1;31m[AIEngine] Section {i+1} synthesis failed: {e}\033[0m")
                continue  # One bad section doesn't kill the entire batch

        return all_docs
    
    def merge_knowledge_docs(self, existing_json: str, new_json: str) -> str:
        """
        Merges an existing knowledge doc with a new synthesis from the same topic.
        Returns the merged JSON string, or empty string on failure.
        """
        prompt = f"""You are merging two knowledge documents about the same topic.
        Existing document:
        {existing_json[:4000]}
        New information captured:
        {new_json[:4000]}
        Produce ONE merged document. Keep all unique code snippets and facts from both. Remove redundancy.
        Output EXACTLY in this text format:
        TOPIC: <one-line topic title>
        TAGS: <tag1, tag2, tag3>
        CONTENT:
        <full markdown synthesis with code blocks if present>
        Do NOT wrap in JSON. Do NOT output anything before TOPIC:"""

        try:
            response = ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                think=False,
                options={
                    "num_ctx": 8192,
                    "num_predict": 2048,
                    "temperature": 0.1,
                    "flash_attn": False,
                }
            )
            raw = ""
            if hasattr(response, 'message') and response.message:
                raw = (response.message.content or '').strip()
                
            topic = ""
            tags = []
            content = ""
            
            lines = raw.split('\n')
            for i, line in enumerate(lines):
                if line.startswith("TOPIC:"):
                    topic = line.replace("TOPIC:", "").strip()
                elif line.startswith("TAGS:"):
                    tags_raw = line.replace("TAGS:", "").strip()
                    tags = [t.strip() for t in tags_raw.split(',') if t.strip()]
                elif line.startswith("CONTENT:"):
                    content = "\n".join(lines[i+1:]).strip()
                    break

            if topic and content:
                doc = {"topic": topic, "tags": tags, "content": content}
                return json.dumps(doc)
            return ""

        except Exception as e:
            print(f"\033[1;31m[AIEngine] merge_knowledge_docs failed: {e}\033[0m")
            return ""
