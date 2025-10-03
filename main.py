# main.py
import os
import re
import time
import json
import requests
import datetime
from typing import List, Tuple, Dict, Optional
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
import logging

# Optional OpenAI import (used only for Whisper transcription if key present)
try:
    import openai
except Exception:
    openai = None

# -----------------------
# Configuration (ENV)
# -----------------------

# read public base URL from environment (no trailing slash)
RENDER_URL = os.getenv("RENDER_URL", "").rstrip("/")

# example usage when building TwiML action target:
action_url = f"{RENDER_URL}/voice" if RENDER_URL else "/voice"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # optional: only used for transcription
DEBUG = os.getenv("DEBUG", "true").lower() in ("1", "true", "yes")
SESSION_TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT_SECONDS", "600"))  # 10 minutes default
LOG_PATH = os.getenv("LOG_PATH", "call_logs.txt")

if OPENAI_API_KEY and openai:
    openai.api_key = OPENAI_API_KEY

# -----------------------
# Logging setup
# -----------------------
logger = logging.getLogger("pizza_ai")
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
handler = logging.FileHandler(LOG_PATH)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(console)

def save_log_line(s: str):
    logger.info(s)

# -----------------------
# Demo menus (per-client)
# You can later replace this with DB lookup keyed by 'To' phone number.
# -----------------------
DEFAULT_MENU = {
    "margherita": 12.00,
    "pepperoni": 14.00,
    "veggie": 13.00,
    "garlic bread": 5.00,
    "soda": 2.00,
    "water": 1.00,
}

DEFAULT_COMBOS = {
    "combo 1": (["margherita", "soda"], 13.00),
    "combo 2": (["pepperoni", "garlic bread", "soda"], 18.00),
    "combo 3": (["veggie", "soda", "water"], 16.00),
}

# For demonstration you can set multiple clients like:
# CLIENT_CONFIG["+15551234567"] = {"name": "GoldStone Pizza", "menu": {...}, "combos": {...}}
CLIENT_CONFIG: Dict[str, Dict] = {}

# If a number isn't in CLIENT_CONFIG, we use DEFAULT_MENU/COMBOS
def get_client_config(to_number: Optional[str]):
    if to_number and to_number in CLIENT_CONFIG:
        cfg = CLIENT_CONFIG[to_number]
        menu = cfg.get("menu", DEFAULT_MENU)
        combos = cfg.get("combos", DEFAULT_COMBOS)
        name = cfg.get("name", "Our Pizza Shop")
    else:
        menu = DEFAULT_MENU
        combos = DEFAULT_COMBOS
        name = "Demo Pizza"
    return {"name": name, "menu": menu, "combos": combos}

# -----------------------
# In-memory sessions
# sessions keyed by CallSid
# -----------------------
sessions: Dict[str, Dict] = {}

def cleanup_expired_sessions():
    now = time.time()
    expired = [sid for sid, s in sessions.items() if now - s.get("last_active", 0) > SESSION_TIMEOUT_SECONDS]
    for sid in expired:
        logger.debug(f"Cleaning expired session: {sid}")
        sessions.pop(sid, None)

# -----------------------
# Utilities: parsing and totals
# -----------------------
NUMBER_WORDS = {
    "zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10
}
def word_to_int(w: str) -> Optional[int]:
    return NUMBER_WORDS.get(w)

qty_pattern = re.compile(r"(?P<qty>\d+)\s+(?P<item>[\w\s]+)")
qty_word_pattern = re.compile(r"(?P<qty_word>\b(?:" + "|".join(NUMBER_WORDS.keys()) + r")\b)\s+(?P<item>[\w\s]+)")

def normalize_text(s: str) -> str:
    return re.sub(r"[^\w\s]", " ", s.lower()).strip()

