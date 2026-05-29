import os
import random
import traceback
from flask import Flask, request, jsonify
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
    ("business_name", "Welcome to VanOffice! Lets get you set up - only takes 1 minute.\n\nWhat is your business name?"),
    ("owner_name", "What is your name?"),
    ("phone", "What is your mobile number? Include country code e.g. +447504544469"),
    ("trade", "What is your trade? e.g. Carpenter, Plumber, Plasterer"),
    ("pin", "Finally, set a 4-digit PIN for your dashboard login. Choose any 4 numbers."),
]


def clean_number(value, default="0"):
    cleaned = str(value).replace("£", "").replace("%", "").replace(",", "").replace(" ", "").strip()
    if not cleaned:
        return default
    return cleaned


def format_phone(phone):
    phone = phone.strip().replace(" ", "")
    if phone.startswith("0"):
        phone = "+44" + phone[1:]
    return phone


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

        bookings_today = supabase.table("bookings").select("*").eq("date", str(__import__("datetime").date.today())).execute().data

        if enquiries or bookings_today:
            summary = "Good morning!\n\n"
            if bookings_today:
                summary += "Jobs today:\n"
                for b in bookings_today:
                    summary += "- " + b.get("client_name", "Unknown") + " " + b.get("time", "") + " - " + b.get("job_type", "") + " - " + b.get("location", "") + "\n"
                summary += "\n"
            if enquiries:
                summary += "Outstanding enquiries: " + str(len(enquiries)) + "\n"
                for e in enquiries[:3]:
                    summary += "- " + e.get("client_name", "Unknown") + " - " + e.get("job_type", "Unknown") + "\n"
        else:
            summary = "Good morning!\n\nNo jobs today and no outstanding enquiries. Have a great day!"

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


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")

    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()

    profile = get_user_profile(sender)

    if not profile:
        onboarding = get_onboarding_state(sender)

        if not onboarding:
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

            question_key, _ = ONBOARDING_QUESTIONS[step]

            if question_key == "pin":
                pin = incoming_msg.strip()
                if not pin.isdigit() or len(pin) != 4:
                    resp.message("Please enter exactly 4 digits for your PIN. e.g. 1234")
                    return str(resp)

            data[question_key] = incoming_msg

            if step + 1 < len(ONBOARDING_QUESTIONS):
                next_step = step + 1
                _, next_question = ONBOARDING_QUESTIONS[next_step]

                supabase.table("onboarding").update({
                    "step": next_step,
                    "data": data
                }).eq("sender", sender).execute()

                resp.message(next_question)

            else:
                try:
                    phone = format_phone(data.get("phone", ""))
                    supabase.table("profiles").insert({
                        "sender": sender,
                        "business_name": data.get("business_name", ""),
                        "owner_name": data.get("owner_name", ""),
                        "phone": phone,
                        "trade": data.get("trade", ""),
                        "day_rate": "0",
                        "half_day_rate": "0",
                        "hourly_rate": "0",
                        "materials_markup": "20",
                        "payment_terms": "30",
                        "vat_registered": "no",
                        "vat_number": "",
                        "twilio_number": "",
                        "pin": data.get("pin", ""),
                        "reset_code": ""
                    }).execute()

                    supabase.table("onboarding").delete().eq("sender", sender).execute()

                    resp.message("All set " + data.get("owner_name", "") + "! You are ready to go.\n\nVisit your dashboard at:\nhttps://trades-pa-trades-pa.up.railway.app/dashboard\n\nLog in with your mobile number and PIN.\n\nTry saying:\n- Quote for John Smith, kitchen fitting, 3 days labour\n- Book in Mrs Jones, bathroom refit, Tuesday 3rd June 9am\n- What jobs are outstanding?")

                except Exception as e:
                    print("Profile save error: " + str(e))
                    print(traceback.format_exc())
                    resp.message("Sorry something went wrong. Please try again.")
                    supabase.table("onboarding").delete().eq("sender", sender).execute()

            return str(resp)

    if sender not in conversation_history:
        conversation_history[sender] = []

    conversation_history[sender].append({
        "role": "user",
        "content": incoming_msg
    })

    import datetime
    today = datetime.date.today().strftime("%A %d %B %Y")

    system_prompt = "You are a PA for a trades business. Today is " + today + ".\n"
    system_prompt += "The user details are:\n"
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
    system_prompt += "3. Booking in jobs - extract client, job type, location, date, time, duration\n"
    system_prompt += "4. Tracking outstanding jobs and payments\n"
    system_prompt += "5. General scheduling and reminders\n\n"
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
    system_prompt += "LOG:name=<client name>|job=<job type>|location=<location>|status=quoted\n\n"
    system_prompt += "If BOOKING a job, confirm the details clearly then end your reply with:\n"
    system_prompt += "BOOK:name=<client name>|job=<job type>|location=<location>|date=<YYYY-MM-DD>|time=<HH:MM>|days=<number of days>\n"
    system_prompt += "For the date, convert relative dates like 'Tuesday 3rd June' to YYYY-MM-DD format using today's date as reference."

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

    if "BOOK:" in reply:
        try:
            book_line = reply.split("BOOK:")[1].strip().split("\n")[0]
            parts = dict(p.split("=") for p in book_line.split("|"))
            supabase.table("bookings").insert({
                "sender": sender,
                "client_name": parts.get("name", ""),
                "job_type": parts.get("job", ""),
                "location": parts.get("location", ""),
                "date": parts.get("date", ""),
                "time": parts.get("time", ""),
                "duration_days": parts.get("days", "1"),
                "notes": "",
                "status": "booked"
            }).execute()
        except Exception as e:
            print("Booking error: " + str(e))

    clean_reply = reply.split("LOG:")[0].split("BOOK:")[0].strip()
    resp.message(clean_reply)
    return str(resp)


