import os
import random
import traceback
import datetime
import io
import json
from flask import Flask, request, jsonify, send_file, redirect, session, Response
import anthropic
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client as TwilioClient
from vanoffice_intelligence import send_intelligent_briefing

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "vanoffice-secret-key-change-me")
client = anthropic.Anthropic()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = "https://trades-pa-trades-pa.up.railway.app/auth/gmail/callback"

MICROSOFT_CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID")
MICROSOFT_CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET")
MICROSOFT_REDIRECT_URI = "https://trades-pa-trades-pa.up.railway.app/auth/outlook/callback"

conversation_history = {}

# ─────────────────────────────────────────────────────────────
# CHANNEL-AWARE OUTBOUND
# One place that decides HOW to send a message to a client:
#   - Reply on the channel the client last used (whatsapp or sms).
#   - If that channel is WhatsApp but their last inbound was >24h ago,
#     WhatsApp's free-form window has closed -> fall back to SMS.
#   - Always log the outbound message with the channel actually used.
# Every outbound path (chatbox reply, AI quote, invoice chase, etc.)
# should call send_to_client() instead of calling Twilio directly.
# ─────────────────────────────────────────────────────────────
WHATSAPP_WINDOW_HOURS = 24

def _strip_wa(n):
    return str(n or "").replace("whatsapp:", "")

def _last_inbound_channel(twilio_number, client_number):
    """Return (channel, last_inbound_dt) for this client, or ('whatsapp', None)."""
    try:
        rows = (supabase.table("client_chats")
                .select("channel,created_at,direction")
                .eq("twilio_number", _strip_wa(twilio_number))
                .eq("client_number", _strip_wa(client_number))
                .eq("direction", "inbound")
                .order("created_at", desc=True).limit(1).execute().data or [])
        if not rows:
            # No history: also try matching with whatsapp: prefixed numbers
            return ("whatsapp", None)
        ch = rows[0].get("channel") or "whatsapp"
        ts = rows[0].get("created_at")
        dt = None
        if ts:
            try:
                dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                dt = None
        return (ch, dt)
    except Exception as e:
        print("_last_inbound_channel error:", e)
        return ("whatsapp", None)

def _within_whatsapp_window(last_inbound_dt):
    if not last_inbound_dt:
        return False
    try:
        now = datetime.datetime.now(last_inbound_dt.tzinfo) if last_inbound_dt.tzinfo else datetime.datetime.now()
        return (now - last_inbound_dt) <= datetime.timedelta(hours=WHATSAPP_WINDOW_HOURS)
    except Exception:
        return False

def send_to_client(profile, client_number, message, prefer=None):
    """
    Send a message to a client on the right channel, with SMS fallback.
    prefer: optional 'whatsapp' or 'sms' to force a starting preference.
    Returns dict: {ok, channel, error}.
    """
    try:
        twilio_number = profile.get("twilio_number") or os.environ.get("TWILIO_NUMBER", "")
        sender = profile.get("sender", "")
        if not twilio_number:
            return {"ok": False, "channel": None, "error": "No business number set (profiles.twilio_number)."}
        if not client_number or not message:
            return {"ok": False, "channel": None, "error": "Missing client number or message."}

        last_channel, last_dt = _last_inbound_channel(twilio_number, client_number)
        chosen = prefer or last_channel or "whatsapp"

        # If we'd choose WhatsApp but the 24h window has closed, fall back to SMS.
        if chosen == "whatsapp" and not _within_whatsapp_window(last_dt):
            chosen = "sms"

        tc = TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
        clean_from = _strip_wa(twilio_number)
        clean_to = _strip_wa(client_number)

        def _send(channel):
            if channel == "whatsapp":
                return tc.messages.create(body=message, from_="whatsapp:" + clean_from, to="whatsapp:" + clean_to)
            return tc.messages.create(body=message, from_=clean_from, to=clean_to)

        used = chosen
        try:
            _send(chosen)
        except Exception as primary_err:
            # If WhatsApp send failed for any reason, try SMS as a safety net.
            if chosen == "whatsapp":
                print("send_to_client whatsapp failed, falling back to sms:", repr(primary_err))
                try:
                    _send("sms"); used = "sms"
                except Exception as sms_err:
                    print("send_to_client sms fallback failed:", repr(sms_err))
                    return {"ok": False, "channel": None, "error": str(sms_err)}
            else:
                print("send_to_client sms failed:", repr(primary_err))
                return {"ok": False, "channel": None, "error": str(primary_err)}

        # Log the outbound with the channel actually used.
        try:
            supabase.table("client_chats").insert({
                "twilio_number": clean_from, "client_number": clean_to,
                "message": message, "direction": "outbound",
                "sender_profile": sender, "channel": used
            }).execute()
        except Exception as le:
            print("send_to_client log error:", le)

        return {"ok": True, "channel": used, "error": None}
    except Exception as e:
        print("send_to_client error:", e)
        return {"ok": False, "channel": None, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# USAGE METERING (silent — counts credits, does NOT block anything yet)
# Credit weights mirror real cost: text=1, sms=1, quote=1, voice=4.
# Call track_usage(sender, action) wherever a metered action happens.
# Wrapped so it can NEVER break a user action if metering fails.
# ─────────────────────────────────────────────────────────────
def notify_owner(profile, message):
    """
    Send a notification to the tradesperson's own mobile.
    SMS-first so it works today without WhatsApp setup; never raises.
    Also fires a web push to any installed PWA devices (best-effort).
    """
    try:
        send_web_push(profile.get("sender", ""), message)
    except Exception as e:
        print("notify_owner push error:", e)
    try:
        owner_mobile = profile.get("phone", "")
        biz_from = profile.get("twilio_number") or os.environ.get("TWILIO_NUMBER", "")
        if not owner_mobile or not biz_from:
            print("notify_owner: missing owner mobile or business number")
            return False
        tc = TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
        clean_from = str(biz_from).replace("whatsapp:", "")
        clean_to = str(owner_mobile).replace("whatsapp:", "")
        # Plain SMS — reliable, no Meta/WhatsApp setup needed.
        tc.messages.create(body=message, from_=clean_from, to=clean_to)
        return True
    except Exception as e:
        print("notify_owner error:", e)
        return False


CREDIT_WEIGHTS = {"text": 1, "sms": 1, "quote": 1, "voice": 4}

def track_usage(sender, action):
    """Increment this tradesperson's credit usage for the current month. Never raises."""
    try:
        if not sender:
            return
        credits = CREDIT_WEIGHTS.get(action, 1)
        period = datetime.datetime.now().strftime("%Y-%m")
        # fetch-or-create this period's row
        existing = (supabase.table("usage_meter").select("*")
                    .eq("sender", sender).eq("period", period).limit(1).execute().data or [])
        col = {"text": "text_actions", "voice": "voice_actions",
               "sms": "sms_actions", "quote": "quote_actions"}.get(action, "text_actions")
        if existing:
            row = existing[0]
            supabase.table("usage_meter").update({
                "credits_used": float(row.get("credits_used", 0) or 0) + credits,
                col: int(row.get(col, 0) or 0) + 1,
                "updated_at": datetime.datetime.now().isoformat()
            }).eq("id", row["id"]).execute()
        else:
            supabase.table("usage_meter").insert({
                "sender": sender, "period": period, "credits_used": credits,
                col: 1
            }).execute()
        # fine-grained event log (optional table)
        try:
            supabase.table("usage_events").insert({
                "sender": sender, "action": action, "credits": credits
            }).execute()
        except Exception:
            pass
    except Exception as e:
        print("track_usage error:", e)


def get_usage_summary(sender):
    """Return this month's usage for display. Never raises."""
    try:
        period = datetime.datetime.now().strftime("%Y-%m")
        rows = (supabase.table("usage_meter").select("*")
                .eq("sender", sender).eq("period", period).limit(1).execute().data or [])
        if not rows:
            return {"period": period, "credits_used": 0, "text_actions": 0,
                    "voice_actions": 0, "sms_actions": 0, "quote_actions": 0}
        r = rows[0]
        return {
            "period": period,
            "credits_used": float(r.get("credits_used", 0) or 0),
            "text_actions": int(r.get("text_actions", 0) or 0),
            "voice_actions": int(r.get("voice_actions", 0) or 0),
            "sms_actions": int(r.get("sms_actions", 0) or 0),
            "quote_actions": int(r.get("quote_actions", 0) or 0),
        }
    except Exception as e:
        print("get_usage_summary error:", e)
        return {"period": "", "credits_used": 0, "text_actions": 0,
                "voice_actions": 0, "sms_actions": 0, "quote_actions": 0}


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


# ── CLIENT CONTACTS: name <-> number directory, per owner ──
# Lets us show client names in the inbox and resolve "message Dave" to a number.
_CONTACT_NONAMES = {"", "unknown", "client", "customer", "there", "mate", "hi", "hello"}


def upsert_contact(owner_sender, client_number, name=None):
    """Remember a client's number, and their name once we learn it."""
    if not owner_sender or not client_number:
        return
    try:
        nm = (name or "").strip()
        if nm.lower() in _CONTACT_NONAMES:
            nm = ""
        existing = (supabase.table("client_contacts").select("id,name")
                    .eq("sender", owner_sender).eq("client_number", client_number)
                    .limit(1).execute().data or [])
        if existing:
            cur = (existing[0].get("name") or "").strip()
            if nm and nm != cur:
                supabase.table("client_contacts").update(
                    {"name": nm, "updated_at": datetime.datetime.utcnow().isoformat()}
                ).eq("id", existing[0]["id"]).execute()
        else:
            supabase.table("client_contacts").insert(
                {"sender": owner_sender, "client_number": client_number, "name": (nm or None)}
            ).execute()
    except Exception as e:
        print("upsert_contact error:", e)


def contact_name_for(owner_sender, client_number):
    """Return the saved name for a client number, or '' if none known."""
    try:
        r = (supabase.table("client_contacts").select("name")
             .eq("sender", owner_sender).eq("client_number", client_number)
             .limit(1).execute().data or [])
        if r and (r[0].get("name") or "").strip():
            return r[0]["name"].strip()
    except Exception as e:
        print("contact_name_for error:", e)
    return ""


def contact_number_for(owner_sender, name):
    """Resolve a client name to their saved number (partial match, owner-scoped)."""
    name = (name or "").strip()
    if not name:
        return ""
    try:
        r = (supabase.table("client_contacts").select("client_number,name")
             .eq("sender", owner_sender).ilike("name", "%" + name + "%")
             .limit(1).execute().data or [])
        if not r and name.split():
            r = (supabase.table("client_contacts").select("client_number,name")
                 .eq("sender", owner_sender).ilike("name", "%" + name.split()[0] + "%")
                 .limit(1).execute().data or [])
        if r:
            return r[0].get("client_number", "")
    except Exception as e:
        print("contact_number_for error:", e)
    return ""


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
        else:
            summary = "Good morning!\n\nNo outstanding enquiries. Have a great day!"
        twilio_client.messages.create(from_="whatsapp:" + twilio_number, to="whatsapp:" + your_whatsapp, body=summary)
    except Exception as e:
        print("Morning summary error: " + str(e))


def scan_emails_for_user(profile):
    try:
        gmail_token = profile.get("gmail_token")
        gmail_refresh = profile.get("gmail_refresh_token")
        if not gmail_token:
            return

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=gmail_token,
            refresh_token=gmail_refresh,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET
        )

        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            supabase.table("profiles").update({
                "gmail_token": creds.token
            }).eq("sender", profile["sender"]).execute()

        service = build("gmail", "v1", credentials=creds)
        results = service.users().messages().list(userId="me", maxResults=10, q="is:unread newer_than:1d").execute()
        messages = results.get("messages", [])

        sender = profile.get("sender", "")

        for msg in messages:
            msg_id = msg["id"]
            existing = supabase.table("emails").select("id").eq("gmail_id", msg_id).execute()
            if existing.data:
                continue

            msg_data = service.users().messages().get(userId="me", id=msg_id, format="metadata", metadataHeaders=["From", "Subject"]).execute()
            headers = {h["name"]: h["value"] for h in msg_data.get("payload", {}).get("headers", [])}
            from_email = headers.get("From", "Unknown")
            subject = headers.get("Subject", "No subject")

            snippet = msg_data.get("snippet", "")

            ai_response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=200,
                system="You categorise emails for a tradesperson. Respond with ONLY valid JSON. Categories: payment, client, supplier, quote, invoice, spam, other. Also determine if important (true/false) and write a one-line summary.",
                messages=[{"role": "user", "content": "From: " + from_email + "\nSubject: " + subject + "\nPreview: " + snippet + "\n\nRespond with JSON only: {\"category\": \"...\", \"is_important\": true/false, \"summary\": \"...\"}"}]
            )

            try:
                ai_text = ai_response.content[0].text.strip()
                ai_text = ai_text.replace("```json", "").replace("```", "").strip()
                parsed = json.loads(ai_text)
            except:
                parsed = {"category": "other", "is_important": False, "summary": subject}

            supabase.table("emails").insert({
                "sender": sender,
                "from_email": from_email,
                "subject": subject,
                "summary": parsed.get("summary", subject),
                "category": parsed.get("category", "other"),
                "is_important": parsed.get("is_important", False),
                "read": False,
                "gmail_id": msg_id
            }).execute()

    except Exception as e:
        print("Email scan error for " + profile.get("sender", "unknown") + ": " + str(e))


def scan_outlook_for_user(profile):
    try:
        outlook_token = profile.get("outlook_token")
        outlook_refresh = profile.get("outlook_refresh_token")
        if not outlook_token:
            return

        import requests as req

        # Refresh token if needed
        headers = {"Authorization": "Bearer " + outlook_token}
        test = req.get("https://graph.microsoft.com/v1.0/me", headers=headers)
        if test.status_code == 401 and outlook_refresh:
            refresh_response = req.post("https://login.microsoftonline.com/common/oauth2/v2.0/token", data={
                "client_id": MICROSOFT_CLIENT_ID,
                "client_secret": MICROSOFT_CLIENT_SECRET,
                "refresh_token": outlook_refresh,
                "grant_type": "refresh_token",
                "scope": "Mail.Read"
            })
            tokens = refresh_response.json()
            if "access_token" in tokens:
                outlook_token = tokens["access_token"]
                supabase.table("profiles").update({"outlook_token": outlook_token}).eq("sender", profile["sender"]).execute()
                headers = {"Authorization": "Bearer " + outlook_token}

        response = req.get(
            "https://graph.microsoft.com/v1.0/me/messages?$filter=isRead eq false&$top=10&$orderby=receivedDateTime desc&$select=from,subject,bodyPreview,id",
            headers=headers
        )

        if response.status_code != 200:
            print("Outlook API error: " + str(response.status_code))
            return

        messages = response.json().get("value", [])
        sender = profile.get("sender", "")

        for msg in messages:
            msg_id = msg.get("id", "")
            existing = supabase.table("emails").select("id").eq("gmail_id", msg_id).execute()
            if existing.data:
                continue

            from_email = msg.get("from", {}).get("emailAddress", {}).get("address", "Unknown")
            from_name = msg.get("from", {}).get("emailAddress", {}).get("name", "")
            subject = msg.get("subject", "No subject")
            snippet = msg.get("bodyPreview", "")

            ai_response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=200,
                system="You categorise emails for a tradesperson. Respond with ONLY valid JSON. Categories: payment, client, supplier, quote, invoice, spam, other. Also determine if important (true/false) and write a one-line summary.",
                messages=[{"role": "user", "content": "From: " + from_name + " <" + from_email + ">\nSubject: " + subject + "\nPreview: " + snippet + "\n\nRespond with JSON only: {\"category\": \"...\", \"is_important\": true/false, \"summary\": \"...\"}"}]
            )

            try:
                ai_text = ai_response.content[0].text.strip()
                ai_text = ai_text.replace("```json", "").replace("```", "").strip()
                parsed = json.loads(ai_text)
            except:
                parsed = {"category": "other", "is_important": False, "summary": subject}

            supabase.table("emails").insert({
                "sender": sender,
                "from_email": from_name + " <" + from_email + ">",
                "subject": subject,
                "summary": parsed.get("summary", subject),
                "category": parsed.get("category", "other"),
                "is_important": parsed.get("is_important", False),
                "read": False,
                "gmail_id": msg_id
            }).execute()

    except Exception as e:
        print("Outlook scan error for " + profile.get("sender", "unknown") + ": " + str(e))


def scan_all_emails():
    try:
        profiles = supabase.table("profiles").select("*").execute()
        if profiles.data:
            for profile in profiles.data:
                if profile.get("gmail_token"):
                    scan_emails_for_user(profile)
                if profile.get("outlook_token"):
                    scan_outlook_for_user(profile)
    except Exception as e:
        print("Scan all emails error: " + str(e))

def run_invoice_chase():
    try:
        from datetime import date
        from twilio.rest import Client as TwilioClient

        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")
        from_number = os.environ.get("TWILIO_NUMBER")
        twilio_client = TwilioClient(account_sid, auth_token)

        today = date.today()
        result = supabase.table("invoices").select("*").in_("status", ["unpaid", "overdue"]).lt("due_date", today.isoformat()).execute()
        invoices = result.data or []

        CHASE_SCHEDULE = [1, 7, 14]

        for inv in invoices:
            inv_id = inv.get("id")
            try:
                due_date     = date.fromisoformat(inv["due_date"])
                days_overdue = (today - due_date).days

                # How many chases already sent?
                chases = supabase.table("invoice_chases").select("id", count="exact").eq("invoice_id", inv_id).execute()
                chase_count = chases.count or 0

                if chase_count >= len(CHASE_SCHEDULE):
                    continue
                if days_overdue < CHASE_SCHEDULE[chase_count]:
                    continue

                # Use stored client_number if available, otherwise look up from client_chats
                phone = inv.get("client_number")
                if not phone:
                    tradesperson = supabase.table("profiles").select("twilio_number").eq("sender", inv["sender"]).limit(1).execute()
                    if not tradesperson.data:
                        print("No profile for sender: " + inv.get("sender", ""))
                        continue
                    twilio_num = tradesperson.data[0]["twilio_number"]
                    chats = supabase.table("client_chats").select("client_number").eq("twilio_number", twilio_num).eq("sender_profile", inv["sender"]).limit(1).execute()
                    if not chats.data:
                        print("No phone found for client: " + inv.get("client_name", ""))
                        continue
                    phone = chats.data[0]["client_number"]
                first_name = inv.get("client_name", "there").split()[0]
                inv_num    = inv.get("invoice_number", "")
                total      = inv.get("total", "")
                sender     = inv.get("sender", "")
                next_chase = chase_count + 1

                # Get tradesperson's business name for the message
                biz_name = sender
                try:
                    prof = supabase.table("profiles").select("business_name,owner_name").eq("sender", sender).limit(1).execute()
                    if prof.data:
                        biz_name = prof.data[0].get("business_name") or prof.data[0].get("owner_name") or sender
                except:
                    pass

                if next_chase == 1:
                    msg = f"Hi {first_name}, just a friendly reminder that invoice {inv_num} for £{total} is now due. Please arrange payment at your earliest convenience. Many thanks, {biz_name}"
                elif next_chase == 2:
                    msg = f"Hi {first_name}, second reminder — invoice {inv_num} for £{total} is now {days_overdue} days overdue. Please settle this as soon as possible. {biz_name}"
                else:
                    msg = f"FINAL NOTICE: Hi {first_name}, invoice {inv_num} for £{total} is {days_overdue} days overdue. Immediate payment is required. Please contact us urgently. {biz_name}"

                _chase_tn = locals().get("twilio_num") or from_number or os.environ.get("TWILIO_NUMBER","")
                _chase_profile = {"sender": sender, "twilio_number": _chase_tn, "business_name": biz_name}
                send_to_client(_chase_profile, phone, msg)

                supabase.table("invoice_chases").insert({
                    "invoice_id":   inv_id,
                    "sent_to":      phone,
                    "message":      msg,
                    "chase_number": next_chase,
                    "sent_at":      today.isoformat()
                }).execute()

                supabase.table("invoices").update({"status": "overdue"}).eq("id", inv_id).execute()
                print("Chase #" + str(next_chase) + " sent for invoice " + str(inv_num))

            except Exception as e:
                print("Chase error for invoice " + str(inv.get("invoice_number")) + ": " + str(e))

    except Exception as e:
        print("run_invoice_chase error: " + str(e))

scheduler = BackgroundScheduler()
def _twilio_factory():
    return TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
scheduler.add_job(lambda: send_intelligent_briefing(supabase, client, _twilio_factory), "cron", hour=7, minute=0)
scheduler.add_job(scan_all_emails, "interval", minutes=15)
scheduler.add_job(run_invoice_chase, "cron", hour=9, minute=0)
scheduler.start()


