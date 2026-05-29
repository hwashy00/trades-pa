import os
import traceback
from flask import Flask, request
import anthropic
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client as TwilioClient

app = Flask(__name__)
client = anthropic.Anthropic()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

conversation_history = {}

ONBOARDING_QUESTIONS = [
    ("business_name", "Welcome to TradesPA! Lets get you set up - only takes 2 minutes. What is your business name?"),
    ("owner_name", "What is your name?"),
    ("phone", "What is your phone number?"),
    ("trade", "What is your trade? e.g. Carpenter, Plumber, Plasterer"),
]


def clean_number(value, default="0"):
    cleaned = str(value).replace("£", "").replace("%", "").replace(",", "").replace(" ", "").strip()
    if not cleaned:
        return default
    return cleaned


def get_user_profile(sender):
    try:
        result = supabase.table("profiles").select("*").eq("sender", sender).execute()
        if result.data:
            return result.data[0]
    except Exception as e:
        print("Get profile error: " + str(e))
    return None


def get_onboarding_state(sender):
    try:
        result = supabase.table("onboarding").select("*").eq("sender", sender).execute()
        if result.data:
            return result.data[0]
    except Exception as e:
        print("Get onboarding error: " + str(e))
    return None


def send_morning_summary():
    try:
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        twilio_number = os.environ.get("TWILIO_NUMBER")
        your_whatsapp = os.environ.get("YOUR_WHATSAPP")

        twilio_client = TwilioClient(account_sid, auth_token)

        result = supabase.table("enquiries").select("*").eq("status", "new").execute()
        enquiries = result.data

        if enquiries:
            summary = "Good morning!\n\nOutstanding enquiries: " + str(len(enquiries)) + "\n\n"
            for e in enquiries[:5]:
                summary += "- " + e.get("client_name", "Unknown") + " - " + e.get("job_type", "Unknown") + " - " + e.get("location", "Unknown") + "\n"
            summary += "\nReply with any job details to log them."
        else:
            summary = "Good morning!\n\nNo outstanding enquiries. Have a great day!"

        twilio_client.messages.create(
            from_="whatsapp:" + twilio_number,
            to="whatsapp:" + your_whatsapp,
            body=summary
        )
    except Exception as e:
        print("Morning summary error: " + str(e))


scheduler = BackgroundScheduler()
scheduler.add_job(send_morning_summary, "cron", hour=8, minute=0)
scheduler.start()


@app.route("/call", methods=["POST"])
def incoming_call():
    caller = request.form.get("From", "")
    called = request.form.get("To", "")

    # Look up which tradesperson owns this Twilio number
    try:
        result = supabase.table("profiles").select("*").eq("twilio_number", called).execute()
        profile = result.data[0] if result.data else None
    except Exception as e:
        print("Profile lookup error: " + str(e))
        profile = None

    # Log the call
    try:
        supabase.table("enquiries").insert({
            "sender": caller,
            "message": "INCOMING CALL",
            "summary": "Call from " + caller,
            "client_name": "",
            "job_type": "incoming call",
            "location": "",
            "status": "incoming call"
        }).execute()
    except Exception as e:
        print("Logging error: " + str(e))

    from twilio.twiml.voice_response import VoiceResponse, Dial
    resp = VoiceResponse()

    if profile and profile.get("phone"):
        # Forward to tradesperson's mobile
        resp.say("Please hold while we connect your call.")
        dial = Dial(action="/call-status", method="POST")
        dial.number(profile.get("phone"))
        resp.append(dial)
    else:
        # No profile found - leave voicemail
        resp.say("Sorry, we are unable to connect your call right now. Please try again later.")

    return str(resp)


