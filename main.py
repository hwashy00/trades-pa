import os
from flask import Flask, request
import anthropic
from supabase import create_client

app = Flask(__name__)
client = anthropic.Anthropic()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

conversation_history = {}

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

    # Extract and save log if present
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

    # Remove LOG line from reply before sending
    clean_reply = reply.split("LOG:")[0].strip()

    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()
    resp.message(clean_reply)
    return str(resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