def build_quote_html(quote, profile, template, is_invoice=False):
    accent = "#c9a84c"
    dark = "#1a1a1a"
    design_style = "gold"
    biz_name = profile.get("business_name", "Your Business")
    trade = profile.get("trade", "")
    phone_num = profile.get("phone", "")
    email = ""
    location = ""
    scope_items = []
    inclusion_items = ["All labour", "All materials", "Site clean-down"]
    note = ""
    commitment = "We take pride in delivering quality workmanship. Your satisfaction is our priority."
    lead_time = "To be arranged at a convenient time."
    payment_terms = "50% deposit, balance on completion."
    show_vat = False
    show_note = False
    intro = "Thank you for the opportunity to provide this quotation. Please find below the scope of works and cost breakdown for your consideration."

    if template:
        try:
            dt = json.loads(template.get("design_template", "{}")) if template.get("design_template") else {}
            design_style = template.get("design_style", "gold")
            accent = dt.get("accent", template.get("brand_colour", accent))
            dark = dt.get("dark", dark)
            biz_name = dt.get("bizName", template.get("business_name", biz_name))
            trade = dt.get("trade", trade)
            phone_num = dt.get("phone", template.get("business_phone", phone_num))
            email = dt.get("email", template.get("business_email", email))
            location = dt.get("location", template.get("business_address", location))
            scope_items = dt.get("scopeItems", scope_items)
            inclusion_items = dt.get("inclusionItems", inclusion_items)
            note = dt.get("note", note)
            commitment = dt.get("commitment", commitment)
            lead_time = dt.get("leadTime", lead_time)
            payment_terms = dt.get("paymentTerms", payment_terms)
            show_vat = dt.get("showVat", show_vat)
            show_note = dt.get("showNote", True) and bool(note)
            intro = dt.get("intro", intro)
        except Exception as e:
            print("Template parse error: " + str(e))

    if is_invoice:
        doc_label = "INVOICE"
        ref_num = quote.get("invoice_number", "INV-001")
        due_line = f'<div style="font-size:11px;margin-bottom:14px"><span style="font-weight:700;font-size:9px;text-transform:uppercase;letter-spacing:0.06em">DUE: </span>{quote.get("due_date","On completion")}</div>'
    else:
        doc_label = "QUOTATION"
        ref_num = quote.get("quote_number", "QU-001")
        due_line = ""

    client_name = quote.get("client_name", "Client")
    client_address = quote.get("client_address", "")
    total = quote.get("total", "0")
    today_str = datetime.date.today().strftime("%d %B %Y")
    client_full = client_name + (", " + client_address if client_address else "")
    logo_data = profile.get("logo", "")

    scope_items_used = quote.get("line_items") or scope_items
    if isinstance(scope_items_used, list) and scope_items_used and isinstance(scope_items_used[0], dict):
        scope_items_used = [(f'{i.get("description","")} — £{i.get("amount","")}' if str(i.get("amount","")).strip() else i.get("description","")) for i in scope_items_used]

    vat_html = '<div style="font-size:11px;font-weight:700;margin-top:4px">+ VAT</div>' if show_vat else ""
    note_html = f'<div style="border:1.5px solid {accent};border-radius:5px;padding:10px 12px;margin-top:14px;background:#fffef5"><div style="font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px">PLEASE NOTE</div><div style="font-size:10px;color:#555;line-height:1.6">{note}</div></div>' if show_note else ""

    base_style = "<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:'Inter',Arial,sans-serif;font-size:14px;background:#fff;width:210mm;min-height:297mm}</style>"
    font_link = '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap" rel="stylesheet">'

    if design_style in ("gold", "george", "custom_html"):
        logo_html = f'<img src="{logo_data}" style="width:64px;height:64px;object-fit:contain;margin-bottom:8px;display:block">' if logo_data else f'<div style="font-size:22px;font-weight:900;color:{accent}">{biz_name[:2].upper()}</div>'
        scope_html = "".join([f'<div style="display:flex;gap:10px;margin-bottom:14px;font-size:13px;color:#333;line-height:1.7"><span style="color:{accent};font-weight:700;flex-shrink:0">&#10003;</span><span>{item}</span></div>' for item in scope_items_used])
        inc_html = "".join([f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;font-size:13px;color:#ccc"><span style="color:{accent}">&#10003;</span>{item}</div>' for item in inclusion_items])
        contact_html = "".join([f'<div style="font-size:11px;color:#ccc;margin-bottom:5px">{v}</div>' for v in [phone_num, email, location] if v])
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">{font_link}{base_style}</head><body>
<table width="100%" cellpadding="0" cellspacing="0" style="background:{dark}"><tr>
<td width="50%" style="padding:32px 36px;border-right:2px solid {accent};vertical-align:middle">{logo_html}
<div style="font-size:20px;font-weight:900;color:{accent};letter-spacing:0.02em;margin-top:6px">{biz_name}</div>
<div style="font-size:9px;color:{accent};letter-spacing:0.14em;text-transform:uppercase;margin-top:4px;opacity:0.85">{trade}</div>
<div style="width:50px;height:1.5px;background:{accent};margin-top:10px"></div></td>
<td width="50%" style="padding:24px 28px;vertical-align:middle">
<div style="font-size:30px;font-weight:900;color:{accent};letter-spacing:0.08em;margin-bottom:14px">{doc_label}</div>
<div style="width:100%;height:1px;background:{accent};margin-bottom:10px;opacity:0.4"></div>
{contact_html}</td></tr></table>
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="60%" style="padding:22px 28px;vertical-align:top;border-right:1px solid #e8dfc8">
<div style="font-size:11px;margin-bottom:5px"><span style="color:{accent};font-weight:700;font-size:9px;text-transform:uppercase;letter-spacing:0.06em">Date: </span>{today_str}</div>
<div style="font-size:11px;margin-bottom:5px"><span style="color:{accent};font-weight:700;font-size:9px;text-transform:uppercase;letter-spacing:0.06em">Ref: </span>{ref_num}</div>
<div style="font-size:11px;margin-bottom:16px"><span style="color:{accent};font-weight:700;font-size:9px;text-transform:uppercase;letter-spacing:0.06em">Client: </span>{client_full}</div>
{due_line}<div style="font-size:11px;color:#444;line-height:1.7;margin-bottom:18px">{intro}</div>
<div style="font-size:13px;font-weight:900;text-transform:uppercase;letter-spacing:0.04em;color:#1a1a1a;border-bottom:2px solid {accent};padding-bottom:8px;margin-bottom:12px">Scope of Works</div>
{scope_html}{note_html}</td>
<td width="40%" style="padding:20px 18px;background:#f9f7f0;vertical-align:top">
<div style="text-align:center;padding-bottom:18px;margin-bottom:18px;border-bottom:1.5px solid {accent}">
<div style="width:52px;height:52px;border-radius:50%;border:2.5px solid {accent};display:inline-flex;align-items:center;justify-content:center;font-size:22px;font-weight:900;color:{accent};margin-bottom:8px">&#163;</div>
<div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:{accent};display:block;margin-bottom:6px">Total Price</div>
<div style="font-size:30px;font-weight:900;color:#1a1a1a;letter-spacing:-0.02em">&#163;{total}</div>{vat_html}</div>
<div style="font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:0.12em;color:{accent};margin-bottom:12px;text-align:center">Inclusions</div>
{inc_html}
<div style="margin-top:18px;border-top:1px solid #333;padding-top:12px">
<div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:{accent};margin-bottom:4px">Lead Time</div>
<div style="font-size:9px;color:#777;line-height:1.6">{lead_time}</div></div></td></tr></table>
<table width="100%" cellpadding="0" cellspacing="0" style="background:{dark};border-top:2px solid {accent}"><tr>
<td width="50%" style="padding:14px 24px;vertical-align:top">
<div style="font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:{accent};margin-bottom:4px">Our Commitment</div>
<div style="font-size:9px;color:#aaa;line-height:1.6">{commitment}</div></td>
<td width="50%" style="padding:14px 24px;vertical-align:top;border-left:1px solid rgba(255,255,255,0.1)">
<div style="font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:{accent};margin-bottom:4px">Payment Terms</div>
<div style="font-size:9px;color:#aaa;line-height:1.6">{payment_terms}</div></td></tr></table>
<table width="100%" cellpadding="0" cellspacing="0" style="background:{dark};border-top:1px solid {accent}44"><tr>
<td style="padding:12px 28px"><div style="font-size:14px;font-weight:900;color:{accent};text-transform:uppercase;letter-spacing:0.1em">Thank You</div>
<div style="font-size:9px;color:#555;margin-top:2px">We look forward to working with you.</div></td>
<td style="padding:12px 28px;text-align:right"><div style="font-size:13px;font-weight:700;color:{accent};font-style:italic">{biz_name}</div></td></tr></table>
</body></html>"""

    elif design_style == "navy":
        logo_html = f'<img src="{logo_data}" style="width:52px;height:52px;object-fit:contain">' if logo_data else ""
        scope_html = "".join([f'<div style="display:flex;gap:10px;margin-bottom:14px;font-size:13px;color:#333;line-height:1.7"><span style="color:{accent};font-weight:700;font-size:16px">›</span><span>{item}</span></div>' for item in scope_items_used])
        inc_html = "".join([f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:13px;color:#444"><span style="color:{accent}">&#10003;</span>{item}</div>' for item in inclusion_items])
        contact_html = "  |  ".join([v for v in [phone_num, email, location] if v])
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">{font_link}{base_style}</head><body>
<table width="100%" cellpadding="0" cellspacing="0" style="background:{dark}"><tr>
<td style="padding:22px 28px;vertical-align:middle">
<div style="display:flex;justify-content:space-between;align-items:center">
<div style="display:flex;align-items:center;gap:14px">{logo_html}
<div><div style="font-size:20px;font-weight:900;color:#fff">{biz_name}</div>
<div style="font-size:9px;color:rgba(255,255,255,0.6);letter-spacing:0.1em;text-transform:uppercase;margin-top:3px">{trade}</div></div></div>
<div style="background:{accent};padding:10px 18px;border-radius:6px;text-align:center">
<div style="font-size:13px;font-weight:900;color:{dark};letter-spacing:0.08em">{doc_label}</div>
<div style="font-size:9px;color:{dark};opacity:0.7;margin-top:2px">{ref_num}</div></div></div></td></tr></table>
<div style="padding:18px 28px;border-bottom:1px solid #e8e8e8">
<div style="display:flex;gap:28px;font-size:11px">
<div><div style="color:#999;font-size:9px;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px">Client</div><div style="font-weight:700;color:#111">{client_full}</div></div>
<div><div style="color:#999;font-size:9px;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px">Date</div><div style="font-weight:700;color:#111">{today_str}</div></div>
<div><div style="color:#999;font-size:9px;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px">Valid</div><div style="font-weight:700;color:#111">30 days</div></div></div></div>
<div style="padding:20px 28px">
<div style="font-size:12px;font-weight:800;color:{dark};text-transform:uppercase;letter-spacing:0.06em;margin-bottom:10px">Scope of Works</div>
{scope_html}
<div style="background:#f0f4ff;border-left:4px solid {dark};padding:12px 16px;margin-top:18px;display:flex;justify-content:space-between;align-items:center;border-radius:0 6px 6px 0">
<div><div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:{dark}">Total Price</div>{vat_html}</div>
<div style="font-size:32px;font-weight:900;color:{dark}">&#163;{total}</div></div>
{note_html}
<div style="margin-top:18px;display:grid;grid-template-columns:1fr 1fr;gap:16px">
<div><div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:{dark};margin-bottom:6px">Inclusions</div>{inc_html}</div>
<div><div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:{dark};margin-bottom:6px">Lead Time</div>
<div style="font-size:10px;color:#555;line-height:1.6">{lead_time}</div></div></div></div>
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f7f7f7;border-top:1px solid #eee"><tr>
<td style="padding:10px 28px"><div style="font-size:9px;color:#999">{contact_html}</div></td>
<td style="padding:10px 28px;text-align:right"><div style="font-size:9px;color:#999">{payment_terms}</div></td></tr></table>
</body></html>"""

    elif design_style == "slate":
        logo_html = f'<img src="{logo_data}" style="width:64px;height:64px;object-fit:contain">' if logo_data else ""
        scope_html = "".join([f'<div style="display:flex;gap:14px;margin-bottom:16px;font-size:13px;color:#333;line-height:1.7;align-items:flex-start"><span style="width:24px;height:24px;background:{accent};border-radius:50%;display:inline-flex;align-items:center;justify-content:center;color:#fff;font-size:10px;font-weight:800;flex-shrink:0;margin-top:2px">{i+1}</span><span>{item}</span></div>' for i, item in enumerate(scope_items_used)])
        inc_html = "".join([f'<div style="font-size:13px;color:#444;padding:8px 0;border-bottom:0.5px solid #eee;line-height:1.5">&#10003; {item}</div>' for item in inclusion_items])
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">{font_link}{base_style}</head><body style="padding:48px;min-height:297mm">
<div style="display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:20px;border-bottom:3px solid {accent};margin-bottom:32px">
<div style="display:flex;align-items:center;gap:18px">{logo_html}
<div><div style="font-size:28px;font-weight:900;color:#1a1a1a;letter-spacing:-0.03em">{biz_name}</div>
<div style="font-size:12px;color:#aaa;margin-top:4px;letter-spacing:0.05em">{trade}</div></div></div>
<div style="text-align:right"><div style="font-size:11px;color:#aaa;text-transform:uppercase;letter-spacing:0.1em">{doc_label}</div>
<div style="font-size:18px;font-weight:800;color:#1a1a1a;margin-top:2px">{ref_num}</div></div></div>
<div style="display:flex;gap:40px;margin-bottom:32px">
<div><div style="color:#bbb;font-size:10px;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px">Client</div><div style="font-size:14px;font-weight:700;color:#1a1a1a">{client_full}</div></div>
<div><div style="color:#bbb;font-size:10px;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px">Date</div><div style="font-size:14px;font-weight:700;color:#1a1a1a">{today_str}</div></div>
<div><div style="color:#bbb;font-size:10px;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px">Ref</div><div style="font-size:14px;font-weight:700;color:#1a1a1a">{ref_num}</div></div></div>
{due_line}
<div style="font-size:11px;font-weight:700;color:{accent};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:16px">Scope of Works</div>
{scope_html}{note_html}
<div style="display:flex;justify-content:space-between;align-items:center;padding:20px 0;margin-top:28px;border-top:2px solid #eee;border-bottom:2px solid #eee">
<div><div style="font-size:11px;color:#aaa;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px">Total Price</div>{vat_html}</div>
<div style="font-size:42px;font-weight:900;color:#1a1a1a;letter-spacing:-0.04em">&#163;{total}</div></div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:32px;margin-top:28px">
<div><div style="font-size:11px;font-weight:700;color:{accent};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:12px">Inclusions</div>{inc_html}</div>
<div><div style="font-size:11px;font-weight:700;color:{accent};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:10px">Lead Time</div>
<div style="font-size:13px;color:#555;line-height:1.7">{lead_time}</div>
<div style="font-size:11px;font-weight:700;color:{accent};text-transform:uppercase;letter-spacing:0.08em;margin-top:20px;margin-bottom:8px">Payment Terms</div>
<div style="font-size:13px;color:#555;line-height:1.7">{payment_terms}</div></div></div>
<div style="margin-top:40px;padding-top:16px;border-top:0.5px solid #ddd;display:flex;justify-content:space-between;align-items:center">
<div style="font-size:11px;color:#bbb">{"  ·  ".join([v for v in [phone_num, email, location] if v])}</div>
<div style="font-size:12px;font-weight:700;color:{accent}">{commitment}</div></div>
</body></html>"""

    elif design_style == "green":
        logo_html = f'<img src="{logo_data}" style="width:52px;height:52px;object-fit:contain">' if logo_data else ""
        scope_html = "".join([f'<div style="display:flex;gap:10px;margin-bottom:14px;font-size:13px;color:#333;line-height:1.7"><span style="color:{accent};font-weight:700">&#9658;</span><span>{item}</span></div>' for item in scope_items_used])
        inc_html = "".join([f'<div style="font-size:13px;color:#333;margin-bottom:10px;line-height:1.5">&#10003; {item}</div>' for item in inclusion_items])
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">{font_link}{base_style}</head><body>
<table width="100%" cellpadding="0" cellspacing="0" style="background:{dark}"><tr>
<td style="padding:22px 28px">
<div style="display:flex;justify-content:space-between;align-items:center">
<div style="display:flex;align-items:center;gap:14px">{logo_html}
<div><div style="font-size:20px;font-weight:900;color:#fff">{biz_name}</div>
<div style="font-size:9px;color:rgba(255,255,255,0.5);letter-spacing:0.1em;text-transform:uppercase;margin-top:3px">{trade}</div></div></div>
<div style="text-align:right;font-size:10px;color:rgba(255,255,255,0.6);line-height:2">{"<br>".join([v for v in [phone_num, email, location] if v])}</div></div>
<div style="height:2px;background:{accent};border-radius:2px;margin-top:14px"></div></td></tr></table>
<div style="padding:22px 28px">
<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:18px">
<div><div style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:3px">Prepared for</div>
<div style="font-size:13px;font-weight:800;color:#1a1a1a">{client_full}</div>
<div style="font-size:10px;color:#666;margin-top:2px">{today_str}  ·  {ref_num}</div>
{due_line}</div>
<div style="background:#f0f7eb;border:2px solid {accent};border-radius:8px;padding:12px 20px;text-align:center">
<div style="font-size:9px;color:{dark};font-weight:700;text-transform:uppercase;letter-spacing:0.06em">Total</div>
<div style="font-size:28px;font-weight:900;color:{dark}">&#163;{total}</div>{vat_html}</div></div>
<div style="font-size:12px;font-weight:800;color:{dark};text-transform:uppercase;letter-spacing:0.06em;border-bottom:2px solid {accent};padding-bottom:6px;margin-bottom:12px">Works Included</div>
{scope_html}{note_html}
<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px">
<div><div style="font-size:9px;font-weight:700;color:{dark};text-transform:uppercase;letter-spacing:0.06em;margin-bottom:8px">Inclusions</div>{inc_html}</div>
<div><div style="font-size:9px;font-weight:700;color:{dark};text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px">Lead Time</div>
<div style="font-size:10px;color:#555;line-height:1.6">{lead_time}</div>
<div style="font-size:9px;font-weight:700;color:{dark};text-transform:uppercase;letter-spacing:0.06em;margin-top:10px;margin-bottom:4px">Payment</div>
<div style="font-size:10px;color:#555;line-height:1.6">{payment_terms}</div></div></div></div>
<table width="100%" cellpadding="0" cellspacing="0" style="background:{dark}"><tr>
<td style="padding:10px 28px;display:flex;justify-content:space-between;align-items:center">
<div style="font-size:9px;color:rgba(255,255,255,0.5)">{commitment}</div>
<div style="font-size:11px;font-weight:700;color:{accent};margin-left:20px">Thank you</div></td></tr></table>
</body></html>"""

    else:
        logo_html = f'<img src="{logo_data}" style="width:48px;height:48px;object-fit:contain">' if logo_data else ""
        scope_html = "".join([f'<div style="font-size:13px;color:#333;padding:10px 0;border-bottom:0.5px solid #f0f0f0;line-height:1.7">— {item}</div>' for item in scope_items_used])
        inc_html = "".join([f'<div style="font-size:13px;color:#555;padding:4px 0;line-height:1.6">— {item}</div>' for item in inclusion_items])
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">{font_link}{base_style}</head><body style="padding:36px">
<div style="display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:12px;border-bottom:0.5px solid #ddd;margin-bottom:18px">
<div style="display:flex;align-items:center;gap:12px">{logo_html}
<div><div style="font-size:22px;font-weight:900;color:#111;letter-spacing:-0.04em">{biz_name}</div>
<div style="font-size:9px;color:#ccc;margin-top:2px;letter-spacing:0.06em">{trade}</div></div></div>
<div style="text-align:right">
<div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em">{doc_label} · {ref_num}</div>
<div style="font-size:10px;color:#bbb;margin-top:3px;line-height:1.8">{"<br>".join([v for v in [today_str, phone_num, email] if v])}</div></div></div>
<div style="margin-bottom:18px">
<div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px">Prepared for</div>
<div style="font-size:12px;font-weight:700;color:#111">{client_full}</div>
{due_line}</div>
<div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:8px">Works</div>
{scope_html}{note_html}
<div style="display:flex;justify-content:space-between;align-items:center;padding:16px 0;margin-top:20px;border-top:0.5px solid #ddd">
<div><div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em">Total</div>{vat_html}</div>
<div style="font-size:36px;font-weight:900;color:#111;letter-spacing:-0.04em">&#163;{total}</div></div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:12px;padding-top:12px;border-top:0.5px solid #ddd">
<div><div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:6px">Inclusions</div>{inc_html}</div>
<div><div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px">Lead Time</div>
<div style="font-size:10px;color:#555;line-height:1.6">{lead_time}</div>
<div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em;margin-top:10px;margin-bottom:4px">Payment</div>
<div style="font-size:10px;color:#555;line-height:1.6">{payment_terms}</div></div></div>
<div style="margin-top:24px;padding-top:10px;border-top:0.5px solid #eee;font-size:9px;color:#ccc;text-align:center">{commitment}</div>
</body></html>"""


def generate_quote_pdf(quote, profile, template):
    import requests as req
    html = build_quote_html(quote, profile, template, is_invoice=False)
    buffer = io.BytesIO()
    try:
        api_key = os.environ.get("PDFSHIFT_API_KEY", "")
        response = req.post(
            "https://api.pdfshift.io/v3/convert/pdf",
            auth=(api_key, ""),
            json={"source": html, "format": "A4", "margin": "0"},
            timeout=30
        )
        if response.status_code == 200:
            buffer.write(response.content)
        else:
            raise Exception("PDFShift error: " + str(response.status_code))
    except Exception as e:
        print("PDF generation error: " + str(e))
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        styles = getSampleStyleSheet()
        biz_name = profile.get("business_name", "Your Business")
        doc.build([Paragraph(biz_name, styles["Title"]), Spacer(1, 12), Paragraph("Quote for: " + quote.get("client_name", ""), styles["Normal"]), Spacer(1, 12), Paragraph("Total: £" + str(quote.get("total", "0")), styles["Normal"])])
    buffer.seek(0)
    return buffer


def generate_invoice_pdf(invoice, profile, template):
    import requests as req
    html = build_quote_html(invoice, profile, template, is_invoice=True)
    buffer = io.BytesIO()
    try:
        api_key = os.environ.get("PDFSHIFT_API_KEY", "")
        response = req.post(
            "https://api.pdfshift.io/v3/convert/pdf",
            auth=(api_key, ""),
            json={"source": html, "format": "A4", "margin": "0"},
            timeout=30
        )
        if response.status_code == 200:
            buffer.write(response.content)
            buffer.seek(0)
            return buffer
        else:
            raise Exception("PDFShift error: " + str(response.status_code))
    except Exception as e:
        print("Invoice PDF error, falling back to ReportLab: " + str(e))

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=20*mm, leftMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)

    brand_colour = template.get("brand_colour", "#1a1a2e") if template else "#1a1a2e"
    try:
        brand = colors.HexColor(brand_colour)
    except:
        brand = colors.HexColor("#1a1a2e")

    styles = getSampleStyleSheet()
    s_normal = styles["Normal"]
    s_right = ParagraphStyle("right_inv", parent=s_normal, alignment=TA_RIGHT)
    s_center = ParagraphStyle("center_inv", parent=s_normal, alignment=TA_CENTER)

    story = []

    biz_name = (template.get("business_name") if template else None) or profile.get("business_name", "")
    biz_address = (template.get("business_address") if template else None) or ""
    biz_phone = (template.get("business_phone") if template else None) or profile.get("phone", "")
    biz_email = (template.get("business_email") if template else None) or ""
    payment_details = (template.get("payment_details") if template else None) or ""
    terms_text = (template.get("terms") if template else None) or ""
    footer_text = (template.get("footer_text") if template else None) or "Thank you for your business."

    logo_data = profile.get("logo") or (template.get("logo") if template else None) or ""
    logo_image = None
    if logo_data and logo_data.startswith("data:image"):
        try:
            import base64
            from reportlab.lib.utils import ImageReader
            header_parts = logo_data.split(",", 1)
            if len(header_parts) == 2:
                img_bytes = base64.b64decode(header_parts[1])
                logo_image = ImageReader(io.BytesIO(img_bytes))
        except Exception as e:
            print("Logo error: " + str(e))

    if logo_image:
        from reportlab.platypus import Image
        logo_el = Image(logo_image, width=30*mm, height=30*mm)
        logo_el.hAlign = 'LEFT'
        story.append(logo_el)
        story.append(Spacer(1, 4*mm))

    header_data = [[
        Paragraph("<font size=16><b>" + biz_name.upper() + "</b></font>", s_normal),
        Paragraph("<font size=14><b>INVOICE</b></font>", s_right)
    ]]
    header_table = Table(header_data, colWidths=[100*mm, 70*mm])
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), brand),
        ("TEXTCOLOR", (0,0), (-1,-1), colors.white),
        ("TOPPADDING", (0,0), (-1,-1), 14),
        ("BOTTOMPADDING", (0,0), (-1,-1), 14),
        ("LEFTPADDING", (0,0), (0,-1), 16),
        ("RIGHTPADDING", (-1,0), (-1,-1), 16),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 8*mm))

    inv_num = invoice.get("invoice_number", "INV-001")
    due_date = invoice.get("due_date", "")
    today_str = datetime.date.today().strftime("%d %B %Y")

    from_text = biz_name
    if biz_address:
        from_text += "<br/>" + biz_address
    if biz_phone:
        from_text += "<br/>" + biz_phone
    if biz_email:
        from_text += "<br/>" + biz_email

    info_data = [[
        Paragraph("<font size=8 color='grey'>FROM</font><br/><br/>" + from_text, s_normal),
        Paragraph(
            "<font size=8 color='grey'>INVOICE NUMBER</font><br/>" + inv_num +
            "<br/><br/><font size=8 color='grey'>DATE</font><br/>" + today_str +
            "<br/><br/><font size=8 color='grey'>DUE DATE</font><br/>" + due_date,
            s_right
        )
    ]]
    info_table = Table(info_data, colWidths=[95*mm, 75*mm])
    info_table.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP")]))
    story.append(info_table)
    story.append(Spacer(1, 6*mm))

    client_name = invoice.get("client_name", "")
    client_address = invoice.get("client_address", "")
    to_text = "<font size=8 color='grey'>BILL TO</font><br/><br/><b>" + client_name + "</b>"
    if client_address:
        to_text += "<br/>" + client_address
    story.append(Paragraph(to_text, s_normal))
    story.append(Spacer(1, 8*mm))

    line_items = invoice.get("line_items", [])
    if line_items and len(line_items) > 0:
        table_data = [["Description", "Qty", "Unit Price", "Amount"]]
        subtotal = 0
        for item in line_items:
            desc = item.get("description", "")
            qty = item.get("qty", 1)
            unit_price = item.get("unit_price", 0)
            amount = float(qty) * float(unit_price)
            subtotal += amount
            table_data.append([desc, str(qty), "\u00a3{:.2f}".format(float(unit_price)), "\u00a3{:.2f}".format(amount)])

        items_table = Table(table_data, colWidths=[85*mm, 20*mm, 30*mm, 35*mm])
        items_table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), brand),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTSIZE", (0,0), (-1,0), 9),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("TOPPADDING", (0,0), (-1,-1), 8),
            ("BOTTOMPADDING", (0,0), (-1,-1), 8),
            ("LEFTPADDING", (0,0), (-1,-1), 8),
            ("GRID", (0,0), (-1,-1), 0.5, colors.Color(0.85, 0.85, 0.85)),
            ("ALIGN", (1,0), (-1,-1), "RIGHT"),
            ("FONTSIZE", (0,1), (-1,-1), 9),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.Color(0.97, 0.97, 0.97)]),
        ]))
        story.append(items_table)
        story.append(Spacer(1, 4*mm))

        vat_rate = 0.2 if profile.get("vat_registered", "no").lower() in ["yes", "y", "true"] else 0
        vat_amount = subtotal * vat_rate
        total = subtotal + vat_amount

        totals_data = [["Subtotal", "\u00a3{:.2f}".format(subtotal)]]
        if vat_rate > 0:
            totals_data.append(["VAT (20%)", "\u00a3{:.2f}".format(vat_amount)])
        totals_data.append(["TOTAL DUE", "\u00a3{:.2f}".format(total)])

        totals_table = Table(totals_data, colWidths=[135*mm, 35*mm])
        totals_table.setStyle(TableStyle([
            ("ALIGN", (0,0), (-1,-1), "RIGHT"),
            ("FONTSIZE", (0,0), (-1,-1), 10),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold"),
            ("FONTSIZE", (0,-1), (-1,-1), 12),
            ("LINEABOVE", (0,-1), (-1,-1), 1.5, brand),
        ]))
        story.append(totals_table)
    else:
        invoice_text = invoice.get("invoice_text", "")
        if invoice_text:
            skip_phrases = ["Reply INVPDF", "reply invpdf", "---"]
            story.append(Paragraph("<b>Invoice Details</b>", s_normal))
            story.append(Spacer(1, 3*mm))
            for line in invoice_text.split("\n"):
                stripped = line.strip().replace("**", "")
                if not stripped:
                    continue
                skip = False
                for phrase in skip_phrases:
                    if phrase.lower() in stripped.lower():
                        skip = True
                        break
                if not skip:
                    if "total" in stripped.lower():
                        story.append(Spacer(1, 2*mm))
                        story.append(Paragraph("<b>" + stripped + "</b>", s_normal))
                    else:
                        story.append(Paragraph(stripped, s_normal))

    story.append(Spacer(1, 10*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=brand))
    story.append(Spacer(1, 4*mm))

    if payment_details:
        story.append(Paragraph("<font size=8 color='grey'>PAYMENT DETAILS</font>", s_normal))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph(payment_details.replace("\n", "<br/>"), s_normal))
        story.append(Spacer(1, 6*mm))

    if terms_text:
        story.append(Paragraph("<font size=8 color='grey'>TERMS & CONDITIONS</font>", s_normal))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("<font size=8>" + terms_text.replace("\n", "<br/>") + "</font>", s_normal))
        story.append(Spacer(1, 6*mm))

    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.Color(0.85, 0.85, 0.85)))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph("<font size=9 color='grey'>" + footer_text + "</font>", s_center))

    doc.build(story)
    buffer.seek(0)
    return buffer


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")
    num_media = int(request.form.get("NumMedia", 0))
    wa_image_data = None
    wa_image_type = "image/jpeg"

    if num_media > 0:
        media_type = request.form.get("MediaContentType0", "")
        media_url = request.form.get("MediaUrl0", "")
        if "audio" in media_type or "ogg" in media_type:
            try:
                import requests as req
                from openai import OpenAI
                account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
                audio_response = req.get(media_url, auth=(account_sid, auth_token))
                audio_path = "/tmp/voice_" + sender.replace("+", "").replace(":", "") + ".ogg"
                with open(audio_path, "wb") as f:
                    f.write(audio_response.content)
                openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
                with open(audio_path, "rb") as audio_file:
                    transcript = openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                incoming_msg = transcript.text.strip()
                print("Voice note transcribed: " + incoming_msg)
            except Exception as e:
                print("Voice transcription error: " + str(e))
                from twilio.twiml.messaging_response import MessagingResponse
                resp = MessagingResponse()
                resp.message("Sorry, could not transcribe your voice note. Try again or type your message.")
                return str(resp)
        elif "image" in media_type:
            try:
                import requests as req
                account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
                img_response = req.get(media_url, auth=(account_sid, auth_token))
                wa_image_data = base64.b64encode(img_response.content).decode()
                wa_image_type = media_type
                if not incoming_msg:
                    incoming_msg = "Please analyse this photo and help me quote this job."
                print("WhatsApp image received, size: " + str(len(wa_image_data)))
            except Exception as e:
                print("WhatsApp image error: " + str(e))

    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()

    profile = get_user_profile(sender)

    if not profile:
        onboarding = get_onboarding_state(sender)

        if not onboarding:
            question_key, question_text = ONBOARDING_QUESTIONS[0]
            supabase.table("onboarding").insert({"sender": sender, "step": 0, "data": {}}).execute()
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
                supabase.table("onboarding").update({"step": next_step, "data": data}).eq("sender", sender).execute()
                resp.message(next_question)
            else:
                try:
                    phone = format_phone(data.get("phone", ""))
                    supabase.table("profiles").insert({
                        "sender": sender, "business_name": data.get("business_name", ""),
                        "owner_name": data.get("owner_name", ""), "phone": phone,
                        "trade": data.get("trade", ""), "day_rate": "250", "half_day_rate": "125",
                        "hourly_rate": "35", "materials_markup": "20", "payment_terms": "30",
                        "vat_registered": "no", "vat_number": "", "twilio_number": "",
                        "pin": data.get("pin", ""), "reset_code": "",
                        "gmail_token": "", "gmail_refresh_token": ""
                    }).execute()
                    supabase.table("onboarding").delete().eq("sender", sender).execute()

                    # 60-SECOND WOW: instantly generate a real sample quote in their trade
                    fresh_profile = get_user_profile(sender) or {
                        "sender": sender, "business_name": data.get("business_name", ""),
                        "owner_name": data.get("owner_name", ""), "trade": data.get("trade", ""),
                        "day_rate": "250", "materials_markup": "20", "phone": phone
                    }
                    owner_fw = (data.get("owner_name", "") or "").split(" ")[0]
                    resp.message("All set, " + owner_fw + "! ✅ Give me two seconds - I'll show you what I can do...")

                    sample = send_welcome_sample_quote(fresh_profile)
                    try:
                        from twilio.rest import Client as _TC
                        _tc = _TC(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
                        def _wa(n): return n if str(n).startswith("whatsapp:") else "whatsapp:" + str(n)
                        _from = _wa(os.environ.get("TWILIO_NUMBER"))
                        if sample:
                            wow = ("Here is a sample quote I just made for you - a real \"" + sample["job"][:60] +
                                   "\" job, priced and branded with your business name:\n\n" + sample["url"] +
                                   "\n\nThat is about 30 seconds of work, done for you. 💷 Your real quotes will look just like this.\n\n"
                                   "Try it now - just describe any job (or send a photo) and I will quote it. "
                                   "Or open your dashboard:\nhttps://trades-pa-trades-pa.up.railway.app/dashboard")
                        else:
                            wow = ("You are ready to go. Try me now - describe a job or send a photo and I will build you a quote.\n\n"
                                   "Your dashboard:\nhttps://trades-pa-trades-pa.up.railway.app/dashboard")
                        _tc.messages.create(body=wow, from_=_from, to=_wa(sender))
                    except Exception as _we:
                        print("welcome wow send error:", _we)
                except Exception as e:
                    print("Profile save error: " + str(e))
                    print(traceback.format_exc())
                    resp.message("Sorry something went wrong. Please try again.")
                    supabase.table("onboarding").delete().eq("sender", sender).execute()

            return str(resp)

    # Handle Invoice PDF request
    if incoming_msg.strip().upper() == "INVPDF":
        try:
            latest_inv = supabase.table("invoices").select("*").eq("sender", sender).order("created_at", desc=True).limit(1).execute()
            if latest_inv.data:
                inv = latest_inv.data[0]
                pdf_url = "https://trades-pa-trades-pa.up.railway.app/generate-invoice-pdf/" + str(inv["id"])
                resp.message("Here is your invoice PDF:\n" + pdf_url + "\n\nOpen the link to download and send to your client.")
            else:
                resp.message("No recent invoice found. Generate an invoice first.")
            return str(resp)
        except Exception as e:
            print("INVPDF request error: " + str(e))
            resp.message("Sorry, could not generate invoice PDF. Try again.")
            return str(resp)

    # Handle PDF request
    if incoming_msg.strip().upper() == "PDF":
        try:
            latest_quote = supabase.table("quotes").select("*").eq("sender", sender).order("created_at", desc=True).limit(1).execute()
            if latest_quote.data:
                quote = latest_quote.data[0]
                pdf_url = "https://trades-pa-trades-pa.up.railway.app/generate-pdf/" + str(quote["id"])
                resp.message("Here is your quote PDF:\n" + pdf_url + "\n\nOpen the link to download and share with your client.")
            else:
                resp.message("No recent quote found. Generate a quote first.")
            return str(resp)
        except Exception as e:
            print("PDF request error: " + str(e))
            resp.message("Sorry, could not generate PDF. Try again.")
            return str(resp)

    # Handle email check request
    if incoming_msg.strip().lower() in ["check emails", "check my emails", "emails", "any emails"]:
        try:
            scan_emails_for_user(profile)
            scan_outlook_for_user(profile)
            important = supabase.table("emails").select("*").eq("sender", sender).eq("is_important", True).eq("read", False).order("created_at", desc=True).limit(5).execute()
            if important.data:
                email_summary = "You have " + str(len(important.data)) + " important email(s):\n\n"
                for em in important.data:
                    email_summary += "- " + em.get("from_email", "").split("<")[0].strip() + "\n  " + em.get("summary", em.get("subject", "")) + "\n\n"
                resp.message(email_summary)
            else:
                resp.message("No important unread emails right now.")
            return str(resp)
        except Exception as e:
            print("Email check error: " + str(e))
            resp.message("Could not check emails. Make sure Gmail is connected in your dashboard settings.")
            return str(resp)

    if sender not in conversation_history:
        conversation_history[sender] = []

    if wa_image_data:
        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": wa_image_type, "data": wa_image_data}},
            {"type": "text", "text": incoming_msg}
        ]
        conversation_history[sender].append({"role": "user", "content": user_content})
    else:
        conversation_history[sender].append({"role": "user", "content": incoming_msg})

    today = datetime.date.today().strftime("%A %d %B %Y")

    day_rate_wa = float(str(profile.get("day_rate") or "250").replace("£","").replace(",","").strip() or 250)
    if day_rate_wa == 0: day_rate_wa = 250
    markup_wa = float(str(profile.get("materials_markup") or "20").replace("%","").strip() or 20)

    system_prompt = f"""You are a smart PA and quoting assistant for a trades business on WhatsApp. Today is {today}.

BUSINESS DETAILS:
- Business: {profile.get("business_name","")}
- Owner: {profile.get("owner_name","")}
- Trade: {profile.get("trade","")}
- Day rate: £{day_rate_wa:.0f}
- Half day rate: £{float(str(profile.get("half_day_rate") or day_rate_wa/2).replace("£","") or day_rate_wa/2):.0f}
- Materials markup: {markup_wa:.0f}% (apply silently — never show % to client)
- Payment terms: {profile.get("payment_terms","30")} days
- VAT registered: {profile.get("vat_registered","no")}

YOU CAN HELP WITH:
1. Logging job enquiries
2. Generating professional quotes (with PDF)
3. Booking in jobs
4. Tracking jobs and payments
5. Scheduling and reminders
6. Answering general questions

QUOTING — TWO MODES:

MODE 1 - MANUAL: If the user gives their own numbers (e.g. "materials £340, 2 days labour") use them exactly. Don't add markup — they've priced it themselves.

MODE 2 - AI CALCULATED: If just describing the job, calculate using rates above.
- labourCost = days × £{day_rate_wa:.0f}
- markupAmount = raw materials × {markup_wa:.0f} / 100
- materialsCost = raw materials + markupAmount
- totalPrice = labourCost + materialsCost rounded to nearest £5

WHEN GENERATING A QUOTE — always end reply with QUOTE_READY: tag on its own line:
QUOTE_READY:{{"clientName":"Client","scopeItems":["item 1","item 2","item 3"],"totalPrice":0,"labourDays":0,"labourCost":0,"materials":[{{"item":"material","qty":"qty","unitCost":0,"total":0}}],"materialsCost":0,"markupAmount":0,"leadTimeDays":"3-5","note":"","summary":"Quote ready"}}

Before the QUOTE_READY tag, send a brief WhatsApp-friendly summary:
Quote for [client] — [job]
Labour: [X] days @ £{day_rate_wa:.0f} = £[amount]
Materials: £[amount]
*TOTAL: £[amount]*
Payment due within {profile.get("payment_terms","30")} days
Generating your PDF now... 📄

ALSO end reply with LOG: tag:
LOG:name=<client>|job=<job>|location=<location>|status=quoted

If logging enquiry only (no quote): LOG:name=<client>|job=<job>|location=<location>|status=new

WHEN BOOKING: end with BOOK:name=<client>|job=<job>|location=<location>|date=<date>|time=<time>|days=<duration>

WHEN INVOICING: present invoice clearly then end with:
INV:name=<client>|job=<job>|location=<location>|total=<amount>|due=<date>
INVOICEDATA:{{"items":[{{"description":"item","qty":1,"unit_price":0}}],"subtotal":"0","total":"0"}}

Always be concise — this is WhatsApp. Never say you can't do something."""
    system_prompt += "If generating an INVOICE, present it on WhatsApp like this:\n"
    system_prompt += "Invoice for [client name]\n"
    system_prompt += "[job description] at [address]\n\n"
    system_prompt += "Labour: [X] days @ [rate] = [amount]\n"
    system_prompt += "Materials: [total amount after markup already applied]\n"
    system_prompt += "TOTAL DUE: [amount]\n"
    system_prompt += "Payment due by [date based on payment terms]\n\n"
    system_prompt += "Reply INVPDF to get this as a professional PDF invoice.\n\n"
    system_prompt += "Then end your reply with:\n"
    system_prompt += "INV:name=<client name>|job=<job type>|location=<location>|total=<total amount>|due=<YYYY-MM-DD>\n"
    system_prompt += "INVOICEDATA:" + json.dumps({"items": [{"description": "example", "qty": 1, "unit_price": 0}], "subtotal": "0", "total": "0"}) + "\n"
    system_prompt += "Replace the INVOICEDATA with the actual invoice line items as valid JSON.\n\n"
    system_prompt += "If BOOKING a job, confirm the details clearly then end your reply with:\n"
    system_prompt += "BOOK:name=<client name>|job=<job type>|location=<location>|date=<YYYY-MM-DD>|time=<HH:MM>|days=<number of days>\n"
    system_prompt += "For the date, convert relative dates to YYYY-MM-DD format using today's date as reference."

    response = client.messages.create(model="claude-sonnet-4-5", max_tokens=1500, system=system_prompt, messages=conversation_history[sender])

    reply = response.content[0].text

    conversation_history[sender].append({"role": "assistant", "content": reply})

    quote_items = []
    quote_subtotal = "0"
    quote_total = "0"
    # Handle QUOTE_READY (new unified format)
    pdf_url = None
    if "QUOTE_READY:" in reply:
        try:
            import re as re_mod
            qr_raw = reply.split("QUOTE_READY:")[1].strip().split("\n")[0]
            qr_raw = re_mod.sub(r'```json|```', '', qr_raw).strip()
            qd = json.loads(qr_raw)

            # Fetch template for branding
            template_result = supabase.table("quote_templates").select("*").eq("sender", sender).execute()
            template = template_result.data[0] if template_result.data else {}

            # Build quote object for PDF
            quote_count = supabase.table("quotes").select("id").eq("sender", sender).execute()
            quote_num = "QU-" + str(len(quote_count.data) + 1).zfill(3)
            scope_items = qd.get("scopeItems", [])
            total = str(qd.get("totalPrice", "0"))

            quote_obj = {
                "client_name": qd.get("clientName", "Client"),
                "client_address": "",
                "total": total,
                "quote_number": quote_num,
                "line_items": scope_items
            }

            # Generate PDF using shared build_quote_html
            html = build_quote_html(quote_obj, profile, template, is_invoice=False)

            # Save to Supabase quotes table
            saved_quote = supabase.table("quotes").insert({
                "sender": sender,
                "client_name": qd.get("clientName", "Client"),
                "client_address": "",
                "job_description": ", ".join(scope_items[:2]),
                "line_items": scope_items,
                "subtotal": total,
                "vat": "0",
                "total": total,
                "status": "sent",
                "quote_number": quote_num,
                "quote_text": html,
                "client_number": ""
            }).execute()

            if saved_quote.data:
                quote_id = saved_quote.data[0]["id"]
                pdf_url = "https://trades-pa-trades-pa.up.railway.app/generate-pdf/" + str(quote_id)
                try:
                    save_pricing_memory(profile, ", ".join(scope_items) or qd.get("clientName",""), {
                        "scope": scope_items, "total": total,
                        "labour_days": qd.get("labourDays"), "labour_cost": qd.get("labourCost"),
                        "materials_cost": qd.get("materialsCost"), "client_name": qd.get("clientName","")
                    })
                except Exception as _se:
                    print("pricing save (whatsapp path):", _se)

        except Exception as e:
            print("QUOTE_READY handler error: " + str(e))
            import traceback
            print(traceback.format_exc())

    # Also handle old QUOTEDATA format for backwards compat
    elif "QUOTEDATA:" in reply:
        try:
            qd_line = reply.split("QUOTEDATA:")[1].strip().split("\n")[0]
            qd = json.loads(qd_line)
            quote_items = qd.get("items", [])
            quote_total = str(qd.get("total", "0"))
        except Exception as e:
            print("QUOTEDATA parse error: " + str(e))

    if "LOG:" in reply:
        try:
            log_line = reply.split("LOG:")[1].strip().split("\n")[0]
            parts = dict(p.split("=") for p in log_line.split("|") if "=" in p)
            status = parts.get("status", "new")
            supabase.table("enquiries").insert({
                "sender": sender, "message": incoming_msg,
                "summary": reply.split("LOG:")[0].strip(),
                "client_name": parts.get("name", ""),
                "job_type": parts.get("job", ""),
                "location": parts.get("location", ""),
                "status": status
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

    # Parse INVOICEDATA if present
    inv_items = []
    inv_subtotal = "0"
    inv_total = "0"
    if "INVOICEDATA:" in reply:
        try:
            inv_line = reply.split("INVOICEDATA:")[1].strip().split("\n")[0]
            inv_data = json.loads(inv_line)
            inv_items = inv_data.get("items", [])
            inv_subtotal = str(inv_data.get("subtotal", "0"))
            inv_total = str(inv_data.get("total", "0"))
        except Exception as e:
            print("INVOICEDATA parse error: " + str(e))

    if "INV:" in reply:
        try:
            inv_line = reply.split("INV:")[1].strip().split("\n")[0]
            parts = dict(p.split("=") for p in inv_line.split("|"))
            inv_count = supabase.table("invoices").select("id").eq("sender", sender).execute()
            inv_num = "INV-" + str(len(inv_count.data) + 1).zfill(3)
            clean_text = reply.split("INV:")[0].split("INVOICEDATA:")[0].strip()
            # Look up client number from enquiries by name
            client_num = ""
            try:
                enq = supabase.table("enquiries").select("sender").ilike("client_name", parts.get("name", "")).order("created_at", desc=True).limit(1).execute()
                if enq.data:
                    client_num = enq.data[0].get("sender", "")
            except Exception as lookup_err:
                print("Client number lookup error: " + str(lookup_err))
            supabase.table("invoices").insert({
                "sender": sender,
                "client_name": parts.get("name", ""),
                "client_address": parts.get("location", ""),
                "job_description": parts.get("job", ""),
                "line_items": inv_items,
                "subtotal": inv_subtotal,
                "vat": "0",
                "total": parts.get("total", inv_total),
                "status": "unpaid",
                "invoice_number": inv_num,
                "invoice_text": clean_text,
                "due_date": parts.get("due", ""),
                "client_number": client_num
            }).execute()
        except Exception as e:
            print("Invoice logging error: " + str(e))

    clean_reply = reply.split("QUOTE_READY:")[0].split("LOG:")[0].split("BOOK:")[0].split("QUOTEDATA:")[0].split("INV:")[0].split("INVOICEDATA:")[0].strip()

    if pdf_url:
        clean_reply += "\n\n📄 *Your quote PDF:*\n" + pdf_url + "\n\nOpen to download and share with your client."

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
    from twilio.twiml.voice_response import VoiceResponse, Dial
    from urllib.parse import quote
    resp = VoiceResponse()
    if profile and profile.get("phone"):
        resp.say("Please hold while we connect your call. Please note, calls may be recorded and transcribed for quality and training purposes.")
        rec_cb = "/call-recording?caller=" + quote(caller) + "&called=" + quote(called)
        dial = Dial(action="/call-status", method="POST", timeout=20,
                    record="record-from-answer-dual",
                    recording_status_callback=rec_cb,
                    recording_status_callback_method="POST",
                    recording_status_callback_event="completed")
        dial.number(profile.get("phone"))
        resp.append(dial)
    else:
        resp.say("Sorry, we are unable to connect your call right now. Please try again later.")
    return str(resp)


@app.route("/call-status", methods=["POST"])
def call_status():
    from twilio.twiml.voice_response import VoiceResponse
    dial_status = request.form.get("DialCallStatus", "")
    caller = request.form.get("From", "")
    called = request.form.get("To", "")
    resp = VoiceResponse()
    if dial_status != "completed":
        # Missed — text the caller back, then take a voicemail we'll transcribe.
        try:
            account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
            auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
            twilio_client = TwilioClient(account_sid, auth_token)
            twilio_client.messages.create(
                body="Hi, sorry I missed your call! I'm on site at the moment. Send me a quick message about the job and I'll get back to you as soon as I can.",
                from_=called, to=caller)
        except Exception as e:
            print("Missed-call SMS error: " + str(e))
        resp.say("Sorry, we can't take your call right now. Please leave a short message after the tone and we'll get back to you.")
        resp.record(action="/voicemail", method="POST", max_length=120, play_beep=True, timeout=4, trim="trim-silence")
    return str(resp)


@app.route("/voicemail", methods=["POST"])
def voicemail():
    """Caller left a voicemail: transcribe it with Whisper, log it as an enquiry, alert the owner."""
    from twilio.twiml.voice_response import VoiceResponse
    caller = request.form.get("From", "")
    called = request.form.get("To", "")
    recording_url = request.form.get("RecordingUrl", "")
    resp = VoiceResponse()
    resp.say("Thanks, we've got that and we'll be in touch shortly. Goodbye.")
    try:
        result = supabase.table("profiles").select("*").eq("twilio_number", called).execute()
        profile = result.data[0] if result.data else None
    except Exception:
        profile = None
    if not profile:
        return str(resp)
    snd = profile.get("sender", "")
    try:
        name = contact_name_for(snd, caller) or ""
    except Exception:
        name = ""
    transcript = ""
    if recording_url:
        try:
            import requests as _rq
            from openai import OpenAI
            import time
            sid = os.environ.get("TWILIO_ACCOUNT_SID")
            tok = os.environ.get("TWILIO_AUTH_TOKEN")
            audio = None
            for _attempt in range(2):  # the recording can take a moment to be ready
                audio = _rq.get(recording_url + ".mp3", auth=(sid, tok), timeout=30)
                if audio.ok and audio.content:
                    break
                time.sleep(2)
            if audio is not None and audio.ok and audio.content:
                path = "/tmp/vm_" + caller.replace("+", "").replace(":", "") + ".mp3"
                with open(path, "wb") as f:
                    f.write(audio.content)
                oc = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
                with open(path, "rb") as af:
                    tr = oc.audio.transcriptions.create(model="whisper-1", file=af)
                transcript = (getattr(tr, "text", "") or "").strip()
        except Exception as e:
            print("Voicemail transcription error:", e)
    try:
        if transcript:
            summary = "Voicemail from " + (name or caller) + ": " + transcript[:140]
            supabase.table("enquiries").insert({
                "sender": snd, "message": transcript, "summary": summary,
                "client_name": name, "job_type": "voicemail", "location": "", "status": "new"
            }).execute()
            notify_owner(profile, "New voicemail from " + (name or caller) + " (" + caller + "):\n\n\u201c" + transcript + "\u201d")
        else:
            supabase.table("enquiries").insert({
                "sender": snd, "message": "", "summary": "Missed call from " + caller,
                "client_name": name, "job_type": "missed call", "location": "", "status": "missed call"
            }).execute()
    except Exception as e:
        print("Voicemail logging error:", e)
    return str(resp)


@app.route("/call-recording", methods=["POST"])
def call_recording():
    """An answered call finished recording: transcribe it with Whisper and log a call summary."""
    caller = request.args.get("caller", "") or request.form.get("From", "")
    called = request.args.get("called", "") or request.form.get("To", "")
    recording_url = request.form.get("RecordingUrl", "")
    if not recording_url:
        return ("", 204)
    try:
        result = supabase.table("profiles").select("*").eq("twilio_number", called).execute()
        profile = result.data[0] if result.data else None
    except Exception:
        profile = None
    if not profile:
        return ("", 204)
    snd = profile.get("sender", "")
    try:
        name = contact_name_for(snd, caller) or ""
    except Exception:
        name = ""
    transcript = ""
    try:
        import requests as _rq
        from openai import OpenAI
        import time
        sid = os.environ.get("TWILIO_ACCOUNT_SID")
        tok = os.environ.get("TWILIO_AUTH_TOKEN")
        audio = None
        for _attempt in range(3):  # recording can take a few seconds to finalise
            audio = _rq.get(recording_url + ".mp3", auth=(sid, tok), timeout=45)
            if audio.ok and audio.content:
                break
            time.sleep(2)
        if audio is not None and audio.ok and audio.content:
            path = "/tmp/call_" + caller.replace("+", "").replace(":", "") + ".mp3"
            with open(path, "wb") as f:
                f.write(audio.content)
            oc = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
            with open(path, "rb") as af:
                tr = oc.audio.transcriptions.create(model="whisper-1", file=af)
            transcript = (getattr(tr, "text", "") or "").strip()
    except Exception as e:
        print("Call recording transcription error:", e)
    if transcript:
        try:
            summary = "Call with " + (name or caller) + ": " + transcript[:140]
            # status 'call' (not 'new') so answered calls don't inflate the new-enquiry count
            supabase.table("enquiries").insert({
                "sender": snd, "message": transcript, "summary": summary,
                "client_name": name, "job_type": "call", "location": "", "status": "call"
            }).execute()
            notify_owner(profile, "Call summary \u2014 " + (name or caller) + " (" + caller + "):\n\n\u201c" + transcript + "\u201d")
        except Exception as e:
            print("Call recording logging error:", e)
    return ("", 204)


@app.route("/auth/gmail")
def gmail_auth():
    phone = request.args.get("phone", "")
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        {"web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI]
        }},
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        redirect_uri=GOOGLE_REDIRECT_URI
    )
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent", state=phone)
    return redirect(auth_url)


@app.route("/auth/gmail/callback")
def gmail_callback():
    code = request.args.get("code", "")
    phone = request.args.get("state", "")

    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        {"web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI]
        }},
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        redirect_uri=GOOGLE_REDIRECT_URI
    )
    flow.fetch_token(code=code)
    creds = flow.credentials

    phone = format_phone(phone)
    supabase.table("profiles").update({
        "gmail_token": creds.token,
        "gmail_refresh_token": creds.refresh_token
    }).eq("phone", phone).execute()

    return "<html><body style='background:#0c0c0c;color:#fff;font-family:Inter,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;text-align:center'><div><h1 style='color:#5b6cff'>Gmail Connected!</h1><p style='color:#888;margin-top:16px'>You can close this window and go back to your dashboard.</p></div></body></html>"