def parse_items_from_text(text: str, menu_keys: List[str], combo_keys: List[str]) -> List[Tuple[str,int]]:
    """
    Returns list of (item_key, qty)
    Strategy:
    - check combos first (exact phrase)
    - find "N item" patterns, number words, or plain mentions
    - avoid double-counting
    """
    found = []
    t = normalize_text(text)
    # detect combos
    for combo in combo_keys:
        if combo in t:
            found.append((combo, 1))
            t = t.replace(combo, "")
    # numeric digits
    for m in qty_pattern.finditer(t):
        qty = int(m.group("qty"))
        item_text = m.group("item").strip()
        # try match menu or combo key by prefix matching
        matched = match_item_name(item_text, menu_keys + combo_keys)
        if matched:
            found.append((matched, qty))
            t = t.replace(m.group(0), "")
    # number words
    for m in qty_word_pattern.finditer(t):
        w = m.group("qty_word")
        qty = word_to_int(w) or 1
        item_text = m.group("item").strip()
        matched = match_item_name(item_text, menu_keys + combo_keys)
        if matched:
            found.append((matched, qty))
            t = t.replace(m.group(0), "")
    # plain mentions (single qty)
    for key in menu_keys + combo_keys:
        if key in t.split():
            found.append((key,1))
            t = t.replace(key, "")
    # fallback: try fuzzy contains
    for key in menu_keys + combo_keys:
        if key in normalize_text(text) and key not in [k for k,_ in found]:
            found.append((key,1))
    return found

def match_item_name(fragment: str, candidates: List[str]) -> Optional[str]:
    frag = fragment.strip()
    # exact or startswith match
    for c in candidates:
        if frag == c:
            return c
    for c in candidates:
        if frag.startswith(c) or c.startswith(frag):
            return c
    # contains
    for c in candidates:
        if frag in c or c in frag:
            return c
    return None

def calculate_total(order: List[Tuple[str,int,float]]) -> float:
    # Sum qty * unit_price
    total = 0.0
    for name, qty, price in order:
        # careful arithmetic digit-by-digit approach (simple but correct)
        subtotal = qty * price
        total += subtotal
    return round(total, 2)

def format_order_for_speech(order: List[Tuple[str,int,float]]) -> str:
    if not order:
        return "nothing"
    parts = []
    for name, qty, price in order:
        parts.append(f"{qty} {name} (${price:.2f} each)")
    return "; ".join(parts)

# -----------------------
# Transcription (Whisper) - optional
# -----------------------
def transcribe_recording(recording_url: str) -> str:
    if not OPENAI_API_KEY or not openai:
        return ""
    try:
        # Twilio recordings can be fetched as .wav
        if recording_url.endswith(".wav"):
            url = recording_url
        else:
            url = recording_url + ".wav"
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        audio_path = f"tmp_{int(time.time())}.wav"
        with open(audio_path, "wb") as f:
            f.write(r.content)
        with open(audio_path, "rb") as audio_file:
            transcription = openai.Audio.transcribe("whisper-1", audio_file)
        text = transcription.get("text","").strip()
        save_log_line(f"Transcription ({recording_url}): {text}")
        return text
    except Exception as e:
        logger.exception("Transcription failed")
        return ""

# -----------------------
# TwiML helpers and localized prompts
# -----------------------
def say_text(t: str) -> str:
    vr = VoiceResponse()
    vr.say(t)
    return str(vr)

def gather_speech(action_url: str, prompt: str, timeout: int = 5) -> str:
    vr = VoiceResponse()
    g = Gather(input="speech", action=action_url, method="POST", timeout=timeout, speech_timeout="auto")
    g.say(prompt)
    vr.append(g)
    return str(vr)

PROMPTS = {
    "en": {
        "ask_language": "Welcome to Demo Pizza. Please say English or Spanish to continue.",
        "ask_order": "What would you like to order? We have Margherita, Pepperoni, Veggie, Garlic Bread, Soda, Water, and combos.",
        "did_not_hear": "Sorry, I didn't catch that. Please say your order.",
        "confirm_final": "You ordered {items}. Your total is ${total}. Say Yes to confirm or No to change your order."
    },
    "es": {
        "ask_language": "Bienvenido a Demo Pizza. Por favor diga Inglés o Español para continuar.",
        "ask_order": "¿Qué te gustaría pedir? Tenemos Margherita, Pepperoni, Veggie, Pan de Ajo, Refresco y combos.",
        "did_not_hear": "Lo siento, no entendí. Por favor diga su pedido.",
        "confirm_final": "Has pedido {items}. El total es ${total}. Diga Sí para confirmar o No para cambiar su pedido."
    }
}

def lang_for_session(session: Dict) -> str:
    return "es" if session.get("language") == "Spanish" else "en"

# -----------------------
# FastAPI app and routes
# -----------------------
app = FastAPI()

@app.get("/health")
def health():
    return {"status":"ok", "time": datetime.datetime.utcnow().isoformat()}

