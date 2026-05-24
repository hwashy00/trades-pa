import os
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
    ("business_name", "Welcome to TradesPA! 👷\n\nLet's get you set up — only takes 2 minutes.\n\nWhat's your *business name*?"),
    ("owner_name", "What's your *name*?"),
    ("phone", "What's your *phone number*?"),
    ("trade", "What's your *trade*? (e.g. Carpenter, Plumber, Plasterer)"),
"day_rate": float(data.get("day_rate", "0").replace(",", "").strip()),
"half_day_rate": float(data.get("half_day_rate", "0").replace(",", "").strip()),
"hourly_rate": float(data.get("hourly_rate", "0").replace(",", "").strip()),
"materials_markup": float(data.get("materials_markup", "20").replace("%", "").strip()),
    ("payment_terms", "What are your *payment terms* in days? (e.g. 30)"),
    ("vat_registered", "Are you *VAT registered*? (yes/no)"),
]

def get_user_profile(sender):
    try:
        result = supabase.table("profiles").select("*").eq("sender", sender).execute()
        if result.data:
            return result.data[0]
    except:
        pass
    return None

def get_onboarding_state(sender):
    try:
        result = supabase.table("onboarding").select("*").eq("sender", sender).execute()
        if result.data:
            return result.data[0]
    except:
        pass
    return None

def send_morning_summary():
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_number = os.environ.get("TWILIO_NUMBER")
    your_whatsapp = os.environ.get("YOUR_WHATSAPP")

    twilio_client = TwilioClient(account_sid, auth_token)

    result = supabase.table("enquiries").select("*").eq("status", "new").execute()
    enquiries = result.data

    if enquiries:
        summary = f"Good morning 👋\n\n📋 Outstanding enquiries: {len(enquiries)}\n\n"
        for e in enquiries[:5]:
            summary += f"• {e.get('client_name', 'Unknown')} - {e.get('job_type', 'Unknown')} - {e.get('location', 'Unknown')}\n"
        summary += "\nReply with any job details to log them."
    else:
        summary = "Good morning 👋\n\nNo outstanding enquiries. Have a great day!"

    twilio_client.messages.create(
        from_=f"whatsapp:{twilio_number}",
        to=f"whatsapp:{your_whatsapp}",
        body=summary
    )

scheduler = BackgroundScheduler()
scheduler.add_job(send_morning_summary, 'cron', hour=8, minute=0)
scheduler.start()

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")

    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()

    # Check if user has a profile
    profile = get_user_profile(sender)

    if not profile:
        # Check onboarding state
        onboarding = get_onboarding_state(sender)

        if not onboarding:
            # Start onboarding
            question_key, question_text = ONBOARDING_QUESTIONS[0]
            supabase.table("onboarding").insert({
                "sender": sender,
                "step": 0,
                "data": {}
            }).execute()
            resp.message(question_text)
            return str(resp)

        else:
            step = onboarding["step"]
            data = onboarding["data"] or {}

            # Save answer to current step
            question_key, _ = ONBOARDING_QUESTIONS[step]
            data[question_key] = incoming_msg

            # Check if VAT registered to maybe ask for VAT number
            if step + 1 < len(ONBOARDING_QUESTIONS):
                # Move to next question
                next_step = step + 1
                _, next_question = ONBOARDING_QUESTIONS[next_step]

                supabase.table("onboarding").update({
                    "step": next_step,
                    "data": data
                }).eq("sender", sender).execute()

                # If they said yes to VAT, ask for VAT number
                if question_key == "vat_registered" and incoming_msg.lower() in ["yes", "y"]:
                    resp.message("What's your *VAT number*?")
                    supabase.table("onboarding").update({
                        "step": next_step,
                        "data": {**data, "awaiting_vat_number": True}
                    }).eq("sender", sender).execute()
                else:
                    resp.message(next_question)

            else:
                # Onboarding complete — save profile
                supabase.table("profiles").insert({
                    "sender": sender,
                    "business_name": data.get("business_name", ""),
                    "owner_name": data.get("owner_name", ""),
                    "phone": data.get("phone", ""),
                    "trade": data.get("trade", ""),
                    "day_rate": float(data.get("day_rate", 0)),
                    "half_day_rate": float(data.get("half_day_rate", 0)),
                    "hourly_rate": float(data.get("hourly_rate", 0)),
                    "materials_markup": float(data.get("materials_markup", 20)),
                    "payment_terms": int(data.get("payment_terms", 30)),
                    "vat_registered": data.get("vat_registered", "no").lower() in ["yes", "y"],
                    "vat_number": data.get("vat_number", "")
                }).execute()

                # Clean up onboarding
                supabase.table("onboarding").delete().eq("sender", sender).execute()

                resp.message(f"✅ All set {data.get('owner_name', '')}! You're ready to go.\n\nTry saying:\n• 'Quote for John Smith, kitchen fitting, 3 days labour'\n• 'Log a call from Sarah Jones, wants bathroom tiled'\n• 'What jobs are outstanding?'")

            return str(resp)

    # User has profile — normal PA mode
    if sender not in conversation_history:
        conversation_history[sender] = []

    conversation_history[sender].append({
        "role": "user",
        "content": incoming_msg
    })

    # Build system prompt with their rates
    system_prompt = f"""You are a PA for a trades business. The user's details are:
- Business: {profile.get('business_name')}
- Name: {profile.get('owner_name')}
- Trade: {profile.get('trade')}
- Day rate: £{profile.get('day_rate')}
- Half day rate: £{profile.get('half_day_rate')}
- Hourly rate: £{profile.get('hourly_rate')}
- Materials markup: {profile.get('materials_markup')}%
- Payment terms: {profile.get('payment_terms')} days
- VAT registered: {profile.get('vat_registered')}

You help with:
1. Logging job enquiries - extract client name, address, job type, urgency
2. Generating quotes - use their exact rates above
3. Tracking outstanding jobs and payments
4. General scheduling and reminders

Always be concise - this is WhatsApp.

If generating a QUOTE, format it clearly with:
- Client name and job description
- Labour breakdown (days x day rate)
- Materials estimate with markup
- Total
- Payment terms

If logging an enquiry, end your reply with:
LOG:name=<client name>|job=<job type>|location=<location>|status=new

If generating a quote, end your reply with:
LOG:name=<client name>|job=<job type>|location=<location>|status=quoted"""

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
            print(f"Logging error: {e}")

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
            "summary": f"Missed call from {caller}",
            "client_name": "",
            "job_type": "missed call",
            "location": "",
            "status": "missed call"
        }).execute()
    except Exception as e:
        print(f"Logging error: {e}")

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_client = TwilioClient(account_sid, auth_token)

    try:
        twilio_client.messages.create(
            body="Hi, sorry I missed your call! I'm currently on site. What's the job? I'll get back to you as soon as I can.",
            from_=called,
            to=caller
        )
    except Exception as e:
        print(f"SMS error: {e}")

    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()
    resp.say("Sorry I missed your call, I have sent you a text message and will be in touch shortly.")
    return str(resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