@app.route("/auth/outlook")
def outlook_auth():
    phone = request.args.get("phone", "").strip()
    digits = "".join(c for c in phone if c.isdigit())
    auth_url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    auth_url += "?client_id=" + MICROSOFT_CLIENT_ID
    auth_url += "&redirect_uri=" + MICROSOFT_REDIRECT_URI
    auth_url += "&response_type=code"
    auth_url += "&scope=Mail.Read offline_access"
    auth_url += "&state=" + digits
    return redirect(auth_url)


@app.route("/auth/outlook/callback")
def outlook_callback():
    code = request.args.get("code", "")
    phone = "+" + request.args.get("state", "").strip()
    import requests as req
    token_response = req.post("https://login.microsoftonline.com/common/oauth2/v2.0/token", data={
        "code": code,
        "client_id": MICROSOFT_CLIENT_ID,
        "client_secret": MICROSOFT_CLIENT_SECRET,
        "redirect_uri": MICROSOFT_REDIRECT_URI,
        "grant_type": "authorization_code",
        "scope": "Mail.Read offline_access"
    })
    tokens = token_response.json()
    if "access_token" not in tokens:
        return "Error connecting Outlook: " + str(tokens.get("error_description", "Unknown error")), 400
    phone = format_phone(phone)
    supabase.table("profiles").update({
        "outlook_token": tokens.get("access_token", ""),
        "outlook_refresh_token": tokens.get("refresh_token", "")
    }).eq("phone", phone).execute()
    return "<html><body style='background:#0c0c0c;color:#fff;font-family:Inter,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;text-align:center'><div><h1 style='color:#5b6cff'>Outlook Connected!</h1><p style='color:#888;margin-top:16px'>You can close this window and go back to your dashboard.</p></div></body></html>"


@app.route("/health")
def health():
    status = {"status": "ok", "weasyprint": False}
    pdfshift_key = os.environ.get("PDFSHIFT_API_KEY", "")
    status["pdfshift"] = bool(pdfshift_key)
    return jsonify(status)


