import ollama

class AIEngine:
    def __init__(self):
        # Using gemma4:e2b which successfully ran without crashing
        self.model_name = "gemma4:e2b"

    def generate_insight(self, context_str):
        system_prompt = f"""You are Jugnu, a highly intelligent background AI assistant for a developer.
        The developer seems stuck. Here is their current OS context:
        {context_str}
        Provide a short, proactive suggestion to help them. Do NOT be annoying. Keep it under 3 sentences."""

        print(f"\n\033[1;35m[AIEngine]\033[0m Querying local model {self.model_name}...")

        try:
            # We use ollama.chat to talk to the local background service
            response = ollama.chat(model = self.model_name, messages = [
                {"role": "system", "content": system_prompt}
            ])
            return response['message']['content']
        
        except Exception as e:
            return f"Ollama Connection Failed! {e}"