@app.route("/call", methods=["POST"])
def incoming_call():
    caller = request.form.get("From", "")
    called = request.form.get("To", "")

    try:
        result = supabase.table("profiles").select("*").eq("twilio_number", called).execute()
        profile = result.data[0] if result.data else None
    except Exception as e:
        print("Profile lookup error: " + str(e))
        profile = None

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
        resp.say("Please hold while we connect your call. This call may be recorded for quality purposes.")
        dial = Dial(action="/call-status", method="POST")
        dial.number(profile.get("phone"))
        resp.append(dial)
    else:
        resp.say("Sorry, we are unable to connect your call right now. Please try again later.")

    return str(resp)


@app.route("/call-status", methods=["POST"])
def call_status():
    dial_status = request.form.get("DialCallStatus", "")
    caller = request.form.get("From", "")
    called = request.form.get("To", "")

    if dial_status != "completed":
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


@app.route("/dashboard")
def dashboard():
    with open("dashboard.html", "r") as f:
        return f.read()


@app.route("/api/stats")
def api_stats():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()

        if not phone or not pin:
            return jsonify({"error": "Phone and PIN required"}), 401

        result = supabase.table("profiles").select("*").eq("phone", phone).execute()

        if not result.data:
            sender = "whatsapp:" + phone
            result = supabase.table("profiles").select("*").eq("sender", sender).execute()

        if not result.data:
            return jsonify({"error": "Profile not found"}), 404

        profile = result.data[0]

        if str(profile.get("pin", "")) != str(pin):
            return jsonify({"error": "Invalid PIN"}), 401

        new_enq = supabase.table("enquiries").select("*").eq("status", "new").execute().data
        missed = supabase.table("enquiries").select("*").eq("status", "missed call").execute().data
        quoted = supabase.table("enquiries").select("*").eq("status", "quoted").execute().data
        recent = supabase.table("enquiries").select("*").order("created_at", desc=True).limit(20).execute().data
        bookings = supabase.table("bookings").select("*").eq("sender", profile.get("sender", "")).order("date").execute().data

        return jsonify({
            "enquiries": len(new_enq),
            "unpaid": len(quoted),
            "missed": len(missed),
            "recent": recent,
            "bookings": bookings,
            "profile": profile
        })

    except Exception as e:
        print("API stats error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset-pin")
def reset_pin():
    try:
        phone = format_phone(request.args.get("phone", "").strip())

        if not phone:
            return jsonify({"error": "Phone required"}), 400

        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data:
            return jsonify({"error": "No account found with that number"}), 404

        code = str(random.randint(100000, 999999))
        supabase.table("profiles").update({"reset_code": code}).eq("phone", phone).execute()

        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        twilio_number = os.environ.get("TWILIO_NUMBER")
        twilio_client = TwilioClient(account_sid, auth_token)
        twilio_client.messages.create(
            body="Your VanOffice reset code is: " + code + ". Valid for 10 minutes.",
            from_=twilio_number,
            to=phone
        )

        return jsonify({"success": True})

    except Exception as e:
        print("Reset pin error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/confirm-reset")
def confirm_reset():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        code = request.args.get("code", "").strip()
        newpin = request.args.get("newpin", "").strip()

        if not phone or not code or not newpin:
            return jsonify({"error": "All fields required"}), 400

        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data:
            return jsonify({"error": "Account not found"}), 404

        profile = result.data[0]
        if str(profile.get("reset_code", "")) != str(code):
            return jsonify({"error": "Invalid reset code"}), 401

        supabase.table("profiles").update({"pin": newpin, "reset_code": ""}).eq("phone", phone).execute()
        return jsonify({"success": True})

    except Exception as e:
        print("Confirm reset error: " + str(e))
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