@app.route("/dashboard")
def dashboard():
    with open("dashboard.html", "r") as f:
        html = f.read()
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


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
        _snd = profile.get("sender", "")
        new_enq = supabase.table("enquiries").select("*").eq("sender", _snd).eq("status", "new").execute().data
        missed = supabase.table("enquiries").select("*").eq("sender", _snd).eq("status", "missed call").execute().data
        quoted = supabase.table("enquiries").select("*").eq("sender", _snd).eq("status", "quoted").execute().data
        recent = supabase.table("enquiries").select("*").eq("sender", _snd).order("created_at", desc=True).limit(20).execute().data
        bookings = supabase.table("bookings").select("*").eq("sender", profile.get("sender", "")).order("date").execute().data
        template_result = supabase.table("quote_templates").select("*").eq("sender", profile.get("sender", "")).execute()
        template = template_result.data[0] if template_result.data else None
        emails_result = supabase.table("emails").select("*").eq("sender", profile.get("sender", "")).eq("read", False).order("created_at", desc=True).limit(10).execute()
        emails = emails_result.data if emails_result.data else []
        invoices_result = supabase.table("invoices").select("*").eq("sender", profile.get("sender", "")).order("created_at", desc=True).execute()
        invoices = invoices_result.data if invoices_result.data else []
        quotes_result = supabase.table("quotes").select("*").eq("sender", profile.get("sender", "")).order("created_at", desc=True).execute()
        quotes = quotes_result.data if quotes_result.data else []
        gmail_connected = bool(profile.get("gmail_token"))
        outlook_connected = bool(profile.get("outlook_token"))
        return jsonify({
            "enquiries": len(new_enq), "unpaid": len(quoted), "missed": len(missed),
            "recent": recent, "bookings": bookings, "profile": profile, "template": template,
            "emails": emails, "invoices": invoices, "quotes": quotes, "gmail_connected": gmail_connected, "outlook_connected": outlook_connected
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
        twilio_client.messages.create(body="Your VanOffice reset code is: " + code, from_=twilio_number, to=phone)
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


@app.route("/api/save-template", methods=["POST"])
def save_template():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        if not phone or not pin:
            return jsonify({"error": "Phone and PIN required"}), 401
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data:
            return jsonify({"error": "Profile not found"}), 404
        profile = result.data[0]
        if str(profile.get("pin", "")) != str(pin):
            return jsonify({"error": "Invalid PIN"}), 401
        sender = profile.get("sender", "")
        data = request.json
        existing = supabase.table("quote_templates").select("*").eq("sender", sender).execute()
        if existing.data:
            supabase.table("quote_templates").update(data).eq("sender", sender).execute()
        else:
            data["sender"] = sender
            supabase.table("quote_templates").insert(data).execute()
        return jsonify({"success": True})
    except Exception as e:
        print("Save template error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/quote-view/<quote_id>")
def quote_view(quote_id):
    try:
        quote_result = supabase.table("quotes").select("*").eq("id", quote_id).execute()
        if not quote_result.data:
            return "Quote not found", 404
        quote = quote_result.data[0]
        profile_result = supabase.table("profiles").select("*").eq("sender", quote["sender"]).execute()
        profile = profile_result.data[0] if profile_result.data else {}
        template_result = supabase.table("quote_templates").select("*").eq("sender", quote["sender"]).execute()
        template = template_result.data[0] if template_result.data else None
        html = build_quote_html(quote, profile, template, is_invoice=False)
        return Response(html, mimetype="text/html")
    except Exception as e:
        print("Quote view error: " + str(e))
        return "Error generating quote: " + str(e), 500


@app.route("/generate-pdf/<quote_id>")
def serve_pdf(quote_id):
    try:
        quote_result = supabase.table("quotes").select("*").eq("id", quote_id).execute()
        if not quote_result.data:
            return "Quote not found", 404
        quote = quote_result.data[0]
        profile_result = supabase.table("profiles").select("*").eq("sender", quote["sender"]).execute()
        profile = profile_result.data[0] if profile_result.data else {}
        template_result = supabase.table("quote_templates").select("*").eq("sender", quote["sender"]).execute()
        template = template_result.data[0] if template_result.data else None
        pdf_buffer = generate_quote_pdf(quote, profile, template)
        return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name="Quote-" + quote.get("quote_number", "001") + ".pdf")
    except Exception as e:
        print("PDF error: " + str(e))
        print(traceback.format_exc())
        return "Error generating PDF: " + str(e), 500


@app.route("/invoice-view/<invoice_id>")
def invoice_view(invoice_id):
    try:
        inv_result = supabase.table("invoices").select("*").eq("id", invoice_id).execute()
        if not inv_result.data:
            return "Invoice not found", 404
        invoice = inv_result.data[0]
        profile_result = supabase.table("profiles").select("*").eq("sender", invoice["sender"]).execute()
        profile = profile_result.data[0] if profile_result.data else {}
        template_result = supabase.table("quote_templates").select("*").eq("sender", invoice["sender"]).execute()
        template = template_result.data[0] if template_result.data else None
        html = build_quote_html(invoice, profile, template, is_invoice=True)
        return Response(html, mimetype="text/html")
    except Exception as e:
        print("Invoice view error: " + str(e))
        return "Error generating invoice: " + str(e), 500


@app.route("/generate-invoice-pdf/<invoice_id>")
def serve_invoice_pdf(invoice_id):
    try:
        inv_result = supabase.table("invoices").select("*").eq("id", invoice_id).execute()
        if not inv_result.data:
            return "Invoice not found", 404
        invoice = inv_result.data[0]
        profile_result = supabase.table("profiles").select("*").eq("sender", invoice["sender"]).execute()
        profile = profile_result.data[0] if profile_result.data else {}
        template_result = supabase.table("quote_templates").select("*").eq("sender", invoice["sender"]).execute()
        template = template_result.data[0] if template_result.data else None
        pdf_buffer = generate_invoice_pdf(invoice, profile, template)
        return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name="Invoice-" + invoice.get("invoice_number", "001") + ".pdf")
    except Exception as e:
        print("Invoice PDF error: " + str(e))
        print(traceback.format_exc())
        return "Error generating invoice PDF: " + str(e), 500


@app.route("/sms", methods=["POST"])
def incoming_sms():
    incoming_msg = request.form.get("Body", "").strip()
    client_number = request.form.get("From", "")
    twilio_number = request.form.get("To", "")

    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()

    try:
        # Find which tradesperson owns this number
        profile_result = supabase.table("profiles").select("*").eq("twilio_number", twilio_number).execute()
        if not profile_result.data:
            resp.message("Sorry, this number is not currently active.")
            return str(resp)

        profile = profile_result.data[0]
        sender = profile.get("sender", "")
        biz_name = profile.get("business_name", "the business")
        owner_name = profile.get("owner_name", "")

        # Save incoming message
        supabase.table("client_chats").insert({
            "twilio_number": twilio_number,
            "client_number": client_number,
            "message": incoming_msg,
            "direction": "inbound",
            "sender_profile": sender,
            "channel": "sms"
        }).execute()
        # Remember this client's number (name filled in when we learn it)
        upsert_contact(sender, client_number)

        # Get conversation history with this client
        history = supabase.table("client_chats").select("*").eq("twilio_number", twilio_number).eq("client_number", client_number).order("created_at").execute().data
        chat_messages = []
        for h in history[-10:]:
            if h.get("direction") == "inbound":
                chat_messages.append({"role": "user", "content": h.get("message", "")})
            else:
                chat_messages.append({"role": "assistant", "content": h.get("message", "")})

        # Pull the diary so the bot can propose / confirm real times.
        today_iso = datetime.date.today().isoformat()
        upcoming = supabase.table("bookings").select("*").eq("sender", sender).gte("date", today_iso).order("date").limit(20).execute().data or []
        diary_lines = []
        for b in upcoming[:15]:
            diary_lines.append((b.get("date","") or "") + " " + (b.get("time","") or "") + " - " + (b.get("client_name","") or "job"))
        diary_text = "\n".join(diary_lines) if diary_lines else "Diary is currently clear."

        today_name = datetime.date.today().strftime("%A %d %B %Y")
        system = "You are the customer-facing assistant for " + biz_name + ", a " + str(profile.get("trade","")) + " business run by " + owner_name + ".\n"
        system += "Today is " + today_name + ". You are texting a CUSTOMER who contacted the business.\n\n"
        system += "PERSONALITY: Think for yourself and hold a real, natural back-and-forth like a sharp office manager would. "
        system += "Never give robotic brush-offs like 'I'll get " + owner_name + " to call you' unless you genuinely need to escalate (see below). "
        system += "Speak as 'we', warm and concise (this is SMS). Never pretend to BE " + owner_name + " personally.\n\n"
        system += "THE DIARY (upcoming jobs):\n" + diary_text + "\n\n"
        system += "YOU CAN HANDLE THESE YOURSELF:\n"
        system += "- Simple questions: opening hours, areas covered, what trade/work we do.\n"
        system += "- Gathering details for a quote: what the job is, where, rough timing.\n"
        system += "- You do NOT book, confirm or agree appointment times yourself \u2014 anything about timing always goes to " + owner_name + " (see the critical rule below), even if a slot looks free in the diary.\n\n"
        system += "GET THEIR NAME: early in a new conversation, if you don't already know who you're speaking to, politely ask for their name (e.g. 'Happy to help — can I take your name?'). Work it into the chat naturally, don't interrogate. The moment you know it, add the CONTACT: line described below.\n\n"
        system += "CRITICAL RULE ON TIMES & BOOKINGS:\n"
        system += "You must NEVER confirm, agree, or commit to a specific appointment time yourself. "
        system += owner_name + " has a personal life the work diary does not show, so ONLY " + owner_name + " can approve a time.\n"
        system += "This applies EVERY time, on EVERY message \u2014 including when the customer proposes a specific slot, replies with more timing detail, or you have already said you'd check the diary. Never decide it's fine just because the slot looks free.\n"
        system += "NEVER send a confirmation. Do NOT say things like 'I'll pop you in', 'you're booked', 'booked you in', 'I'll put you down for', 'see you then', 'see you Wednesday', 'all set' or state a confirmed time. If the customer proposes a time, you HAND IT OVER \u2014 you do not accept it.\n"
        system += "Whenever the conversation reaches ANY of these, you hand the decision to " + owner_name + ":\n"
        system += "- the customer asks when you can come / proposes or asks about timing / wants to book or rearrange;\n"
        system += "- ANYTHING about price, cost, money, deposits, or discounts;\n"
        system += "- complaints or an unhappy customer; emergencies/urgent; anything you are unsure about.\n\n"
        system += "TO HAND IT OVER: send the customer a warm, honest holding line that does NOT promise a specific time or answer "
        system += "(e.g. 'Let me check with " + owner_name + "\u2019s diary and come right back to you on that'). "
        system += "Then on its OWN FINAL LINE add:\n"
        system += "NEEDYOU:reason=<short reason e.g. choose a day to view>|options=<2-5 short choices " + owner_name + " could pick, separated by commas>\n"
        system += "The options must be SMART and based on what the customer said. Example: if they say 'any evening next week', options could be: "
        system += "Mon eve,Tue eve,Wed eve,Thu eve,Fri eve. If they ask a price, options could be: Send rough quote,Arrange a call,I\u2019ll reply myself. "
        system += "Always make the LAST option a sensible catch-all. Never invent the answer yourself \u2014 the options are for " + owner_name + " to choose from.\n\n"
        system += "WHEN YOU HAVE ENOUGH JOB DETAIL for a quote (job, location, rough timing) and it is NOT a timing/price/booking moment, end with:\n"
        system += "NEWJOB:name=<name or Unknown>|job=<job type>|location=<location>\n\n"
        system += "IF THE CUSTOMER TELLS YOU THEIR NAME at any point (e.g. 'it's Dave', 'this is Sarah Jones'), add on its OWN FINAL LINE:\n"
        system += "CONTACT:name=<their name>\n"
        system += "Always include CONTACT: once you know their name, even on a later message. The customer NEVER sees it.\n\n"
        system += "Keep replies short and human (SMS). Put any tag (NEEDYOU/NEWJOB) on its own final line; the customer NEVER sees the tags."

        ai_response = client.messages.create(model="claude-sonnet-4-5", max_tokens=400, system=system, messages=chat_messages)
        reply = ai_response.content[0].text.strip()

        # SAFETY NET: the bot must never tell a customer a time is booked. If a
        # commitment phrase slips through and it didn't already hand over, swap it
        # for a holding line and force the decision to the owner.
        import re as _re_commit
        _COMMIT = _re_commit.compile(
            r"\b(i'?ll pop you in|pop you in|booked you in|you'?re booked|all booked|"
            r"i'?ll put you down|put you down for|confirmed for|i'?ve booked|i'?ll book you|"
            r"you'?re all set|pencill?ed you in|locked in|see you (then|on |at |mon|tue|wed|thu|fri|sat|sun))",
            _re_commit.I)
        if _COMMIT.search(reply) and "NEEDYOU:" not in reply:
            reply = ("Thanks \u2014 let me just check " + (owner_name or "the diary") +
                     "'s diary and I'll come right back to you to confirm.\n"
                     "NEEDYOU:reason=customer proposed a time, confirm it or suggest another"
                     "|options=Confirm the time,Suggest another time,I\u2019ll reply myself")

        # Check if client is confirming payment
        overdue = supabase.table("invoices").select("*").eq("sender", sender).in_("status", ["unpaid", "overdue"]).execute()
        client_invoices = [i for i in (overdue.data or []) if i.get("client_number") == client_number]
        if client_invoices:
            most_recent = sorted(client_invoices, key=lambda x: x.get("due_date", ""), reverse=True)[0]
            payment_check = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=10,
                system="You detect if a message means someone has paid an invoice. Reply with only YES or NO.",
                messages=[{"role": "user", "content": incoming_msg}]
            )
            if payment_check.content[0].text.strip().upper() == "YES":
                inv_id = most_recent["id"]
                inv_num = most_recent.get("invoice_number", "")
                total = most_recent.get("total", "")
                supabase.table("invoices").update({"status": "paid"}).eq("id", inv_id).execute()
                notify_owner(profile, "Invoice " + inv_num + " for £" + str(total) + " marked as PAID - client confirmed via message.")
        
        # Check for new job extraction
        if "NEWJOB:" in reply:
            try:
                job_line = reply.split("NEWJOB:")[1].strip().split("\n")[0]
                parts = dict(p.split("=") for p in job_line.split("|"))
                _nm = (parts.get("name", "") or "").strip()
                # prefer a name we already have on file for this number
                _known = contact_name_for(sender, client_number)
                if _known and (not _nm or _nm.lower() in _CONTACT_NONAMES):
                    _nm = _known
                supabase.table("enquiries").insert({
                    "sender": sender,
                    "message": "Client SMS from " + client_number,
                    "summary": "New enquiry via text from " + client_number,
                    "client_name": _nm or "Unknown",
                    "job_type": parts.get("job", ""),
                    "location": parts.get("location", ""),
                    "status": "new"
                }).execute()
                if _nm:
                    upsert_contact(sender, client_number, _nm)

                # Notify tradesperson (SMS-first)
                notify_owner(profile, "New enquiry: " + parts.get("name", "Unknown") + " - " + parts.get("job", "") + " - " + parts.get("location", "") + " (from " + client_number + "). Logged in VanOffice.")
            except Exception as e:
                print("Job extraction error: " + str(e))

        # ── CONTACT: customer told us their name — save it against their number ──
        if "CONTACT:" in reply:
            try:
                cl = reply.split("CONTACT:")[1].strip().split("\n")[0]
                cp = dict(p.split("=", 1) for p in cl.split("|") if "=" in p)
                nm = (cp.get("name", "") or "").strip()
                if nm:
                    upsert_contact(sender, client_number, nm)
            except Exception as e:
                print("CONTACT parse error:", e)

        # ── NEEDYOU: bot is handing a decision to the owner — park it + nudge, never auto-commit ──
        if "NEEDYOU:" in reply:
            try:
                nl = reply.split("NEEDYOU:")[1].strip().split("\n")[0]
                np = dict(p.split("=", 1) for p in nl.split("|") if "=" in p)
                reason = (np.get("reason", "") or "needs your reply").strip()
                opts_raw = (np.get("options", "") or "").strip()
                options = [o.strip() for o in opts_raw.split(",") if o.strip()]
                if not options:
                    options = ["Arrange a call", "I\u2019ll reply myself"]
                # best-known client name from enquiries
                cname = ""
                try:
                    enq = supabase.table("enquiries").select("client_name").eq("sender", sender).order("created_at", desc=True).limit(5).execute().data or []
                    for e in enq:
                        if e.get("client_name"):
                            cname = e["client_name"]; break
                except Exception:
                    pass
                # park the pending decision for the dashboard
                supabase.table("pending_actions").insert({
                    "sender": sender, "client_number": client_number, "client_name": cname,
                    "twilio_number": twilio_number, "kind": "decision",
                    "customer_msg": incoming_msg, "reason": reason,
                    "options": options, "status": "pending"
                }).execute()
                # nudge the owner (SMS-first, works today)
                who = cname or client_number
                notify_owner(profile, "\u26A0 " + who + " needs you: " + reason + ". Open VanOffice > Inbox to choose what to send.")
            except Exception as e:
                print("NEEDYOU parse error:", e)

        # Strip ALL control tags so the customer only sees the human-readable reply.
        # Cut at the EARLIEST tag so nothing leaks if several are present.
        _cut = len(reply)
        for _tag in ("NEWJOB:", "NEEDYOU:", "BOOKED:", "ESCALATE:", "CONTACT:"):
            _i = reply.find(_tag)
            if _i != -1:
                _cut = min(_cut, _i)
        clean_reply = reply[:_cut].strip()
        if not clean_reply:
            clean_reply = "Thanks for your message \u2014 we\u2019ll be in touch shortly."

        # Save outgoing message
        supabase.table("client_chats").insert({
            "twilio_number": twilio_number,
            "client_number": client_number,
            "message": clean_reply,
            "direction": "outbound",
            "sender_profile": sender,
            "channel": "sms"
        }).execute()

        resp.message(clean_reply)

    except Exception as e:
        print("SMS handler error: " + str(e))
        resp.message("Thanks for your message. We will get back to you shortly.")

    return str(resp)

@app.route("/api/preview-quote", methods=["POST"])
def preview_quote():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]

        data = request.json or {}
        style = data.get("style", {})
        design_style = data.get("designStyle", "gold")
        quote_data = data.get("quoteData", None)

        template_result = supabase.table("quote_templates").select("*").eq("sender", profile.get("sender","")).execute()
        saved_template = template_result.data[0] if template_result.data else {}

        template = {
            "design_style": design_style,
            "design_template": json.dumps(style) if style else saved_template.get("design_template", "{}")
        }

        if quote_data:
            scope_items = quote_data.get("scopeItems", [])
            sample_quote = {
                "client_name": quote_data.get("clientName", "Example Client"),
                "client_address": "",
                "total": str(quote_data.get("totalPrice", "0")),
                "quote_number": "QU-001",
                "line_items": scope_items
            }
        else:
            sample_quote = {
                "client_name": "Example Client",
                "client_address": "123 High Street, Devon",
                "total": "1,900.00",
                "quote_number": "QU-001",
                "line_items": style.get("scopeItems", [])
            }

        html = build_quote_html(sample_quote, profile, template, is_invoice=False)
        return jsonify({"html": html})
    except Exception as e:
        print("Preview quote error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-quote-html", methods=["POST"])
def generate_quote_html():
    """Generate a bespoke HTML quote template using Claude."""
    try:
        data = request.json
        phone = format_phone(data.get("phone", "").strip())
        pin = data.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")

        style = data.get("style", {})
        quote = data.get("quote", {})

        biz_name = style.get("bizName", profile.get("business_name", "Your Business"))
        trade = style.get("trade", profile.get("trade", "Tradesperson"))
        phone_num = style.get("phone", profile.get("phone", ""))
        email = style.get("email", "")
        location = style.get("location", "")
        accent = style.get("accent", "#c9a227")
        dark = style.get("dark", "#1a1a1a")
        scope_items = style.get("scopeItems", [])
        inclusion_items = style.get("inclusionItems", [])
        note = style.get("note", "")
        commitment = style.get("commitment", "")
        lead_time = style.get("leadTime", "")
        payment_terms = style.get("paymentTerms", "")
        show_note = style.get("showNote", True)
        show_commitment = style.get("showCommitment", True)
        show_lead_time = style.get("showLeadTime", True)
        show_vat = style.get("showVat", False)
        intro = style.get("intro", "Thank you for the opportunity to provide this quotation.")

        client_name = quote.get("client_name", "Client Name")
        client_address = quote.get("client_address", "")
        total = quote.get("total", "0")
        quote_num = quote.get("quote_number", "QU-001")
        today_str = datetime.date.today().strftime("%d %B %Y")

        logo_data = profile.get("logo", "")

        scope_text = "\n".join([f"- {item}" for item in scope_items])
        inc_text = "\n".join([f"- {item}" for item in inclusion_items])

        logo_tag = f'<img src="{logo_data}" style="width:70px;height:70px;object-fit:contain">' if logo_data else f'<svg width="60" height="60" viewBox="0 0 60 60"><circle cx="30" cy="30" r="28" fill="none" stroke="{accent}" stroke-width="2"/><text x="30" y="36" text-anchor="middle" fill="{accent}" font-size="18" font-weight="bold">{biz_name[:2].upper()}</text></svg>'

        scope_html = "".join([f'<div style="display:flex;gap:8px;margin-bottom:6px;font-size:11px"><span style="color:{accent}">✓</span>{item}</div>' for item in scope_items[:6]])
        inc_html = "".join([f'<div style="display:flex;gap:8px;margin-bottom:5px;font-size:11px"><span style="color:{accent}">✓</span>{item}</div>' for item in inclusion_items[:5]])
        note_html = f'<div style="border:1px solid {accent};border-radius:4px;padding:10px;margin-top:10px;font-size:10px;color:#555">{note}</div>' if show_note and note else ""
        vat_html = f'<div style="font-size:11px;color:{accent};margin-top:4px">+ VAT</div>' if show_vat else ""
        commitment_html = f'<div><div style="font-size:9px;font-weight:700;color:{accent};text-transform:uppercase;margin-bottom:3px">OUR COMMITMENT</div><div style="font-size:9px;color:#aaa">{commitment}</div></div>' if show_commitment and commitment else ""
        lead_html = f'<div><div style="font-size:9px;font-weight:700;color:{accent};text-transform:uppercase;margin-bottom:3px">LEAD TIME</div><div style="font-size:9px;color:#aaa">{lead_time}</div></div>' if show_lead_time and lead_time else ""

        prompt = f"""Create a professional A4 quote HTML for: {biz_name} ({trade}). Accent: {accent}, Dark: {dark}.
Header: 2 cols, dark bg. Left: logo + business name. Right: QUOTATION + contact ({phone_num}, {email}, {location}).
Body: 2 cols. Left: Date {today_str}, Client {client_name}, intro, SCOPE OF WORKS, items, note. Right: dark bg, £ circle, TOTAL PRICE, £{total}{", +VAT" if show_vat else ""}, INCLUSIONS.
Footer: dark bg, commitment + lead time. Thank you bar.
Use inline CSS only. A4 width. Google Fonts Inter. Premium Canva-style design. Return ONLY raw HTML, no markdown."""

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )

        html = response.content[0].text.strip()
        if html.startswith("```"):
            html = html.split("\n", 1)[1]
            html = html.rsplit("```", 1)[0]

        # Save the generated HTML template
        existing = supabase.table("quote_templates").select("*").eq("sender", sender).execute()
        save_data = {
            "sender": sender,
            "design_style": "custom_html",
            "design_template": json.dumps(style),
            "generated_html": html,
            "brand_colour": accent,
            "business_name": biz_name,
            "business_phone": phone_num,
            "business_email": email,
            "business_address": location,
        }
        if existing.data:
            supabase.table("quote_templates").update(save_data).eq("sender", sender).execute()
        else:
            supabase.table("quote_templates").insert(save_data).execute()

        return jsonify({"ok": True, "html": html})
    except Exception as e:
        print("Generate quote HTML error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-logo", methods=["POST"])
def generate_logo():
    try:
        from openai import OpenAI
        data = request.json
        phone = format_phone(data.get("phone", "").strip())
        pin = data.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401

        prompt = data.get("prompt", "")
        trade = data.get("trade", "tradesperson")
        biz_name = data.get("biz_name", "")

        if not prompt:
            prompt = f"A professional logo icon for {biz_name}, a {trade} business. Clean, modern, single colour on white background. No text, just the symbol or icon."

        openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        response = openai_client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1
        )
        url = response.data[0].url
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        print("Generate logo error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/save-logo", methods=["POST"])
def save_logo():
    try:
        import requests as req
        import base64
        data = request.json
        phone = format_phone(data.get("phone", "").strip())
        pin = data.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")

        logo_url = data.get("logo_url", "")
        if not logo_url:
            return jsonify({"error": "No logo URL"}), 400

        img_response = req.get(logo_url, timeout=10)
        if img_response.status_code != 200:
            return jsonify({"error": "Could not fetch logo"}), 400

        img_b64 = "data:image/png;base64," + base64.b64encode(img_response.content).decode()
        supabase.table("profiles").update({"logo": img_b64}).eq("sender", sender).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print("Save logo error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/design-template", methods=["POST"])
def design_template():
    try:
        data = request.json
        messages = data.get("messages", [])
        system = data.get("system", "")
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=system,
            messages=messages
        )
        return jsonify({"content": [{"text": response.content[0].text}]})
    except Exception as e:
        err = str(e)
        print("Design template error: " + err)
        if "Could not process image" in err or "invalid" in err.lower():
            return jsonify({"error": "Could not read the file. Please try a clearer PDF or JPG image of your quote."}), 400
        if "too large" in err.lower() or "size" in err.lower():
            return jsonify({"error": "File too large for analysis. Try compressing it or use a JPG screenshot instead."}), 400
        return jsonify({"error": "Analysis failed: " + err}), 500


@app.route("/api/save-design-template", methods=["POST"])
def save_design_template():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        data = request.json
        style = data.get("style", "george")
        template_data = data.get("template", {})
        existing = supabase.table("quote_templates").select("*").eq("sender", sender).execute()
        save_data = {
            "sender": sender,
            "design_style": style,
            "design_template": json.dumps(template_data),
            "brand_colour": template_data.get("accent", "#1a1a2e"),
            "business_name": template_data.get("bizName", ""),
            "business_phone": template_data.get("phone", ""),
            "business_email": template_data.get("email", ""),
            "business_address": template_data.get("location", ""),
            "footer_text": template_data.get("commitment", ""),
            "terms": template_data.get("paymentTerms", ""),
        }
        if existing.data:
            supabase.table("quote_templates").update(save_data).eq("sender", sender).execute()
        else:
            supabase.table("quote_templates").insert(save_data).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print("Save design template error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/save-profile", methods=["POST"])
def save_profile():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        data = request.json or {}
        update = {}
        fields = ["business_name", "trade", "location", "day_rate", "half_day_rate", "hourly_rate",
                  "materials_markup", "payment_terms", "vat_registered", "logo"]
        for f in fields:
            if f in data and data[f] is not None:
                update[f] = data[f]
        if update:
            supabase.table("profiles").update(update).eq("sender", profile["sender"]).execute()
        return jsonify({"success": True})
    except Exception as e:
        print("Save profile error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/create-quote", methods=["POST"])
