from torch import mode
import ollama
import ollama

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

        prompt = f"""You are Jugnu, a smart background AI assistant for a developer.
The developer seems stuck. Here is their current OS context:
{context_str}
Provide a short, proactive suggestion to help them move forward. Keep it under 3 sentences. Be direct and specific. Do NOT show your reasoning or thinking process."""

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
    

    def extract_ocr_chunk(self, chunk: str) -> str:
        """
        Uses Gemma as an extraction engine, not a classifier.
        Given a noisy OCR chunk, extracts only genuinely useful technical content.
        
        Returns the cleaned text string, or empty string if nothing useful found.
        
        Key settings:
        - num_predict: 300 — enough for a full code snippet or explanation
        - temperature: 0.1 — near-deterministic, we don't want creative extraction
        - think: False — no reasoning monologue, just the extracted text
        """

        prompt = f"""You are a technical knowledge extractor for a developer AI assistant.
        You are given raw text captured from a developer's screen via OCR. The text contains a mix of actual content and UI noise (window titles, tab names, menu bars, button labels, scrollbar text).
        Your job: extract ONLY the genuinely useful technical information.
        Keep: code snippets, error messages, algorithm explanations, problem statements, documentation paragraphs, technical concepts.
        Remove: window chrome, browser navigation, tab titles, menu items, OS UI elements, random single words.
        If there is no useful technical content at all, return exactly: NONE

        Text:
        {chunk}

        Extracted content:"""

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