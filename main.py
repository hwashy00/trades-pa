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


# ─────────────────────────────────────────────────────────────
# WHATSAPP CLOUD API (Meta direct) — replaces Twilio for WhatsApp.
# Free customer-initiated conversations, no per-message markup.
# Voice + SMS stay on Twilio; only WhatsApp moves here.
# Config via env vars so test number -> real number is a value swap.
# ─────────────────────────────────────────────────────────────
WA_CLOUD_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WA_CLOUD_PHONE_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WA_CLOUD_VERIFY = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
WA_CLOUD_API = "https://graph.facebook.com/v21.0"

def wa_cloud_enabled():
    return bool(WA_CLOUD_TOKEN and WA_CLOUD_PHONE_ID)

def send_whatsapp_cloud(to_number, message):
    """Send a free-form WhatsApp text via Meta Cloud API. Returns True/False.
    Works inside the 24h customer-service window; never raises."""
    try:
        import requests as _req
        to = _strip_wa(to_number).lstrip("+")
        url = WA_CLOUD_API + "/" + WA_CLOUD_PHONE_ID + "/messages"
        headers = {"Authorization": "Bearer " + WA_CLOUD_TOKEN, "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text",
                   "text": {"preview_url": False, "body": message}}
        r = _req.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code in (200, 201):
            return True
        print("send_whatsapp_cloud non-200:", r.status_code, r.text[:300])
        return False
    except Exception as e:
        print("send_whatsapp_cloud error:", e)
        return False

def fetch_wa_cloud_media(media_id):
    """Resolve a Cloud API media id to its bytes. Returns (content_bytes, mime) or (None, None)."""
    try:
        import requests as _req
        headers = {"Authorization": "Bearer " + WA_CLOUD_TOKEN}
        meta = _req.get(WA_CLOUD_API + "/" + str(media_id), headers=headers, timeout=20).json()
        media_url = meta.get("url")
        mime = meta.get("mime_type", "")
        if not media_url:
            return None, None
        media = _req.get(media_url, headers=headers, timeout=30)
        return media.content, mime
    except Exception as e:
        print("fetch_wa_cloud_media error:", e)
        return None, None

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
                if wa_cloud_enabled():
                    ok = send_whatsapp_cloud(clean_to, message)
                    if not ok:
                        raise Exception("whatsapp cloud send failed")
                    return True
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


def _phone_last10(num):
    """Last 10 digits of a phone number, for matching regardless of +44/0 formatting."""
    import re as _r
    return _r.sub(r"\D", "", str(num or ""))[-10:]


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


# ─────────────────────────────────────────────────────────────
# MISSED-CALL TRIAGE
# Caller-ID style filtering: personal list -> stay out of it,
# known client -> personalised reply, unknown -> soft lead text.
# In 'ask' mode (default) the reply is a DRAFT parked in
# pending_actions for one-tap / reply-by-text approval.
# Set profiles.missed_call_mode = 'auto' to send instantly.
# ─────────────────────────────────────────────────────────────
def _clean_num(n):
    return str(n or "").replace("whatsapp:", "").strip()


def _is_personal_contact(owner_sender, number):
    """True if this caller is on the owner's personal (mates/family) list."""
    try:
        r = (supabase.table("personal_contacts").select("id,name")
             .eq("sender", owner_sender).eq("phone", _clean_num(number))
             .limit(1).execute().data or [])
        if r:
            return r[0].get("name") or "Someone on your personal list"
    except Exception as e:
        print("personal_contacts lookup error (table may not exist yet):", e)
    return ""


def _known_client_name(owner_sender, number):
    """Return (is_known, name). Known = saved contact or existing chat history."""
    num = _clean_num(number)
    name = contact_name_for(owner_sender, num)
    if name:
        return True, name
    try:
        r = (supabase.table("client_chats").select("id")
             .eq("sender_profile", owner_sender).eq("client_number", num)
             .limit(1).execute().data or [])
        if r:
            return True, ""
    except Exception as e:
        print("client_chats lookup error:", e)
    return False, ""


def _client_context_hint(owner_sender, name):
    """One short phrase about this client's live work, for the draft. Never raises."""
    try:
        if name:
            q = (supabase.table("quotes").select("status,total,job_type")
                 .eq("sender", owner_sender).ilike("client_name", "%" + name + "%")
                 .order("created_at", desc=True).limit(1).execute().data or [])
            if q:
                st = (q[0].get("status") or "").lower()
                jt = q[0].get("job_type") or "your job"
                if st == "sent":
                    return "the quote we sent for " + jt
                if st in ("accepted", "booked"):
                    return "the " + str(jt) + " job"
            b = (supabase.table("bookings").select("date,job_type")
                 .eq("sender", owner_sender).ilike("client_name", "%" + name + "%")
                 .gte("date", datetime.date.today().isoformat())
                 .order("date").limit(1).execute().data or [])
            if b:
                return "your booking on " + str(b[0].get("date", ""))
    except Exception as e:
        print("client context error:", e)
    return ""


def _missed_call_draft(profile, caller, is_client, client_name):
    """Compose the reply draft for a missed caller. Deterministic fallback, AI polish for clients."""
    owner_first = (profile.get("owner_name") or "the boss").split(" ")[0]
    biz = profile.get("business_name") or "the business"
    if not is_client:
        return ("Hi, sorry we missed your call — " + owner_first + " is on the tools. "
                "I'm the assistant at " + biz + ". If it's about a job, reply here with what "
                "you need and roughly where you are, and I'll get things moving.")
    first = (client_name or "").split(" ")[0]
    hint = _client_context_hint(profile.get("sender", ""), client_name)
    fallback = ("Hi" + ((" " + first) if first else "") + ", sorry " + owner_first +
                " missed your call — I'm the assistant at " + biz + ". " +
                (("Is this about " + hint + ", or something new? ") if hint else "What can we help with? ") +
                "Reply here and I'll sort it.")
    try:
        sys_p = ("Write one short, warm SMS from a tradesperson's assistant to a known customer whose call was just missed. "
                 "British English, no emojis, under 300 characters, no sign-off. Mention the live work naturally if given. Never invent details.")
        ctx = json.dumps({"customer_first_name": first, "owner_first_name": owner_first,
                          "business": biz, "live_work_hint": hint})
        ai = client.messages.create(model="claude-sonnet-4-5", max_tokens=160, system=sys_p,
                                    messages=[{"role": "user", "content": ctx}])
        txt = (ai.content[0].text or "").strip()
        if 20 < len(txt) < 320:
            return txt
    except Exception as e:
        print("missed-call draft AI error:", e)
    return fallback


def _handle_missed_call(profile, caller):
    """Triage one missed call. Sends / drafts / stays quiet as appropriate. Never raises."""
    try:
        sender = profile.get("sender", "")
        caller = _clean_num(caller)
        if not sender or not caller:
            return "skipped"

        # Tier 1: personal — stay out of it, just tell the owner.
        pname = _is_personal_contact(sender, caller)
        if pname:
            notify_owner(profile, pname + " (" + caller + ") called — personal list, so I stayed out of it.")
            return "personal"

        # Tier 2 / 3: known client or unknown lead.
        is_client, cname = _known_client_name(sender, caller)
        draft = _missed_call_draft(profile, caller, is_client, cname)
        who = (cname + " (" + caller + ")") if cname else (caller + (" (existing customer)" if is_client else " (new caller)"))
        mode = (profile.get("missed_call_mode") or "ask").lower()

        if mode == "auto":
            res = send_to_client({"sender": sender, "twilio_number": profile.get("twilio_number", ""),
                                  "business_name": profile.get("business_name", "")}, caller, draft)
            if res.get("ok"):
                notify_owner(profile, "Missed call from " + who + " — I've texted them:\n\n\u201c" + draft +
                             "\u201d\n\nI'll let you know when they reply.")
            else:
                notify_owner(profile, "Missed call from " + who + " — I tried to text them but it failed. Worth a ring back.")
            return "auto-sent"

        # Ask-first (default): park the draft for approval.
        supabase.table("pending_actions").insert({
            "sender": sender, "client_number": caller, "client_name": cname,
            "twilio_number": profile.get("twilio_number", ""), "kind": "missed_call",
            "customer_msg": "(missed call — no message left yet)",
            "reason": "Missed call" + (" from an existing customer" if is_client else " from a new number") + " — here's my draft reply",
            "options": [draft, "Ignore", "That's a mate"], "status": "pending"
        }).execute()
        notify_owner(profile, "Missed call from " + who + ". My draft reply:\n\n\u201c" + draft +
                     "\u201d\n\n1. Send it\n2. Ignore\n3. That's a mate\n\nReply with a number, or tell me what to say instead.")
        return "drafted"
    except Exception as e:
        print("_handle_missed_call error:", e)
        return "error"


# ── Per-trade voicemail greeting ──────────────────────────────────────────
# Spoken to a caller who's been diverted to voicemail. Each trade can set their
# own line in profiles.voicemail_greeting; if they haven't, we build a branded
# default from their business_name so every number sounds like *their* business
# (e.g. GW Plastering's number greets callers as GW Plastering, not generically).
# British English Text-to-Speech voice for every spoken call prompt.
# Tiers, cheapest→best:  Standard (robotic) · Neural (natural, GA) · Generative (most human, beta).
# Just change this one string and redeploy — no Twilio Console setup needed.
#   Female (neural): Polly.Amy-Neural    Male (neural): Polly.Brian-Neural / Polly.Arthur-Neural
#   Most human (gen): Polly.Amy-Generative  ·  Google.en-GB-Chirp3-HD-Aoede
VOICE_NAME = "Polly.Amy-Neural"


def _voicemail_greeting(profile):
    """Return the spoken greeting for a diverted/voicemail call, branded per trade."""
    if profile:
        custom = (profile.get("voicemail_greeting") or "").strip()
        if custom:
            return custom
        biz = (profile.get("business_name") or "").strip()
        if biz:
            return ("You've reached " + biz + ". We can't take your call right now, but if you "
                    "leave a short message after the tone we'll get straight back to you.")
    return ("Sorry, we can't take your call right now. Please leave a short message after the "
            "tone and we'll get back to you.")


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

# ─────────────────────────────────────────────────────────────
# RENEWALS — the boring admin nobody remembers until it bites:
# public liability insurance, van MOT/tax, CSCS/SMSTS cards,
# Gas Safe / NICEIC registrations, waste carrier licence...
# The assistant nags at 30/14/7/1/0 days and flags expiry.
# ─────────────────────────────────────────────────────────────
RENEWAL_STAGES = [30, 14, 7, 1, 0]

def check_renewals():
    """Daily: remind owners about upcoming renewals. Stage-tracked so each
    threshold fires exactly once. Never raises."""
    try:
        rows = supabase.table("renewals").select("*").execute().data or []
    except Exception as e:
        print("renewals fetch error (run the setup SQL?):", e)
        return
    profiles_cache = {}
    today = datetime.date.today()
    for r in rows:
        try:
            sender = r.get("sender", "")
            due = datetime.date.fromisoformat(str(r.get("due_date", ""))[:10])
            days_left = (due - today).days
            last = r.get("last_stage")
            stage = None
            if days_left < 0 and last != -1:
                stage = -1
            else:
                for s in RENEWAL_STAGES:
                    if days_left == s and (last is None or last > s):
                        stage = s
                        break
            if stage is None:
                continue
            if sender not in profiles_cache:
                res = supabase.table("profiles").select("*").eq("sender", sender).execute()
                profiles_cache[sender] = res.data[0] if res.data else None
            profile = profiles_cache[sender]
            if not profile:
                continue
            label = r.get("label", "a renewal")
            if stage == -1:
                msg = "\u26a0\ufe0f " + label + " EXPIRED " + (("on " + due.strftime("%d %b")) if days_left < -1 else "yesterday") + ". Worth sorting today — working without it can void jobs and insurance claims."
            elif stage == 0:
                msg = "\u26a0\ufe0f " + label + " is due TODAY (" + due.strftime("%d %b") + ")."
            else:
                msg = "Heads up — " + label + " renews in " + str(stage) + " day" + ("" if stage == 1 else "s") + " (" + due.strftime("%d %b") + ")." + ((" " + r.get("notes")) if r.get("notes") else "")
            notify_owner(profile, msg)
            supabase.table("renewals").update({"last_stage": stage}).eq("id", r.get("id")).execute()
        except Exception as e:
            print("renewal row error:", e)