def create_quote():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        data = request.json
        quote_count = supabase.table("quotes").select("id").eq("sender", sender).execute()
        quote_num = "QU-" + str(len(quote_count.data) + 1).zfill(3)
        quote = {
            "sender": sender,
            "client_name": data.get("client_name", ""),
            "client_address": data.get("client_address", ""),
            "job_description": data.get("job_description", ""),
            "total": str(data.get("total", "0")),
            "subtotal": str(data.get("subtotal", data.get("total", "0"))),
            "vat": "0",
            "line_items": data.get("line_items", []),
            "status": "sent",
            "quote_number": quote_num,
            "quote_text": "",
            "client_number": data.get("client_number", "")
        }
        res = supabase.table("quotes").insert(quote).execute()
        saved = res.data[0] if res.data else quote
        return jsonify({"ok": True, "quote": saved})
    except Exception as e:
        print("Create quote error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/create-invoice", methods=["POST"])
def create_invoice():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        data = request.json
        inv_count = supabase.table("invoices").select("id").eq("sender", sender).execute()
        inv_num = "INV-" + str(len(inv_count.data) + 1).zfill(3)
        invoice = {
            "sender": sender,
            "client_name": data.get("client_name", ""),
            "client_address": data.get("client_address", ""),
            "job_description": data.get("job_description", ""),
            "total": str(data.get("total", "0")),
            "subtotal": str(data.get("subtotal", data.get("total", "0"))),
            "vat": "0",
            "line_items": data.get("line_items", []),
            "status": "unpaid",
            "invoice_number": inv_num,
            "due_date": data.get("due_date", ""),
            "client_number": data.get("client_number", ""),
            "invoice_text": ""
        }
        res = supabase.table("invoices").insert(invoice).execute()
        saved = res.data[0] if res.data else invoice
        return jsonify({"ok": True, "invoice": saved})
    except Exception as e:
        print("Create invoice error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/invoices/<int:invoice_id>/mark-paid", methods=["POST"])
def mark_invoice_paid(invoice_id):
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401

        supabase.table("invoices").update({"status": "paid"}).eq("id", invoice_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/chat", methods=["POST"])
def api_chat():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        if not phone or not pin:
            return jsonify({"error": "Auth required"}), 401
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data:
            return jsonify({"error": "Profile not found"}), 404
        profile = result.data[0]
        if str(profile.get("pin", "")) != str(pin):
            return jsonify({"error": "Invalid PIN"}), 401

        data = request.json
        message = data.get("message", "")
        history = data.get("history", [])

        sender = profile.get("sender", "")
        enquiries = supabase.table("enquiries").select("*").eq("sender", sender).order("created_at", desc=True).limit(10).execute().data
        bookings = supabase.table("bookings").select("*").eq("sender", sender).order("date").execute().data
        quotes = supabase.table("quotes").select("*").eq("sender", sender).order("created_at", desc=True).limit(10).execute().data
        invoices = supabase.table("invoices").select("*").eq("sender", sender).order("created_at", desc=True).limit(10).execute().data

        system = "You are VanOffice AI, a helpful assistant for a tradesperson. Be friendly, concise and practical.\n\n"
        system += "User details:\n"
        system += "Name: " + str(profile.get("owner_name")) + "\n"
        system += "Business: " + str(profile.get("business_name")) + "\n"
        system += "Trade: " + str(profile.get("trade")) + "\n"
        system += "Day rate: " + str(profile.get("day_rate")) + "\n"
        system += "Hourly rate: " + str(profile.get("hourly_rate")) + "\n\n"

        if enquiries:
            system += "Recent enquiries:\n"
            for e in enquiries[:5]:
                system += "- " + e.get("client_name", "") + " - " + e.get("job_type", "") + " - " + e.get("status", "") + "\n"
            system += "\n"

        if bookings:
            system += "Booked jobs:\n"
            for b in bookings[:5]:
                system += "- " + b.get("client_name", "") + " - " + b.get("job_type", "") + " - " + b.get("date", "") + "\n"
            system += "\n"

        if quotes:
            system += "Recent quotes:\n"
            for q in quotes[:5]:
                system += "- " + q.get("quote_number", "") + " - " + q.get("client_name", "") + " - " + q.get("total", "") + " - " + q.get("status", "") + "\n"
            system += "\n"

        if invoices:
            system += "Recent invoices:\n"
            for i in invoices[:5]:
                system += "- " + i.get("invoice_number", "") + " - " + i.get("client_name", "") + " - " + i.get("total", "") + " - " + i.get("status", "") + "\n"
            system += "\n"

        system += "Help the user with any questions about their business, jobs, quotes, invoices, scheduling or general trade advice."

        messages = []
        for h in history[-10:]:
            messages.append({"role": h["role"], "content": h["content"]})

        response = client.messages.create(model="claude-sonnet-4-5", max_tokens=1000, system=system, messages=messages)
        reply = response.content[0].text.strip()

        return jsonify({"reply": reply})

    except Exception as e:
        print("Chat API error: " + str(e))
        return jsonify({"error": str(e)}), 500



# ════════════════════════════════════════════════════════════════
#  AI ASSISTANT — conversational PA with tool-calling
# ════════════════════════════════════════════════════════════════
ASSISTANT_TOOLS = [
    {
        "name": "create_invoice",
        "description": "Create an invoice for a client. If the job was already quoted, you do NOT need an amount — leave total out and it will be taken from the client's saved quote (with the same line items). Only pass total if the user states a specific figure or there is no quote on file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string", "description": "Customer's name"},
                "total": {"type": "string", "description": "Total amount in pounds, digits only e.g. '450'. Omit to use the saved quote's figure."},
                "job_description": {"type": "string", "description": "Short description of the work"}
            },
            "required": ["client_name"]
        }
    },
    {
        "name": "create_quote",
        "description": "Build and save a detailed itemised quote for a client. Estimates materials and labour and produces a full breakdown automatically. Pass the job description with as much detail as known. Only pass materials_cost or labour_days if the user explicitly stated their own figures.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string"},
                "job_description": {"type": "string", "description": "What the work involves"},
                "materials_cost": {"type": "number", "description": "Only if the user stated their own materials cost in pounds"},
                "labour_days": {"type": "number", "description": "Only if the user stated the number of labour days"}
            },
            "required": ["client_name", "job_description"]
        }
    },
    {
        "name": "get_schedule",
        "description": "Look up the user's upcoming jobs/bookings. Use for questions like 'what's on tomorrow' or 'what have I got this week'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": "Natural description of the time period e.g. 'today', 'tomorrow', 'this week'"}
            }
        }
    },
    {
        "name": "add_booking",
        "description": "Add a job/booking to the diary. Resolve relative dates like 'next Wednesday' or 'tomorrow' to an absolute ISO date (YYYY-MM-DD) using today's date. If the user references an existing job/client, call find_quote first and pass that job's description here.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string"},
                "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "time": {"type": "string", "description": "e.g. '09:00'"},
                "description": {"type": "string", "description": "What the job is (e.g. 'fit 4 oak doors'). If known from an existing quote, use that."},
                "location": {"type": "string", "description": "Job address/area if known"},
                "duration_days": {"type": "string", "description": "How many days the job will take, digits only e.g. '2'. Default '1'."}
            },
            "required": ["client_name", "date"]
        }
    },
    {
        "name": "add_quote_extra",
        "description": "Add an agreed EXTRA (variation) to a client's existing saved quote — e.g. 'add an extra socket for £45 to Dave's quote', 'put £120 of extra tiling on the Patel job', 'stick another day's labour on the Smith quote at £220'. Finds the most recent quote for that client, adds the extra as a line item and increases the quote total. Use this whenever the user wants to add work or cost to a quote that ALREADY exists, rather than creating a new quote.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string", "description": "Client whose quote to add the extra to"},
                "description": {"type": "string", "description": "What the extra is, e.g. 'fit additional socket'"},
                "amount": {"type": "string", "description": "Cost of the extra in pounds, digits only e.g. '45'"}
            },
            "required": ["client_name", "description", "amount"]
        }
    },
    {
        "name": "find_quote",
        "description": "Look up a quote the user has ALREADY created, by client name. Use this whenever the user refers to a job or client as if you should already know it — e.g. 'won the Smith job', 'book in Dave's job', 'how much was the Patel quote'. Returns the saved quote's client, job description and value so you don't make the user repeat details. Omit client_name to list the most recent quotes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string", "description": "Client name to search for (partial is fine)"}
            }
        }
    },
    {
        "name": "list_enquiries",
        "description": "List recent customer enquiries. Use for 'any new enquiries' or 'who's messaged me'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Optional filter: 'new', 'quoted', etc."}
            }
        }
    },
    {
        "name": "mark_invoice_paid",
        "description": "Mark an invoice as paid, found by client name or invoice number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string"},
                "invoice_number": {"type": "string"}
            }
        }
    },
    {
        "name": "get_summary",
        "description": "Get an overview of the business right now: unpaid invoices total, jobs coming up, new enquiries count.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "draft_client_message",
        "description": "Draft (and, if the client's number is known, send) a short friendly WhatsApp message to a CLIENT on the tradesperson's behalf - e.g. to tell them when work can start, confirm a visit, or chase a decision. Use this when the user says things like 'tell her I can start Tuesday' or 'let him know I'll pop round Friday'. Always keep it warm, brief and professional in the tradesperson's voice.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string", "description": "The client's name"},
                "message": {"type": "string", "description": "The exact message to send to the client, written in first person as the tradesperson"}
            },
            "required": ["client_name", "message"]
        }
    }
]


def _map_qd_to_quote(qd, profile, client_name="Customer", materials_cost=None, labour_days=None, job_description=""):
    day_rate = float(str(profile.get("day_rate") or "250").replace("\u00a3","").replace(",","").strip() or 250) or 250
    scope = qd.get("scopeItems", []) or []
    total = float(qd.get("totalPrice", 0) or 0)
    labour_cost = float(qd.get("labourCost", 0) or 0)
    ld = float(qd.get("labourDays", 0) or (labour_days or 1))
    materials = qd.get("materials", []) or []
    mats_final = float(qd.get("materialsCost", 0) or 0)
    markup_amt = float(qd.get("markupAmount", 0) or 0)
    note = qd.get("note", "") or ""
    lead = qd.get("leadTimeDays", "3-5")
    if not scope:
        scope = ["Supply all labour and materials for: " + (job_description or "the works"),
                 "All work carried out to current UK building standards",
                 "Full site clean-down on completion"]
    if labour_cost <= 0:
        labour_cost = ld * day_rate
    if total <= 0:
        total = round((labour_cost + mats_final) / 5.0) * 5 or round(day_rate / 5.0) * 5
    cname = qd.get("clientName") or client_name or "Customer"
    if cname in ("Customer", "Example Client", "Client") and client_name not in ("Customer", "", None):
        cname = client_name
    return {"client_name": cname, "scope": scope, "materials": materials,
            "labour_days": ld, "labour_cost": labour_cost, "materials_cost": mats_final,
            "markup_amount": markup_amt, "total": total, "note": note, "lead_time": lead}


def _build_detailed_quote(profile, job_description, client_name="Customer", materials_cost=None, labour_days=None, image_data=None, image_type=None):
    msg = job_description
    if materials_cost: msg += ". Materials " + str(materials_cost) + " pounds."
    if labour_days: msg += ". " + str(labour_days) + " days labour."
    msg += " Generate the quote now with sensible professional assumptions. Do not ask any questions."
    qd = _quote_gen_core(profile, msg, image_data=image_data, image_type=image_type)
    if qd.get("type") != "quote":
        day_rate = float(str(profile.get("day_rate") or "250").replace("\u00a3","").replace(",","").strip() or 250) or 250
        ld = float(labour_days or 1)
        labour_cost = ld * day_rate
        mats_final = float(materials_cost) if materials_cost else labour_cost * 0.4
        total = round((labour_cost + mats_final) / 5.0) * 5
        return {"client_name": client_name, "scope": ["Supply all labour and materials for: " + job_description,
                "All work carried out to current UK building standards", "Full site clean-down on completion"],
                "materials": [], "labour_days": ld, "labour_cost": labour_cost, "materials_cost": mats_final,
                "markup_amount": 0, "total": total, "note": "", "lead_time": "3-5"}
    return _map_qd_to_quote(qd, profile, client_name, materials_cost, labour_days, job_description)


def _save_and_send_quote(profile, sender, q):
    try:
        client_name = q["client_name"]
        client_number = ""
        fw = client_name.split()[0] if client_name.split() else client_name
        try:
            enq = supabase.table("enquiries").select("sender").ilike("client_name", "%" + fw + "%").order("created_at", desc=True).limit(1).execute()
            if enq.data:
                client_number = enq.data[0].get("sender", "")
        except Exception as le:
            print("Quote client lookup:", le)
        cnt = supabase.table("quotes").select("id").eq("sender", sender).execute()
        num = "QU-" + str(len(cnt.data) + 1).zfill(3)
        line_items = [{"description": s, "amount": ""} for s in q["scope"]]
        ins = supabase.table("quotes").insert({
            "sender": sender, "client_name": client_name,
            "job_description": "; ".join(q["scope"]), "total": str(int(q["total"])),
            "subtotal": str(int(q["total"])), "vat": "0", "line_items": line_items,
            "status": "draft", "quote_number": num, "quote_text": "",
            "client_number": client_number
        }).execute()
        quote_id = ins.data[0].get("id", "") if ins.data else ""
        try:
            save_pricing_memory(profile, q.get("job_description") or "; ".join(q.get("scope", [])), q)
        except Exception as _se:
            print("pricing save (assistant path):", _se)
        sent = False
        if client_number and quote_id:
            try:
                def wa(n): return n if n.startswith("whatsapp:") else "whatsapp:" + n
                twilio_num = profile.get("twilio_number") or os.environ.get("TWILIO_NUMBER", "")
                try:
                    host = request.host_url.rstrip("/")
                except Exception:
                    host = "https://trades-pa-trades-pa.up.railway.app"
                view_url = host + "/quote-view/" + str(quote_id)
                biz = profile.get("business_name") or profile.get("owner_name") or "your tradesperson"
                body_msg = ("Hi! Here is your quote from " + biz + ". " + num + " - "
                            + "; ".join(q["scope"]) + ". Total " + str(int(q["total"]))
                            + " pounds. View: " + view_url)
                _qsend = send_to_client(profile, client_number, body_msg)
                sent = _qsend["ok"]
            except Exception as se:
                print("Quote send error:", se)
        if sent:
            try:
                supabase.table("quotes").update({"status": "sent"}).eq("id", quote_id).execute()
                supabase.table("enquiries").update({"status": "quoted"}).ilike("client_name", "%" + fw + "%").execute()
            except Exception:
                pass
        def _money(v):
            try: return str(int(round(float(v))))
            except: return ""
        mat_breakdown = ""
        for x in q["materials"]:
            item = x.get("item", "")
            if not item: continue
            qty = str(x.get("qty", "") or "").strip()
            tot = _money(x.get("total", ""))
            mat_breakdown += "\n  - " + ((qty + "x ") if qty and qty not in ("1","") else "") + item + ((" - " + tot + " pounds") if tot else "")
        if not mat_breakdown:
            mat_breakdown = "\n  - Materials estimated within the job"
        base = ("Quote " + num + " for " + client_name + ", total " + str(int(q["total"])) + " pounds."
                + "\n\nMaterials breakdown:" + mat_breakdown
                + "\nMaterials subtotal: " + str(int(q["materials_cost"])) + " pounds"
                + "\nLabour: " + str(q["labour_days"]) + " day(s) - " + str(int(q["labour_cost"])) + " pounds")
        if sent:
            return base + " Saved and sent to " + client_name + " on WhatsApp. View it in the Quotes tab."
        elif client_number:
            return base + " Saved to the Quotes tab as a draft. I tried to send it but it didn't go through, so send it manually from there."
        else:
            return base + " Saved to the Quotes tab as a draft. I don't have " + client_name + "'s number, so it hasn't been sent - open it from the Quotes tab."
    except Exception as e:
        print("save_and_send error:", e)
        return "I had trouble saving that quote: " + str(e)


def _assistant_create_quote(profile, sender, job_description, client_name="Customer", materials_cost=None, labour_days=None, image_data=None, image_type=None):
    q = _build_detailed_quote(profile, job_description, client_name, materials_cost, labour_days, image_data, image_type)
    return _save_and_send_quote(profile, sender, q)


def _assistant_execute_tool(name, ti, profile):
    sender = profile.get("sender", "")
    try:
        if name == "create_invoice":
            client_name = ti.get("client_name", "")
            job_desc = ti.get("job_description", "")
            total_in = str(ti.get("total", "") or "").replace("\u00a3", "").replace(",", "").strip()
            line_items, subtotal = [], ""
            client_address, client_number = "", ""
            # If no amount given, build it from the client's most recent saved quote.
            if (not total_in or total_in in ("0", "0.0", "0.00")):
                try:
                    fw = client_name.split()[0] if client_name.split() else client_name
                    qz = (supabase.table("quotes").select("*").eq("sender", sender)
                          .ilike("client_name", "%" + (fw or client_name) + "%")
                          .order("created_at", desc=True).limit(1).execute().data or [])
                    if qz:
                        qt = qz[0]
                        total_in = str(qt.get("total", "") or "").replace("\u00a3", "").replace(",", "").strip()
                        subtotal = str(qt.get("subtotal", "") or total_in)
                        line_items = qt.get("line_items", []) or []
                        if not job_desc:
                            job_desc = qt.get("job_description", "") or job_desc
                        client_address = qt.get("client_address", "") or ""
                        client_number = qt.get("client_number", "") or ""
                except Exception as qe:
                    print("invoice-from-quote lookup:", qe)
            # Still no figure and no quote to draw from — ask rather than invent one.
            if not total_in or total_in in ("0", "0.0", "0.00"):
                return ("I couldn't find a saved quote for " + (client_name or "that client") +
                        " to take the amount from. How much should the invoice be for?")
            if not subtotal:
                subtotal = total_in
            cnt = supabase.table("invoices").select("id").eq("sender", sender).execute()
            num = "INV-" + str(len(cnt.data) + 1).zfill(3)
            due = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
            supabase.table("invoices").insert({
                "sender": sender, "client_name": client_name,
                "job_description": job_desc, "total": total_in,
                "subtotal": subtotal, "vat": "0", "line_items": line_items, "status": "unpaid",
                "invoice_number": num, "due_date": due, "client_number": client_number, "invoice_text": ""
            }).execute()
            src = " (from the saved quote)" if line_items else ""
            return "Created invoice " + num + " for " + client_name + ", total \u00a3" + total_in + src + ", due in 30 days."

        if name == "create_quote":
            return _assistant_create_quote(profile, sender, ti.get("job_description", ""),
                                           ti.get("client_name", "Customer"),
                                           ti.get("materials_cost"), ti.get("labour_days"))

        if name == "get_schedule":
            bookings = supabase.table("bookings").select("*").eq("sender", sender).order("date").execute().data or []
            if not bookings:
                return "No jobs in the diary."
            lines = []
            for b in bookings[:15]:
                lines.append(b.get("date", "") + " " + (b.get("time", "") or "") + " - " + (b.get("client_name", "") or "") + " (" + (b.get("description", "") or b.get("job_type", "") or "job") + ")")
            return "Diary:\n" + "\n".join(lines)

        if name == "add_booking":
            job_txt = ti.get("description", "") or ti.get("job_type", "")
            location = ti.get("location", "")
            # If no job text given, backfill from the latest matching quote so the
            # diary entry is meaningful (user often says "book in the Smith job").
            cn = (ti.get("client_name", "") or "").strip()
            if not job_txt and cn:
                try:
                    fw = cn.split()[0]
                    qz = (supabase.table("quotes").select("job_description,client_address")
                          .eq("sender", sender).ilike("client_name", "%" + fw + "%")
                          .order("created_at", desc=True).limit(1).execute().data or [])
                    if qz:
                        job_txt = qz[0].get("job_description", "") or job_txt
                        if not location:
                            location = qz[0].get("client_address", "") or ""
                except Exception:
                    pass
            supabase.table("bookings").insert({
                "sender": sender, "client_name": ti.get("client_name", ""),
                "job_type": job_txt, "location": location,
                "date": ti.get("date", ""), "time": ti.get("time", ""),
                "duration_days": str(ti.get("duration_days", "1") or "1"),
                "notes": "", "status": "booked"
            }).execute()
            return ("Booked " + ti.get("client_name", "") + " on " + ti.get("date", "") +
                    (" at " + ti.get("time", "") if ti.get("time") else "") +
                    (" — " + job_txt if job_txt else "") + ".")

        if name == "find_quote":
            cn = (ti.get("client_name", "") or "").strip()
            rows = []
            if cn:
                rows = (supabase.table("quotes").select("*").eq("sender", sender)
                        .ilike("client_name", "%" + cn + "%")
                        .order("created_at", desc=True).limit(5).execute().data or [])
                if not rows and cn.split():
                    rows = (supabase.table("quotes").select("*").eq("sender", sender)
                            .ilike("client_name", "%" + cn.split()[0] + "%")
                            .order("created_at", desc=True).limit(5).execute().data or [])
            else:
                rows = (supabase.table("quotes").select("*").eq("sender", sender)
                        .order("created_at", desc=True).limit(5).execute().data or [])
            if not rows:
                return "No saved quote found for " + (cn or "that client") + "."
            lines = []
            for r in rows:
                lines.append((r.get("client_name", "") or "Client") + " — " +
                             (r.get("job_description", "") or "job") + " — \u00a3" +
                             str(r.get("total", "0")) +
                             (" (" + r.get("quote_number", "") + ")" if r.get("quote_number") else ""))
            return "Matching quotes:\n" + "\n".join(lines)

        if name == "add_quote_extra":
            cn = (ti.get("client_name", "") or "").strip()
            desc = ti.get("description", "") or "Extra"
            amount = ti.get("amount", 0)
            rows = []
            if cn:
                rows = (supabase.table("quotes").select("*").eq("sender", sender)
                        .ilike("client_name", "%" + cn + "%")
                        .order("created_at", desc=True).limit(1).execute().data or [])
                if not rows and cn.split():
                    rows = (supabase.table("quotes").select("*").eq("sender", sender)
                            .ilike("client_name", "%" + cn.split()[0] + "%")
                            .order("created_at", desc=True).limit(1).execute().data or [])
            if not rows:
                return "No saved quote found for " + (cn or "that client") + " to add an extra to. Want me to create a new quote instead?"
            q, err = _apply_quote_extra(sender, rows[0]["id"], desc, amount)
            if err:
                return err
            return ("Added \u201c" + desc + "\u201d (\u00a3" + _num_str(amount) + ") to " +
                    (q.get("client_name", "") or "the") + "'s quote. New total \u00a3" + str(q.get("total", "")) +
                    (" (" + q.get("quote_number", "") + ")" if q.get("quote_number") else "") + ".")

        if name == "list_enquiries":
            q = supabase.table("enquiries").select("*").eq("sender", sender).order("created_at", desc=True).limit(10)
            if ti.get("status"):
                q = q.eq("status", ti.get("status"))
            enq = q.execute().data or []
            if not enq:
                return "No enquiries."
            lines = []
            for e in enq[:10]:
                lines.append((e.get("client_name", "") or "Unknown") + " - " + (e.get("job_type", "") or e.get("summary", "") or "enquiry") + " [" + (e.get("status", "") or "new") + "]")
            return "Enquiries:\n" + "\n".join(lines)

        if name == "mark_invoice_paid":
            q = supabase.table("invoices").select("*").eq("sender", sender)
            if ti.get("invoice_number"):
                q = q.eq("invoice_number", ti.get("invoice_number"))
            elif ti.get("client_name"):
                q = q.ilike("client_name", "%" + ti.get("client_name") + "%")
            found = q.execute().data or []
            if not found:
                return "Couldn't find that invoice."
            inv = found[0]
            supabase.table("invoices").update({"status": "paid"}).eq("id", inv["id"]).execute()
            return "Marked invoice " + inv.get("invoice_number", "") + " for " + inv.get("client_name", "") + " as paid."

        if name == "draft_client_message":
            client_name = ti.get("client_name", "").strip()
            body_msg = ti.get("message", "").strip()
            if not body_msg:
                return "I need the message text to send."
            # Find the client's number: contacts directory first, then recent enquiry/chat
            client_number = contact_number_for(sender, client_name)
            if not client_number:
                try:
                    fw = client_name.split()[0] if client_name.split() else client_name
                    if fw:
                        enq = supabase.table("enquiries").select("sender").ilike("client_name", "%" + fw + "%").order("created_at", desc=True).limit(1).execute()
                        if enq.data:
                            client_number = enq.data[0].get("sender", "")
                except Exception as le:
                    print("client lookup:", le)
            if not client_number:
                return ("DRAFTED (not sent - no number on file for " + (client_name or "this client") +
                        "). Here is the message I would send: \"" + body_msg + "\". "
                        "Ask the user for the client's number to send it.")
            try:
                def _wa(n): return n if str(n).startswith("whatsapp:") else "whatsapp:" + str(n)
                twilio_num = profile.get("twilio_number") or os.environ.get("TWILIO_NUMBER", "")
                tc = TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
                tc.messages.create(body=body_msg, from_=_wa(twilio_num), to=_wa(client_number))
                return "SENT to " + (client_name or "client") + " on WhatsApp: \"" + body_msg + "\""
            except Exception as se:
                print("draft_client_message send error:", se)
                return ("Couldn't send it (" + str(se) + "), but here's the message drafted for you to send manually: \"" + body_msg + "\"")

        if name == "get_summary":
            invoices = supabase.table("invoices").select("*").eq("sender", sender).execute().data or []
            unpaid = [i for i in invoices if i.get("status") != "paid"]
            unpaid_total = sum(float(str(i.get("total", "0")).replace("\u00a3", "") or 0) for i in unpaid)
            bookings = supabase.table("bookings").select("*").eq("sender", sender).execute().data or []
            new_enq = supabase.table("enquiries").select("id").eq("sender", sender).eq("status", "new").execute().data or []
            return ("Summary: \u00a3" + str(int(unpaid_total)) + " unpaid across " + str(len(unpaid)) +
                    " invoices, " + str(len(bookings)) + " jobs in the diary, " + str(len(new_enq)) + " new enquiries.")

        return "Unknown action."
    except Exception as e:
        print("Tool exec error (" + name + "):", e)
        return "That didn't work: " + str(e)


