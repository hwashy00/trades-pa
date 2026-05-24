from flask import Flask, request
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic()

conversation_history = {}

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")
    
    # Keep conversation history per user
    if sender not in conversation_history:
        conversation_history[sender] = []
    
    conversation_history[sender].append({
        "role": "user",
        "content": incoming_msg
    })
    
    response = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=1000,
        system="""You are a PA for a small trades business. You help with:
- Logging job enquiries and extracting key details (client name, address, job type, urgency)
- Generating quotes and invoices
- Reminders and scheduling
- Tracking outstanding payments

Always be concise - this is WhatsApp. Extract and confirm key info clearly.
If someone describes a job enquiry, pull out: name, address, job type, urgency, contact number.""",
        messages=conversation_history[sender]
    )
    
    reply = response.content[0].text
    
    conversation_history[sender].append({
        "role": "assistant", 
        "content": reply
    })
    
    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