@app.route("/api/renewals", methods=["GET", "POST", "DELETE"])
def api_renewals():
    """List / add / remove renewals for the dashboard. Same phone+pin auth as other APIs."""
    try:
        phone = format_phone((request.args.get("phone") or "").strip())
        pin = (request.args.get("pin") or "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        sender = result.data[0].get("sender", "")
        if request.method == "GET":
            rows = (supabase.table("renewals").select("id,label,due_date,notes")
                    .eq("sender", sender).order("due_date").execute().data or [])
            return jsonify({"renewals": rows})
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            label = (body.get("label") or "").strip()
            due = (body.get("due_date") or "").strip()[:10]
            datetime.date.fromisoformat(due)  # validates
            if not label:
                return jsonify({"error": "Missing label"}), 400
            ins = supabase.table("renewals").insert({
                "sender": sender, "label": label, "due_date": due,
                "notes": (body.get("notes") or "").strip()
            }).execute()
            return jsonify({"ok": True, "renewal": (ins.data or [{}])[0]})
        if request.method == "DELETE":
            rid = request.args.get("id", "")
            supabase.table("renewals").delete().eq("id", rid).eq("sender", sender).execute()
            return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/cron/renewals-check", methods=["GET", "POST"])
def cron_renewals_check():
    expected = os.environ.get("CRON_KEY", "")
    if not expected or request.args.get("key", "") != expected:
        return jsonify({"error": "Unauthorised"}), 401
    check_renewals()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────
# DAILY DEBRIEF — the assistant reports in at end of day.
# Counterpart to the 7am briefing: what happened today, what's
# owed, what's on tomorrow, and what it suggests chasing.
# Never raises; per-profile failures are isolated.
# ─────────────────────────────────────────────────────────────
def _gbp(v):
    try:
        n = float(str(v).replace("£", "").replace(",", "").strip() or 0)
    except Exception:
        n = 0.0
    if n == int(n):
        return "£{:,.0f}".format(n)
    return "£{:,.2f}".format(n)


def _debrief_facts(sender):
    """Gather today's facts for one tradesperson. Returns (facts dict, worth_sending bool)."""
    today = datetime.date.today()
    today_iso = today.isoformat()
    tomorrow_iso = (today + datetime.timedelta(days=1)).isoformat()
    stale_cutoff = (today - datetime.timedelta(days=3)).isoformat()

    f = {"new_enquiries": [], "missed_calls": 0, "quotes_today": [], "quoted_today_total": 0.0,
         "owed_total": 0.0, "owed_count": 0, "oldest_overdue": None,
         "tomorrow": [], "stale_quotes": []}

    try:
        rows = supabase.table("enquiries").select("client_name,status,created_at").eq("sender", sender).gte("created_at", today_iso).execute().data or []
        f["new_enquiries"] = [(r.get("client_name") or "Unknown caller") for r in rows]
    except Exception as e:
        print("debrief enquiries error:", e)
    try:
        rows = supabase.table("enquiries").select("id").eq("sender", sender).eq("status", "missed call").execute().data or []
        f["missed_calls"] = len(rows)
    except Exception as e:
        print("debrief missed error:", e)
    try:
        rows = supabase.table("quotes").select("client_name,total,created_at,status").eq("sender", sender).gte("created_at", today_iso).execute().data or []
        for q in rows:
            f["quotes_today"].append(q.get("client_name") or "a customer")
            try:
                f["quoted_today_total"] += float(str(q.get("total") or 0).replace("£", "").replace(",", "") or 0)
            except Exception:
                pass
    except Exception as e:
        print("debrief quotes error:", e)
    try:
        rows = supabase.table("invoices").select("client_name,invoice_number,total,due_date,status").eq("sender", sender).in_("status", ["unpaid", "overdue"]).execute().data or []
        f["owed_count"] = len(rows)
        worst_days = -1
        for inv in rows:
            try:
                f["owed_total"] += float(str(inv.get("total") or 0).replace("£", "").replace(",", "") or 0)
            except Exception:
                pass
            try:
                dd = datetime.date.fromisoformat(str(inv.get("due_date", ""))[:10])
                days = (today - dd).days
                if days > 0 and days > worst_days:
                    worst_days = days
                    f["oldest_overdue"] = {"who": inv.get("client_name") or ("invoice " + str(inv.get("invoice_number", ""))),
                                           "amount": _gbp(inv.get("total", 0)), "days": days}
            except Exception:
                pass
    except Exception as e:
        print("debrief invoices error:", e)
    try:
        rows = supabase.table("bookings").select("client_name,job_type,time,date").eq("sender", sender).eq("date", tomorrow_iso).order("time").execute().data or []
        f["tomorrow"] = [{"time": b.get("time") or "", "who": b.get("client_name") or "job", "what": b.get("job_type") or ""} for b in rows]
    except Exception as e:
        print("debrief bookings error:", e)
    try:
        rows = supabase.table("quotes").select("client_name,total,created_at,client_number,quote_number").eq("sender", sender).eq("status", "sent").lte("created_at", stale_cutoff).order("created_at", desc=True).limit(3).execute().data or []
        f["stale_quotes"] = [{"who": q.get("client_name") or "a customer", "amount": _gbp(q.get("total", 0)),
                              "number": _clean_num(q.get("client_number") or "")} for q in rows]
        for sq in f["stale_quotes"]:
            if not sq["number"] and sq["who"] != "a customer":
                try:
                    c = (supabase.table("client_contacts").select("client_number").eq("sender", sender)
                         .ilike("name", "%" + sq["who"].split(" ")[0] + "%").limit(1).execute().data or [])
                    if c:
                        sq["number"] = _clean_num(c[0].get("client_number") or "")
                except Exception:
                    pass
    except Exception as e:
        print("debrief stale quotes error:", e)

    try:
        rows = (supabase.table("renewals").select("label,due_date").eq("sender", sender)
                .gte("due_date", today_iso)
                .lte("due_date", (today + datetime.timedelta(days=14)).isoformat())
                .order("due_date").limit(2).execute().data or [])
        f["renewals_due"] = [{"label": r.get("label", ""), "due": r.get("due_date", "")} for r in rows]
    except Exception as e:
        f["renewals_due"] = []

    worth_sending = bool(f["new_enquiries"] or f["quotes_today"] or f["tomorrow"]
                         or f["stale_quotes"] or f["missed_calls"] or f["owed_total"] > 0
                         or f["renewals_due"])
    return f, worth_sending


def _debrief_fallback_text(first_name, f):
    """Deterministic template if the AI write-up fails. Never raises."""
    lines = ["Evening " + first_name + " — end of day from your VanOffice:"]
    if f["new_enquiries"]:
        lines.append("• " + str(len(f["new_enquiries"])) + " new enquir" + ("y" if len(f["new_enquiries"]) == 1 else "ies") + " (" + ", ".join(f["new_enquiries"][:3]) + ")")
    if f["missed_calls"]:
        lines.append("• " + str(f["missed_calls"]) + " missed call" + (" still needs a reply" if f["missed_calls"] == 1 else "s still need a reply"))
    if f["quotes_today"]:
        lines.append("• Quoted " + _gbp(f["quoted_today_total"]) + " today (" + ", ".join(f["quotes_today"][:3]) + ")")
    if f["stale_quotes"]:
        sq = f["stale_quotes"][0]
        lines.append("• " + sq["who"] + " hasn't replied to their " + sq["amount"] + " quote — worth a chase")
    if f["owed_total"] > 0:
        owed = "• You're owed " + _gbp(f["owed_total"]) + " across " + str(f["owed_count"]) + " invoice" + ("" if f["owed_count"] == 1 else "s")
        if f["oldest_overdue"]:
            owed += " (" + f["oldest_overdue"]["who"] + " is " + str(f["oldest_overdue"]["days"]) + " days over)"
        lines.append(owed)
    for r in f.get("renewals_due", [])[:1]:
        lines.append("\u2022 " + r["label"] + " renews " + r["due"] + " \u2014 don't let it lapse")
    if f["tomorrow"]:
        t = f["tomorrow"][0]
        first = (t["time"] + " " if t["time"] else "") + t["who"] + ((" — " + t["what"]) if t["what"] else "")
        extra = "" if len(f["tomorrow"]) == 1 else " +" + str(len(f["tomorrow"]) - 1) + " more"
        lines.append("• Tomorrow: " + first + extra)
    else:
        lines.append("• Nothing in the diary tomorrow")
    return "\n".join(lines)


def send_daily_debrief():
    """5:30pm UK: text every tradesperson a short end-of-day report."""
    try:
        profiles = supabase.table("profiles").select("*").execute().data or []
    except Exception as e:
        print("debrief profiles error:", e)
        return
    for profile in profiles:
        try:
            sender = profile.get("sender", "")
            if not sender or not profile.get("phone"):
                continue
            facts, worth_sending = _debrief_facts(sender)
            if not worth_sending:
                continue
            first_name = (profile.get("owner_name") or "boss").split(" ")[0]
            msg = ""
            try:
                system = ("You write a short end-of-day SMS from a tradesperson's AI office assistant to its boss. "
                          "Tone: capable PA who handled things all day — warm, plain English, slightly proud of the good numbers, direct about what needs attention. "
                          "British English. Under 440 characters total. Use short lines or • bullets. No greetings like 'I hope', no sign-off, no emojis. "
                          "Open with 'Evening {name} —' then the report. If a quote needs chasing or money is overdue, suggest the action plainly. "
                          "Only mention things present in the data. Never invent figures.").replace("{name}", first_name)
                resp = client.messages.create(model="claude-sonnet-4-5", max_tokens=300, system=system,
                                              messages=[{"role": "user", "content": "Today's data as JSON:\n" + json.dumps(facts)}])
                msg = (resp.content[0].text or "").strip()
            except Exception as e:
                print("debrief AI error:", e)
            if not msg or len(msg) > 600:
                msg = _debrief_fallback_text(first_name, facts)
            # Make the stale-quote nudge actionable: park a ready-to-send chase
            # so the owner can fire it with a one-word reply.
            try:
                chase = next((sq for sq in facts.get("stale_quotes", []) if sq.get("number")), None)
                if chase:
                    week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
                    dupe = (supabase.table("pending_actions").select("id")
                            .eq("sender", sender).eq("kind", "quote_chase")
                            .eq("client_number", chase["number"])
                            .gte("created_at", week_ago).limit(1).execute().data or [])
                    if not dupe:
                        cfirst = chase["who"].split(" ")[0] if chase["who"] != "a customer" else ""
                        draft = ("Hi" + ((" " + cfirst) if cfirst else "") + ", just following up on the quote we sent over (" +
                                 chase["amount"] + "). Any questions at all, give us a shout - happy to adjust bits if needed.")
                        supabase.table("pending_actions").insert({
                            "sender": sender, "client_number": chase["number"], "client_name": chase["who"],
                            "twilio_number": profile.get("twilio_number", ""), "kind": "quote_chase",
                            "customer_msg": "(no reply to their " + chase["amount"] + " quote)",
                            "reason": "Quote unanswered for 3+ days - chase ready to go",
                            "options": [draft, "Leave it"], "status": "pending"
                        }).execute()
                        msg += "\n\nReply 1 and I'll send " + (chase["who"] if chase["who"] != "a customer" else "them") + " the chase, 2 to leave it."
            except Exception as e:
                print("debrief chase setup error:", e)
            notify_owner(profile, msg)
            print("debrief sent to", sender)
        except Exception as e:
            print("debrief error for profile:", e)


@app.route("/cron/daily-debrief", methods=["GET", "POST"])
def cron_daily_debrief():
    """Manual trigger for testing. Protected by CRON_KEY. Optional ?sender= to test one account."""
    expected = os.environ.get("CRON_KEY", "")
    if not expected or request.args.get("key", "") != expected:
        return jsonify({"error": "Unauthorised"}), 401
    only = request.args.get("sender", "")
    if only:
        try:
            res = supabase.table("profiles").select("*").eq("sender", only).execute()
            profile = res.data[0] if res.data else None
            if not profile:
                return jsonify({"error": "No profile for that sender"}), 404
            facts, worth_sending = _debrief_facts(only)
            first_name = (profile.get("owner_name") or "boss").split(" ")[0]
            msg = _debrief_fallback_text(first_name, facts)
            sent = notify_owner(profile, msg) if worth_sending else False
            return jsonify({"facts": facts, "worth_sending": worth_sending, "sent": bool(sent), "preview": msg})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    send_daily_debrief()
    return jsonify({"ok": True})


scheduler = BackgroundScheduler()
def _twilio_factory():
    return TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
scheduler.add_job(lambda: send_intelligent_briefing(supabase, client, _twilio_factory), "cron", hour=7, minute=0)
scheduler.add_job(scan_all_emails, "interval", minutes=15)
scheduler.add_job(run_invoice_chase, "cron", hour=9, minute=0)
scheduler.add_job(send_daily_debrief, "cron", hour=17, minute=30, timezone="Europe/London")
scheduler.add_job(check_renewals, "cron", hour=8, minute=15, timezone="Europe/London")
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
    font_link = '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&family=Big+Shoulders+Display:wght@700;800&display=swap" rel="stylesheet">'
    bs = "font-family:'Big Shoulders Display',Arial,sans-serif;"
    def _ts_icon(path, acc2):
        return ('<div style="width:32px;height:32px;border-radius:50%;border:2px solid ' + acc2 + ';display:inline-flex;align-items:center;justify-content:center;margin-bottom:6px">'
                '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="' + acc2 + '" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">' + path + '</svg></div>')
    def _trust_strip(acc2, light=False):
        feats = [('<path d="M3 21l8-8M9 7l8 8M12 4l8 8"/>', 'Quality Craftsmanship'),
                 ('<path d="M12 3l8 3v6c0 4.5-3.4 7.8-8 9-4.6-1.2-8-4.5-8-9V6l8-3z"/>', 'Fully Insured'),
                 ('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>', 'Tidy &amp; On Time'),
                 ('<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5.5-6"/>', 'Built To Last')]
        lab = '#777' if light else 'rgba(255,255,255,0.85)'
        bgst = '' if light else 'background:' + dark + ';'
        cells = ''
        for i, (pth, lb) in enumerate(feats):
            bl = ('border-left:1px solid ' + ('#eee' if light else 'rgba(255,255,255,0.1)') + ';') if i else ''
            cells += ('<td width="25%" style="text-align:center;padding:12px 6px 14px;' + bl + '">' + _ts_icon(pth, acc2) +
                      '<div style="' + bs + 'font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.09em;color:' + lab + '">' + lb + '</div></td>')
        return ('<table width="100%" cellpadding="0" cellspacing="0" style="' + bgst + '">'
                '<tr><td colspan="4" style="padding:0 24px"><div style="height:2px;background:linear-gradient(90deg,' + acc2 + ',rgba(0,0,0,0))"></div></td></tr>'
                '<tr>' + cells + '</tr></table>')
    trust_dark = _trust_strip(accent, light=False)
    trust_light = _trust_strip(accent, light=True)

    if design_style in ("gold", "george", "custom_html"):
        logo_html = f'<img src="{logo_data}" style="width:64px;height:64px;object-fit:contain;margin-bottom:8px;display:block">' if logo_data else f'<div style="font-size:22px;font-weight:900;color:{accent}">{biz_name[:2].upper()}</div>'
        scope_html = "".join([f'<div style="display:flex;gap:10px;margin-bottom:14px;font-size:13px;color:#333;line-height:1.7"><span style="color:{accent};font-weight:700;flex-shrink:0">&#10003;</span><span>{item}</span></div>' for item in scope_items_used])
        inc_html = "".join([f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;font-size:13px;color:#ccc"><span style="color:{accent}">&#10003;</span>{item}</div>' for item in inclusion_items])
        contact_html = "".join([f'<div style="font-size:11px;color:#ccc;margin-bottom:5px">{v}</div>' for v in [phone_num, email, location] if v])
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">{font_link}{base_style}</head><body>
<table width="100%" cellpadding="0" cellspacing="0" style="background:{dark}"><tr>
<td width="50%" style="padding:32px 36px;border-right:2px solid {accent};vertical-align:middle">{logo_html}
<div style="{bs}font-size:27px;font-weight:800;color:{accent};letter-spacing:0.03em;text-transform:uppercase;margin-top:6px">{biz_name}</div>
<div style="font-size:9px;color:{accent};letter-spacing:0.14em;text-transform:uppercase;margin-top:4px;opacity:0.85">{trade}</div>
<div style="width:50px;height:1.5px;background:{accent};margin-top:10px"></div></td>
<td width="50%" style="padding:24px 28px;vertical-align:middle">
<div style="{bs}font-size:42px;font-weight:800;color:{accent};letter-spacing:0.05em;margin-bottom:14px">{doc_label}</div>
<div style="width:100%;height:1px;background:{accent};margin-bottom:10px;opacity:0.4"></div>
{contact_html}</td></tr></table>
{trust_dark}
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="60%" style="padding:22px 28px;vertical-align:top;border-right:1px solid #e8dfc8">
<div style="font-size:11px;margin-bottom:5px"><span style="color:{accent};font-weight:700;font-size:9px;text-transform:uppercase;letter-spacing:0.06em">Date: </span>{today_str}</div>
<div style="font-size:11px;margin-bottom:5px"><span style="color:{accent};font-weight:700;font-size:9px;text-transform:uppercase;letter-spacing:0.06em">Ref: </span>{ref_num}</div>
<div style="font-size:11px;margin-bottom:16px"><span style="color:{accent};font-weight:700;font-size:9px;text-transform:uppercase;letter-spacing:0.06em">Client: </span>{client_full}</div>
{due_line}<div style="font-size:11px;color:#444;line-height:1.7;margin-bottom:18px">{intro}</div>
<div style="{bs}font-size:17px;font-weight:800;text-transform:uppercase;letter-spacing:0.06em;color:#1a1a1a;border-bottom:2px solid {accent};padding-bottom:8px;margin-bottom:12px">Scope of Works</div>
{scope_html}{note_html}</td>
<td width="40%" style="padding:20px 18px;background:#f9f7f0;vertical-align:top">
<div style="text-align:center;padding-bottom:18px;margin-bottom:18px;border-bottom:1.5px solid {accent}">
<div style="width:52px;height:52px;border-radius:50%;border:2.5px solid {accent};display:inline-flex;align-items:center;justify-content:center;font-size:22px;font-weight:900;color:{accent};margin-bottom:8px">&#163;</div>
<div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:{accent};display:block;margin-bottom:6px">Total Price</div>
<div style="{bs}font-size:40px;font-weight:800;color:#1a1a1a;letter-spacing:0.01em">&#163;{total}</div>{vat_html}</div>
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
<div><div style="{bs}font-size:25px;font-weight:800;color:#fff;text-transform:uppercase;letter-spacing:0.03em">{biz_name}</div>
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
<div><div style="{bs}font-size:25px;font-weight:800;color:#fff;text-transform:uppercase;letter-spacing:0.03em">{biz_name}</div>
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

    elif design_style == "showcase":
        hh = accent.lstrip('#')
        if len(hh) == 3:
            hh = hh[0]*2 + hh[1]*2 + hh[2]*2
        try:
            _r, _g, _b = int(hh[0:2], 16), int(hh[2:4], 16), int(hh[4:6], 16)
        except Exception:
            _r, _g, _b = 255, 138, 30
        ink = '#1f1402' if (0.299*_r + 0.587*_g + 0.114*_b)/255 > 0.5 else '#ffffff'
        sdark = '#141b2c'
        logo_html = f'<img src="{logo_data}" style="width:54px;height:54px;object-fit:contain">' if logo_data else ""
        scope_html = "".join([
            '<table cellpadding="0" cellspacing="0" style="margin-bottom:11px"><tr>'
            f'<td style="width:24px;height:24px;border-radius:50%;background:{accent};color:{ink};font-size:11px;font-weight:800;text-align:center;vertical-align:middle">{i+1}</td>'
            f'<td style="padding-left:12px;font-size:13px;color:#333;line-height:1.6">{item}</td></tr></table>'
            for i, item in enumerate(scope_items_used)])
        inc_html = "".join([f'<div style="font-size:12px;color:#444;padding:4px 0;line-height:1.6"><span style="color:{accent};font-weight:800">&#10003;</span>&nbsp; {item}</div>' for item in inclusion_items])
        sc_note = f'<div style="border-left:3px solid {accent};background:#fbf7f1;padding:11px 14px;margin:6px 0 18px"><div style="{bs}font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:#1a1a1a;margin-bottom:3px">Please note</div><div style="font-size:10.5px;color:#555;line-height:1.6">{note}</div></div>' if show_note else ""
        footer_bits = "&nbsp;&nbsp;&#8226;&nbsp;&nbsp;".join([v for v in [biz_name.upper(), (trade or "").upper(), phone_num] if v])
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">{font_link}{base_style}</head><body>
<table width="100%" cellpadding="0" cellspacing="0" style="background:{sdark}"><tr>
<td style="padding:24px 30px;vertical-align:middle">
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td style="vertical-align:middle">{logo_html}</td>
<td style="vertical-align:middle;padding-left:14px">
<div style="{bs}font-size:30px;font-weight:800;color:#fff;text-transform:uppercase;letter-spacing:0.03em;line-height:1.05">{biz_name}</div>
<div style="{bs}font-size:11px;font-weight:700;color:{accent};letter-spacing:0.18em;text-transform:uppercase;margin-top:4px">{trade}</div></td>
<td style="text-align:right;vertical-align:middle">
<div style="{bs}font-size:40px;font-weight:800;color:{accent};text-transform:uppercase;letter-spacing:0.04em;line-height:1">{doc_label}</div>
<div style="font-size:10px;color:rgba(255,255,255,0.55);margin-top:5px;letter-spacing:0.06em">{ref_num} &nbsp;&#8226;&nbsp; {today_str}</div></td>
</tr></table></td></tr></table>
{trust_dark}
<div style="padding:28px 30px 0">
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px"><tr>
<td><div style="{bs}font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:#999">Prepared for</div>
<div style="font-size:14px;font-weight:700;color:#111;margin-top:3px">{client_full}</div>{due_line}</td>
</tr></table>
<div style="height:2px;background:linear-gradient(90deg,{accent},rgba(0,0,0,0));margin-bottom:18px"></div>
<div style="{bs}font-size:16px;font-weight:800;text-transform:uppercase;letter-spacing:0.06em;color:#1a1a1a;margin-bottom:14px">Scope of works</div>
{scope_html}{sc_note}
<table width="100%" cellpadding="0" cellspacing="0" style="background:{sdark};margin:8px 0 20px"><tr>
<td style="padding:16px 22px"><div style="{bs}font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.12em;color:rgba(255,255,255,0.6)">Total price</div></td>
<td style="padding:16px 22px;text-align:right"><div style="{bs}font-size:42px;font-weight:800;color:{accent};line-height:1">&#163;{total}</div></td>
</tr></table>
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="50%" style="vertical-align:top;padding-right:16px">
<div style="{bs}font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:#1a1a1a;margin-bottom:8px">Inclusions</div>{inc_html}</td>
<td width="50%" style="vertical-align:top;padding-left:16px;border-left:1px solid #eee">
<div style="{bs}font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:#1a1a1a;margin-bottom:6px">Lead time</div>
<div style="font-size:11px;color:#555;line-height:1.65;margin-bottom:14px">{lead_time}</div>
<div style="{bs}font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:#1a1a1a;margin-bottom:6px">Payment terms</div>
<div style="font-size:11px;color:#555;line-height:1.65">{payment_terms}</div></td>
</tr></table>
<div style="font-size:9px;color:#bbb;text-align:center;margin:24px 0 14px">{commitment}</div>
</div>
<table width="100%" cellpadding="0" cellspacing="0" style="background:{accent}"><tr>
<td style="padding:14px;text-align:center"><div style="{bs}font-size:15px;font-weight:700;letter-spacing:0.06em;color:{ink}">{footer_bits}</div></td>
</tr></table>
</body></html>"""

    else:
        logo_html = f'<img src="{logo_data}" style="width:48px;height:48px;object-fit:contain">' if logo_data else ""
        scope_html = "".join([f'<div style="font-size:13px;color:#333;padding:10px 0;border-bottom:0.5px solid #f0f0f0;line-height:1.7">— {item}</div>' for item in scope_items_used])
        inc_html = "".join([f'<div style="font-size:13px;color:#555;padding:4px 0;line-height:1.6">— {item}</div>' for item in inclusion_items])
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">{font_link}{base_style}</head><body style="padding:36px">
<div style="display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:12px;border-bottom:0.5px solid #ddd;margin-bottom:18px">
<div style="display:flex;align-items:center;gap:12px">{logo_html}
<div><div style="{bs}font-size:26px;font-weight:800;color:#111;letter-spacing:0.02em;text-transform:uppercase">{biz_name}</div>
<div style="font-size:9px;color:#ccc;margin-top:2px;letter-spacing:0.06em">{trade}</div></div></div>
<div style="text-align:right">
<div style="{bs}font-size:22px;font-weight:800;color:{accent};text-transform:uppercase;letter-spacing:0.05em">{doc_label}</div><div style="font-size:9px;color:#bbb;letter-spacing:0.08em;margin-top:2px">{ref_num}</div>
<div style="font-size:10px;color:#bbb;margin-top:3px;line-height:1.8">{"<br>".join([v for v in [today_str, phone_num, email] if v])}</div></div></div>
<div style="margin-bottom:18px">
<div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px">Prepared for</div>
<div style="font-size:12px;font-weight:700;color:#111">{client_full}</div>
{due_line}</div>
<div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:8px">Works</div>
{scope_html}{note_html}
<div style="display:flex;justify-content:space-between;align-items:center;padding:16px 0;margin-top:20px;border-top:0.5px solid #ddd">
<div><div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em">Total</div>{vat_html}</div>
<div style="{bs}font-size:44px;font-weight:800;color:#111;letter-spacing:0.01em">&#163;{total}</div></div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:12px;padding-top:12px;border-top:0.5px solid #ddd">
<div><div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:6px">Inclusions</div>{inc_html}</div>
<div><div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px">Lead Time</div>
<div style="font-size:10px;color:#555;line-height:1.6">{lead_time}</div>
<div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.1em;margin-top:10px;margin-bottom:4px">Payment</div>
<div style="font-size:10px;color:#555;line-height:1.6">{payment_terms}</div></div></div>
<div style="margin-top:20px">{trust_light}</div>
<div style="margin-top:8px;padding-top:10px;border-top:0.5px solid #eee;font-size:9px;color:#ccc;text-align:center">{commitment}</div>
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


@app.route("/whatsapp-cloud", methods=["GET", "POST"])
def whatsapp_cloud():
    """Meta Cloud API webhook. GET = verification handshake. POST = inbound message.
    Translates Meta's JSON into the Twilio-style fields the existing bot brain expects,
    runs that brain, and sends its reply back out via the Cloud API."""
    # ---- Verification handshake (Meta pings this once when you save the webhook) ----
    if request.method == "GET":
        mode = request.args.get("hub.mode", "")
        token = request.args.get("hub.verify_token", "")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and token == WA_CLOUD_VERIFY:
            return challenge, 200
        return "Verification failed", 403

    # ---- Inbound message ----
    try:
        data = request.get_json(force=True, silent=True) or {}
        entry = (data.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        value = change.get("value", {})
        messages = value.get("messages") or []
        if not messages:
            return "ok", 200  # status callbacks etc. — ack and ignore
        msg = messages[0]
        from_number = msg.get("from", "")          # customer's number, no +
        msg_type = msg.get("type", "text")
        text_body = ""
        media_id = None
        media_kind = None
        if msg_type == "text":
            text_body = (msg.get("text") or {}).get("body", "")
        elif msg_type == "audio":
            media_id = (msg.get("audio") or {}).get("id"); media_kind = "audio"
        elif msg_type == "image":
            media_id = (msg.get("image") or {}).get("id"); media_kind = "image"
        elif msg_type == "interactive":
            inter = msg.get("interactive", {})
            text_body = (inter.get("button_reply") or inter.get("list_reply") or {}).get("title", "")
        else:
            text_body = ""

        reply_text = _run_bot_for_cloud(from_number, text_body, media_id, media_kind)
        if reply_text:
            send_whatsapp_cloud(from_number, reply_text)
        return "ok", 200
    except Exception as e:
        print("whatsapp_cloud inbound error:", e)
        import traceback; print(traceback.format_exc())
        return "ok", 200  # always 200 so Meta doesn't disable the webhook


def _run_bot_for_cloud(from_number, text_body, media_id, media_kind):
    """Bridge Cloud API messages into a reply. For this first live test it runs a
    self-contained Claude responder so we can verify the round-trip end to end.
    Once the pipe is proven, this widens to the full quote/booking flow that the
    /whatsapp route implements."""
    try:
        sender = "+" + str(from_number).lstrip("+")
        # Voice note -> transcribe via Whisper
        if media_kind == "audio" and media_id:
            content, mime = fetch_wa_cloud_media(media_id)
            if content:
                try:
                    from openai import OpenAI
                    apath = "/tmp/wac_" + str(from_number) + ".ogg"
                    with open(apath, "wb") as f:
                        f.write(content)
                    oc = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
                    with open(apath, "rb") as af:
                        text_body = oc.audio.transcriptions.create(model="whisper-1", file=af).text.strip()
                    print("cloud voice transcribed:", text_body)
                except Exception as e:
                    print("cloud voice transcribe error:", e)
                    return "Sorry, I couldn't make out that voice note - could you type it instead?"
        if media_kind == "image":
            return "Thanks for the photo! Send me a quick description of the job and I'll help you get a price together."

        if not text_body:
            return "Hi! Send me a message about the job you need and I'll help you out."

        profile = get_user_profile(sender)
        biz = (profile or {}).get("business_name", "the business")
        trade = (profile or {}).get("trade", "trade")
        sys_p = ("You are the friendly assistant for " + str(biz) + ", a UK " + str(trade) + " business, "
                 "replying to a customer on WhatsApp. Keep replies short, warm, plain British English, no emojis. "
                 "Help them describe their job so it can be quoted. If they ask about price, say you'll get the owner to confirm "
                 "an exact figure and take their job details. Never invent prices or confirm bookings yourself.")
        if sender not in conversation_history:
            conversation_history[sender] = []
        conversation_history[sender].append({"role": "user", "content": text_body})
        conversation_history[sender] = conversation_history[sender][-12:]
        ai = client.messages.create(model="claude-sonnet-4-5", max_tokens=400,
                                    system=sys_p, messages=conversation_history[sender])
        reply = (ai.content[0].text or "").strip()
        conversation_history[sender].append({"role": "assistant", "content": reply})
        # log inbound (best-effort)
        try:
            biz_num = (profile or {}).get("twilio_number", "") if profile else ""
            supabase.table("client_chats").insert({"twilio_number": _strip_wa(biz_num), "client_number": _strip_wa(sender),
                "message": text_body, "direction": "inbound", "sender_profile": (profile or {}).get("sender", ""), "channel": "whatsapp"}).execute()
        except Exception:
            pass
        return reply or "Got it - one moment."
    except Exception as e:
        print("_run_bot_for_cloud error:", e)
        import traceback; print(traceback.format_exc())
        return "Thanks for your message - I'll get back to you shortly."


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


_recent_dial_attempts = {}  # caller -> timestamp of our last outbound dial to the owner (loop guard)

@app.route("/call", methods=["POST"])
def incoming_call():
    caller = request.form.get("From", "")
    called = request.form.get("To", "")
    # Carriers signal a diverted call in different ways; EE doesn't always send one.
    forwarded_from = request.form.get("ForwardedFrom", "") or request.form.get("CalledVia", "")
    try:
        result = supabase.table("profiles").select("*").eq("twilio_number", called).execute()
        profile = result.data[0] if result.data else None
    except Exception as e:
        print("Profile lookup error: " + str(e))
        profile = None
    from twilio.twiml.voice_response import VoiceResponse, Dial
    from urllib.parse import quote

    # Loop guard: if we dialled the owner for this same caller in the last 45s and
    # the call is arriving back at us, the owner's divert bounced our own dial —
    # treat it as the missed call it is instead of dialling again forever.
    import time as _time
    _now = _time.time()
    for _k in list(_recent_dial_attempts.keys()):
        if _now - _recent_dial_attempts[_k] > 90:
            _recent_dial_attempts.pop(_k, None)
    bounced = caller in _recent_dial_attempts and (_now - _recent_dial_attempts.get(caller, 0)) < 45

    resp = VoiceResponse()
    # Catcher mode: this number is purely a divert destination (the trade's real
    # number is public). Every call landing here is by definition a missed call —
    # no point dialling the owner whose phone just bounced it to us.
    catcher = bool(profile) and (profile.get("number_mode") or "").lower() == "catcher"
    if profile and (forwarded_from or bounced or catcher):
        # Diverted from the trade's own phone (busy / no answer): this IS a missed
        # call already — re-dialling their phone would just bounce back here in a
        # loop. Triage the caller, then take a voicemail.
        print("missed call detected:", "header" if forwarded_from else "loop-guard", caller)
        _recent_dial_attempts.pop(caller, None)
        try:
            _handle_missed_call(profile, caller)
        except Exception as e:
            print("forwarded missed-call triage error:", e)
        resp.say(_voicemail_greeting(profile), voice=VOICE_NAME)
        resp.record(action="/voicemail", method="POST", max_length=120, play_beep=True, timeout=4, trim="trim-silence")
        return str(resp)
    if profile and profile.get("phone"):
        _recent_dial_attempts[caller] = _now
        resp.say("Please hold while we connect your call. Please note, calls may be recorded and transcribed for quality and training purposes.", voice=VOICE_NAME)
        rec_cb = "/call-recording?caller=" + quote(caller) + "&called=" + quote(called)
        dial = Dial(action="/call-status", method="POST", timeout=20,
                    record="record-from-answer-dual",
                    recording_status_callback=rec_cb,
                    recording_status_callback_method="POST",
                    recording_status_callback_event="completed")
        dial.number(profile.get("phone"))
        resp.append(dial)
    else:
        resp.say("Sorry, we are unable to connect your call right now. Please try again later.", voice=VOICE_NAME)
    return str(resp)


@app.route("/call-status", methods=["POST"])
def call_status():
    from twilio.twiml.voice_response import VoiceResponse
    dial_status = request.form.get("DialCallStatus", "")
    caller = request.form.get("From", "")
    called = request.form.get("To", "")
    resp = VoiceResponse()
    if dial_status != "completed":
        # Missed — triage the caller (personal / known client / new lead),
        # then take a voicemail we'll transcribe.
        profile = None
        try:
            result = supabase.table("profiles").select("*").eq("twilio_number", called).execute()
            profile = result.data[0] if result.data else None
            if profile:
                _handle_missed_call(profile, caller)
        except Exception as e:
            print("Missed-call triage error: " + str(e))
        resp.say(_voicemail_greeting(profile), voice=VOICE_NAME)
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
    resp.say("Thanks, we've got that and we'll be in touch shortly. Goodbye.", voice=VOICE_NAME)
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
        # Never dead-end: show the styled HTML invoice instead (printable/shareable).
        return redirect("/invoice-view/" + str(invoice_id))


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

        # ── Owner texting their OWN VanOffice number → reply-to-approve, not a customer. ──
        if client_number and _phone_last10(client_number) and \
           _phone_last10(client_number) == _phone_last10(profile.get("phone", "")):
            try:
                owner_msg = handle_owner_reply(profile, incoming_msg)
            except Exception as oe:
                print("handle_owner_reply error:", oe)
                owner_msg = "Sorry, something went wrong actioning that \u2014 open VanOffice to manage it."
            resp.message(owner_msg)
            return str(resp)

        # Customer sent a photo (MMS)? Fetch it so the bot can actually see the job.
        cust_image_data = None
        cust_image_type = "image/jpeg"
        try:
            num_media = int(request.form.get("NumMedia", 0) or 0)
        except Exception:
            num_media = 0
        if num_media > 0:
            mtype = request.form.get("MediaContentType0", "") or ""
            murl = request.form.get("MediaUrl0", "") or ""
            if "image" in mtype and murl:
                try:
                    import requests as req, base64 as _b64
                    sid = os.environ.get("TWILIO_ACCOUNT_SID")
                    tok = os.environ.get("TWILIO_AUTH_TOKEN")
                    ir = req.get(murl, auth=(sid, tok), timeout=20)
                    if ir.ok and ir.content:
                        cust_image_data = _b64.b64encode(ir.content).decode()
                        cust_image_type = mtype
                except Exception as e:
                    print("customer image fetch error:", e)
            if not incoming_msg:
                incoming_msg = "[Photo of the job]" if cust_image_data else "[Sent an attachment]"

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

        # If a photo came in with this message, attach it to the latest turn so the bot can see it.
        if cust_image_data and chat_messages and chat_messages[-1]["role"] == "user":
            _txt = chat_messages[-1]["content"] or "Here's a photo of the job."
            chat_messages[-1] = {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": cust_image_type, "data": cust_image_data}},
                {"type": "text", "text": _txt}
            ]}

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
        system += "PHOTOS: customers often send a photo of the job. If one comes in, look at it and refer naturally to what you can see (so they know you've taken it in), and use it to ask sharper questions \u2014 but you still arrange a quote VISIT and NEVER give a price from a photo.\n\n"
        system += "WHAT WE'RE DOING: for almost every enquiry the next step is a QUOTE VISIT \u2014 we come round to look at the work and give a price. We do NOT agree to carry out the job over text, and you must NEVER imply the work itself is booked or that we're turning up to do it. Everything you arrange is a visit to take a look and quote (unless it's something tiny we could clearly price right away). Always get the FULL ADDRESS of where the work is, since we need it to come and quote.\n\n"
        system += "YOU CAN HANDLE THESE YOURSELF:\n"
        system += "- Simple questions: opening hours, areas covered, what trade/work we do.\n"
        system += "- Gathering what we need to quote: what the job is, the FULL ADDRESS (street + postcode if you can get it, not just the town), and when roughly suits them for a visit.\n"
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
        system += "TO HAND IT OVER: send the customer ONE short, neutral holding line that does NOT name anyone and does NOT promise a specific time "
        system += "(e.g. 'Leave that with me and I\u2019ll confirm shortly' or 'Let me come right back to you on that'). Don't say you're checking a particular person's diary, and don't repeat the holding line on every message. "
        system += "Then on its OWN FINAL LINE add:\n"
        system += "NEEDYOU:reason=<short reason, e.g. arrange a time to come and quote>|options=<2-5 short choices " + owner_name + " could pick, separated by commas>\n"
        system += "The options must be SMART and based on what the customer said. Example: if they say 'any evening next week', options could be: "
        system += "Mon eve,Tue eve,Wed eve,Thu eve,Fri eve. If they ask a price, options could be: Send rough quote,Arrange a call,I\u2019ll reply myself. "
        system += "Always make the LAST option a sensible catch-all. Never invent the answer yourself \u2014 the options are for " + owner_name + " to choose from.\n\n"
        system += "WHEN YOU HAVE ENOUGH TO QUOTE (job + full address) and it is NOT a timing/price/booking moment, end with:\n"
        system += "NEWJOB:name=<name or Unknown>|job=<job type>|location=<full address incl postcode if known>\n\n"
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
                _opts_txt = ""
                for _i, _o in enumerate(options[:5], 1):
                    _opts_txt += "\n" + str(_i) + ". " + str(_o)
                notify_owner(profile, "\u26A0 " + who + " needs you: " + reason + _opts_txt +
                             "\n\nReply with a number, or just tell me what to do.")
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
            max_tokens=3000,
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


@app.route("/api/help", methods=["POST"])
def help_assistant():
    try:
        data = request.json or {}
        messages = data.get("messages", [])
        messages = messages[-12:] if isinstance(messages, list) else []
        system = (
            "You are the friendly in-app help assistant for VanOffice, an app for UK self-employed tradespeople "
            "(builders, plumbers, electricians, carpenters, decorators, roofers and the like). You help the user "
            "understand and use the app. Talk in plain, warm, everyday language like a helpful mate \u2014 never "
            "technical jargon. Keep answers short (2 to 5 sentences), practical, and give clear step-by-step when "
            "explaining how to do something.\n\n"
            "What VanOffice does:\n"
            "- Answers their customers automatically: when a customer texts or calls their VanOffice number, the AI "
            "replies, answers questions, and can build a quote and take a booking \u2014 even while they're on the tools. "
            "Customers reach them by TEXT or CALL to their VanOffice number (WhatsApp may come later).\n"
            "- Quotes: create and send branded quote PDFs, add extras to a quote, and choose a quote style with their "
            "logo and colours on the 'Style' tab.\n"
            "- Invoices: turn a job into an invoice, send it, and mark it paid.\n"
            "- Accounts: scan receipts, log expenses and mileage, and see an estimate of profit and how much tax to set aside.\n"
            "- Inbox: every customer conversation in one place.\n"
            "- Jobs/Diary: track jobs and bookings.\n"
            "- Profile: set business name, trade, area and number, and upload a logo.\n"
            "- Settings: turn on push notifications.\n\n"
            "If you're not sure VanOffice can do something, say so honestly and suggest they check Settings or try it "
            "\u2014 never invent features. If the user seems stuck or frustrated, be reassuring and concrete. "
            "You're here to help, not to sell."
        )
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            system=system,
            messages=messages
        )
        return jsonify({"reply": resp.content[0].text})
    except Exception as e:
        print("Help assistant error:", e)
        return jsonify({"reply": "Sorry, I couldn't answer just now \u2014 give it another go in a moment."})


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
        try:
            eq_id = data.get("enquiry_id")
            if eq_id:
                supabase.table("enquiries").update({"status": "quoted"}).eq("id", eq_id).execute()
        except Exception as e:
            print("enquiry->quoted update error: " + str(e))
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


@app.route("/api/quote/<quote_id>/to-invoice", methods=["POST"])
def quote_to_invoice(quote_id):
    """Clone a quote (line items + extras + client + total) into an invoice and
    return its styled PDF URL. The invoice renders through the SAME template as the
    quote via build_quote_html(is_invoice=True) — identical look, just INVOICE."""
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        qrows = (supabase.table("quotes").select("*").eq("id", quote_id)
                 .eq("sender", sender).limit(1).execute().data or [])
        if not qrows:
            return jsonify({"error": "Quote not found"}), 404
        q = qrows[0]
        total_str = str(q.get("total", "0"))
        # Reuse an existing matching invoice so repeated taps don't spawn duplicates
        try:
            existing = (supabase.table("invoices").select("*").eq("sender", sender)
                        .eq("client_name", q.get("client_name", "")).eq("total", total_str)
                        .limit(1).execute().data or [])
        except Exception:
            existing = []
        if existing:
            inv = existing[0]
            iid = inv.get("id")
            return jsonify({"ok": True, "invoice": inv, "invoice_id": iid,
                            "pdf_url": "/generate-invoice-pdf/" + str(iid),
                            "view_url": "/invoice-view/" + str(iid)})
        inv_count = supabase.table("invoices").select("id").eq("sender", sender).execute()
        inv_num = "INV-" + str(len(inv_count.data) + 1).zfill(3)
        due = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%d %B %Y")
        invoice = {
            "sender": sender,
            "client_name": q.get("client_name", ""),
            "client_address": q.get("client_address", ""),
            "job_description": q.get("job_description", ""),
            "total": total_str,
            "subtotal": str(q.get("subtotal", q.get("total", "0"))),
            "vat": "0",
            "line_items": q.get("line_items", []),
            "status": "unpaid",
            "invoice_number": inv_num,
            "due_date": due,
            "client_number": q.get("client_number", ""),
            "invoice_text": ""
        }
        res = supabase.table("invoices").insert(invoice).execute()
        saved = res.data[0] if res.data else invoice
        iid = saved.get("id")
        return jsonify({"ok": True, "invoice": saved, "invoice_id": iid,
                        "pdf_url": "/generate-invoice-pdf/" + str(iid),
                        "view_url": "/invoice-view/" + str(iid)})
    except Exception as e:
        print("Quote to invoice error: " + str(e))
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


# --- Pro post designs via Templated.io --------------------------------------
# Photos are held in memory for a few minutes and served at a public URL so
# Templated's renderer can fetch them. No external storage needed.
_post_photo_cache = {}

@app.route("/post-photo/<pid>")
def serve_post_photo(pid):
    import time as _t
    item = _post_photo_cache.get(pid)
    if not item:
        return "Not found", 404
    raw, mime, exp = item
    if _t.time() > exp:
        _post_photo_cache.pop(pid, None)
        return "Expired", 404
    return Response(raw, mimetype=mime or "image/jpeg")


@app.route("/api/pro-post", methods=["POST"])
def pro_post():
    """Render a professionally-designed social post via a Templated.io template.
    Requires env vars TEMPLATED_API_KEY and TEMPLATED_TEMPLATE_ID. The template
    should contain layers named: photo (image), business, headline, cta, tag,
    and optionally logo (image)."""
    import base64 as _b64, time as _t, uuid as _uuid, requests as req
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        api_key = os.environ.get("TEMPLATED_API_KEY", "")
        template_id = os.environ.get("TEMPLATED_TEMPLATE_ID", "")
        if not api_key or not template_id:
            return jsonify({"ok": False, "not_configured": True,
                            "error": "Pro designs aren't switched on yet."})
        data = request.json or {}
        headline = (data.get("headline") or "").strip()
        cta = (data.get("cta") or "FREE QUOTES \u00b7 MESSAGE US").strip()
        tag = (data.get("tag") or "").strip()
        colour = (data.get("colour") or "#ff8a1e").strip()
        business = profile.get("business_name", "") or profile.get("trade", "")

        # Host the photo briefly so Templated can fetch it by URL
        photo_url = ""
        photo_b64 = data.get("photo", "") or ""
        if photo_b64:
            b64 = photo_b64
            mime = "image/jpeg"
            if photo_b64.startswith("data:") and "," in photo_b64:
                header, b64 = photo_b64.split(",", 1)
                try:
                    mime = header.split(":", 1)[1].split(";", 1)[0] or "image/jpeg"
                except Exception:
                    mime = "image/jpeg"
            try:
                raw = _b64.b64decode(b64)
            except Exception:
                raw = b""
            if raw:
                pid = _uuid.uuid4().hex
                _post_photo_cache[pid] = (raw, mime, _t.time() + 300)
                photo_url = request.host_url.rstrip("/") + "/post-photo/" + pid

        layers = {
            "business": {"text": business},
            "headline": {"text": headline},
            "cta": {"text": cta},
            "tag": {"text": tag},
        }
        if photo_url:
            layers["photo"] = {"image_url": photo_url}
        logo = profile.get("logo", "")
        if logo and str(logo).startswith("http"):
            layers["logo"] = {"image_url": logo}

        body = {"template": template_id, "format": "png", "layers": layers}
        r = req.post("https://api.templated.io/v1/render",
                     headers={"Authorization": "Bearer " + api_key,
                              "Content-Type": "application/json"},
                     json=body, timeout=60)
        if r.status_code not in (200, 201):
            return jsonify({"ok": False, "error": "Render failed (" + str(r.status_code) + ")"})
        rj = r.json()
        url = rj.get("url", "")
        status = rj.get("status", "")
        rid = rj.get("id", "")
        tries = 0
        while (not url or status == "PENDING") and rid and tries < 6:
            _t.sleep(1.3)
            try:
                gr = req.get("https://api.templated.io/v1/render/" + str(rid),
                             headers={"Authorization": "Bearer " + api_key}, timeout=30)
                if gr.status_code == 200:
                    gj = gr.json()
                    url = gj.get("url", url)
                    status = gj.get("status", status)
                    if status == "COMPLETED" and url:
                        break
            except Exception:
                pass
            tries += 1
        if not url:
            return jsonify({"ok": False, "error": "Still processing \u2014 try again in a moment."})
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        print("Pro post error: " + str(e))
        print(traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)})


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

@app.route("/api/create-booking", methods=["POST"])
def create_booking_api():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        profile = result.data[0]
        sender = profile.get("sender", "")
        data = request.json or {}
        if not data.get("date"):
            return jsonify({"error": "Missing date"}), 400
        booking = {
            "sender": sender,
            "client_name": data.get("client_name", ""),
            "job_type": data.get("job_type", "") or "Job",
            "location": data.get("location", ""),
            "date": data.get("date", ""),
            "time": data.get("time", ""),
            "duration_days": str(data.get("duration_days", "1")),
            "status": "booked",
            "client_number": data.get("client_number", "")
        }
        res = supabase.table("bookings").insert(booking).execute()
        saved = res.data[0] if res.data else booking
        qid = data.get("quote_id")
        if qid:
            try:
                supabase.table("quotes").update({"status": "booked"}).eq("id", qid).execute()
            except Exception as e:
                print("quote->booked update error: " + str(e))
        return jsonify({"ok": True, "booking": saved})
    except Exception as e:
        print("Create booking error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/quote/<quote_id>/done", methods=["POST"])
def done_quote(quote_id):
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        supabase.table("quotes").update({"status": "done"}).eq("id", quote_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print("Done quote error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/post-stats", methods=["GET"])
def post_stats():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        sender = result.data[0].get("sender", "")
        clicks = 0
        enquiries = 0
        try:
            cr = supabase.table("social_clicks").select("id", count="exact").eq("sender", sender).eq("source", "fb").execute()
            clicks = cr.count or 0
        except Exception as e:
            print("post-stats clicks error: " + str(e))
        try:
            er = supabase.table("enquiries").select("id", count="exact").eq("sender", sender).ilike("source", "%facebook%").execute()
            enquiries = er.count or 0
        except Exception as e:
            print("post-stats enquiries error: " + str(e))
        return jsonify({"ok": True, "clicks": clicks, "enquiries": enquiries})
    except Exception as e:
        print("Post stats error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/quote/<quote_id>/accept", methods=["POST"])
def accept_quote(quote_id):
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        supabase.table("quotes").update({"status": "accepted"}).eq("id", quote_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print("Accept quote error: " + str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete-enquiry", methods=["POST"])
def delete_enquiry():
    try:
        phone = format_phone(request.args.get("phone", "").strip())
        pin = request.args.get("pin", "").strip()
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data or str(result.data[0].get("pin", "")) != str(pin):
            return jsonify({"error": "Unauthorised"}), 401
        data = request.json or {}
        eq_id = data.get("enquiry_id")
        if not eq_id:
            return jsonify({"error": "Missing enquiry_id"}), 400
        supabase.table("enquiries").delete().eq("id", eq_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        print("Delete enquiry error: " + str(e))
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
        "name": "add_renewal",
        "description": "Log a renewal or expiry date the tradesperson must not miss - insurance (public liability, van, tools), van MOT or tax, CSCS/SMSTS/Gas Safe/NICEIC cards and registrations, waste carrier licence. Reminders go out at 30/14/7/1/0 days. Use whenever the user mentions something expiring, renewing, or being due on a date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "What renews, e.g. 'Van MOT' or 'Public liability insurance'"},
                "due_date": {"type": "string", "description": "Due date as YYYY-MM-DD"},
                "notes": {"type": "string", "description": "Optional note, e.g. provider or policy number"}
            },
            "required": ["label", "due_date"]
        }
    },
    {
        "name": "list_renewals",
        "description": "List the tradesperson's upcoming renewals and expiry dates (insurance, MOT, cards, licences) with days remaining.",
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
    },
    {
        "name": "respond_to_customer",
        "description": "Reply to a customer who is currently waiting on the owner (a parked decision: confirming/proposing an appointment time, answering a question, or giving a price). Use this when the owner is responding to such a customer - e.g. a bare number picking an offered option, 'Weds 2pm works', or 'tell him £400 all in'. If a specific time is confirmed it is added to the diary automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What to tell the customer, in plain words. It will be turned into a warm, brief message."},
                "client_name": {"type": "string", "description": "Optional: which waiting customer, if more than one. Leave blank for the most recent."}
            },
            "required": ["message"]
        }
    },
    {
        "name": "dismiss_customer_request",
        "description": "Dismiss/park a waiting customer decision without replying, because the owner will handle it themselves.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string", "description": "Optional: which waiting customer. Leave blank for the most recent."}
            }
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

        if name == "add_renewal":
            label = (ti.get("label", "") or "").strip()
            due = (ti.get("due_date", "") or "").strip()[:10]
            if not label:
                return "What is it that renews?"
            try:
                d = datetime.date.fromisoformat(due)
            except Exception:
                return "I need the date as YYYY-MM-DD - what's the exact due date?"
            supabase.table("renewals").insert({
                "sender": sender, "label": label, "due_date": due,
                "notes": (ti.get("notes", "") or "").strip()
            }).execute()
            days = (d - datetime.date.today()).days
            return ("Logged: " + label + " due " + d.strftime("%d %b %Y") + " (" + str(days) +
                    " days away). I'll remind you at 30, 14, 7 and 1 day out, and on the day.")

        if name == "list_renewals":
            rows = (supabase.table("renewals").select("label,due_date").eq("sender", sender)
                    .order("due_date").execute().data or [])
            if not rows:
                return "No renewals logged yet. Tell me things like 'van MOT due 14 March' and I'll track them."
            today = datetime.date.today()
            out = []
            for r in rows:
                d = datetime.date.fromisoformat(str(r.get("due_date", ""))[:10])
                dl = (d - today).days
                out.append(r.get("label", "") + " - " + d.strftime("%d %b") + (" (EXPIRED)" if dl < 0 else " (" + str(dl) + " days)"))
            return "Renewals:\n" + "\n".join(out)

        if name == "respond_to_customer":
            msg = (ti.get("message", "") or "").strip()
            cn = (ti.get("client_name", "") or "").strip()
            if not msg:
                return "No message given to send."
            pa = _latest_pending(sender, cn)
            if not pa:
                return "There's no customer waiting on a reply right now."
            res = _resolve_pending_action(profile, pa, msg, send_verbatim=False)
            if not res.get("ok"):
                return "Couldn't send that to the customer just now."
            out = "Sent to " + (pa.get("client_name") or "the customer") + "."
            if res.get("booked"):
                out += " " + res["booked"] + "."
            return out

        if name == "dismiss_customer_request":
            cn = (ti.get("client_name", "") or "").strip()
            pa = _latest_pending(sender, cn)
            if not pa:
                return "Nothing waiting to dismiss."
            try:
                supabase.table("pending_actions").update({
                    "status": "dismissed", "resolved_at": datetime.datetime.now().isoformat()
                }).eq("id", pa.get("id")).execute()
            except Exception as de:
                print("dismiss tool error:", de)
                return "Couldn't dismiss that just now."
            return "Dismissed " + (pa.get("client_name") or "that request") + "."

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


def _booking_from_approval(profile, pa, choice):
    """When the owner approves a decision that confirms a specific appointment,
    create a diary booking so it lands in the calendar. Returns a short summary
    string if a booking was created, else None. Never raises."""
    sender = profile.get("sender", "")
    today_str = datetime.date.today().strftime("%A %d %B %Y")
    customer_msg = pa.get("customer_msg", "") or ""
    cname = pa.get("client_name", "") or ""

    # Best job/location context from this client's recent enquiries.
    job_ctx, loc_ctx = "", ""
    try:
        enq = (supabase.table("enquiries").select("job_type,location")
               .eq("sender", sender).order("created_at", desc=True).limit(8).execute().data or [])
        for e in enq:
            if not job_ctx and e.get("job_type"): job_ctx = e["job_type"]
            if not loc_ctx and e.get("location"): loc_ctx = e["location"]
            if job_ctx and loc_ctx: break
    except Exception:
        pass

    ext_sys = (
        "You decide whether an owner's decision CONFIRMS a specific appointment with a customer, "
        "and if so extract it. Today is " + today_str + ".\n"
        "The customer said: \"" + customer_msg + "\"\n"
        "The owner decided: \"" + choice + "\"\n"
        "Known job: \"" + job_ctx + "\". Known location: \"" + loc_ctx + "\".\n"
        "Reply with ONLY a JSON object, no prose and no code fences:\n"
        "{\"book\": true|false, \"date\": \"YYYY-MM-DD\", \"time\": \"HH:MM\" or \"\", "
        "\"job\": \"short\", \"location\": \"short\"}\n"
        "Set book=true ONLY if the decision names or agrees a specific day that resolves to a real date "
        "(resolve relative days like 'Wednesday', 'tomorrow', 'next Tue' against today). "
        "Set book=false for vague replies (e.g. 'suggest another time', 'send a quote', 'arrange a call', 'I'll reply myself'). "
        "If the visit is to quote or survey the work, set job to 'Quote visit'. Use 24h time; leave time empty if none was agreed."
    )
    try:
        ai = client.messages.create(model="claude-sonnet-4-5", max_tokens=160,
                                    system=ext_sys, messages=[{"role": "user", "content": "Decide and extract."}])
        raw = ai.content[0].text.strip()
        s = raw.find("{"); e = raw.rfind("}")
        info = json.loads(raw[s:e + 1]) if (s != -1 and e != -1) else {}
    except Exception as ex:
        print("booking extract error:", ex)
        return None

    if not info.get("book"):
        return None
    date_str = (info.get("date", "") or "").strip()
    if not date_str:
        return None
    time_str = (info.get("time", "") or "").strip()
    job_txt = (info.get("job", "") or job_ctx or "Job").strip()
    loc_txt = (info.get("location", "") or loc_ctx or "").strip()

    # Don't double-book: same client already has something that day.
    try:
        dupe = (supabase.table("bookings").select("id")
                .eq("sender", sender).eq("date", date_str)
                .eq("client_name", cname or "Customer").limit(1).execute().data or [])
        if dupe:
            return None
    except Exception:
        pass

    try:
        ins = supabase.table("bookings").insert({
            "sender": sender,
            "client_name": cname or "Customer",
            "job_type": job_txt,
            "location": loc_txt,
            "date": date_str,
            "time": time_str,
            "duration_days": "1",
            "notes": "Added automatically from an approved appointment.",
            "status": "booked"
        }).execute()
    except Exception as ie:
        print("auto-booking insert error:", ie)
        return None
    # Best-effort: remember the customer's number so we can send a day-of reminder.
    # (Needs the bookings.client_number column; silently skipped if it's not there.)
    try:
        new_id = (ins.data or [{}])[0].get("id")
        cnum = pa.get("client_number", "")
        if new_id and cnum:
            supabase.table("bookings").update({"client_number": cnum}).eq("id", new_id).execute()
    except Exception:
        pass
    return ("Booked " + (cname or "customer") + " for " + date_str +
            (" at " + time_str if time_str else "") + (" \u2014 " + job_txt if job_txt else ""))


def _resolve_pending_action(profile, pa, choice, send_verbatim=False):
    """Shared core for actioning a pending decision (used by both the dashboard
    Inbox and reply-by-text): phrase the owner's choice into a customer message,
    send it on the customer's channel, mark the action done, and create a diary
    booking if the choice confirmed a time. Returns {ok, sent, booked, error}."""
    client_number = pa.get("client_number", "")
    pid = pa.get("id")

    # ── Missed-call drafts behave differently: tapping/replying with the draft
    # sends it word-for-word, and there are two no-send choices.
    if (pa.get("kind") or "") in ("missed_call", "quote_chase"):
        opts = pa.get("options") or []
        if isinstance(opts, str):
            try:
                opts = json.loads(opts)
            except Exception:
                opts = [opts]
        draft = str(opts[0]) if opts else ""
        c = (choice or "").strip().lower()
        if c in ("ignore", "leave it", "no", "skip"):
            if pid is not None:
                supabase.table("pending_actions").update({
                    "status": "dismissed", "resolved_at": datetime.datetime.now().isoformat()
                }).eq("id", pid).execute()
            return {"ok": True, "sent": None, "booked": None}
        if "mate" in c or c in ("personal", "family", "friend"):
            try:
                supabase.table("personal_contacts").insert({
                    "sender": profile.get("sender", ""), "phone": _clean_num(client_number),
                    "name": pa.get("client_name") or ""
                }).execute()
            except Exception as e:
                print("personal_contacts insert error (run the setup SQL?):", e)
            if pid is not None:
                supabase.table("pending_actions").update({
                    "status": "dismissed", "resolved_at": datetime.datetime.now().isoformat()
                }).eq("id", pid).execute()
            return {"ok": True, "sent": None, "booked": None}
        if draft and " ".join(choice.split()) == " ".join(draft.split()):
            send_verbatim = True
            choice = draft

    if send_verbatim:
        customer_message = choice
    else:
        owner_name = profile.get("owner_name", "")
        biz = profile.get("business_name", "the business")
        convo = pa.get("customer_msg", "")
        phr_sys = ("You write a single short, warm SMS to a customer on behalf of " + biz + ".\n"
                   "The customer said: \"" + convo + "\".\n"
                   "The owner (" + owner_name + ") has decided: \"" + choice + "\".\n"
                   "Write ONLY the message to send to the customer relaying that decision naturally. "
                   "If a time is being agreed, frame it as us coming round to take a look and give a quote "
                   "(e.g. 'we\u2019ll pop round Wednesday at 5pm to take a look and quote') \u2014 do NOT imply the job "
                   "itself is booked to be carried out, unless the owner clearly means starting the work. "
                   "Do not add quotes, signatures, or tags. Keep it brief and human.")
        try:
            ai = client.messages.create(model="claude-sonnet-4-5", max_tokens=180,
                                        system=phr_sys, messages=[{"role": "user", "content": "Write the message."}])
            customer_message = ai.content[0].text.strip()
        except Exception as ae:
            print("phrasing error:", ae)
            customer_message = choice

    send_res = send_to_client(profile, client_number, customer_message)
    if not send_res.get("ok"):
        return {"ok": False, "error": send_res.get("error") or "Send failed"}

    if pid is not None:
        supabase.table("pending_actions").update({
            "status": "done", "resolved_at": datetime.datetime.now().isoformat()
        }).eq("id", pid).execute()

    booking_note = None
    try:
        booking_note = _booking_from_approval(profile, pa, choice)
    except Exception as _be:
        print("auto-booking error:", _be)

    return {"ok": True, "sent": customer_message, "booked": booking_note}


def _latest_pending(sender, client_name=""):
    """Most recent customer decision still waiting on the owner. If client_name
    is given, prefer a match on that name. Returns the row or None."""
    try:
        rows = (supabase.table("pending_actions").select("*")
                .eq("sender", sender).eq("status", "pending")
                .order("created_at", desc=True).limit(10).execute().data or [])
    except Exception as e:
        print("_latest_pending error:", e)
        return None
    if client_name:
        cl = client_name.lower()
        for r in rows:
            if cl in (r.get("client_name", "") or "").lower():
                return r
    return rows[0] if rows else None


def run_owner_assistant(profile, user_text):
    """Run the owner's assistant brain over a single text command and return a
    short reply. Reuses ASSISTANT_TOOLS + _assistant_execute_tool, and is aware
    of any customers currently waiting so it can reply to them too."""
    sender = profile.get("sender", "")
    owner_name = profile.get("owner_name", "") or "there"
    biz = profile.get("business_name", "your business")
    today = datetime.date.today().strftime("%A %d %B %Y")

    pend_txt = ""
    try:
        pend = (supabase.table("pending_actions").select("*").eq("sender", sender)
                .eq("status", "pending").order("created_at", desc=True).limit(5).execute().data or [])
    except Exception:
        pend = []
    if pend:
        pend_txt = "\n\nCUSTOMERS WAITING ON YOU RIGHT NOW (most recent first):\n"
        for p in pend:
            opts = p.get("options") or []
            if isinstance(opts, str):
                try:
                    opts = json.loads(opts)
                except Exception:
                    opts = [o.strip() for o in opts.split(",") if o.strip()]
            who = p.get("client_name") or p.get("client_number") or "a customer"
            line = "- " + who + ": " + (p.get("reason", "") or "needs a reply")
            if isinstance(opts, list) and opts:
                line += " [options: " + ", ".join(str(o) for o in opts) + "]"
            pend_txt += line + "\n"

    system_prompt = (
        "You are " + biz + "'s assistant, texting with the OWNER " + owner_name + ". "
        "Today is " + today + ". This is SMS \u2014 keep every reply very short, plain and human, no markdown. "
        "You can create quotes and invoices, check and add diary bookings, read enquiries, mark invoices paid, "
        "message clients, and reply to waiting customers, all using your tools. "
        "Pick the right tool and just do it; don't ask for confirmation unless something is genuinely ambiguous.\n"
        "If the owner is responding to a customer who is waiting (a bare number choosing an option, confirming a time, "
        "giving a price, or saying what to tell them), use respond_to_customer \u2014 a bare number refers to the option "
        "list of the most recent waiting customer. If they say to leave/ignore it, use dismiss_customer_request. "
        "Otherwise help them with the right tool. Never mention tool names or internal tags; just confirm what you did "
        "in a few words." + pend_txt
    )

    messages = [{"role": "user", "content": user_text}]
    try:
        for _ in range(5):
            resp = client.messages.create(model="claude-sonnet-4-5", max_tokens=600,
                                           system=system_prompt, tools=ASSISTANT_TOOLS, messages=messages)
            if resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                tool_results = []
                for block in resp.content:
                    if getattr(block, "type", "") == "tool_use":
                        out = _assistant_execute_tool(block.name, block.input, profile)
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                messages.append({"role": "user", "content": tool_results})
                continue
            parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            return ("\n".join(parts)).strip() or "Done."
        return "Done."
    except Exception as e:
        print("run_owner_assistant error:", e)
        return "Sorry, I couldn't action that just now \u2014 try again, or open VanOffice."


def handle_owner_reply(profile, owner_text):
    """The owner texted their own VanOffice number. Action it by reply: instant
    approve/dismiss when a decision is waiting, otherwise hand to the assistant
    so they can run the business by text (quotes, diary, invoices, replies)."""
    sender = profile.get("sender", "")
    text = (owner_text or "").strip()
    if not text:
        return "Send me a quick instruction \u2014 e.g. 'what's on tomorrow', or reply to a waiting customer."

    try:
        pend = (supabase.table("pending_actions").select("*")
                .eq("sender", sender).eq("status", "pending")
                .order("created_at", desc=True).limit(5).execute().data or [])
    except Exception as e:
        print("owner reply pending fetch error:", e)
        pend = []

    # Fast, deterministic path for the commonest actions when a decision is waiting.
    if pend:
        pa = pend[0]
        who = pa.get("client_name") or pa.get("client_number") or "the customer"
        low = text.lower()
        if low in ("ignore", "dismiss", "skip", "leave it", "leave", "nothing", "no"):
            try:
                supabase.table("pending_actions").update({
                    "status": "dismissed", "resolved_at": datetime.datetime.now().isoformat()
                }).eq("id", pa.get("id")).execute()
            except Exception as e:
                print("owner dismiss error:", e)
            more = (" (" + str(len(pend) - 1) + " more waiting.)") if len(pend) > 1 else ""
            return "Okay, left that with " + who + " for now." + more

        options = pa.get("options") or []
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except Exception:
                options = [o.strip() for o in options.split(",") if o.strip()]
        if text.isdigit() and isinstance(options, list) and options:
            idx = int(text) - 1
            if 0 <= idx < len(options):
                res = _resolve_pending_action(profile, pa, options[idx], send_verbatim=False)
                if not res.get("ok"):
                    return "Couldn't send that to " + who + " just now \u2014 try again, or open VanOffice."
                msg = "\u2713 Sent to " + who + "."
                if res.get("booked"):
                    msg += " " + res["booked"] + "."
                if len(pend) > 1:
                    msg += " (" + str(len(pend) - 1) + " more waiting \u2014 reply to action the next.)"
                return msg

    # Everything else (natural-language approvals + all other commands) → the assistant.
    return run_owner_assistant(profile, text)


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

        res = _resolve_pending_action(profile, pa, choice, send_verbatim)
        if not res.get("ok"):
            return jsonify({"ok": False, "error": res.get("error") or "Send failed"}), 200
        return jsonify({"ok": True, "sent": res.get("sent"), "booked": res.get("booked")})
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


@app.route("/cron/quote-reminders", methods=["GET", "POST"])
def cron_quote_reminders():
    """Run daily by a scheduler. Texts every customer who has a visit booked for
    TODAY a short 'still ok for later?' confirmation. Protected by CRON_KEY."""
    expected = os.environ.get("CRON_KEY", "")
    if not expected or request.args.get("key", "") != expected:
        return jsonify({"error": "Unauthorised"}), 401
    today = datetime.date.today().isoformat()
    sent, skipped = 0, 0
    try:
        rows = supabase.table("bookings").select("*").eq("date", today).execute().data or []
    except Exception as e:
        print("cron bookings fetch error:", e)
        return jsonify({"error": str(e)}), 500

    prof_cache = {}
    for b in rows:
        try:
            if b.get("reminder_sent"):
                skipped += 1
                continue
            sender = b.get("sender", "")
            if not sender:
                continue
            cnum = (b.get("client_number", "") or "").strip()
            cname = (b.get("client_name", "") or "").strip()
            # Fall back to the contacts directory if the booking has no number stored.
            if not cnum and cname:
                try:
                    cc = (supabase.table("client_contacts").select("client_number")
                          .eq("sender", sender).ilike("name", "%" + cname + "%")
                          .limit(1).execute().data or [])
                    if cc:
                        cnum = (cc[0].get("client_number", "") or "").strip()
                except Exception:
                    pass
            if not cnum:
                skipped += 1
                continue
            if sender not in prof_cache:
                pr = supabase.table("profiles").select("*").eq("sender", sender).limit(1).execute().data or []
                prof_cache[sender] = pr[0] if pr else None
            profile = prof_cache[sender]
            if not profile:
                continue
            time_txt = (b.get("time", "") or "").strip()
            when = ("at " + time_txt) if time_txt else "later today"
            is_quote = "quote" in (b.get("job_type", "") or "").lower()
            what = " to take a look and quote" if is_quote else ""
            first = cname.split()[0] if cname else "there"
            msg = ("Hi " + first + ", just confirming we're still good to pop round " + when + what +
                   ". Let us know if that still works \u2014 thanks!")
            res = send_to_client(profile, cnum, msg)
            if res.get("ok"):
                try:
                    supabase.table("bookings").update({"reminder_sent": True}).eq("id", b.get("id")).execute()
                except Exception:
                    pass
                sent += 1
        except Exception as e:
            print("cron reminder error:", e)
    return jsonify({"ok": True, "sent": sent, "skipped": skipped})


# ---- Lead capture page: a tagged front door that turns scans/links into enquiries ----
def ensure_capture_slug(profile):
    """Return the profile's public capture slug, generating + saving one if missing."""
    slug = (profile.get("capture_slug") or "").strip()
    if slug:
        return slug
    import secrets
    slug = secrets.token_urlsafe(8).replace("-", "").replace("_", "").lower()[:8] or "vo"
    try:
        supabase.table("profiles").update({"capture_slug": slug}).eq("phone", profile.get("phone", "")).execute()
    except Exception as e:
        print("capture_slug save error:", e)
    return slug


def _capture_source_label(src):
    m = {"van": "Van QR", "site": "Site board", "card": "Business card",
         "google": "Google", "fb": "Facebook", "fbad": "Facebook ad",
         "insta": "Instagram", "referral": "Referral", "web": "Website", "link": "Quote link"}
    return m.get((src or "").lower(), "Quote link")


@app.route("/q/<slug>", methods=["GET"])
def capture_page(slug):
    import html as _h, re as _re
    try:
        res = supabase.table("profiles").select("*").eq("capture_slug", slug).limit(1).execute().data or []
    except Exception:
        res = []
    if not res:
        return "Not found", 404
    profile = res[0]
    biz = _h.escape(profile.get("business_name") or "us")
    trade = _h.escape(profile.get("trade") or "")
    src = _re.sub(r"[^a-zA-Z0-9_-]", "", request.args.get("src", "link"))[:20] or "link"
    try:
        supabase.table("social_clicks").insert({"sender": profile.get("sender", ""), "slug": slug, "source": src}).execute()
    except Exception as e:
        print("click log error: " + str(e))
    return """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Get a free quote - """ + biz + """</title>
<style>*{box-sizing:border-box}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0e1013;color:#f2f4f7}
.wrap{max-width:480px;margin:0 auto;padding:28px 20px 40px}.card{background:#171a1f;border:1px solid #262b33;border-radius:16px;padding:22px}
h1{font-size:1.5rem;margin:0 0 6px;letter-spacing:-.02em}.sub{color:#9aa3ad;font-size:.92rem;margin-bottom:18px;line-height:1.5}
label{display:block;font-size:.82rem;color:#c4ccd4;margin:14px 0 6px}
input,textarea{width:100%;padding:13px;border-radius:10px;border:1px solid #2c323b;background:#0e1013;color:#f2f4f7;font-size:1rem;font-family:inherit}
textarea{min-height:90px;resize:vertical}
button{width:100%;margin-top:20px;padding:15px;border:none;border-radius:11px;background:#ff8a1e;color:#2a1503;font-weight:800;font-size:1.02rem;cursor:pointer}
.foot{text-align:center;color:#6b7480;font-size:.72rem;margin-top:18px}</style></head><body><div class=wrap><div class=card>
<h1>Get a free quote</h1>
<div class=sub>""" + biz + (" - " + trade if trade else "") + """. Pop your details in and we'll text you straight back to sort it.</div>
<form method=post action="/q/""" + _h.escape(slug) + """/submit">
<input type=hidden name=src value=\"""" + _h.escape(src) + """\">
<label>Your name</label><input name=name required>
<label>Mobile number</label><input name=phone type=tel inputmode=tel required>
<label>What do you need doing?</label><textarea name=job required placeholder="e.g. 6 doors fitted and a couple of shelves"></textarea>
<label>Address / area (optional)</label><input name=location placeholder="Street or postcode">
<button type=submit>Get my quote</button></form></div>
<div class=foot>Powered by VanOffice</div></div></body></html>"""


@app.route("/q/<slug>/submit", methods=["POST"])
def capture_submit(slug):
    import html as _h, re as _re
    try:
        res = supabase.table("profiles").select("*").eq("capture_slug", slug).limit(1).execute().data or []
    except Exception:
        res = []
    if not res:
        return "Not found", 404
    profile = res[0]
    sender = profile.get("sender", "")
    twilio_number = profile.get("twilio_number") or os.environ.get("TWILIO_NUMBER", "")
    biz = profile.get("business_name") or "us"

    name = (request.form.get("name", "") or "").strip()
    phone = (request.form.get("phone", "") or "").strip()
    job = (request.form.get("job", "") or "").strip()
    location = (request.form.get("location", "") or "").strip()
    src = _re.sub(r"[^a-zA-Z0-9_-]", "", request.form.get("src", "link"))[:20] or "link"
    source_label = _capture_source_label(src)
    if not (name and phone and job):
        return "Please go back and add your name, number and what you need.", 400
    cust = format_phone(phone)

    # 1) Log the enquiry, tagged with where it came from.
    try:
        supabase.table("enquiries").insert({
            "sender": sender, "message": job, "summary": job,
            "client_name": name, "job_type": job, "location": location,
            "status": "new", "source": source_label
        }).execute()
    except Exception as e:
        print("capture enquiry insert error:", e)

    # 2) Remember the contact + seed the conversation so the bot has context.
    try:
        upsert_contact(sender, cust, name)
    except Exception:
        pass
    seed = "Hi, I'm " + name + ". " + job + ((" Address: " + location) if location else "")
    try:
        supabase.table("client_chats").insert({
            "twilio_number": twilio_number, "client_number": cust,
            "message": seed, "direction": "inbound", "sender_profile": sender, "channel": "sms"
        }).execute()
    except Exception as e:
        print("capture seed chat error:", e)

    # 3) Text the customer to kick off the bot.
    first = name.split()[0] if name else "there"
    opening = ("Hi " + first + ", thanks for your enquiry to " + biz + "! To get you a quick quote, "
               "what's the best address for us to come and take a look, and when roughly suits you?")
    try:
        send_res = send_to_client(profile, cust, opening)
        if send_res.get("ok"):
            supabase.table("client_chats").insert({
                "twilio_number": twilio_number, "client_number": cust,
                "message": opening, "direction": "outbound",
                "sender_profile": sender, "channel": send_res.get("channel", "sms")
            }).execute()
    except Exception as e:
        print("capture opening send error:", e)

    # 4) The "VanOffice got you this" moment.
    try:
        notify_owner(profile, "\u2728 VanOffice just landed you an enquiry via " + source_label + ":\n"
                     + name + " \u2014 " + job + ((" (" + location + ")") if location else "")
                     + "\nWe've texted them to get the details - it's in your inbox.")
    except Exception as e:
        print("capture notify error:", e)

    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0e1013;color:#f2f4f7;"
            "min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;padding:24px\">"
            "<div><div style='font-size:2.4rem'>\u2705</div><h2 style='letter-spacing:-.02em'>Thanks "
            + _h.escape(first) + "!</h2><p style='color:#9aa3ad;max-width:300px;line-height:1.5'>"
            + _h.escape(biz) + " has got your enquiry and will text you in the next few minutes to sort your quote.</p></div></div>")


@app.route("/api/capture-link", methods=["GET"])
def api_capture_link():
    phone = format_phone(request.args.get("phone", "").strip())
    pin = request.args.get("pin", "")
    if not phone or not pin:
        return jsonify({"error": "Phone and PIN required"}), 401
    try:
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data:
            return jsonify({"error": "Profile not found"}), 404
        profile = result.data[0]
        if str(profile.get("pin", "")) != str(pin):
            return jsonify({"error": "Invalid PIN"}), 401
        slug = ensure_capture_slug(profile)
        base = request.url_root.rstrip("/")
        link = base + "/q/" + slug
        sources = [
            {"label": "Van / sticker", "src": "van", "url": link + "?src=van"},
            {"label": "Site board", "src": "site", "url": link + "?src=site"},
            {"label": "Business card", "src": "card", "url": link + "?src=card"},
            {"label": "Facebook / social", "src": "fb", "url": link + "?src=fb"},
            {"label": "Plain link", "src": "link", "url": link},
        ]
        return jsonify({"ok": True, "slug": slug, "link": link, "sources": sources})
    except Exception as e:
        print("api_capture_link error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-ad", methods=["POST"])
def api_generate_ad():
    phone = format_phone(request.args.get("phone", "").strip())
    pin = request.args.get("pin", "")
    if not phone or not pin:
        return jsonify({"error": "Phone and PIN required"}), 401
    try:
        result = supabase.table("profiles").select("*").eq("phone", phone).execute()
        if not result.data:
            return jsonify({"error": "Profile not found"}), 404
        profile = result.data[0]
        if str(profile.get("pin", "")) != str(pin):
            return jsonify({"error": "Invalid PIN"}), 401
        data = request.get_json(force=True, silent=True) or {}
        job = (data.get("job_type", "") or "").strip()
        location = (data.get("location", "") or "").strip()
        biz = profile.get("business_name") or "our business"
        trade = profile.get("trade") or ""
        slug = ensure_capture_slug(profile)
        link = request.url_root.rstrip("/") + "/q/" + slug + "?src=fb"

        sys = ("You write short, upbeat social posts for a UK tradesperson to put on their OWN "
               "Facebook/Instagram, showing off a recently completed job to win more work.\n"
               "Business: " + biz + (". Trade: " + trade if trade else "") + ". "
               "Recent job: " + (job or "a recent job") + ((" in " + location) if location else "") + ".\n"
               "Write 3 DIFFERENT ready-to-post captions. Rules: friendly and natural (a real tradesperson, not corporate); "
               "1-3 short sentences each; a few tasteful emojis are fine; do NOT include any customer names or personal details; "
               "do NOT invent prices or specific claims. End each caption with a clear call to action for a free quote and this "
               "exact link on its own final line: " + link + "\n"
               "Return ONLY the 3 captions, separated by a line containing just ---. No numbering, no preamble.")
        ai = client.messages.create(model="claude-sonnet-4-5", max_tokens=600,
                                    system=sys, messages=[{"role": "user", "content": "Write the 3 posts."}])
        raw = ai.content[0].text.strip()
        variants = [v.strip() for v in raw.split("---") if v.strip()]
        variants = [(v if link in v else (v + "\n" + link)) for v in variants][:3]
        if not variants:
            variants = ["Another job done by " + biz + "! Get in touch for a free quote:\n" + link]
        return jsonify({"ok": True, "variants": variants, "link": link, "business": biz, "trade": trade, "logo": profile.get("logo") or "", "phone": profile.get("twilio_number") or ""})
    except Exception as e:
        print("api_generate_ad error:", e)
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
    # 2) SMS — uses ADMIN_SMS_FROM if set, otherwise your (SMS-capable) TWILIO_NUMBER
    try:
        admin_phone = os.environ.get("ADMIN_PHONE", "")
        sms_from = os.environ.get("ADMIN_SMS_FROM", "") or os.environ.get("TWILIO_NUMBER", "")
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
PWA_ICON_192 = "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"
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
        "background_color": "#0d0c0a", "theme_color": "#0d0c0a",
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