@app.route("/api/pending", methods=["GET"])
def api_pending():
    """List the owner's pending decisions for the dashboard."""
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        sender = result.data[0].get("sender", "")
        rows = (supabase.table("pending_actions").select("*")
                .eq("sender", sender).eq("status", "pending")
                .order("created_at", desc=True).limit(50).execute().data or [])
        return jsonify({"pending": rows})
    except Exception as e:
        print("api_pending error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/pending/resolve", methods=["POST"])
def api_pending_resolve():
    """
    Owner picked an option (or typed an instruction) for a pending decision.
    The bot turns the owner's choice into a natural customer message and sends it.
    If the choice implies a specific bookable time, we create a follow-up
    'confirm_booking' pending action rather than writing the diary directly.
    """
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        d = request.json or {}
        pid = d.get("id")
        choice = (d.get("choice") or "").strip()           # the tapped option or typed instruction
        send_verbatim = bool(d.get("verbatim", False))     # send exactly what owner typed
        if not pid or not choice:
            return jsonify({"error": "Missing id or choice"}), 400

        pa_rows = supabase.table("pending_actions").select("*").eq("id", pid).eq("sender", sender).limit(1).execute().data or []
        if not pa_rows:
            return jsonify({"error": "Not found"}), 404
        pa = pa_rows[0]
        client_number = pa.get("client_number", "")
        cname = pa.get("client_name") or "there"

        if send_verbatim:
            customer_message = choice
        else:
            # Ask the AI to phrase the owner's instruction as a warm message to the customer.
            owner_name = profile.get("owner_name", "")
            biz = profile.get("business_name", "the business")
            convo = pa.get("customer_msg", "")
            phr_sys = ("You write a single short, warm SMS to a customer on behalf of " + biz + ".\n"
                       "The customer said: \"" + convo + "\".\n"
                       "The owner (" + owner_name + ") has decided: \"" + choice + "\".\n"
                       "Write ONLY the message to send to the customer relaying that decision naturally. "
                       "Do not add quotes, signatures, or tags. Keep it brief and human.")
            try:
                ai = client.messages.create(model="claude-sonnet-4-5", max_tokens=180,
                                            system=phr_sys, messages=[{"role": "user", "content": "Write the message."}])
                customer_message = ai.content[0].text.strip()
            except Exception as ae:
                print("phrasing error:", ae)
                customer_message = choice

        # Send to the customer on their channel.
        send_res = send_to_client(profile, client_number, customer_message)
        if not send_res["ok"]:
            return jsonify({"ok": False, "error": send_res.get("error") or "Send failed"}), 200

        # Mark this pending action done.
        supabase.table("pending_actions").update({
            "status": "done", "resolved_at": datetime.datetime.now().isoformat()
        }).eq("id", pid).execute()

        return jsonify({"ok": True, "sent": customer_message})
    except Exception as e:
        print("api_pending_resolve error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/pending/dismiss", methods=["POST"])
def api_pending_dismiss():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        sender = result.data[0].get("sender", "")
        pid = (request.json or {}).get("id")
        if not pid:
            return jsonify({"error": "Missing id"}), 400
        supabase.table("pending_actions").update({
            "status": "dismissed", "resolved_at": datetime.datetime.now().isoformat()
        }).eq("id", pid).eq("sender", sender).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print("api_pending_dismiss error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/usage", methods=["GET"])
def api_usage():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        sender = result.data[0].get("sender", "")
        summary = get_usage_summary(sender)
        # Show an indicative allowance so the meter has context (Pro tier default).
        # This is display-only for now — no enforcement.
        summary["allowance"] = 1000
        return jsonify(summary)
    except Exception as e:
        print("api_usage error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/briefing", methods=["GET"])
def briefing():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        owner = (profile.get("owner_name") or profile.get("business_name") or "").split(" ")[0]
        now = datetime.datetime.now()
        hour = now.hour
        part = "Morning" if hour < 12 else ("Afternoon" if hour < 18 else "Evening")
        today = now.strftime("%Y-%m-%d")
        bookings = supabase.table("bookings").select("*").eq("sender", sender).execute().data or []
        today_jobs = [b for b in bookings if str(b.get("date", "")).startswith(today)]
        invoices = supabase.table("invoices").select("*").eq("sender", sender).execute().data or []
        unpaid = [i for i in invoices if i.get("status") != "paid"]
        def _num(v):
            try: return float(str(v).replace("£", "").replace(",", "") or 0)
            except: return 0.0
        unpaid_total = sum(_num(i.get("total", 0)) for i in unpaid)
        quotes = supabase.table("quotes").select("*").eq("sender", sender).execute().data or []
        awaiting = [q for q in quotes if q.get("status") == "sent"]
        new_enq = supabase.table("enquiries").select("*").eq("sender", sender).eq("status", "new").execute().data or []

        parts = [part + ((", " + owner) if owner else "") + "."]
        if today_jobs:
            first = sorted(today_jobs, key=lambda b: (b.get("time", "") or ""))[0]
            t = first.get("time", ""); nm = first.get("client_name", "")
            if len(today_jobs) == 1:
                parts.append("You've got one job today" + ((" — " + nm) if nm else "") + ((" at " + t) if t else "") + ".")
            else:
                parts.append("You've got " + str(len(today_jobs)) + " jobs today, first" + ((" " + nm) if nm else "") + ((" at " + t) if t else "") + ".")
        else:
            parts.append("Nothing in the diary today.")
        if awaiting:
            if len(awaiting) == 1:
                parts.append((awaiting[0].get("client_name", "A client") or "A client") + "'s quote hasn't had a reply yet.")
            else:
                parts.append(str(len(awaiting)) + " quotes are still awaiting a reply.")
        if unpaid:
            parts.append("£" + str(int(unpaid_total)) + " outstanding across " + str(len(unpaid)) + (" invoices" if len(unpaid) != 1 else " invoice") + ".")
        if new_enq:
            parts.append(str(len(new_enq)) + (" new enquiries" if len(new_enq) != 1 else " new enquiry") + " to look at.")
        if not (today_jobs or awaiting or unpaid or new_enq):
            parts.append("All clear right now — ask me to quote, invoice or check your diary whenever you need.")

        chips = []
        if new_enq: chips.append("Any new enquiries?")
        if today_jobs: chips.append("What's on today?")
        if unpaid: chips.append("Show my unpaid invoices")
        chips.append("How's the business looking?")
        return jsonify({"text": " ".join(parts), "chips": chips[:4]})
    except Exception as e:
        print("briefing error:", e)
        return jsonify({"text": "", "chips": []})


@app.route("/api/assistant", methods=["POST"])
def assistant():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        data = request.json or {}
        user_msg = (data.get("message") or "").strip()
        history = data.get("history", [])
        image_data = data.get("image", None)
        image_type = data.get("imageType", "image/jpeg")
        quote_mode = data.get("quote_mode", False)
        quote_history = data.get("quote_history", []) or []

        # QUOTE MODE: a photo, or an ongoing quote conversation. Let the engine ASK about
        # materials/specifics before pricing, then save + send once it has what it needs.
        if image_data or quote_mode:
            ask_hint = (" Important: if the materials to be used are not clearly specified and would affect the price "
                        "(for example boxing-in, cladding, board or timber choice, or finishes), do NOT assume - ask the "
                        "user which materials they will use. Ask one short question at a time about materials and any key "
                        "dimensions, then produce the quote once you have what you need.")
            qmsg = (user_msg or "Please quote this job from the photo.") + ask_hint
            qd = _quote_gen_core(profile, qmsg, history=quote_history, image_data=image_data, image_type=image_type)
            if qd.get("type") == "question":
                return jsonify({"reply": qd.get("message", ""), "quote_pending": True})
            cname = "Customer"
            try:
                import re as _re2
                joined = (user_msg or "") + " " + " ".join((h.get("content","") for h in quote_history if h.get("role")=="user"))
                mt = _re2.search(r"(?:for|quote)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)", joined)
                if mt:
                    cname = mt.group(1)
            except Exception:
                pass
            q = _map_qd_to_quote(qd, profile, cname, job_description=(user_msg or ""))
            reply = _save_and_send_quote(profile, sender, q)
            try: track_usage(sender, "quote")
            except Exception: pass
            return jsonify({"reply": reply, "actions": ["create_quote"], "quote_done": True})

        if not user_msg:
            return jsonify({"error": "No message"}), 400

        biz = profile.get("business_name") or profile.get("owner_name") or "the business"
        trade = profile.get("trade", "tradesperson")
        today = datetime.datetime.now().strftime("%A %d %B %Y")
        system_prompt = (
            "You are the personal assistant for " + biz + ", a " + trade + " business. "
            "Today is " + today + ". You help them stay on top of admin hands-free while they work. "
            "You can create quotes and invoices, check and add diary bookings, read enquiries, and mark invoices paid using your tools. "
            "Be brief, warm and practical — like a sharp office manager. Confirm actions in one short sentence. "
            "If you need a key detail (like an amount or client name) ask one quick question rather than guessing. "
            "Amounts are in pounds. Keep replies short enough to be read aloud. "
            "CRITICAL: To DO anything — create a quote or invoice, book a job, mark something paid — you MUST call the matching tool. "
            "NEVER say you have created, saved, sent or booked something unless you have actually called the tool and seen its result. "
            "Do not claim a quote or invoice is saved to any tab unless the tool confirmed it. "
            "When the user clearly asks for an action and you have enough detail, call the tool straight away rather than just describing what you will do. "
            "When a quote is created, relay the full materials and labour breakdown that the tool returns to the user - keep the itemised list, do not shorten it. "
            "Before creating a quote, if the job involves a material choice that affects price (boxing-in, cladding, boards, timber, finishes) and the user has not said what they will use, ask them which materials first, then call the tool with the chosen material included in the job description. "
            "MULTI-STEP REQUESTS: The user often asks for several things in one breath, e.g. 'just seen Mrs Patel about a fence, 6 panels, quote her about 900 and tell her I can start Tuesday'. "
            "When that happens, carry out ALL the steps by calling the matching tools in sequence in the SAME turn: log/quote the job (create_quote), add any booking they mention (add_booking), and draft the client message (draft_client_message). "
            "Do them all before you reply. Then give ONE short combined confirmation that lists what you did, like: 'Done — quoted Mrs Patel \u00a3900 for the fence, pencilled her in for Tuesday, and messaged her to confirm.' "
            "If one detail is missing for ONE of the steps but the others are clear, do the steps you can and mention the one thing you still need. Never refuse the whole request because one part is fuzzy. "
            "KNOWN JOBS: if the user refers to a client or job as though you already know it (e.g. 'won the Smith job', 'book Dave in for his job'), call find_quote to pull the existing job details rather than asking them to repeat anything, then act on it. "
            "Resolve relative dates like 'next Wednesday' or 'tomorrow' into an absolute YYYY-MM-DD yourself from today's date — don't ask the user for the exact date when you can work it out."
            " INVOICING A QUOTED JOB: if the user asks to invoice a job that was already quoted, call create_invoice with just the client name and leave the amount out — it pulls the figure and line items from the saved quote. Only ask for an amount if there is no quote on file."
        )

        messages = []
        for h in history[-10:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": user_msg})

        actions = []
        final_text = ""
        for _ in range(6):
            resp = client.messages.create(
                model="claude-sonnet-4-5", max_tokens=2048,
                system=system_prompt, tools=ASSISTANT_TOOLS, messages=messages
            )
            if resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        out = _assistant_execute_tool(block.name, block.input, profile)
                        actions.append(block.name)
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id, "content": out
                        })
                messages.append({"role": "user", "content": tool_results})
                continue
            else:
                for block in resp.content:
                    if block.type == "text":
                        final_text += block.text
                break

        try: track_usage(sender, "text")
        except Exception: pass
        return jsonify({"reply": final_text or "Done.", "actions": actions, "usage": get_usage_summary(sender)})
    except Exception as e:
        print("Assistant error:", e)
        return jsonify({"error": str(e)}), 500


import re as _re_sent
# Boundary = sentence punctuation followed by real whitespace/closing mark, or a newline.
# Note: no end-of-string ($) match — mid-stream the buffer end just means "more is coming",
# so we only split on a confirmed whitespace boundary and flush the tail on force.
_SENT_BOUNDARY = _re_sent.compile(r'[.!?\u2026](?=[\s"\'\)\]])|\n')
_ABBREV = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "no", "vs", "etc",
           "eg", "ie", "approx", "dept", "co", "ltd", "inc", "rd", "ave", "min", "max", "ft"}


def _clean_for_speech(s):
    """Strip markdown so the spoken sentence reads cleanly. Keep £ and numbers."""
    s = s.replace("**", "").replace("__", "")
    s = _re_sent.sub(r'[*_#`>]', '', s)
    s = _re_sent.sub(r'\s+', ' ', s).strip()
    return s


def _pop_sentences(buf, force=False, max_len=220):
    """Pull complete sentences out of a growing buffer for fast TTS.
    Returns (list_of_sentences, remainder). Skips abbreviation/decimal periods
    (so 'Mrs. Patel' and '£3.50' aren't chopped). On a long run-on with no
    punctuation, flushes at a word break so speech can start promptly."""
    out = []
    last_cut = 0
    search_from = 0
    while True:
        m = _SENT_BOUNDARY.search(buf, search_from)
        if not m:
            break
        start, end = m.start(), m.end()
        if buf[start] == '.':
            # word immediately before the dot
            k = start - 1
            while k >= 0 and buf[k].isalpha():
                k -= 1
            word = buf[k + 1:start].lower()
            prev = buf[start - 1] if start > 0 else ''
            if word in _ABBREV or prev.isdigit():   # abbreviation or decimal/number → not a real stop
                search_from = end
                continue
        sent = buf[last_cut:end].strip()
        if sent:
            out.append(sent)
        last_cut = end
        search_from = end
    buf = buf[last_cut:].lstrip()
    if len(buf) > max_len:
        cut = buf.rfind(' ', 0, max_len)
        if cut > 40:
            out.append(buf[:cut].strip())
            buf = buf[cut:].lstrip()
    if force and buf.strip():
        out.append(buf.strip())
        buf = ''
    return out, buf


@app.route("/api/assistant_stream", methods=["POST"])
def assistant_stream():
    """Streaming sibling of /api/assistant for the hands-free voice loop.
    Streams the reply as newline-delimited JSON events so the client can fire
    each sentence to TTS the moment its boundary lands, instead of waiting for
    the whole reply. Event shapes:
      {"type":"status","value":"thinking"}
      {"type":"sentence","text":"..."}
      {"type":"done","actions":[...],"usage":{...},"full":"..."}
      {"type":"error","error":"..."}
    Tool calls run exactly as in /api/assistant (propose-and-approve, usage
    metering, etc. all unchanged)."""
    # Auth + profile up front so failures return a clean HTTP error, not a stream.
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        data = request.json or {}
        user_msg = (data.get("message") or "").strip()
        history = data.get("history", [])
        if not user_msg:
            return jsonify({"error": "No message"}), 400
    except Exception as e:
        print("Assistant stream auth error:", e)
        return jsonify({"error": str(e)}), 500

    biz = profile.get("business_name") or profile.get("owner_name") or "the business"
    trade = profile.get("trade", "tradesperson")
    today = datetime.datetime.now().strftime("%A %d %B %Y")
    system_prompt = (
        "You are the personal assistant for " + biz + ", a " + trade + " business. "
        "Today is " + today + ". You help them stay on top of admin hands-free while they work. "
        "You can create quotes and invoices, check and add diary bookings, read enquiries, and mark invoices paid using your tools. "
        "Be brief, warm and practical — like a sharp office manager. Confirm actions in one short sentence. "
        "This reply will be spoken aloud, so keep it conversational and short, and don't narrate that you are about to use a tool — just use it and confirm the result. "
        "KNOWN JOBS: if the user refers to a client or job as though you already know it (e.g. 'won the Smith job', 'book Dave in'), call find_quote to pull the existing job details rather than asking them to repeat anything, then act on it. "
        "Resolve relative dates like 'next Wednesday' or 'tomorrow' into an absolute YYYY-MM-DD yourself from today's date — don't ask for the exact date when you can work it out. "
        "INVOICING A QUOTED JOB: if asked to invoice a job that was already quoted, call create_invoice with just the client name and no amount — it pulls the figure from the saved quote. Only ask for an amount if there's no quote. "
        "If you need a key detail (like an amount or client name) ask one quick question rather than guessing. "
        "Amounts are in pounds. "
        "CRITICAL: To DO anything — create a quote or invoice, book a job, mark something paid — you MUST call the matching tool. "
        "NEVER say you have created, saved, sent or booked something unless you have actually called the tool and seen its result. "
        "When the user clearly asks for an action and you have enough detail, call the tool straight away rather than just describing what you will do. "
        "When a quote is created, relay the materials and labour breakdown the tool returns. "
        "Before creating a quote, if the job involves a material choice that affects price and the user has not said what they will use, ask which materials first. "
        "MULTI-STEP REQUESTS: if the user asks for several things in one breath, carry out ALL the steps by calling the matching tools in sequence in the SAME turn, then give ONE short combined confirmation. "
        "If one detail is missing for one step but the others are clear, do the steps you can and mention the one thing you still need."
    )

    messages = []
    for h in history[-10:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_msg})

    def _ev(obj):
        return json.dumps(obj) + "\n"

    def generate():
        actions = []
        full_text = ""
        buf = ""
        try:
            yield _ev({"type": "status", "value": "thinking"})
            for _ in range(6):
                with client.messages.stream(
                    model="claude-sonnet-4-5", max_tokens=2048,
                    system=system_prompt, tools=ASSISTANT_TOOLS, messages=messages
                ) as stream:
                    for text in stream.text_stream:
                        full_text += text
                        buf += text
                        sents, buf = _pop_sentences(buf)
                        for s in sents:
                            cs = _clean_for_speech(s)
                            if cs:
                                yield _ev({"type": "sentence", "text": cs})
                    final = stream.get_final_message()

                if final.stop_reason == "tool_use":
                    # Flush any spoken preamble before the tool runs.
                    sents, buf = _pop_sentences(buf, force=True)
                    for s in sents:
                        cs = _clean_for_speech(s)
                        if cs:
                            yield _ev({"type": "sentence", "text": cs})
                    buf = ""
                    messages.append({"role": "assistant", "content": final.content})
                    tool_results = []
                    for block in final.content:
                        if block.type == "tool_use":
                            out = _assistant_execute_tool(block.name, block.input, profile)
                            actions.append(block.name)
                            tool_results.append({
                                "type": "tool_result", "tool_use_id": block.id, "content": out
                            })
                    messages.append({"role": "user", "content": tool_results})
                    yield _ev({"type": "status", "value": "thinking"})
                    continue
                else:
                    sents, buf = _pop_sentences(buf, force=True)
                    for s in sents:
                        cs = _clean_for_speech(s)
                        if cs:
                            yield _ev({"type": "sentence", "text": cs})
                    break

            try:
                track_usage(sender, "text")
            except Exception:
                pass
            usage = {}
            try:
                usage = get_usage_summary(sender)
            except Exception:
                pass
            yield _ev({"type": "done", "actions": actions, "usage": usage, "full": full_text})
        except Exception as e:
            print("Assistant stream error:", e)
            yield _ev({"type": "error", "error": str(e)})

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


def _normalise_job_type(text):
    """Reduce a free-text job description to a stable key for pricing memory."""
    if not text:
        return "general"
    t = text.lower()
    # Common trade job buckets - first match wins
    buckets = [
        ("fencing", ["fence", "fencing", "panel", "feather edge", "post and rail"]),
        ("decking", ["deck", "decking", "balustrade"]),
        ("bathroom", ["bathroom", "ensuite", "wet room", "shower room"]),
        ("kitchen", ["kitchen", "units", "worktop", "cabinet"]),
        ("plastering", ["plaster", "skim", "render", "artex", "boarding", "board and skim"]),
        ("flooring", ["floor", "laminate", "lvt", "vinyl", "tiling floor"]),
        ("tiling", ["tile", "tiling", "splashback"]),
        ("doors", ["door", "doors", "architrave", "skirting"]),
        ("loft", ["loft", "attic"]),
        ("roofing", ["roof", "felt", "fascia", "soffit", "guttering"]),
        ("painting", ["paint", "decorat", "emulsion", "gloss"]),
        ("garden", ["patio", "paving", "driveway", "landscap", "turf"]),
        ("extension", ["extension", "single storey", "garage conversion"]),
        ("electrical", ["socket", "consumer unit", "rewire", "downlight", "spotlight"]),
        ("plumbing", ["pipe", "radiator", "boiler", "tap", "leak", "bathroom plumb"]),
    ]
    for key, words in buckets:
        if any(w in t for w in words):
            return key
    # fallback: first two meaningful words
    cleaned = "".join(ch if ch.isalnum() or ch == " " else " " for ch in t)
    words = [w for w in cleaned.split() if len(w) > 2][:2]
    return " ".join(words) or "general"


def _sample_job_for_trade(trade):
    """A believable sample job + scope + ballpark numbers for the welcome quote."""
    t = (trade or "").lower()
    table = [
        (["carpenter", "joiner", "carpentry"],
         ("Supply and fit 4 internal oak veneer doors with new hinges and handles", "Sample Client",
          ["Remove existing doors and ironmongery", "Hang 4 oak veneer doors, adjusted to frame",
           "Fit new hinges, latches and handles", "Adjust, ease and finish; site clean-down"], 2, 360)),
        (["plasterer", "plastering", "renderer"],
         ("Skim two ceilings and one feature wall to a smooth finish", "Sample Client",
          ["Prepare and tape joints", "Apply bonding coat where needed",
           "Skim two ceilings and one wall to smooth finish", "Make good and clean down"], 2, 180)),
        (["plumber", "plumbing", "heating", "gas"],
         ("Replace bathroom suite - basin, WC and taps", "Sample Client",
          ["Isolate supplies and remove old suite", "Fit new basin, WC and taps",
           "Connect, test for leaks and commission", "Seal and clean down"], 2, 420)),
        (["electrician", "electrical"],
         ("Replace consumer unit and add two double sockets", "Sample Client",
          ["Isolate and remove old consumer unit", "Install new 18th-edition consumer unit",
           "Run and connect two new double sockets", "Test, certify and clean down"], 1, 220)),
        (["fencer", "fencing", "landscaper", "landscaping", "garden"],
         ("Supply and install 6 feather-edge fence panels with posts", "Sample Client",
          ["Remove existing panels", "Set 6 posts in postcrete",
           "Fit 6 feather-edge panels and gravel boards", "Clear site and take away waste"], 1, 300)),
        (["tiler", "tiling"],
         ("Tile bathroom walls - supply and fit, approx 12 sq m", "Sample Client",
          ["Prepare and prime walls", "Fix approx 12 sq m wall tiles",
           "Grout, seal and finish edges", "Clean down"], 2, 260)),
        (["roofer", "roofing"],
         ("Replace 10 slipped tiles and re-point ridge line", "Sample Client",
          ["Access and make safe", "Replace 10 slipped/broken tiles",
           "Re-bed and point ridge line", "Clear debris and clean down"], 1, 140)),
        (["painter", "decorator", "decorating"],
         ("Paint lounge and hallway - walls, ceilings and woodwork", "Sample Client",
          ["Prepare, fill and sand surfaces", "Two coats emulsion to walls and ceilings",
           "Gloss/satin to woodwork", "Clean down and make good"], 2, 160)),
        (["builder", "building", "groundwork"],
         ("Lay 20 sq m patio in porcelain paving", "Sample Client",
          ["Excavate and prepare sub-base", "Lay and compact base",
           "Lay 20 sq m porcelain paving", "Point, seal and clear site"], 3, 650)),
    ]
    for keys, payload in table:
        if any(k in t for k in keys):
            return payload
    return ("Sample job for your trade - typical 2-day project", "Sample Client",
            ["Preparation and set-up", "Carry out the works to a high standard",
             "Finishing and adjustments", "Site clean-down on completion"], 2, 250)


def send_welcome_sample_quote(profile):
    """Build a real SAMPLE quote in the user's trade; return pdf url + blurb data. Never raises."""
    try:
        sender = profile.get("sender", "")
        trade = profile.get("trade", "")
        day_rate = float(str(profile.get("day_rate") or "250").replace("\u00a3", "").replace(",", "").strip() or 250) or 250
        job_desc, client_name, scope, labour_days, materials_cost = _sample_job_for_trade(trade)
        labour_cost = labour_days * day_rate
        total = int(round((labour_cost + materials_cost) / 5.0) * 5)
        template_result = supabase.table("quote_templates").select("*").eq("sender", sender).execute()
        template = template_result.data[0] if template_result.data else {}
        cnt = supabase.table("quotes").select("id").eq("sender", sender).execute()
        num = "QU-" + str(len(cnt.data) + 1).zfill(3)
        quote_obj = {"client_name": client_name, "client_address": "",
                     "total": str(total), "quote_number": num, "line_items": scope}
        try:
            html = build_quote_html(quote_obj, profile, template, is_invoice=False)
        except Exception as he:
            print("welcome quote html error:", he); html = ""
        saved = supabase.table("quotes").insert({
            "sender": sender, "client_name": client_name, "client_address": "",
            "job_description": job_desc, "line_items": scope,
            "subtotal": str(total), "vat": "0", "total": str(total),
            "status": "draft", "quote_number": num, "quote_text": html, "client_number": ""
        }).execute()
        quote_id = saved.data[0].get("id", "") if saved.data else ""
        if not quote_id:
            return None
        return {"url": "https://trades-pa-trades-pa.up.railway.app/generate-pdf/" + str(quote_id),
                "total": total, "job": job_desc, "client": client_name, "number": num}
    except Exception as e:
        print("send_welcome_sample_quote error:", e)
        return None