@app.route("/call-status", methods=["POST"])
def call_status():
    dial_status = request.form.get("DialCallStatus", "")
    caller = request.form.get("From", "")
    called = request.form.get("To", "")

    if dial_status != "completed":
        # Wasn't answered - send auto text to caller
        try:
            result = supabase.table("profiles").select("*").eq("twilio_number", called).execute()
            profile = result.data[0] if result.data else None
        except:
            profile = None

        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        twilio_client = TwilioClient(account_sid, auth_token)

        try:
            twilio_client.messages.create(
                body="Hi, sorry I missed your call! I am currently on site. What is the job? I will get back to you as soon as I can.",
                from_=called,
                to=caller
            )
            # Log as missed call
            supabase.table("enquiries").update({
                "status": "missed call",
                "job_type": "missed call",
                "summary": "Missed call from " + caller
            }).eq("sender", caller).eq("status", "incoming call").execute()
        except Exception as e:
            print("SMS error: " + str(e))

    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()
    return str(resp)

    if sender not in conversation_history:
        conversation_history[sender] = []

    conversation_history[sender].append({
        "role": "user",
        "content": incoming_msg
    })

    system_prompt = "You are a PA for a trades business. The user details are:\n"
    system_prompt += "Business: " + str(profile.get("business_name")) + "\n"
    system_prompt += "Name: " + str(profile.get("owner_name")) + "\n"
    system_prompt += "Trade: " + str(profile.get("trade")) + "\n"
    system_prompt += "Day rate: " + str(profile.get("day_rate")) + "\n"
    system_prompt += "Half day rate: " + str(profile.get("half_day_rate")) + "\n"
    system_prompt += "Hourly rate: " + str(profile.get("hourly_rate")) + "\n"
    system_prompt += "Materials markup: " + str(profile.get("materials_markup")) + "%\n"
    system_prompt += "Payment terms: " + str(profile.get("payment_terms")) + " days\n"
    system_prompt += "VAT registered: " + str(profile.get("vat_registered")) + "\n\n"
    system_prompt += "You help with:\n"
    system_prompt += "1. Logging job enquiries - extract client name, address, job type, urgency\n"
    system_prompt += "2. Generating quotes - use their exact rates above\n"
    system_prompt += "3. Tracking outstanding jobs and payments\n"
    system_prompt += "4. General scheduling and reminders\n\n"
    system_prompt += "Always be concise - this is WhatsApp.\n\n"
    system_prompt += "If generating a QUOTE format it clearly with:\n"
    system_prompt += "- Client name and job description\n"
    system_prompt += "- Labour breakdown (days x day rate)\n"
    system_prompt += "- Materials estimate with markup applied\n"
    system_prompt += "- Total\n"
    system_prompt += "- Payment terms\n\n"
    system_prompt += "If logging an enquiry end your reply with:\n"
    system_prompt += "LOG:name=<client name>|job=<job type>|location=<location>|status=new\n\n"
    system_prompt += "If generating a quote end your reply with:\n"
    system_prompt += "LOG:name=<client name>|job=<job type>|location=<location>|status=quoted"

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        system=system_prompt,
        messages=conversation_history[sender]
    )

    reply = response.content[0].text

    conversation_history[sender].append({
        "role": "assistant",
        "content": reply
    })

    if "LOG:" in reply:
        try:
            log_line = reply.split("LOG:")[1].strip().split("\n")[0]
            parts = dict(p.split("=") for p in log_line.split("|"))
            supabase.table("enquiries").insert({
                "sender": sender,
                "message": incoming_msg,
                "summary": reply.split("LOG:")[0].strip(),
                "client_name": parts.get("name", ""),
                "job_type": parts.get("job", ""),
                "location": parts.get("location", ""),
                "status": parts.get("status", "new")
            }).execute()
        except Exception as e:
            print("Logging error: " + str(e))

    clean_reply = reply.split("LOG:")[0].strip()
    resp.message(clean_reply)
    return str(resp)


@app.route("/call", methods=["POST"])
def incoming_call():
    caller = request.form.get("From", "")
    called = request.form.get("To", "")

    try:
        supabase.table("enquiries").insert({
            "sender": caller,
            "message": "MISSED CALL",
            "summary": "Missed call from " + caller,
            "client_name": "",
            "job_type": "missed call",
            "location": "",
            "status": "missed call"
        }).execute()
    except Exception as e:
        print("Logging error: " + str(e))

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_client = TwilioClient(account_sid, auth_token)

    try:
        twilio_client.messages.create(
            body="Hi, sorry I missed your call! I am currently on site. What is the job? I will get back to you as soon as I can.",
            from_=called,
            to=caller
        )
    except Exception as e:
        print("SMS error: " + str(e))

    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()
    resp.say("Sorry I missed your call. I have sent you a text message and will be in touch shortly.")
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
