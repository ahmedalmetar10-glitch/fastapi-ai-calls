import os
import re
from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from typing import Dict

app = FastAPI()

# Environment variables
RENDER_URL = os.getenv("RENDER_URL", "").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # safe in env

# Menu definition
MENU = {
    "pizza": {"Margherita": 12, "Pepperoni": 14, "Veggie": 13},
    "drinks": {"Coke": 3, "Water": 2, "Sprite": 3},
    "combos": {"Pizza+Drink": 15, "Family Combo": 40}
}

# In-memory call state for demo purposes
CALL_STATE: Dict[str, Dict] = {}  # CallSid -> state

# Helper function: normalize user input
def normalize_input(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)  # remove punctuation
    return text

# Helper function: parse items from speech
def parse_items(user_input: str):
    user_input = normalize_input(user_input)
    # split by 'and' or ',' to catch multiple items
    chunks = re.split(r"\band\b|,", user_input)
    items = []
    for chunk in chunks:
        chunk = chunk.strip()
        for category in MENU:
            for item_name, price in MENU[category].items():
                if item_name.lower() in chunk:
                    items.append((item_name, price))
    return items

@app.post("/call")
async def call_handler():
    """Initial route when Twilio calls"""
    action_url = f"{RENDER_URL}/voice" if RENDER_URL else "/voice"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="{action_url}" method="POST">
        <Say>Welcome to GoldStone Pizza! Please say your preferred language. For English, say English. Para Español, diga Español.</Say>
    </Gather>
</Response>"""
    return PlainTextResponse(content=twiml, media_type="application/xml")

@app.post("/voice")
async def voice_handler(
    SpeechResult: str = Form(default=""),
    CallSid: str = Form(default="")
):
    """Handles Twilio speech input for language and ordering"""
    print(f"[DEBUG] CallSid: {CallSid}, SpeechResult: {SpeechResult}")

    if CallSid not in CALL_STATE:
        # Initialize state
        CALL_STATE[CallSid] = {
            "step": "language",
            "language": "English",
            "order": [],
            "total": 0
        }

    state = CALL_STATE[CallSid]

    # Step 1: Language selection
    if state["step"] == "language":
        user_input = normalize_input(SpeechResult)
        if "spanish" in user_input or "español" in user_input:
            state["language"] = "Spanish"
            greeting = "¡Bienvenido a GoldStone Pizza!"
        else:
            state["language"] = "English"
            greeting = "Welcome to GoldStone Pizza!"
        state["step"] = "order"

        # Prompt for order
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="{RENDER_URL}/voice" method="POST">
        <Say>{greeting} What would you like to order? You can say pizza, drinks, or combos.</Say>
    </Gather>
</Response>"""
        return PlainTextResponse(content=twiml, media_type="application/xml")

    # Step 2: Ordering
    elif state["step"] == "order":
        user_input = SpeechResult.lower()
        items_found = parse_items(user_input)
        done_words = ["done", "finished", "no more"]

        # Check if user finished ordering
        if any(word in user_input for word in done_words):
            if not state["order"]:
                twiml_text = "You didn't order anything. Thank you for visiting GoldStone Pizza!"
            else:
                order_summary = ", ".join(state["order"])
                twiml_text = f"Your order is {order_summary}. The total is ${state['total']}. Thank you for ordering at GoldStone Pizza!"
            # Reset state
            CALL_STATE.pop(CallSid, None)
        elif items_found:
            added_text = []
            for name, price in items_found:
                state["order"].append(name)
                state["total"] += price
                added_text.append(f"{name} for ${price}")
            twiml_text = f"Added {' and '.join(added_text)}. Current total is ${state['total']}. Would you like to add anything else? Say done when finished."
        else:
            twiml_text = "I didn't catch that. Please say pizza, drinks, combos, or done if finished."

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="{RENDER_URL}/voice" method="POST">
        <Say>{twiml_text}</Say>
    </Gather>
</Response>"""
        return PlainTextResponse(content=twiml, media_type="application/xml")

    # Fallback
    else:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, something went wrong.</Say>
</Response>"""
        return PlainTextResponse(content=twiml, media_type="application/xml")