def save_pricing_memory(profile, job_description, quote):
    """Record the price actually used so future similar jobs can reference it."""
    try:
        sender = profile.get("sender", "")
        if not sender:
            return
        jt = _normalise_job_type(job_description or "; ".join(quote.get("scope", []) if isinstance(quote, dict) else []))
        def _f(v):
            try:
                return float(str(v).replace("\u00a3", "").replace(",", "") or 0)
            except Exception:
                return 0.0
        total = _f(quote.get("total"))
        if total <= 0:
            return
        supabase.table("pricing_memory").insert({
            "sender": sender,
            "job_type": jt,
            "job_keywords": (job_description or "")[:300],
            "labour_days": _f(quote.get("labour_days")),
            "labour_cost": _f(quote.get("labour_cost")),
            "materials_cost": _f(quote.get("materials_cost")),
            "total_price": total,
            "client_name": quote.get("client_name", ""),
        }).execute()
    except Exception as e:
        print("save_pricing_memory error:", e)


def lookup_pricing_memory(sender, job_description, limit=3):
    """Return a short human hint about what this person charged for similar jobs, or ''."""
    try:
        if not sender:
            return ""
        jt = _normalise_job_type(job_description)
        rows = (supabase.table("pricing_memory")
                .select("*").eq("sender", sender).eq("job_type", jt)
                .order("created_at", desc=True).limit(limit).execute().data or [])
        if not rows:
            return ""
        bits = []
        for r in rows:
            tot = int(r.get("total_price") or 0)
            days = r.get("labour_days")
            when = str(r.get("created_at", ""))[:10]
            extra = (", " + str(days) + " day(s) labour") if days else ""
            bits.append("\u00a3" + str(tot) + extra + " (" + when + ")")
        return "PAST PRICING for similar '" + jt + "' jobs (most recent first): " + "; ".join(bits) + ". Use these as a strong reference for your estimate unless the job clearly differs."
    except Exception as e:
        print("lookup_pricing_memory error:", e)
        return ""


def _quote_gen_core(profile, message, history=None, image_data=None, image_type="image/jpeg"):
    sender = profile.get("sender", "")
    history = history or []
    try:
        template_result = supabase.table("quote_templates").select("*").eq("sender", sender).execute()
        template = template_result.data[0] if template_result.data else {}

        dt = {}
        if template.get("design_template"):
            try:
                dt = json.loads(template["design_template"])
            except:
                dt = {}

        biz_name = dt.get("bizName") or template.get("business_name") or profile.get("business_name", "")
        trade = dt.get("trade") or profile.get("trade", "tradesperson")
        phone_num = dt.get("phone") or template.get("business_phone") or profile.get("phone", "")
        email = dt.get("email") or template.get("business_email") or ""
        location = dt.get("location") or template.get("business_address") or ""
        accent = dt.get("accent") or template.get("brand_colour") or "#1a1a2e"
        dark = dt.get("dark") or "#1a1a1a"
        design_style = template.get("design_style") or "gold"
        inclusion_items = dt.get("inclusionItems") or ["All labour", "All materials", "Standard preparation", "Site clean-down"]
        commitment = dt.get("commitment") or "We take pride in delivering quality workmanship. Your satisfaction is our priority."
        lead_time = dt.get("leadTime") or "Works to be arranged at a convenient time. Typical duration: 3-5 working days."
        show_vat = dt.get("showVat", False)
        logo_url = profile.get("logo_url") or ""

        day_rate_raw = str(profile.get("day_rate") or "0").replace("£","").replace(",","").strip()
        day_rate = float(day_rate_raw or 0)
        if day_rate == 0:
            day_rate = 250
            day_rate_warning = True
        else:
            day_rate_warning = False
        markup = float(str(profile.get("materials_markup") or 20).replace("%","") or 20)


        system = f"""You are a professional quoting assistant for {biz_name}, a {trade} business in the UK.

TRADESPERSON DETAILS:
- Trade: {trade}
- Day rate: £{day_rate:.0f}
- Materials markup: {markup:.0f}%

━━━ MODE 1: MANUAL (user gives their own numbers) ━━━
If the message contains ANY of these — a £ price for materials, number of days, phrases like
"materials £X", "X days labour", "X day job", "labour X days", "£X materials" — treat as MANUAL.
In manual mode:
- Use EXACTLY the numbers they gave, do not change them
- labourCost = days × £{day_rate:.0f} (or use their exact £ figure if given)
- materialsCost = their exact materials figure (do NOT add markup — they priced it themselves)
- markupAmount = 0
- totalPrice = labourCost + materialsCost
- Write professional scopeItems from their job description
- Generate the quote IMMEDIATELY — never ask follow-up questions

━━━ MODE 2: AI CALCULATED (user describes the job, no numbers given) ━━━
- Ask ONE question at a time, max 3 questions
- For simple jobs generate straight away
- labourCost = labourDays × £{day_rate:.0f}
- markupAmount = raw materials × {markup:.0f} / 100
- materialsCost = raw materials + markupAmount
- totalPrice = labourCost + materialsCost rounded to nearest £5

━━━ PHOTO ANALYSIS ━━━
If the user sends a photo:
- Identify the space, surfaces, condition, visible damage
- Estimate dimensions from context clues
- Note anything affecting price — artex, blown plaster, complex layouts, inspiration style
- Use what you see to reduce questions needed

━━━ REVISING AN EXISTING QUOTE ━━━
If a quote has already been produced earlier in this conversation and the user now asks to CHANGE it
(e.g. "make it £900", "change labour to 3 days", "add £200 materials", "round it to 2 grand",
"drop the price a bit", "take the skirting off"), treat it as a REVISION, NOT a new quote:
- Start from the previous quote's numbers and scope.
- Apply ONLY the change they asked for. Keep everything else identical.
- If they set a target TOTAL (e.g. "make it £900"), keep the scope, adjust so totalPrice matches;
  if needed, back-calculate labour/materials sensibly so the parts still add up to the new total.
- Re-emit the FULL QUOTE_READY JSON with the updated numbers. Never reply with prose-only when revising.
- Keep the same clientName unless they change it.

━━━ WHEN READY — respond with ONLY this JSON (no other text): ━━━
QUOTE_READY:{{
  "clientName": "Customer",
  "scopeItems": ["specific work item 1", "specific work item 2", "specific work item 3"],
  "totalPrice": 0,
  "labourDays": 0,
  "labourCost": 0,
  "materials": [{{"item": "material name", "qty": "quantity", "unitCost": 0, "total": 0}}],
  "materialsCost": 0,
  "markupAmount": 0,
  "leadTimeDays": "3-5",
  "note": "any important caveat or empty string",
  "summary": "one friendly sentence confirming quote is ready",
  "mode": "manual or ai"
}}

MATERIAL COST REFERENCE (UK trade prices 2024/25):
Plastering: plasterboard £8-12/sheet, bonding coat £15/bag, finishing plaster £12/bag, beads £2-3/m
Painting: trade emulsion £20-30/5L, gloss £18/2.5L, undercoat £18/2.5L, prep materials £10-20
Plumbing: copper pipe £3-5/m, fittings £2-5 each, solder/flux £8, PTFE £2, pipe clips £0.50 each
Electrical: 2.5mm twin & earth £1.50/m, 1.5mm £1.20/m, back boxes £1.50, sockets/switches £5-15, consumer unit £80-150
Carpentry: MDF £25-35/sheet, timber £3-6/m, screws/fixings £5-15, adhesive £5-8, hinges/hardware £5-20
Tiling: tiles £15-40/m², adhesive £15/20kg, grout £8/3kg, spacers £3, trim £3-5/m
Roofing: felt £30-50/roll, nails £5, lead £30-50/m², tiles £40-80/m²
Flooring: laminate £15-30/m², underlay £3-5/m², adhesive £20, threshold strips £8"""

        # Smart pricing memory: prepend what this tradesperson actually charged before.
        try:
            _hint_src = message if isinstance(message, str) else ""
            if not _hint_src and history:
                _hint_src = " ".join(h.get("content", "") for h in history if isinstance(h.get("content"), str))
            _phint = lookup_pricing_memory(sender, _hint_src)
            if _phint:
                system = system + "\n\n\u2501\u2501\u2501 YOUR OWN PAST PRICING (use as primary reference) \u2501\u2501\u2501\n" + _phint
        except Exception as _pe:
            print("pricing hint error:", _pe)

        messages = []
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})

        if image_data:
            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": image_type, "data": image_data}},
                {"type": "text", "text": message or "Please analyse this photo and help me quote this job."}
            ]
        else:
            user_content = message

        messages.append({"role": "user", "content": user_content})

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=system,
            messages=messages
        )

        reply = response.content[0].text.strip()

        import re
        if "QUOTE_READY:" in reply:
            json_str = reply.split("QUOTE_READY:")[1].strip()
            json_str = re.sub(r'```json|```', '', json_str).strip()
            quote_data = json.loads(json_str)
            quote_data["type"] = "quote"
            quote_data["profile"] = {
                "bizName": biz_name,
                "trade": trade,
                "phone": phone_num,
                "email": email,
                "location": location,
                "accent": accent,
                "dark": dark,
                "designStyle": design_style,
                "inclusionItems": inclusion_items,
                "commitment": commitment,
                "leadTime": lead_time,
                "showVat": show_vat,
                "logoUrl": logo_url
            }
            return quote_data
        else:
            return {"type": "question", "message": reply}
    except Exception as e:
        print("Quote core error: " + str(e))
        return {"type": "question", "message": "Sorry, I had trouble building that quote. Give me a bit more detail and I will try again."}


@app.route("/api/revise-quote", methods=["POST"])
def revise_quote():
    """Save a manually-edited quote price/labour/materials and remember it for next time."""
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        d = request.json or {}

        def _f(v):
            try:
                return float(str(v).replace("\u00a3", "").replace(",", "") or 0)
            except Exception:
                return 0.0

        client_name = (d.get("client_name") or "Customer").strip()
        job_description = (d.get("job_description") or "").strip()
        scope = d.get("scope") or []
        total = _f(d.get("total"))
        labour_days = _f(d.get("labour_days"))
        labour_cost = _f(d.get("labour_cost"))
        materials_cost = _f(d.get("materials_cost"))
        quote_id = d.get("quote_id")

        if total <= 0:
            return jsonify({"error": "Total must be greater than zero"}), 400

        line_items = [{"description": s, "amount": ""} for s in scope]

        if quote_id:
            # Update existing quote
            supabase.table("quotes").update({
                "client_name": client_name,
                "job_description": "; ".join(scope) if scope else job_description,
                "total": str(int(total)), "subtotal": str(int(total)),
                "line_items": line_items
            }).eq("id", quote_id).eq("sender", sender).execute()
            saved_id = quote_id
        else:
            cnt = supabase.table("quotes").select("id").eq("sender", sender).execute()
            num = "QU-" + str(len(cnt.data) + 1).zfill(3)
            ins = supabase.table("quotes").insert({
                "sender": sender, "client_name": client_name,
                "job_description": "; ".join(scope) if scope else job_description,
                "total": str(int(total)), "subtotal": str(int(total)), "vat": "0",
                "line_items": line_items, "status": "draft",
                "quote_number": num, "quote_text": "", "client_number": ""
            }).execute()
            saved_id = ins.data[0].get("id", "") if ins.data else ""

        # Remember the (possibly overridden) price
        try:
            save_pricing_memory(profile, job_description or "; ".join(scope), {
                "scope": scope, "total": total, "labour_days": labour_days,
                "labour_cost": labour_cost, "materials_cost": materials_cost,
                "client_name": client_name
            })
        except Exception as _se:
            print("pricing save (revise):", _se)

        try: track_usage(sender, "quote")
        except Exception: pass
        return jsonify({"ok": True, "quote_id": saved_id})
    except Exception as e:
        print("revise_quote error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-quote-ai", methods=["POST"])
def generate_quote_ai():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        if not phone or not pin:
            return jsonify({"error": "Auth required"}), 401
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        data = request.json
        history = data.get("history", [])
        message = data.get("message", "")
        image_data = data.get("image", None)
        image_type = data.get("imageType", "image/jpeg")
        if not message and not image_data:
            return jsonify({"error": "No message provided"}), 400
        out = _quote_gen_core(profile, message, history, image_data, image_type)
        return jsonify(out)
    except Exception as e:
        print("Generate quote AI error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations", methods=["GET"])
def conversations():
    try:
        phone = format_phone(request.args.get("phone","").strip())
        pin = request.args.get("pin","").strip()
        result = supabase.table("profiles").select("*").eq("phone",phone).execute()
        if not result.data or str(result.data[0].get("pin","")) != str(pin):
            return jsonify({"error":"Unauthorised"}),401
        profile = result.data[0]
        twilio_num = profile.get("twilio_number") or os.environ.get("TWILIO_NUMBER","")
        # get all messages for this tradesperson's Twilio number
        msgs = supabase.table("client_chats").select("*").eq("twilio_number",twilio_num).order("created_at",desc=True).limit(500).execute().data
        # group by client_number, keep latest message per thread
        threads = {}
        for msg in msgs:
            cn = msg.get("client_number","")
            if cn and cn not in threads:
                threads[cn] = msg
        # look up client names from the contacts directory
        owner_sender = profile.get("sender", "")
        convs = []
        for cn, last in threads.items():
            name = contact_name_for(owner_sender, cn) or cn
            convs.append({"client_number":cn,"client_name":name,"last_message":last.get("message",""),"direction":last.get("direction",""),"created_at":last.get("created_at","")})
        convs.sort(key=lambda x:x.get("created_at",""),reverse=True)
        return jsonify({"conversations":convs})
    except Exception as e:
        print("conversations error:",e); return jsonify({"error":str(e)}),500


@app.route("/api/conversation/<client_number>", methods=["GET"])
def conversation_thread(client_number):
    try:
        phone = format_phone(request.args.get("phone","").strip())
        pin = request.args.get("pin","").strip()
        result = supabase.table("profiles").select("*").eq("phone",phone).execute()
        if not result.data or str(result.data[0].get("pin","")) != str(pin):
            return jsonify({"error":"Unauthorised"}),401
        prof = result.data[0]
        twilio_num = prof.get("twilio_number") or os.environ.get("TWILIO_NUMBER","")
        msgs = supabase.table("client_chats").select("*").eq("twilio_number",twilio_num).eq("client_number",client_number).order("created_at").execute().data
        return jsonify({"messages":msgs})
    except Exception as e:
        print("thread error:",e); return jsonify({"error":str(e)}),500


@app.route("/api/reply-client", methods=["POST"])
def reply_client():
    try:
        phone = format_phone(request.args.get("phone","").strip())
        pin = request.args.get("pin","").strip()
        result = supabase.table("profiles").select("*").eq("phone",phone).execute()
        if not result.data or str(result.data[0].get("pin","")) != str(pin):
            return jsonify({"error":"Unauthorised"}),401
        profile = result.data[0]
        sp = profile.get("sender","")
        data = request.json
        client_number = data.get("client_number","")
        message = (data.get("message") or "").strip()
        if not client_number or not message:
            return jsonify({"error":"Missing fields"}),400
        result_send = send_to_client(profile, client_number, message)
        if not result_send["ok"]:
            return jsonify({"ok":False,"error":result_send["error"] or "Could not send."}),200
        return jsonify({"ok":True,"channel":result_send["channel"]})
    except Exception as e:
        print("reply error:", repr(e)); print(traceback.format_exc()); return jsonify({"ok":False,"error":str(e)}),200


@app.route("/api/backfill-contacts", methods=["POST"])
def backfill_contacts():
    """One-off: read existing client threads and pull out any name the client
    already stated, so the inbox shows names for past conversations too."""
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        owner_sender = profile.get("sender", "")
        twilio_num = profile.get("twilio_number") or os.environ.get("TWILIO_NUMBER", "")

        msgs = (supabase.table("client_chats").select("*")
                .eq("twilio_number", twilio_num).order("created_at")
                .limit(800).execute().data or [])
        # group inbound text per client number
        threads = {}
        for m in msgs:
            if m.get("direction") != "inbound":
                continue
            cn = m.get("client_number", "")
            if not cn:
                continue
            threads.setdefault(cn, []).append((m.get("message", "") or "")[:300])

        # only the ones we don't already have a name for
        unnamed = [(cn, txt) for cn, txt in threads.items() if not contact_name_for(owner_sender, cn)]
        unnamed = unnamed[:40]
        if not unnamed:
            return jsonify({"filled": 0, "unnamed": 0})

        blocks = []
        for i, (cn, txts) in enumerate(unnamed):
            joined = " | ".join(txts[:6])[:600]
            blocks.append(str(i) + ": " + joined)
        prompt = (
            "Each line below is one customer's text messages to a tradesperson, numbered. "
            "If the customer clearly states their own name, return it. Ignore other people's names "
            "and place names. Return ONLY a JSON array like [{\"i\":0,\"name\":\"Dave Smith\"}], "
            "including only lines where you are confident of the customer's name.\n\n" + "\n".join(blocks)
        )
        filled = 0
        try:
            r = client.messages.create(model="claude-sonnet-4-5", max_tokens=600,
                                       messages=[{"role": "user", "content": prompt}])
            raw = "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            start, end = raw.find("["), raw.rfind("]")
            if start != -1 and end != -1:
                arr = json.loads(raw[start:end + 1])
                for item in arr:
                    idx = item.get("i")
                    nm = (item.get("name") or "").strip()
                    if isinstance(idx, int) and 0 <= idx < len(unnamed) and nm:
                        upsert_contact(owner_sender, unnamed[idx][0], nm)
                        filled += 1
        except Exception as le:
            print("backfill LLM error:", le)
        return jsonify({"filled": filled, "unnamed": len(unnamed)})
    except Exception as e:
        print("backfill_contacts error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/check-phone", methods=["GET"])
def check_phone():
    try:
        phone = format_phone(request.args.get("phone","").strip())
        if not phone:
            return jsonify({"exists": False})
        result = supabase.table("profiles").select("phone").eq("phone", phone).execute()
        return jsonify({"exists": bool(result.data)})
    except Exception as e:
        return jsonify({"exists": False, "error": str(e)})


@app.route("/api/pending-clients", methods=["GET"])
def pending_clients():
    try:
        phone = format_phone(request.args.get("phone","").strip())
        pin = request.args.get("pin","").strip()
        result = supabase.table("profiles").select("*").eq("phone",phone).execute()
        if not result.data or str(result.data[0].get("pin","")) != str(pin):
            return jsonify({"error":"Unauthorised"}),401
        pending = supabase.table("profiles").select("phone,owner_name,business_name,trade,location,twilio_number").ilike("twilio_number","pending:%").execute()
        return jsonify({"clients": pending.data or []})
    except Exception as e:
        return jsonify({"error":str(e)}),500


@app.route("/api/activate-client", methods=["POST"])
def activate_client():
    try:
        phone = format_phone(request.args.get("phone","").strip())
        pin = request.args.get("pin","").strip()
        result = supabase.table("profiles").select("*").eq("phone",phone).execute()
        if not result.data or str(result.data[0].get("pin","")) != str(pin):
            return jsonify({"error":"Unauthorised"}),401
        data = request.json or {}
        client_phone = format_phone((data.get("client_phone") or "").strip())
        twilio_num   = (data.get("twilio_number") or "").strip()
        if not client_phone or not twilio_num:
            return jsonify({"error":"client_phone and twilio_number required"}),400
        # update the client profile
        supabase.table("profiles").update({"twilio_number": twilio_num}).eq("phone",client_phone).execute()
        # fetch client profile for the message
        cp = supabase.table("profiles").select("*").eq("phone",client_phone).execute()
        if not cp.data:
            return jsonify({"error":"Client not found"}),404
        client = cp.data[0]
        biz = client.get("business_name") or client.get("owner_name","")
        dashboard_url = "https://trades-pa-trades-pa.up.railway.app/dashboard"
        # send WhatsApp to client
        try:
            account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
            auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")
            from_num    = os.environ.get("TWILIO_NUMBER","")
            from twilio.rest import Client as TwilioClient
            tc = TwilioClient(account_sid, auth_token)
            msg = ("Hi " + (client.get("owner_name") or biz) + "! Your VanOffice bot is now live. "
                   + "Customers can text you on " + twilio_num + " to get quotes and enquiries. "
                   + "Log into your dashboard here: " + dashboard_url)
            tc.messages.create(body=msg, from_="whatsapp:"+from_num, to="whatsapp:"+client_phone)
        except Exception as ne:
            print("Activate notify error:", ne)
            return jsonify({"ok":True, "warned":"Activated but WhatsApp notification failed: "+str(ne)})
        return jsonify({"ok":True, "message":"Activated and client notified"})
    except Exception as e:
        print("activate_client error:",e)
        return jsonify({"error":str(e)}),500


def notify_admin(text):
    """Alert the operator (Harry). Tries Telegram, then SMS, then WhatsApp — uses whatever is configured."""
    # 1) Telegram — most reliable, no number/template issues
    try:
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")
        if tg_token and tg_chat:
            import requests as _rq
            r = _rq.post("https://api.telegram.org/bot" + tg_token + "/sendMessage",
                         json={"chat_id": tg_chat, "text": text}, timeout=15)
            if r.ok:
                return True
    except Exception as e:
        print("notify_admin telegram error:", e)
    # 2) SMS — needs an SMS-capable Twilio number in ADMIN_SMS_FROM
    try:
        admin_phone = os.environ.get("ADMIN_PHONE", "")
        sms_from = os.environ.get("ADMIN_SMS_FROM", "")
        if admin_phone and sms_from:
            tc = TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
            tc.messages.create(body=text, from_=sms_from, to=admin_phone)
            return True
    except Exception as e:
        print("notify_admin sms error:", e)
    # 3) WhatsApp fallback — only delivers if you've messaged the bot in the last 24h
    try:
        admin_phone = os.environ.get("ADMIN_PHONE", "")
        wa_from = os.environ.get("TWILIO_NUMBER", "")
        if admin_phone and wa_from:
            tc = TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
            tc.messages.create(body=text, from_="whatsapp:" + wa_from, to="whatsapp:" + admin_phone)
            return True
    except Exception as e:
        print("notify_admin whatsapp error:", e)
    return False


@app.route("/api/request-setup", methods=["POST"])
def request_setup():
    try:
        phone = format_phone(request.args.get("phone","").strip())
        pin = request.args.get("pin","").strip()
        result = supabase.table("profiles").select("*").eq("phone",phone).execute()
        if not result.data or str(result.data[0].get("pin","")) != str(pin):
            return jsonify({"error":"Unauthorised"}),401
        profile = result.data[0]
        data = request.json or {}
        requested = (data.get("requested_number") or "").strip()
        want_new = data.get("want_new", False)
        # store pending state in twilio_number field
        pending_val = "pending:new" if (want_new or not requested) else "pending:"+requested
        supabase.table("profiles").update({"twilio_number": pending_val}).eq("phone",phone).execute()
        # notify Harry
        try:
            biz = profile.get("business_name") or profile.get("owner_name", "?")
            trade = profile.get("trade", "")
            loc = profile.get("location", "")
            body_msg = ("WhatsApp setup request:\n" + biz + " (" + trade + ", " + loc + ")\n"
                        + "Wants a new number allocated\nPhone: " + phone
                        + "\n\nRegister in Twilio then update their twilio_number in Supabase.")
            notify_admin(body_msg)
        except Exception as ne:
            print("Setup notify error:", ne)
        return jsonify({"ok":True})
    except Exception as e:
        print("request_setup error:",e)
        return jsonify({"error":str(e)}),500


@app.route("/api/register", methods=["POST"])
def register():
    try:
        data = request.json or {}
        phone = format_phone((data.get("phone") or "").strip())
        pin = str(data.get("pin","")).strip()
        if not phone or not pin or len(pin) < 4:
            return jsonify({"error": "Phone and 4-digit PIN required"}), 400
        existing = supabase.table("profiles").select("phone").eq("phone", phone).execute()
        if existing.data:
            return jsonify({"error": "An account already exists for this number"}), 409
        profile = {
            "phone": phone, "pin": pin, "sender": phone,
            "owner_name": (data.get("owner_name") or "").strip(),
            "business_name": (data.get("business_name") or "").strip(),
            "trade": (data.get("trade") or "").strip(),
            "location": (data.get("location") or "").strip(),
            "day_rate": str(data.get("day_rate") or "300"),
            "half_day_rate": str(data.get("half_day_rate") or "175"),
            "hourly_rate": str(data.get("hourly_rate") or "45"),
            "materials_markup": "20", "payment_terms": "30",
            "vat_registered": "no", "twilio_number": "", "logo": ""
        }
        result = supabase.table("profiles").insert(profile).execute()
        if not result.data:
            return jsonify({"error": "Registration failed — please try again"}), 500
        # notify admin
        try:
            admin_phone = os.environ.get("ADMIN_PHONE","")
            twilio_num = os.environ.get("TWILIO_NUMBER","")
            if admin_phone and twilio_num:
                from twilio.rest import Client as TwilioClient
                tc = TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"),
                                  os.environ.get("TWILIO_AUTH_TOKEN"))
                biz = profile["business_name"] or profile["owner_name"]
                tc.messages.create(
                    body=f"New VanOffice signup: {biz} ({profile['trade']}, {profile['location']}). Phone: {phone}. Register their WhatsApp sender in Twilio to activate.",
                    from_="whatsapp:"+twilio_num, to="whatsapp:"+admin_phone)
        except Exception as ne:
            print("Admin notify error:", ne)
        return jsonify({"ok": True})
    except Exception as e:
        print("Register error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/send-quote-voice", methods=["POST"])
