# 🌟 Jugnu — Onboarding Flow Design

The onboarding system runs once on first launch. It opens the Jugnu Native WebView2 interface and has a warm 8-12 turn conversation with the user to build their initial persona profile.

*(Note: For the exact LLM prompts used in this flow, refer to `prompts_design.md`).*

---

## The 4-Track System

The C++ engine orchestrates the onboarding by calling the Python inference service on specific "Tracks" depending on the conversation state.

### Track 1: The Opener
Fired immediately when the WebView2 window loads. Jugnu introduces itself and asks a single warm opening question to get the user talking about their setup or goals.

### Track 2: The Conversational Engine
For turns 2 through 8, the C++ engine sends the conversation history to Python. The LLM is instructed to ask exactly ONE follow-up question per turn to dig deeper into the user's routines (e.g., "Are you mainly prepping for placements right now?"). No advice is given, just curiosity.

### Track 3: The Fact Extractor (Hidden)
Running asynchronously in the background during the conversation, this track reads the user's responses and extracts hard facts (e.g., "User uses Chrome", "User is a CS student"). These facts are instantly saved to the SQLite `core_persona` table.

### Track 4: The Concluder
Triggered after turn 8. Jugnu delivers a warm closing statement expressing excitement to be their companion. The UI transitions from "Setup Mode" to "Active Mode", and the background screen monitoring begins.

---

## Handling Edge Cases

1. **User Closes Window Mid-Onboarding:**
   The SQLite database saves the conversation state. When `Ctrl+Space` is pressed again, the WebView2 window resumes exactly where it left off.

2. **User Gives Short Answers ("yes", "no"):**
   The Track 2 prompt is designed to ask open-ended questions if the user is being brief, encouraging them to elaborate naturally.
