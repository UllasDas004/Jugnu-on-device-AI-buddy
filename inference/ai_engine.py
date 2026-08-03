import ollama
import os
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------
# Path to the SQLite jugnu.db that the C++ side also uses.
DB_PATH = Path(__file__).resolve().parents[1] / "jugnu.db"
# ----------------------------------------------------------------------
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
        context_str = context_str[:3000]

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
        Given a noisy OCR chunk, extracts technical knowledge from an OCR chunk.
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

    def build_rag_context(self, screen_context: str, knowledge_docs: list[dict], situation_type: str) -> str:
        """
        Assembles a structured, token-budgeted context block.
        Pure Python — zero LLM calls. Hard char caps prevent VRAM OOM on Gemma 4 E2B.
        """
        MAX_SCREEN   = 5000   # was 3000 — guarantees full active editor code + problem specification
        MAX_CODE     = 4000   # was 2500 — complete past code reference solutions
        MAX_CONTENT  = 2500   # was 1500 — full problem specifications
        MAX_NOTES    = 1500   # was 600  — comprehensive algorithmic notes and state transition rules
        MAX_SUPPORT  = 1000   # was 800

        parts = []

        # ── Layer 1: Current Screen (always first, always hard-capped) ──────────
        if screen_context:
            capped = screen_context[:MAX_SCREEN]
            if len(screen_context) > MAX_SCREEN:
                capped += "\n...[truncated for token budget]"
            parts.append(f"[RIGHT NOW — Current Screen]\n{capped}")

        if not knowledge_docs:
            return "\n\n".join(parts)

        # ── Layer 2: Primary Knowledge (richest doc, all fields) ─────────────────
        top = knowledge_docs[0]
        topic        = top.get('topic', 'Unknown Topic')
        content      = (top.get('content') or '')[:MAX_CONTENT]
        code_snippet = (top.get('code_snippet') or '')[:MAX_CODE]
        notes        = (top.get('notes') or '')[:MAX_NOTES]
        tags         = top.get('tags', [])
        capture_count = top.get('capture_count', 1)
        last_updated  = top.get('last_updated', '')

        # Human-readable time hint
        time_hint = ""
        if last_updated:
            try:
                dt = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                diff = datetime.now(timezone.utc) - dt
                hours = int(diff.total_seconds() / 3600)

                if hours < 1:
                    time_hint = "< 1 hour ago"
                elif hours < 24:
                    time_hint = f"{hours}h ago"
                else:
                    time_hint = f"{diff.days} days ago"
            except Exception:
                pass

        struggle_warning = ""
        if capture_count >= 4:
            struggle_warning = f"\n⚠️  You have revisited this topic {capture_count} times — you may be stuck at the same point repeatedly.\n"

        layer2 = f"[YOUR PAST WORK: \"{topic}\" — seen {capture_count}x{', ' + time_hint if time_hint else ''}]{struggle_warning}"

        if tags:
            layer2 += f"\nTags: {', '.join(tags)}"
        if content:
            layer2 += f"\nProblem / Concept:\n{content}"
        if code_snippet:
            layer2 += f"\nYour Code Attempt:\n```\n{code_snippet}\n```"
        if notes:
            layer2 += f"\nYour Noted Edge Cases / Constraints:\n{notes}"

        parts.append(layer2)

        # ── Layer 3: Supporting Docs (brief, already deduped by embedder) ────────
        for i, doc in enumerate(knowledge_docs[1:], 1):
            sup_content = (doc.get('content') or '')[:MAX_SUPPORT]
            sup_topic   = doc.get('topic', f'Related Topic {i}')
            sup_count   = doc.get('capture_count', 1)

            if sup_content:
                parts.append(f"[RELATED: \"{sup_topic}\" — seen {sup_count}x]\n{sup_content}")
        
        return "\n\n".join(parts)
                

    
    def answer_with_context(self, user_query: str, context_chunks: list[str], sources: list[str] | None = None,screen_context: str = "", situation_type: str = "GENERAL") ->str:
        """
        RAG answer function. Situation-aware prompt template selection.
        Notes amplification guardrail active for STUCK and REPEATED_STRUGGLE.
        """

        # P2-FIX: Never use mutable default argument. The same [] would be shared
        # across all calls if a caller appended to it — a classic Python footgun.
        if sources is None:
            sources = []

        context_block = "\n\n---\n\n".join(context_chunks)
        # Hard cap context to avoid KV overrun
        context_block = context_block[:6000]

        source_hint = f"Sources: {', '.join(sources)}." if sources else ""

        # ── Situation-aware system instruction ───────────────────────────────────
        if situation_type == "STUCK_ON_OWN_CODE":
            system_role = (
                "You are reviewing the developer's own code and past attempts. "
                "Find the specific bug or missing edge case in THEIR code — not a generic explanation. "
                "Reference specific patterns from their code_snippet directly. "
                "IMPORTANT: Compare their implementation against the constraints listed under "
                "'Your Noted Edge Cases / Constraints'. Explicitly call out if they are violating "
                "a rule they previously documented themselves."
            )
        elif situation_type == "REPEATED_STRUGGLE":
            system_role = (
                "The developer has revisited this topic 4+ times and is still stuck. "
                "Do NOT repeat generic advice. Identify WHAT specifically they are missing "
                "based on their code pattern. "
                "IMPORTANT: Cross-check their code against 'Your Noted Edge Cases / Constraints'. "
                "Explicitly state if they are violating a constraint they themselves documented. "
                "Give them the one precise insight that will unblock them."
            )
        elif situation_type == "READING_NEW_MATERIAL":
            system_role = (
                "The developer just read documentation or learned a new concept. "
                "Connect this new knowledge directly to their active code file or recent work if visible. "
                "Suggest the single most actionable next step to apply what they just read."
            )
        elif situation_type == "CP_READING":
            system_role = (
                "You are an expert competitive programming coach. The developer is currently reading a problem statement "
                "and has not started coding yet. Give a brief 2-sentence high-level intuition on how to categorize and approach this problem category "
                "(e.g., Two Pointers, Dynamic Programming, Graph BFS) without giving away the complete algorithm or writing any code. "
                "If past solved problems in context share the same pattern, briefly mention them as a conceptual reference."
            )
        elif situation_type == "CP_STUCK":
            system_role = (
                "You are an empathetic, senior technical interviewer and competitive programming coach. The developer has started coding a solution "
                "but has paused or gotten stuck. First, compare their code implementation against the problem requirements and algorithmic paradigm in 'Your Noted Edge Cases / Constraints'. "
                "1. POSITIVE REINFORCEMENT: Explicitly validate the parts of their code, state definitions, or logic that are correct so they know what is solid. "
                "2. SOCRATIC DEBUGGING: If there is a logical bug, algorithmic flaw, or missing boundary condition, pinpoint the exact variable, loop condition, or state transition that is failing. "
                "Ask a probing question or give a conceptual nudge to help them spot the bug themselves. "
                "STRICT RULE: DO NOT write syntax, code blocks, or direct solutions under any circumstances. Provide ONLY interview-style guidance and conceptual pointers."
            )

        else:  # GENERAL or NO_MEMORY
            system_role = (
                "You are Jugnu, a personal coding assistant with access to this developer's own "
                "learning history. Answer using the context above where relevant. "
                "Be direct, specific, and technical. "
                "Start with: 'Based on your past work on [topic], ...' when context is relevant. "
                "If context doesn't help, answer from general knowledge but say so."
            )
        prompt = f"""You are Jugnu, a personal AI coding assistant.
            {system_role}
            Context:
            {context_block}
            Developer's question / current situation:
            {user_query}
            {source_hint}
            Answer in under 5 sentences. Be direct and lead with the most actionable insight."""

        try:
            response = ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                think=False,
                options={
                    "num_ctx": 8192,
                    "num_predict": 500,
                    "flash_attn": False,
                }
            )
            if hasattr(response, 'message') and response.message:
                content = (response.message.content or '').strip()
                return content if content else "I couldn't find relevant context. Try asking more specifically."
            return "Error generating answer."
        except Exception as e:
            return f"Ollama error: {e}"

    def extract_section(self, text: str, control_type: str, cleaned_content: str = "") -> dict | None:
        """
            Extracts structured METADATA (TOPIC, TAGS, NOTES) from one UIA section.
            cleaned_content is stored directly — Gemma no longer copies content verbatim,
            so it has full output budget for NOTES and never runs out of tokens.
        """
        prompt = f"""
            You are extracting structured metadata from a developer's screen capture (a {control_type} UI element).
            Ignore noisy UI labels (buttons, menus, navbars).
            If the text does not contain a clear coding problem, algorithm, API documentation, or technical concept, output exactly: NONE
            Output EXACTLY these three fields in this exact order. Do NOT use markdown. Do NOT add conversational filler:
            TOPIC: One line — what is this page/document about?
            TAGS: 3-6 semantic tags (comma separated, lowercase, no hyphens).
            NOTES: The required algorithmic paradigm, core data structures, time/space complexity, and key boundary conditions in 2-4 sentences. If an editorial or solution is visible, extract the core transition logic. Leave blank if none.
            <RAW_TEXT>
            {text}
            </RAW_TEXT>
        """
        try:
            print(f"\033[35m[Gemma]\033[0m Extracting metadata from {control_type} ({len(text)} chars)...")
            response = ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                think=False,
                options={
                    "num_ctx": 4096,
                    "num_predict": 400,   # TOPIC + TAGS + NOTES only — tiny output
                    "temperature": 0.1,
                    "flash_attn": False,
                }
            )
            raw = ""
            if hasattr(response, 'message') and response.message:
                raw = (response.message.content or '').strip()

            # Always show raw response so we can diagnose silent Gemma failures
            print(f"\033[90m[Gemma] Raw ({len(raw)} chars): {repr(raw[:250])}\033[0m")

            if not raw or raw.strip().upper() == "NONE":
                return None
            
            tags = []
            topic = ""
            notes = ""
            content = ""
            mode = None

            for line in raw.split('\n'):
                stripped_line = line.strip()
                if stripped_line.startswith("TOPIC:"):
                    topic = stripped_line.replace("TOPIC:", "").strip()
                    mode = None
                elif stripped_line.startswith("TAGS:"):
                    tags = [t.strip().lower() for t in stripped_line.replace("TAGS:", "").split(',') if t.strip()]
                    mode = None
                elif stripped_line.startswith("NOTES:"):
                    # NOTES now comes AFTER CONTENT so mode switches correctly
                    notes = stripped_line.replace("NOTES:", "").strip()
                    mode = "notes"
                elif mode == "notes":
                    notes += "\n" + line

            # Use full cleaned content passed by caller — no token budget truncation
            content = cleaned_content if cleaned_content else text
            notes   = notes.strip()
            if not content or content.upper() == "NONE":
                print(f"\033[33m[AIEngine] extract_section: no content after parse. Raw: {repr(raw[:150])}\033[0m")
                return None
                
            return {"content": content, "tags": tags, "notes": notes, "topic": topic}
        except Exception as e:
            print(f"\033[1;31m[AIEngine] extract_section failed: {e}\033[0m")
            return None

    def combine_sections(self, extractions: list[dict], file_path: str | None = None) -> dict | None:
        """
        Combines per-section extractions into one unified knowledge doc.
        Pure Python — zero LLM calls, zero CUDA risk.
        """
        valid = [e for e in extractions if e and e.get("content")]

        if not valid:
            return None
        
        # Deduplicated semantic tags, ordered by first appearance (case and hyphen insensitive)
        all_tags = []
        seen = set()
        for e in valid:
            for tag in e.get("tags", []):
                norm_tag = tag.lower().replace("-", " ")
                if norm_tag not in seen:
                    seen.add(norm_tag)
                    all_tags.append(tag)

        # Join content sections
        combined_content = "\n\n".join(e["content"] for e in valid if not e.get("verbatim") and e.get("content"))

        # Join code sections
        combined_code = "\n\n".join(e["content"] for e in valid if e.get("verbatim") and e.get("content"))

        # Join notes sections
        combined_notes = "\n\n".join(e.get("notes", "") for e in valid if e.get("notes"))

        non_verbatim = [e for e in valid if not e.get("verbatim")]
        
        fallback_topic = "Captured session"
        if "Solution" in combined_code and "class " in combined_code:
            fallback_topic = "LeetCode Problem"
        elif combined_code:
            fallback_topic = "Code Snippet"

        topic = non_verbatim[0].get("topic", fallback_topic) if non_verbatim else valid[0].get("topic", fallback_topic)
        # Fast semantic anchor summary
        summary = self.generate_summary(topic, combined_content, combined_code)
        
        is_full_buffer = any(e.get("full_buffer", False) for e in valid if e.get("verbatim"))
        
        return {
            "topic": topic, "tags": all_tags, "notes": combined_notes, 
            "content": combined_content, "code_snippet": combined_code, 
            "summary": summary, "file_path": file_path,
            "source_type": "ide" if file_path else "browser",
            "full_buffer": is_full_buffer
        }
    
    def generate_summary(self, topic: str, content: str, code: str) -> str:
        prompt = f"Write a 1-2 sentence plain prose summary of this developer task, capturing the technical objective and core problem constraints.\nTOPIC: {topic}\nCONTENT: {content[:1500]}\nCODE: {code[:1500]}\nOutput ONLY the summary sentences."
        try:
            response = ollama.chat(
                model=self.model_name, messages=[{"role": "user", "content": prompt}], think=False,
                options={"num_ctx": 2048, "num_predict": 150, "temperature": 0.2, "flash_attn": False}
            )
            if hasattr(response, 'message') and response.message:
                return (response.message.content or '').strip()
        except Exception:
            pass
        return topic

    def check_code_correctness(self, code: str, content: str, hint_history: list[str] = None, last_feedback: str = None) -> dict:
        """
        Combined LLM gate to check if the code correctly solves the problem and generate appropriate response.
        If correct: returns efficiency review
        If incorrect: returns practice hint data
        Returns dict with keys:
          - type: 'efficiency_review' or 'practice_hint'
          - content: the generated text (review or hint)
          - approach: current approach description (only for practice_hint)
          - hint_type: hint category (only for practice_hint)
          - is_solved: 0 or 1 (only for practice_hint)
        """
        history_text = ""
        if hint_history:
            history_text = "<past_hints>\n" + "\n".join(f"- {h}" for h in hint_history) + "\n</past_hints>\n\n"
            
        feedback_text = ""
        if last_feedback:
            feedback_text = f"<feedback>\nUSER FEEDBACK ON LAST HINT: '{last_feedback}' (Adjust your next hint accordingly!)\n</feedback>\n\n"

        prompt = (
            f"<problem>\n{content}\n</problem>\n\n"
            f"<code>\n{code}\n</code>\n\n"
            f"{history_text}{feedback_text}"
            "<task>\n"
            "1. Evaluate whether this code produces correct output for ALL valid inputs given the constraints above. "
            "You MUST check: (a) Is the algorithm logically correct? "
            "(b) Does the solution's time/space complexity fit within the problem's constraints? "
            "A solution that is logically correct but would exceed time/memory limits for the given input sizes is NOT correct.\n"
            "2. If the code is correct AND efficient enough for the constraints, respond ONLY with these two lines:\n"
            "   IS_SOLVED: 1\n"
            "   EFFICIENCY_REVIEW: <brief encouraging review covering time/space complexity and any potential optimizations, max 3-4 sentences>\n"
            "3. If the code is INCORRECT, has wrong logic, or is too slow/memory-heavy for the stated constraints, respond ONLY with:\n"
            "   APPROACH: <Describe their current approach in 2-5 words>\n"
            "   IS_SOLVED: 0\n"
            "   TYPE: <A 1-2 word hint category>\n"
            "   HINT: <Your 1-2 sentence Socratic hint ending with a question. DO NOT repeat past hints!>\n"
            "</task>"
        )
        print("COMBINED CORRECTNESS CHECK & RESPONSE GENERATION")
        print(prompt)
        try:
            # CRITICAL: think=True is intentional here. Correctness evaluation on algorithmic
            # code requires the model to trace through the logic step-by-step before answering.
            # With think=False, Gemma pattern-matches on code structure and gives wrong verdicts
            # (e.g. labelling a correct DP solution as "Logic Flaw" because index variable names
            # look unusual). The thinking block is discarded — only `content` is parsed.
            response = ollama.chat(
                model=self.model_name, messages=[{"role": "user", "content": prompt}], think=True,
                options={"num_ctx": 8192, "num_predict": -1, "temperature": 0.0, "flash_attn": False}
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

            # For debugging
            print(f"DEBUG: content='{content}', thinking='{thinking}'")

            # Parse the response
            content_upper = content.upper().strip()

            has_solved_flag = "IS_SOLVED: 1" in content_upper
            has_efficiency_review = "EFFICIENCY_REVIEW:" in content_upper

            if has_efficiency_review and has_solved_flag:
                # Extract the efficiency review text
                # Find the EFFICIENCY_REVIEW line and grab everything after the colon
                # We truncate at APPROACH: in case Gemma hallucinates both blocks.
                review_text = content[content_upper.index("EFFICIENCY_REVIEW:") + len("EFFICIENCY_REVIEW:"):].strip()
                if "APPROACH:" in review_text.upper():
                    review_text = review_text[:review_text.upper().index("APPROACH:")].strip()
                elif "\n\n" in review_text:
                    review_text = review_text.split("\n\n")[0].strip()
                    
                return {
                    "type": "efficiency_review",
                    "content": review_text,
                    "approach": None,
                    "hint_type": None,
                    "is_solved": None
                }
            else:
                # Parse practice hint format
                approach = "unknown"
                hint_type = "CONCEPTUAL"
                hint_text = "What's your current thinking on the approach?"
                is_solved = 0

                lines = content.splitlines()
                for line in lines:
                    line = line.strip()
                    if line.upper().startswith("APPROACH:"):
                        approach = line.split(":", 1)[1].strip()
                    elif line.upper().startswith("TYPE:"):
                        extracted = line.split(":", 1)[1].strip()
                        extracted = "".join(c for c in extracted if c.isalpha() or c == '_')
                        if extracted:
                            hint_type = extracted
                    elif line.upper().startswith("HINT:"):
                        hint_text = line.split(":", 1)[1].strip()
                        # Add any continuation lines
                        for i in range(lines.index(line) + 1, len(lines)):
                            if lines[i].strip():
                                hint_text += " " + lines[i].strip()
                        break

                return {
                    "type": "practice_hint",
                    "content": hint_text,
                    "approach": approach,
                    "hint_type": hint_type,
                    "is_solved": is_solved
                }
        except Exception as e:
            print(f"\033[1;31m[AIEngine] check_code_correctness error: {e}\033[0m")
            # Fallback to practice hint on error
            return {
                "type": "practice_hint",
                "content": "What's your current thinking on the approach?",
                "approach": "unknown",
                "hint_type": "CONCEPTUAL",
                "is_solved": 0
            }