def send_quote_voice():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        data = request.json
        client_name = data.get("client_name", "Customer")
        job_desc = data.get("job_description", "")
        total = str(data.get("total", "0"))
        scope_items = data.get("scope_items", [])

        # save quote to DB
        quote_count = supabase.table("quotes").select("id").eq("sender", sender).execute()
        quote_num = "QU-" + str(len(quote_count.data) + 1).zfill(3)
        quote = {
            "sender": sender, "client_name": client_name,
            "client_address": data.get("client_address", ""),
            "job_description": job_desc, "total": total, "subtotal": total,
            "vat": "0", "status": "sent",
            "line_items": [{"description": s, "amount": ""} for s in scope_items],
            "quote_number": quote_num, "quote_text": "", "client_number": ""
        }

        # look up client WhatsApp number from enquiries by name
        client_number = ""
        try:
            first_word = client_name.split()[0] if client_name.split() else client_name
            enq = supabase.table("enquiries").select("sender").ilike("client_name", f"%{first_word}%").order("created_at", desc=True).limit(1).execute()
            if enq.data:
                client_number = enq.data[0].get("sender", "")
        except Exception as e:
            print("Client lookup error:", e)

        quote["client_number"] = client_number
        if not client_number:
            quote["status"] = "draft"
        res = supabase.table("quotes").insert(quote).execute()
        saved = res.data[0] if res.data else quote
        quote_id = saved.get("id", "")

        # send via WhatsApp if we found the client number
        sent = False
        if client_number and quote_id:
            try:
                account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
                twilio_number = profile.get("twilio_number") or os.environ.get("TWILIO_NUMBER", "")
                base_url = data.get("base_url", "").rstrip("/")
                pdf_url = f"{base_url}/generate-pdf/{quote_id}"
                biz = profile.get("business_name", "your tradesperson")
                msg = (f"Hi! Here's your quote from {biz}.\n\n"
                       f"📋 {quote_num}\n{job_desc}\n\n"
                       f"*Total: £{total}*\n\n"
                       f"📄 View & download your quote:\n{pdf_url}")
                from twilio.rest import Client as TwilioClient
                tc = TwilioClient(account_sid, auth_token)
                tc.messages.create(body=msg, from_=f"whatsapp:{twilio_number}", to=f"whatsapp:{client_number}")
                # mark enquiry as quoted
                try:
                    supabase.table("enquiries").update({"status": "quoted"}).ilike("client_name", f"%{first_word}%").execute()
                except Exception:
                    pass
                sent = True
            except Exception as e:
                print("WhatsApp send error:", e)

        return jsonify({
            "ok": True, "sent": sent,
            "quote_number": quote_num, "client_number": client_number,
            "message": f"Sent to {client_name} on WhatsApp" if sent else f"Saved as {quote_num} — couldn't find {client_name}'s number"
        })
    except Exception as e:
        print("send_quote_voice error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/stt", methods=["POST"])
def stt():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        if not phone or not pin:
            return jsonify({"error": "Auth required"}), 401
        result = supabase.table("profiles").select("pin,sender").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        _stt_sender = result.data[0].get("sender", "")

        if "audio" not in request.files:
            return jsonify({"error": "No audio"}), 400
        audio = request.files["audio"]
        audio_bytes = audio.read()
        if not audio_bytes:
            return jsonify({"error": "Empty audio"}), 400

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return jsonify({"error": "OpenAI not configured"}), 503

        import requests as http_requests
        filename = audio.filename or "speech.webm"
        r = http_requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": "Bearer " + api_key},
            files={"file": (filename, audio_bytes, audio.mimetype or "audio/webm")},
            data={"model": "whisper-1", "language": "en"},
            timeout=30
        )
        if r.status_code != 200:
            print("STT error:", r.status_code, r.text[:300])
            return jsonify({"error": "STT failed"}), 502
        try: track_usage(_stt_sender, "voice")
        except Exception: pass
        return jsonify({"text": r.json().get("text", "")})
    except Exception as e:
        print("STT error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/tts", methods=["POST"])
def tts():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        if not phone or not pin:
            return jsonify({"error": "Auth required"}), 401
        result = supabase.table("profiles").select("pin").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401

        text = (request.json.get("text") or "")[:800]
        if not text:
            return jsonify({"error": "No text"}), 400

        import requests as http_requests
        voice_id = os.environ.get("ELEVEN_VOICE_ID", "")
        api_key  = os.environ.get("ELEVEN_API_KEY", "")
        if not voice_id or not api_key:
            return jsonify({"error": "ElevenLabs not configured"}), 503

        r = http_requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": "eleven_flash_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
            },
            params={"output_format": "mp3_44100_128"},
            timeout=15
        )
        if r.status_code != 200:
            return jsonify({"error": "ElevenLabs error", "detail": r.text}), 502
        return Response(r.content, mimetype="audio/mpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        print("TTS error:", e)
        return jsonify({"error": str(e)}), 500



# ───────────────────────── PWA + WEB PUSH ─────────────────────────
PWA_ICON_180 = "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"
PWA_ICON_192 = "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAIJElEQVR42u2dWVoc1xWAax66wSBAgBCi1UzdWEhGZrDWkOcsIRvIEvLmt+whz1mA12CQTTxggxBIwpLRZCEsdXX1UJWHDJ/i2BZwaq7/f4MPbk1/3XPuPbeq1NpnnyoAF0XjFAACAQIBAgECASAQIBAgECAQAAIBAgECAQIBIBAgECAQIBAAAgECAQIBAgECASAQIBAgECAQAAIBAgECAQIBIBAgECAQIBAAAgECAQIBAgECASAQIBAgECAQAAIBAgECQeExsrlbnf0nz//6d2Ejl//8R2t+KvVj6T07efqXvwkbGfnTH9zb8/RAZ8WamzLGhoSNtDa/z8KxyHdDq9jOzToh7DyoirvRFLbhfXEv7PVTPxTv811hC+7qomroCHQ+Kp80FVXUQtDy/W8fpByLD497L16LT8USSfT5s7OxIWtWmsG0xHd/6vHLGB+26pMIdKE77470zmt/cxh4fmoHEATe3XsF7n4yL9DHC6opGieGvb735X5a+9/eeRS88YS5YEWcC5ZXINWxnFuzqeewKcYve2FaHxlEoDSjmL//Q//Vz8nveeh3218dFDt+5UAgpzmjD1VlV1Jpbe6lMHr/x/2w0xN1wJaRzcnDPAmkaKq73pBeyzRmFFufSzfqrsyrtolA4igm7sa7T152H79IdPj1c8vfPSp8/MqHQObUqHntcur9wfk2t7WnBKGkBX14wG5MI1BWOiFva08Jw8R22BP7WtloKqqKQBEJtN5QNNGu9k/e+HuPk9nb3tNXnUfPxPdMMxeXJh8CaQOuc6MmDStJpdKtTenMk1WbMCZHEChbUay9vR92kyjOe2KB8tL95Ekg52Zdq9iikZHXaX99EPd+dg5/FJbfVV1z1xoIFDGqoburi6kHlzMM96SbcJbrWtVBoCxGMf/bB8Hbdoy7GATeF/LyezNHFyVPAln1SWN8WNJC2I/gAv9emrXzUFh+16qOvVxHoOx2QrHOKMrjl7vWUHUNgWITaEO6zrVz+GP/5Wkc+xb6XXmSLl99gEC/hz4yaC/IJvjDuFJpb3tfWH43JkesmXEEKmkUk8evXFRPcy+Qe3tetUSLHHpPX3XFpYZf0D9t+Xuy8ruqVjYaCBQ7qm26K3NZ64S8rV1h+d1uXNOHBxAokSgmzjS9u9LlFr80skzli9wLZC9O65dES837pxEs+IowJqq26a7MI1BiYSyCdCHCKBbB6tXb86plIFCexmLyRe/vJEDSRfu5m/7JvUDGxCXr+oSkhUgeu1EUpXMgLb/rI4P2/DQCJd4JbWRiQqgVzepVBYGSxl2TvvSk/Z300eNIqrN5nD8sgkBa1XGWr4uaCIKWLH3xdx4K14dY9SvCJQYIlGoqLZu/iWD6585Sri9BvgWyb1zXBlxRCvzguPf85ILxq90RpuGqoburCwiUGqquVdbE61wvWgT1tu+HXdFEgHNrVnNtBCppFJM/J5TT8kWhBDJnxs0ro5IWes9POg+Oz/tf/ddv/d0fRKd+0HU+rCFQFjoh6X18gSgmf1a6siZ93BaBosHdaCqaaCbOu7unBAHjr5IKpA9V7cY1SQvBG6+98+gcUe/pq+6RqPxuTo2a05cRqDip9Lky4gjKF4XofoojkLsypzqWpIX2Vweh3z3Tn8qX5Wuqu95EoAyhmtLXCYadnrd9phcCyx8McpZm9A8qCJSxKHZHXpzfPdufyad/lgpz2osjkD13VR/9QNKCv3fUP229p6MSl98113JuzSFQBsOYeEIoCL2t93RC8tczuLcXVFNHoIKOxd4XxZj+KbJAxtiQNXtF0kL36Fnv+KffjF/tTvvrQ+kezk0hUKE7od/uY+Tl9wJUTwsukLu6KMwwWpu7ShjP+CuKzzAiUMzHIx7j9F+edg6e/MrvX7/190Tl90g+BItACUQxeXH+V3oab2tXWH6vfrJUvLNdQIGcpZpwntf7cv//v9YrHH+ppuF+vIBAuTgm6Qd+grdtf+fhu7/pHf/UPXou0vqjWWG1DoFyNRb73yhWzpdHlVcg8+qYeXVM0kL7m8PA6/z7h1BpbYkE0oeqTnMGgXLVCckmfMNuv/2f4nzn4Imw/O6uN4RrJhEocYHWm8IVx/+NYpTfyyiQNug6H4qihn/vcf/kTdgPhB8ON69dNqdGEah8qXQYepu78vJ7gbsfRVGMAh/bv577DDz/4lFsc9eYuCS7Q7XKWqPAJ7nIPZD8yfPu4xfe9n2RxDdq2qCLQGWNYopy3ufFShW/ii+QNZvm23e0iu3crCNQ6Tuhi+KuSt+hhkAZECi9NxAWPn6VQqC03oFqjA9b9UkEKkYUa6ax0aUynNtSCJTCe+BVpVK41avlFUh1LPejRJ/lsxem9ZFBBCpQFEv2aaySxK8SCZTk17hUyxS+6QGBMhjGkvseoLsyp9omAhVvLLZUsA0hUKIk801kfXjAbkwjEKn0RTex0VRUFYGKibvWUPV4D7l4T78j0DtHW3Xs5RjL41ZtwpgcQaAiU42zhyhV+lxSgezlulZ1YpkoMHRX/OUXBMo6qq658SxSdm5cj0lNBMrYWCyeKFa29Lm8Alm1CeNKxKmuNuDGmp4jUMY6oY2Is113dTHuCYKMpgS1zz5VAOiBAIEAgQCBABAIEAgQCBAIAIEAgQCBAIEAEAgQCBAIEAgAgQCBAIEAgQCBABAIEAgQCBAIAIEAgQCBAIEAEAgQCBAIEAgAgQCBAIEAgQCBABAIEAgQCBAIAIEAgQCBAIEAEAgQCBAIEAgAgQCBAIEAgQCBABAIEAhyyD8BL1504PrO3SAAAAAASUVORK5CYII="
PWA_ICON_512 = "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"


def _png_response(b64):
    import base64 as _b64
    return Response(_b64.b64decode(b64), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})


@app.route("/icons/icon-180.png")
def _icon_180():
    return _png_response(PWA_ICON_180)


@app.route("/icons/icon-192.png")
def _icon_192():
    return _png_response(PWA_ICON_192)


@app.route("/icons/icon-512.png")
def _icon_512():
    return _png_response(PWA_ICON_512)


@app.route("/manifest.webmanifest")
def web_manifest():
    m = {
        "name": "VanOffice", "short_name": "VanOffice",
        "description": "Your van office \u2014 quotes, invoices, diary and enquiries.",
        "start_url": "/dashboard", "scope": "/",
        "display": "standalone", "orientation": "portrait",
        "background_color": "#0b0c0e", "theme_color": "#0b0c0e",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    }
    return Response(json.dumps(m), mimetype="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


_SERVICE_WORKER_JS = "self.addEventListener('install', function(e){ self.skipWaiting(); });\nself.addEventListener('activate', function(e){ e.waitUntil(self.clients.claim()); });\nself.addEventListener('push', function(e){\n  var d={title:'VanOffice', body:'You have a new notification', url:'/dashboard'};\n  try{ if(e.data){ d=Object.assign(d, e.data.json()); } }catch(_){ try{ if(e.data) d.body=e.data.text(); }catch(__){} }\n  e.waitUntil(self.registration.showNotification(d.title,{\n    body:d.body, icon:'/icons/icon-192.png', badge:'/icons/icon-192.png',\n    data:{url:d.url||'/dashboard'}, vibrate:[60,30,60]\n  }));\n});\nself.addEventListener('notificationclick', function(e){\n  e.notification.close();\n  var url=(e.notification.data&&e.notification.data.url)||'/dashboard';\n  e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(function(ws){\n    for(var i=0;i<ws.length;i++){ if(ws[i].url.indexOf('/dashboard')>-1 && 'focus' in ws[i]) return ws[i].focus(); }\n    if(clients.openWindow) return clients.openWindow(url);\n  }));\n});\n"


@app.route("/sw.js")
def service_worker():
    return Response(_SERVICE_WORKER_JS, mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.route("/api/push/public-key")
def push_public_key():
    return jsonify({"key": os.environ.get("VAPID_PUBLIC_KEY", "")})


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        sender = result.data[0].get("sender", "")
        sub = request.json or {}
        endpoint = sub.get("endpoint") or ""
        if not endpoint:
            return jsonify({"error": "No subscription"}), 400
        existing = (supabase.table("push_subscriptions").select("id")
                    .eq("sender", sender).eq("endpoint", endpoint).limit(1).execute().data or [])
        row = {"sender": sender, "endpoint": endpoint, "subscription": sub}
        if existing:
            supabase.table("push_subscriptions").update(row).eq("id", existing[0]["id"]).execute()
        else:
            supabase.table("push_subscriptions").insert(row).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print("push_subscribe error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/push/test", methods=["POST"])
def push_test():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        sender = result.data[0].get("sender", "")
        if not os.environ.get("VAPID_PUBLIC_KEY") or not os.environ.get("VAPID_PRIVATE_KEY"):
            return jsonify({"ok": False, "error": "Push not configured on the server (VAPID keys missing)."})
        try:
            subs = supabase.table("push_subscriptions").select("id").eq("sender", sender).execute().data or []
        except Exception:
            subs = []
        if not subs:
            return jsonify({"ok": False, "error": "No device registered yet — tap Enable first."})
        send_web_push(sender, "Test notification \u2014 push is working \u2713", title="VanOffice")
        return jsonify({"ok": True, "devices": len(subs)})
    except Exception as e:
        print("push_test error:", e)
        return jsonify({"ok": False, "error": str(e)}), 500


def send_web_push(sender, message, title="VanOffice"):
    """Best-effort web push to the owner's installed devices. Never raises."""
    pub = os.environ.get("VAPID_PUBLIC_KEY", "")
    priv = os.environ.get("VAPID_PRIVATE_KEY", "")
    subj = os.environ.get("VAPID_SUBJECT", "mailto:hello@vanoffice.app")
    if not pub or not priv or not sender:
        return
    try:
        from pywebpush import webpush, WebPushException
    except Exception as e:
        print("pywebpush not installed:", e)
        return
    try:
        subs = supabase.table("push_subscriptions").select("*").eq("sender", sender).execute().data or []
    except Exception as e:
        print("push subs lookup:", e)
        return
    payload = json.dumps({"title": title, "body": message, "url": "/dashboard"})
    for s in subs:
        sub = s.get("subscription")
        if not sub:
            continue
        try:
            webpush(subscription_info=sub, data=payload,
                    vapid_private_key=priv, vapid_claims={"sub": subj})
        except WebPushException as we:
            code = getattr(getattr(we, "response", None), "status_code", None)
            if code in (404, 410):
                try:
                    supabase.table("push_subscriptions").delete().eq("id", s["id"]).execute()
                except Exception:
                    pass
            else:
                print("webpush error:", we)
        except Exception as e:
            print("webpush error:", e)



# ───────────────────────── ACCOUNTS: EXPENSES, MILEAGE, TAX SET-ASIDE ─────────────────────────
# Lightweight bookkeeping for sole-trader users: log expenses (incl. snapping a
# receipt), track mileage, and see income vs expenses with a ROUGH tax set-aside.
# This is a guide to keep records tidy for an accountant / MTD tool — not advice,
# and it does NOT file anything with HMRC.

EXPENSE_CATEGORIES = ["Materials", "Tools", "Fuel", "Vehicle", "Travel",
                      "Phone/Internet", "Insurance", "Subcontractor",
                      "Office/Admin", "Other"]

# HMRC simplified mileage rate (first 10,000 business miles/yr). Update if HMRC changes it.
MILEAGE_RATE = 0.45

# --- Rough self-employed tax estimate (England/Wales/NI bands). CLEARLY an estimate;
#     update these constants each tax year. Ignores the personal-allowance taper above
#     £100k and any other income, so it's a "set aside roughly this" guide only. ---
TAX_PERSONAL_ALLOWANCE = 12570
TAX_BASIC_LIMIT = 50270      # taxable income (after allowance) ceiling for 20%
TAX_HIGHER_LIMIT = 125140    # ceiling for 40%; above is 45%
NIC4_LOWER = 12570
NIC4_UPPER = 50270
NIC4_MAIN_RATE = 0.06        # Class 4 main rate
NIC4_UPPER_RATE = 0.02


def uk_tax_year_bounds(d=None):
    """Return (start_iso, end_iso, label) for the UK tax year containing date d (6 Apr - 5 Apr)."""
    d = d or datetime.date.today()
    start_year = d.year if (d.month > 4 or (d.month == 4 and d.day >= 6)) else d.year - 1
    start = datetime.date(start_year, 4, 6)
    end = datetime.date(start_year + 1, 4, 5)
    label = str(start_year) + "/" + str((start_year + 1) % 100).zfill(2)
    return start.isoformat(), end.isoformat(), label


def estimate_self_employed_tax(profit):
    """Rough Income Tax + Class 4 NIC on a year's profit. A guide, not advice."""
    profit = max(0.0, float(profit or 0))
    taxable = max(0.0, profit - TAX_PERSONAL_ALLOWANCE)
    tax = 0.0
    basic_band = max(0.0, TAX_BASIC_LIMIT - TAX_PERSONAL_ALLOWANCE)
    higher_band = max(0.0, TAX_HIGHER_LIMIT - TAX_BASIC_LIMIT)
    tax += min(taxable, basic_band) * 0.20
    if taxable > basic_band:
        tax += min(taxable - basic_band, higher_band) * 0.40
    if taxable > basic_band + higher_band:
        tax += (taxable - basic_band - higher_band) * 0.45
    nic = 0.0
    if profit > NIC4_LOWER:
        nic += (min(profit, NIC4_UPPER) - NIC4_LOWER) * NIC4_MAIN_RATE
    if profit > NIC4_UPPER:
        nic += (profit - NIC4_UPPER) * NIC4_UPPER_RATE
    return round(tax + nic)


def _money_f(v):
    try:
        return float(str(v).replace("\u00a3", "").replace(",", "").strip() or 0)
    except Exception:
        return 0.0


def _expense_auth():
    phone = format_phone(request.args.get("phone", "").strip())
    pin = request.args.get("pin", "").strip()
    result = supabase.table("profiles").select("*").eq("phone", phone).execute()
    if not result.data or str(result.data[0].get("pin", "")) != str(pin):
        return None
    return result.data[0]


@app.route("/api/expense/scan", methods=["POST"])
def expense_scan():
    """Read a receipt photo with Claude vision and return the fields to confirm."""
    profile = _expense_auth()
    if not profile:
        return jsonify({"error": "Unauthorised"}), 401
    data = request.json or {}
    img = data.get("image", "")
    img_type = data.get("imageType", "image/jpeg")
    if not img:
        return jsonify({"error": "No image"}), 400
    if img.startswith("data:"):
        try:
            img_type = img.split(";")[0].split(":")[1]
            img = img.split(",", 1)[1]
        except Exception:
            pass
    prompt = ("This is a photo of a receipt or invoice for a UK tradesperson's business expense. "
              "Extract these fields: the total amount paid (digits only, in GBP), the date (YYYY-MM-DD if shown, else empty), "
              "the supplier/shop name, the single best category from this exact list "
              "[" + ", ".join(EXPENSE_CATEGORIES) + "], and the VAT amount if shown (digits only, else empty). "
              "Reply with ONLY a JSON object, no other text: "
              '{"amount":"","date":"","supplier":"","category":"","vat":""}')
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=400,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": img_type, "data": img}},
                {"type": "text", "text": prompt}
            ]}])
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        s, e = raw.find("{"), raw.rfind("}")
        out = json.loads(raw[s:e + 1]) if s != -1 and e != -1 else {}
        cat = out.get("category", "")
        if cat not in EXPENSE_CATEGORIES:
            cat = "Materials"
        return jsonify({
            "amount": str(out.get("amount", "") or "").replace("\u00a3", "").replace(",", "").strip(),
            "date": out.get("date", "") or "",
            "supplier": out.get("supplier", "") or "",
            "category": cat,
            "vat": str(out.get("vat", "") or "").replace("\u00a3", "").strip()
        })
    except Exception as ex:
        print("expense_scan error:", ex)
        return jsonify({"error": "Couldn't read that receipt - add the details by hand."}), 200


@app.route("/api/expense", methods=["POST"])
def expense_create():
    profile = _expense_auth()
    if not profile:
        return jsonify({"error": "Unauthorised"}), 401
    sender = profile.get("sender", "")
    d = request.json or {}
    kind = d.get("kind", "manual")
    date = d.get("date", "") or datetime.date.today().isoformat()
    if kind == "mileage":
        miles = int(_money_f(d.get("miles", 0)))
        amount = round(miles * MILEAGE_RATE, 2)
        row = {"sender": sender, "date": date, "amount": str(amount), "category": "Vehicle",
               "supplier": "Mileage (" + str(miles) + " miles)", "notes": d.get("notes", ""),
               "vat": "", "kind": "mileage", "miles": miles}
    else:
        amount = round(_money_f(d.get("amount", 0)), 2)
        if amount <= 0:
            return jsonify({"error": "Enter an amount"}), 400
        cat = d.get("category", "Other")
        if cat not in EXPENSE_CATEGORIES:
            cat = "Other"
        row = {"sender": sender, "date": date, "amount": str(amount), "category": cat,
               "supplier": d.get("supplier", ""), "notes": d.get("notes", ""),
               "vat": str(d.get("vat", "") or ""), "kind": "manual", "miles": 0}
    try:
        res = supabase.table("expenses").insert(row).execute()
        return jsonify({"ok": True, "expense": (res.data[0] if res.data else row)})
    except Exception as ex:
        print("expense_create error:", ex)
        return jsonify({"error": str(ex)}), 500


@app.route("/api/expense/delete", methods=["POST"])
def expense_delete():
    profile = _expense_auth()
    if not profile:
        return jsonify({"error": "Unauthorised"}), 401
    sender = profile.get("sender", "")
    d = request.json or {}
    eid = d.get("id")
    if not eid:
        return jsonify({"error": "No id"}), 400
    try:
        supabase.table("expenses").delete().eq("id", eid).eq("sender", sender).execute()
        return jsonify({"ok": True})
    except Exception as ex:
        print("expense_delete error:", ex)
        return jsonify({"error": str(ex)}), 500


@app.route("/api/expenses", methods=["GET"])
def expenses_list():
    profile = _expense_auth()
    if not profile:
        return jsonify({"error": "Unauthorised"}), 401
    sender = profile.get("sender", "")
    start_iso, end_iso, label = uk_tax_year_bounds()
    try:
        rows = (supabase.table("expenses").select("*").eq("sender", sender)
                .order("date", desc=True).limit(500).execute().data or [])
    except Exception as ex:
        print("expenses_list error:", ex)
        rows = []
    # income from paid invoices in this tax year (by created date)
    try:
        invs = supabase.table("invoices").select("*").eq("sender", sender).execute().data or []
    except Exception:
        invs = []
    def _in_year(iso):
        return bool(iso) and start_iso <= iso[:10] <= end_iso
    income_paid = sum(_money_f(i.get("total")) for i in invs
                      if i.get("status") == "paid" and _in_year(i.get("created_at", "")))
    income_invoiced = sum(_money_f(i.get("total")) for i in invs
                          if _in_year(i.get("created_at", "")))
    exp_year = [r for r in rows if _in_year(r.get("date", ""))]
    expenses_total = sum(_money_f(r.get("amount")) for r in exp_year)
    profit = income_paid - expenses_total
    set_aside = estimate_self_employed_tax(profit)
    summary = {
        "tax_year": label,
        "income_paid": round(income_paid, 2),
        "income_invoiced": round(income_invoiced, 2),
        "expenses": round(expenses_total, 2),
        "profit": round(profit, 2),
        "set_aside": set_aside,
        "mileage_rate": MILEAGE_RATE
    }
    return jsonify({"expenses": rows, "summary": summary, "categories": EXPENSE_CATEGORIES})



# ───────────────────────── QUOTE EXTRAS (variations on an existing quote) ─────────────────────────
def _num_str(x):
    try:
        x = round(float(str(x).replace("\u00a3", "").replace(",", "").strip()), 2)
    except Exception:
        return "0"
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _apply_quote_extra(sender, quote_id, description, amount):
    """Append an agreed extra to an existing quote and bump its total. Returns (quote, error)."""
    try:
        amt = float(str(amount).replace("\u00a3", "").replace(",", "").strip() or 0)
    except Exception:
        amt = 0.0
    if amt <= 0:
        return None, "I need an amount for the extra."
    rows = (supabase.table("quotes").select("*").eq("id", quote_id)
            .eq("sender", sender).limit(1).execute().data or [])
    if not rows:
        return None, "Couldn't find that quote."
    q = rows[0]
    items = q.get("line_items") or []
    if not isinstance(items, list):
        items = []
    desc = (description or "Extra").strip()
    disp = desc if desc.lower().startswith("extra") else ("Extra: " + desc)
    items.append({"description": disp, "amount": _num_str(amt)})
    old_total = float(str(q.get("total", "0")).replace("\u00a3", "").replace(",", "").strip() or 0)
    old_sub = float(str(q.get("subtotal", q.get("total", "0"))).replace("\u00a3", "").replace(",", "").strip() or old_total)
    new_total = _num_str(old_total + amt)
    new_sub = _num_str(old_sub + amt)
    supabase.table("quotes").update({"line_items": items, "total": new_total, "subtotal": new_sub}) \
        .eq("id", quote_id).eq("sender", sender).execute()
    q["line_items"], q["total"], q["subtotal"] = items, new_total, new_sub
    return q, None


@app.route("/api/quote/<quote_id>/add-extra", methods=["POST"])
def quote_add_extra(quote_id):
    profile = _expense_auth()
    if not profile:
        return jsonify({"error": "Unauthorised"}), 401
    sender = profile.get("sender", "")
    d = request.json or {}
    q, err = _apply_quote_extra(sender, quote_id, d.get("description", ""), d.get("amount", 0))
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True, "quote": q})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=os.environ.get("FLASK_DEBUG") == "1")
