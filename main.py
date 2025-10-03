import os
from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from typing import Dict

app = FastAPI()

# Environment variables
RENDER_URL = os.getenv("RENDER_URL", "").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # Keep your key safe in Render env

# Simple pizza menu
MENU = {
    "pizza": {"Margherita": 12, "Pepperoni": 14, "Veggie": 13},
    "drinks": {"Coke": 3, "Water": 2, "Sprite": 3},
    "combos": {"Pizza+Drink": 15, "Family Combo": 40}
}

# Temporary storage for conversation state (in-memory for demo)
CALL_STATE: Dict[str, Dict] = {}  # key = CallSid, value = state dict

@app.post("/call")
async def call_handler():
    """Initial Twilio call route"""
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
    """Handles Twilio speech input"""
    print(f"[DEBUG] CallSid: {CallSid}, SpeechResult: {SpeechResult}")

    # Initialize call state
    if CallSid not in CALL_STATE:
        CALL_STATE[CallSid] = {"step": "language", "order": [], "total": 0, "language": "English"}

    state = CALL_STATE[CallSid]

    # Step 1: Language selection
    if state["step"] == "language":
        lang_choice = SpeechResult.lower()
        if "spanish" in lang_choice or "español" in lang_choice:
            state["language"] = "Spanish"
            greeting = "¡Bienvenido a GoldStone Pizza!"
        else:
            state["language"] = "English"
            greeting = "Welcome to GoldStone Pizza!"
        state["step"] = "order"
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
        added_item = ""
        added_price = 0

        # Check menu items
        for category in MENU:
            for item_name, price in MENU[category].items():
                if item_name.lower() in user_input:
                    state["order"].append(item_name)
                    state["total"] += price
                    added_item = item_name
                    added_price = price
                    break
            if added_item:
                break

        # Response
        if added_item:
            twiml_text = f"Added {added_item} for ${added_price}. Your current total is ${state['total']}. Would you like to add anything else? Say done when finished."
        elif "done" in user_input:
            order_summary = ", ".join(state["order"])
            twiml_text = f"Your order is {order_summary}. The total is ${state['total']}. Thank you for ordering at GoldStone Pizza!"
            # Reset state after completion
            CALL_STATE.pop(CallSid, None)
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
