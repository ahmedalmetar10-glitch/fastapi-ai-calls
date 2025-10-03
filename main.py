# main.py
from fastapi import FastAPI, Form, Response
import os
import requests
import openai
import datetime
import re
from typing import List, Tuple

app = FastAPI()

# -------------------------
# CONFIG (use environment vars)
# -------------------------
RENDER_URL = os.getenv("RENDER_URL", "https://your-app-name.onrender.com")  # update on Render
openai.api_key = os.getenv("OPENAI_API_KEY")  # MUST be set in env (locally and on Render)

LOG_FILE = "call_logs.txt"

# -------------------------
# Menu (editable per client later)
# -------------------------
MENU = {
    "margherita": 12,
    "pepperoni": 14,
    "veggie": 13,
    "garlic bread": 5,
    "soda": 2,
    "water": 1,
}

COMBOS = {
    "combo 1": (["margherita", "soda"], 13),
    "combo 2": (["pepperoni", "garlic bread", "soda"], 18),
    "combo 3": (["veggie", "soda", "water"], 16),
}

# -------------------------
# In-memory sessions (demo only)
# sessions[CallSid] = {"language": "English"/"Spanish", "order":[(name,qty,unit_price)], "step": "..."}
# -------------------------
sessions = {}

# -------------------------
# Helpers
# -------------------------
def save_log(text: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {text}\n")

def speak_twiml(text: str, gather=True):
    # Returns TwiML - if gather True, ask for more speech input
    if gather:
        action = f"{RENDER_URL}/voice"
        return f"""<Response>
  <Gather input="speech" action="{action}" method="POST" timeout="5" speechTimeout="auto">
    <Say>{text}</Say>
  </Gather>
</Response>"""
    else:
        return f"""<Response><Say>{text}</Say></Response>"""

def parse_quantity_and_item(text: str) -> List[Tuple[str,int]]:
    """
    Very simple parser:
    - looks for patterns like '2 pepperoni', 'one margherita', or item names
    - returns list of (item_key, qty)
    """
    results = []
    t = text.lower()
    # numeric words map
    num_map = {
        "one":1, "two":2, "three":3, "four":4, "five":5, "six":6
    }
    # check combos first
    for combo in COMBOS.keys():
        if combo in t:
            results.append((combo, 1))
            # remove that text to avoid double-match
            t = t.replace(combo, "")

    # look for explicit "N item" patterns
    for item in list(MENU.keys()):
        # pattern: number (digit) + item
        m = re.search(r"(\d+)\s+" + re.escape(item), t)
        if m:
            qty = int(m.group(1))
            results.append((item, qty))
            t = t.replace(m.group(0), "")
            continue
        # pattern: word number
        for word,num in num_map.items():
            if re.search(r"\b" + word + r"\s+" + re.escape(item) + r"\b", t):
                results.append((item, num))
                t = re.sub(r"\b" + word + r"\s+" + re.escape(item) + r"\b", "", t)
                break
        # plain item
        if item in t:
            results.append((item, 1))
            t = t.replace(item, "")

    return results

def calculate_total(order: List[Tuple[str,int,float]]) -> int:
    return sum(qty * price for _, qty, price in order)

def format_order(order: List[Tuple[str,int,float]]) -> str:
    parts = [f"{qty} x {name.title()} (${price} each)" for name, qty, price in order]
    return "; ".join(parts) if parts else "nothing"

def transcribe_recording(recording_url: str) -> str:
    """Download recording URL and transcribe with Whisper if key exists."""
    try:
        # Twilio RecordingUrl returns .wav if we append .wav
        audio_url = f"{recording_url}.wav" if not recording_url.endswith(".wav") else recording_url
        r = requests.get(audio_url, timeout=15)
        audio_path = "caller_audio.wav"
        with open(audio_path, "wb") as f:
            f.write(r.content)
        # If OpenAI key exists, transcribe
        if openai.api_key:
            with open(audio_path, "rb") as audio_file:
                transcript = openai.Audio.transcribe("whisper-1", audio_file)
            return transcript.get("text", "").strip()
        else:
            return ""  # no key available
    except Exception as e:
        print("Transcription error:", e)
        return ""

def gpt_clarify(prompt_text: str) -> str:
    """Use GPT as fallback to formulate a friendly clarification question."""
    if not openai.api_key:
        # Without a key, return a simple clarifying question
        return "Sorry, I didn't understand that. Could you please repeat your order or say the item name?"
    try:
        resp = openai.Completion.create(
            model="text-davinci-003",
            prompt=prompt_text,
            max_tokens=80,
            temperature=0.6
        )
        return resp.choices[0].text.strip()
    except Exception as e:
        print("GPT clarify error:", e)
        return "Sorry, I didn't catch that. Can you repeat your item choice?"

# -------------------------
# Routes
# -------------------------
@app.get("/")
def root():
    return {"message": "AI Pizza Ordering Service running"}

@app.post("/call")
async def call_entry():
    """
    First entry point for Twilio: ask for language selection.
    Twilio phone number webhook should point to /call (POST).
    """
    # direct user to pick language
    twiml = speak_twiml(
        "Welcome to GoldStone Pizza. Please say English or Spanish to continue.",
        gather=True
    )
    return Response(content=twiml, media_type="application/xml")

@app.post("/voice")
async def voice(
    CallSid: str = Form(...),
    SpeechResult: str = Form(default=None),
    RecordingUrl: str = Form(default=None),
    To: str = Form(default=None),
    From: str = Form(default=None),
    **form_data
):
    """
    Core voice handler.
    Flow:
    - If new call: ask language
    - If language chosen: ask for order
    - Parse items; if parsed -> add to order and ask more or done
    - If 'done' -> confirm summary and ask confirm
    - If 'yes' confirm -> finalize
    - 'remove' or 'change' keywords handled
    """
    # debug log
    print("Full payload:", dict(form_data), "SpeechResult:", SpeechResult, "RecordingUrl:", RecordingUrl)
    save_log(f"CallSid:{CallSid} | From:{From} | To:{To} | SpeechResult:{SpeechResult} | RecordingUrl:{RecordingUrl}")

    # ensure session exists
    if CallSid not in sessions:
        sessions[CallSid] = {"language": None, "order": [], "step":"language"}  # step: language, ordering, confirm

    session = sessions[CallSid]

    # if we have recording and no SpeechResult, transcribe
    if not SpeechResult and RecordingUrl:
        SpeechResult = transcribe_recording(RecordingUrl)
        save_log(f"Transcription for {CallSid}: {SpeechResult}")
        print("Transcribed:", SpeechResult)

    # Step: language selection
    if session["language"] is None or session["step"] == "language":
        if SpeechResult:
            s = SpeechResult.lower()
            if "spanish" in s or "español" in s:
                session["language"] = "Spanish"
                greeting = "Perfecto. Continuamos en español. ¿Qué te gustaría pedir?"
            else:
                session["language"] = "English"
                greeting = "Great. We'll continue in English. What would you like to order? We have Margherita, Pepperoni, Veggie, Garlic Bread, Soda, Water and combos."
            session["step"] = "ordering"
            twiml = speak_twiml(greeting, gather=True)
            return Response(content=twiml, media_type="application/xml")
        else:
            return Response(content=speak_twiml("Please say English or Spanish to continue.", gather=True), media_type="application/xml")

    # Step: ordering
    if session["step"] == "ordering":
        text = (SpeechResult or "").strip()
        if not text:
            # ask again
            prompt = "Sorry, I didn't catch that. What would you like to order? Margherita, Pepperoni, Veggie, Garlic Bread, Soda, Water, or combos."
            return Response(content=speak_twiml(prompt, gather=True), media_type="application/xml")

        text_low = text.lower()

        # handle 'done' or finish intents
        if any(k in text_low for k in ["done", "that's all", "no more", "finish", "i'm done", "that's it"]):
            if not session["order"]:
                return Response(content=speak_twiml("You haven't ordered anything yet. What would you like?"), media_type="application/xml")
            # go to confirm step
            session["step"] = "confirm"
            items_str = format_order(session["order"])
            total = calculate_total(session["order"])
            msg = f"You ordered: {items_str}. Your total is ${total}. Say Yes to confirm or No to change your order."
            return Response(content=speak_twiml(msg, gather=True), media_type="application/xml")

        # handle remove/change commands
        if any(k in text_low for k in ["remove", "change", "cancel", "delete"]):
            # naive removal: find item name mentioned and remove first matching
            for name, qty, price in list(session["order"]):
                if name in text_low:
                    session["order"].remove((name, qty, price))
                    session["total"] = calculate_total(session["order"])
                    items_str = format_order(session["order"])
                    msg = f"Removed {name}. Current order: {items_str}. Add more or say Done."
                    return Response(content=speak_twiml(msg, gather=True), media_type="application/xml")
            # fallback: if no match
            msg = "I couldn't find that item in your order. Please say the item name you want to remove."
            return Response(content=speak_twiml(msg, gather=True), media_type="application/xml")

        # check combos and direct items
        parsed = parse_quantity_and_item(text)
        added_any = False
        for name, qty in parsed:
            if name in COMBOS:
                _, price = COMBOS[name]
                session["order"].append((name, qty, price))
                added_any = True
            elif name in MENU:
                price = MENU[name]
                session["order"].append((name, qty, price))
                added_any = True

        if added_any:
            total = calculate_total(session["order"])
            items_str = format_order(session["order"])
            msg = f"Added to your order: {items_str}. Current total: ${total}. Would you like to add more or are you done?"
            return Response(content=speak_twiml(msg, gather=True), media_type="application/xml")

        # fallback: ask GPT to clarify if available
        prompt = f"Customer said: '{text}'. They are ordering from menu: {list(MENU.keys())} and combos: {list(COMBOS.keys())}. If this matches an item, return a short confirmation like 'I will add X'. Otherwise ask a short clarifying question."
        gpt_reply = gpt_clarify(prompt)
        return Response(content=speak_twiml(gpt_reply, gather=True), media_type="application/xml")

    # Step: confirm
    if session["step"] == "confirm":
        txt = (SpeechResult or "").lower()
        if any(k in txt for k in ["yes", "confirm", "yup", "sure", "si", "correct"]):
            items_str = format_order(session["order"])
            total = calculate_total(session["order"])
            # Finalize order: placeholder for integration (POS, DB)
            save_log(f"FINAL ORDER CallSid:{CallSid} Order:{session['order']} Total:${total}")
            # Clear session
            del sessions[CallSid]
            return Response(content=speak_twiml(f"Thank you! Your order of {items_str} is confirmed. Your total is ${total}. Goodbye!", gather=False), media_type="application/xml")
        else:
            # user wants to change
            session["step"] = "ordering"
            return Response(content=speak_twiml("Okay, what would you like to change? You can remove items or add new ones." , gather=True), media_type="application/xml")

    # default fallback
    return Response(content=speak_twiml("Sorry, I didn't get that. Can you repeat?"), media_type="application/xml")