@app.post("/call")
async def call_entry():
    """
    Twilio should POST here when a call comes in.
    This route returns TwiML with a Gather that posts to /voice on the full URL.
    """
    cleanup_expired_sessions()
    if not RENDER_URL:
        logger.warning("RENDER_URL not set - TwiML gather actions will be relative and may not work in Twilio.")
    action_url = f"{RENDER_URL}/voice" if RENDER_URL else "/voice"
    prompt = PROMPTS["en"]["ask_language"]
    twiml = gather_speech(action_url=action_url, prompt=prompt, timeout=5)
    save_log_line(f"/call returned Gather -> {action_url}")
    return Response(content=twiml, media_type="application/xml")

@app.post("/voice")
async def voice(
    RequestObj: Request,
    CallSid: str = Form(...),
    SpeechResult: Optional[str] = Form(default=None),
    RecordingUrl: Optional[str] = Form(default=None),
    To: Optional[str] = Form(default=None),
    From: Optional[str] = Form(default=None),
    Confidence: Optional[str] = Form(default=None),
    **form_data
):
    """
    Main voice handler. Handles language selection, ordering, modification, and confirmation.
    Twilio will POST SpeechResult if speech recognition runs, or RecordingUrl if using <Record>.
    """
    cleanup_expired_sessions()
    payload = dict(form_data)
    # Twilio sometimes gives SpeechResult in form field, sometimes not; RecordingUrl may be present.
    save_log_line(f"POST /voice payload: CallSid={CallSid} From={From} To={To} SpeechResult={SpeechResult} RecordingUrl={RecordingUrl} ExtraKeys={list(payload.keys())}")

    # Ensure session
    if CallSid not in sessions:
        sessions[CallSid] = {"language": None, "order": [], "state": "language", "last_active": time.time(), "client_to": To}
    session = sessions[CallSid]
    session["last_active"] = time.time()

    # Determine client menu
    client_cfg = get_client_config(To)
    menu = {k.lower(): v for k,v in client_cfg["menu"].items()} if client_cfg.get("menu") else {k:v for k,v in DEFAULT_MENU.items()}
    combos = {k.lower(): v for k,v in client_cfg.get("combos", DEFAULT_COMBOS).items()}

    # If no SpeechResult but there is a recording, try transcribing with Whisper
    if not SpeechResult and RecordingUrl:
        try:
            SpeechResult = transcribe_recording(RecordingUrl)
        except Exception as e:
            logger.exception("Transcription error")

    # If still no SpeechResult, prompt user
    if not SpeechResult:
        # Twilio may send blank result if silence; ask again
        msg = PROMPTS[lang_for_session(session)]["did_not_hear"]
        action_url = f"{RENDER_URL}/voice" if RENDER_URL else "/voice"
        tw = gather_speech(action_url=action_url, prompt=msg, timeout=5)
        return Response(content=tw, media_type="application/xml")

    # normalize input text
    text = normalize_text(SpeechResult)
    save_log_line(f"CallSid={CallSid} | RecognizedText={text}")

    # -----------------------
    # Language selection state
    # -----------------------
    if session["language"] is None or session["state"] == "language":
        if "spanish" in text or "español" in text or "espanol" in text:
            session["language"] = "Spanish"
            session["state"] = "ordering"
            prompt = PROMPTS["es"]["ask_order"]
            tw = gather_speech(action_url=f"{RENDER_URL}/voice", prompt=prompt, timeout=6)
            return Response(content=tw, media_type="application/xml")
        else:
            # default to English if not Spanish
            session["language"] = "English"
            session["state"] = "ordering"
            prompt = PROMPTS["en"]["ask_order"]
            tw = gather_speech(action_url=f"{RENDER_URL}/voice", prompt=prompt, timeout=6)
            return Response(content=tw, media_type="application/xml")

    # -----------------------
    # If in ordering state
    # -----------------------
    if session["state"] == "ordering":
        # Check for finish keywords
        if any(k in text for k in ["done", "no more", "that's it", "finished", "finish", "im done", "i'm done"]):
            if not session["order"]:
                # nothing ordered yet - ask again
                prompt = PROMPTS[lang_for_session(session)]["ask_order"]
                tw = gather_speech(action_url=f"{RENDER_URL}/voice", prompt=prompt, timeout=6)
                return Response(content=tw, media_type="application/xml")
            # move to confirm
            session["state"] = "confirm"
            items_str = format_order_for_speech(session["order"])
            total = calculate_total(session["order"])
            msg = PROMPTS[lang_for_session(session)]["confirm_final"].format(items=items_str, total=f"{total:.2f}")
            tw = gather_speech(action_url=f"{RENDER_URL}/voice", prompt=msg, timeout=6)
            return Response(content=tw, media_type="application/xml")

        # Remove / change commands
        if any(k in text for k in ["remove", "delete", "cancel", "change"]):
            # find item mention
            removed = False
            for name, qty, price in list(session["order"]):
                if name in text:
                    session["order"].remove((name, qty, price))
                    removed = True
                    save_log_line(f"Removed item {name} from CallSid={CallSid}")
                    break
            if not removed:
                # user might have said "remove pepperoni" or "delete pizza"
                # ask clarifying question
                prompt = "I couldn't find that item in your order. Which item would you like to remove?"
                return Response(content=gather_speech(action_url=f"{RENDER_URL}/voice", prompt=prompt), media_type="application/xml")
            # confirm current order
            items = format_order_for_speech(session["order"])
            total = calculate_total(session["order"])
            msg = f"Removed the item. Your current order: {items}. Total: ${total:.2f}. Would you like anything else?"
            return Response(content=gather_speech(action_url=f"{RENDER_URL}/voice", prompt=msg), media_type="application/xml")

        # Try to parse explicit items & quantities
        menu_keys = list(menu.keys())
        combo_keys = list(combos.keys())
        parsed = parse_items_from_text(text, menu_keys, combo_keys)
        added = False
        for key, qty in parsed:
            if key in combos:
                items_list, price = combos[key]
                # combos may include multiple items; we treat combo as one priced unit
                session["order"].append((key, qty, price))
                added = True
            elif key in menu:
                price = menu[key]
                session["order"].append((key, qty, price))
                added = True

        if added:
            total = calculate_total(session["order"])
            items_str = format_order_for_speech(session["order"])
            msg = f"Added to your order: {items_str}. Current total: ${total:.2f}. Would you like to add more or are you done?"
            return Response(content=gather_speech(action_url=f"{RENDER_URL}/voice", prompt=msg), media_type="application/xml")

        # If nothing matched, ask a clarifying question (rule-based fallback)
        # Keep it concise
        fallback = "I didn't understand that. Please say the item name and quantity, for example 'two pepperoni' or say 'done' to finish.'"
        # Spanish fallback if needed
        if lang_for_session(session) == "es":
            fallback = "No entendí. Por favor diga el nombre del artículo y la cantidad, por ejemplo 'dos pepperoni' o diga 'listo' para finalizar."
        return Response(content=gather_speech(action_url=f"{RENDER_URL}/voice", prompt=fallback, timeout=6), media_type="application/xml")

    # -----------------------
    # If in confirm state
    # -----------------------
    if session["state"] == "confirm":
        if any(k in text for k in ["yes", "confirm", "yep", "si", "sure"]):
            # finalize: optionally push to POS/CRM here
            total = calculate_total(session["order"])
            items_str = format_order_for_speech(session["order"])
            save_log_line(f"FINAL ORDER CallSid={CallSid} From={From} To={To} Order={session['order']} Total={total:.2f}")
            # clear session
            sessions.pop(CallSid, None)
            farewell = f"Thank you! Your order of {items_str} has been placed. Your total is ${total:.2f}. Goodbye!"
            if lang_for_session(session) == "es":
                farewell = f"Gracias! Su pedido de {items_str} ha sido confirmado. Su total es ${total:.2f}. ¡Adiós!"
            vr = VoiceResponse()
            vr.say(farewell)
            return Response(content=str(vr), media_type="application/xml")
        else:
            # user wants changes
            session["state"] = "ordering"
            prompt = "Okay, what would you like to change or add?"
            if lang_for_session(session) == "es":
                prompt = "De acuerdo, ¿qué le gustaría cambiar o agregar?"
            return Response(content=gather_speech(action_url=f"{RENDER_URL}/voice", prompt=prompt, timeout=6), media_type="application/xml")

    # Default fallback
    return Response(content=gather_speech(action_url=f"{RENDER_URL}/voice", prompt="Sorry, I didn't understand that. Please say your order."), media_type="application/xml")
