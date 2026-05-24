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

def send_morning_summary():
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_number = os.environ.get("TWILIO_NUMBER")
    your_whatsapp = os.environ.get("YOUR_WHATSAPP")

    twilio_client = TwilioClient(account_sid, auth_token)

    result = supabase.table("enquiries").select("*").eq("status", "new").execute()
    enquiries = result.data

    if enquiries:
        summary = f"Good morning Harry 👋\n\n📋 Outstanding enquiries: {len(enquiries)}\n\n"
        for e in enquiries[:5]:
            summary += f"• {e.get('client_name', 'Unknown')} - {e.get('job_type', 'Unknown')} - {e.get('location', 'Unknown')}\n"
        summary += "\nReply with any job details to log them."
    else:
        summary = "Good morning Harry 👋\n\nNo outstanding enquiries. Have a great day!"

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

    if sender not in conversation_history:
        conversation_history[sender] = []

    conversation_history[sender].append({
        "role": "user",
        "content": incoming_msg
    })

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system="""You are a PA for a small trades business. You help with:
- Logging job enquiries and extracting key details (client name, address, job type, urgency)
- Generating quotes and invoices
- Reminders and scheduling
- Tracking outstanding payments

Always be concise - this is WhatsApp. Extract and confirm key info clearly.
If someone describes a job enquiry, pull out: name, address, job type, urgency, contact number.

After extracting details, end your reply with this exact format on a new line:
LOG:name=<client name>|job=<job type>|location=<location>|status=new""",
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

    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()
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
