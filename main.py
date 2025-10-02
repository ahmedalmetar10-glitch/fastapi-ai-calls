from fastapi import FastAPI, Form
from fastapi.responses import Response

app = FastAPI()

# Store session state (simple in-memory dictionary for demo)
user_sessions = {}


@app.get("/")
def home():
    return {"message": "AI Call Service is running!"}


@app.post("/voice")
async def voice_response(
    speech_result: str = Form(None),
    CallSid: str = Form(None)  # unique ID for each call from Twilio
):
    # Initialize session for new caller
    if CallSid not in user_sessions:
        user_sessions[CallSid] = {"step": "ask_language", "language": "English"}

    session = user_sessions[CallSid]

    # Step 1: Ask for language
    if session["step"] == "ask_language":
        if not speech_result:
            twiml = """
            <Response>
                <Say voice="alice">Welcome! Please say your preferred language: English, Spanish, or French.</Say>
                <Gather input="speech" action="/voice" method="POST" />
            </Response>
            """
            return Response(content=twiml, media_type="application/xml")
        else:
            chosen = speech_result.lower()
            if "spanish" in chosen or "español" in chosen:
                session["language"] = "es-ES"
                session["step"] = "ask_name"
                msg = "Perfecto! Continuaremos en Español. ¿Cuál es tu nombre?"
            elif "french" in chosen or "français" in chosen:
                session["language"] = "fr-FR"
                session["step"] = "ask_name"
                msg = "Parfait! Nous continuerons en Français. Quel est votre nom?"
            else:
                session["language"] = "en-US"
                session["step"] = "ask_name"
                msg = "Great! We will continue in English. What is your name?"

            twiml = f"""
            <Response>
                <Say voice="alice" language="{session['language']}">{msg}</Say>
                <Gather input="speech" action="/voice" method="POST" />
            </Response>
            """
            return Response(content=twiml, media_type="application/xml")

    # Step 2: Capture name and respond in chosen language
    elif session["step"] == "ask_name":
        if not speech_result:
            msg = {
                "en-US": "Sorry, I didn’t catch that. Please say your name.",
                "es-ES": "Lo siento, no entendí. Por favor dime tu nombre.",
                "fr-FR": "Désolé, je n’ai pas compris. Veuillez dire votre nom."
            }[session["language"]]
            twiml = f"""
            <Response>
                <Say voice="alice" language="{session['language']}">{msg}</Say>
                <Gather input="speech" action="/voice" method="POST" />
            </Response>
            """
            return Response(content=twiml, media_type="application/xml")
        else:
            name = speech_result
            msg = {
                "en-US": f"Nice to meet you, {name}. I’ll help you with your booking.",
                "es-ES": f"Encantado de conocerte, {name}. Te ayudaré con tu reserva.",
                "fr-FR": f"Ravi de vous rencontrer, {name}. Je vais vous aider avec votre réservation."
            }[session["language"]]

            twiml = f"""
            <Response>
                <Say voice="alice" language="{session['language']}">{msg}</Say>
            </Response>
            """
            return Response(content=twiml, media_type="application/xml")
