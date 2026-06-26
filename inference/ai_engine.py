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
Provide a short, proactive suggestion to help them move forward. Keep it under 3 sentences. Be direct and specific."""

        print(f"\n\033[1;35m[AIEngine]\033[0m Querying local model {self.model_name}...")

        try:
            response = ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                options={
                    "num_ctx": 2048,      # Safe window that fits in VRAM without warmup crash
                    "flash_attn": False,  # Disable Flash Attention — fixes CUDA PDL crash on RTX 4050
                    "num_predict": 512,   # Increased to give reasoning models room to think
                }
            )
            msg = response.get('message', {})
            content = msg.get('content', '').strip()
            thinking = msg.get('thinking', '').strip()
            
            if content:
                return content
            elif thinking:
                return f"[Thinking]...\n{thinking}"
            else:
                return "The AI generated an empty response. Try asking again."

        except Exception as e:
            return f"Ollama Connection Failed! {e}"