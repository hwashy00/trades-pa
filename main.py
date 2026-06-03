import os
import random
import traceback
import datetime
import io
import json
from flask import Flask, request, jsonify, send_file, redirect, session
import anthropic
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client as TwilioClient

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

                if next_chase == 1:
                    msg = f"Hi {first_name}, just a reminder that invoice {inv_num} for £{total} is now {days_overdue} day(s) overdue. Please arrange payment at your earliest convenience. Thanks, {sender}"
                elif next_chase == 2:
                    msg = f"Hi {first_name}, second reminder — invoice {inv_num} for £{total} is {days_overdue} days overdue. Please settle this as soon as possible."
                else:
                    msg = f"FINAL NOTICE: {first_name}, invoice {inv_num} for £{total} is {days_overdue} days overdue. Immediate payment required or we may pursue this through small claims. Please contact us urgently."

                twilio_client.messages.create(to=phone, from_=from_number, body=msg)

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
scheduler.add_job(send_morning_summary, "cron", hour=8, minute=0)
scheduler.add_job(scan_all_emails, "interval", minutes=15)
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
        scope_items_used = [f'{i.get("description","")} — £{i.get("amount","")}' for i in scope_items_used]

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
                    resp.message("All set " + data.get("owner_name", "") + "! You are ready to go.\n\nVisit your dashboard at:\nhttps://trades-pa-trades-pa.up.railway.app/dashboard\n\nLog in with your mobile number and PIN.")
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
    try:
        supabase.table("enquiries").insert({
            "sender": caller, "message": "INCOMING CALL",
            "summary": "Call from " + caller, "client_name": "",
            "job_type": "incoming call", "location": "", "status": "incoming call"
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
                from_=called, to=caller
            )
            supabase.table("enquiries").update({
                "status": "missed call", "job_type": "missed call",
                "summary": "Missed call from " + caller
            }).eq("sender", caller).eq("status", "incoming call").execute()
        except Exception as e:
            print("SMS error: " + str(e))
    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()
    return str(resp)


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
            "sender_profile": sender
        }).execute()

        # Get conversation history with this client
        history = supabase.table("client_chats").select("*").eq("twilio_number", twilio_number).eq("client_number", client_number).order("created_at").execute().data
        chat_messages = []
        for h in history[-10:]:
            if h.get("direction") == "inbound":
                chat_messages.append({"role": "user", "content": h.get("message", "")})
            else:
                chat_messages.append({"role": "assistant", "content": h.get("message", "")})

        system = "You are the virtual assistant for " + biz_name + ", run by " + owner_name + ".\n"
        system += "Trade: " + str(profile.get("trade", "")) + "\n\n"
        system += "You are responding to a potential client who has texted the business number.\n"
        system += "Rules:\n"
        system += "- Respond as 'we' not 'I'. You represent the business, not the person.\n"
        system += "- Be friendly, professional, and helpful.\n"
        system += "- Collect: what job they need, where they are, when they want it done.\n"
        system += "- Never pretend to be " + owner_name + " personally.\n"
        system += "- If they ask something you cant answer, say you will get " + owner_name + " to call them back.\n"
        system += "- Keep responses short - this is a text message.\n"
        system += "- If you have collected enough details (job type, location, timing), end with:\n"
        system += "NEWJOB:name=<client name or Unknown>|job=<job type>|location=<location>\n"
        system += "- Only add the NEWJOB tag once you have the key details, not on every message.\n"

        ai_response = client.messages.create(model="claude-sonnet-4-5", max_tokens=300, system=system, messages=chat_messages)
        reply = ai_response.content[0].text.strip()

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
                account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
                notify_client = TwilioClient(account_sid, auth_token)
                notify_client.messages.create(
                    from_="whatsapp:" + twilio_number,
                    to="whatsapp:" + profile.get("phone", ""),
                    body="💰 Invoice " + inv_num + " for £" + str(total) + " has been marked as paid — client replied via SMS."
                )
        
        # Check for new job extraction
        if "NEWJOB:" in reply:
            try:
                job_line = reply.split("NEWJOB:")[1].strip().split("\n")[0]
                parts = dict(p.split("=") for p in job_line.split("|"))
                supabase.table("enquiries").insert({
                    "sender": sender,
                    "message": "Client SMS from " + client_number,
                    "summary": "New enquiry via text from " + client_number,
                    "client_name": parts.get("name", "Unknown"),
                    "job_type": parts.get("job", ""),
                    "location": parts.get("location", ""),
                    "status": "new"
                }).execute()

                # Notify tradesperson
                account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
                twilio_client = TwilioClient(account_sid, auth_token)
                twilio_client.messages.create(
                    body="New enquiry from " + client_number + ":\n" + parts.get("name", "Unknown") + " - " + parts.get("job", "") + " - " + parts.get("location", "") + "\n\nHandled automatically by VanOffice.",
                    from_="whatsapp:+14155238886",
                    to="whatsapp:" + profile.get("phone", "")
                )
            except Exception as e:
                print("Job extraction error: " + str(e))

        clean_reply = reply.split("NEWJOB:")[0].strip()

        # Save outgoing message
        supabase.table("client_chats").insert({
            "twilio_number": twilio_number,
            "client_number": client_number,
            "message": clean_reply,
            "direction": "outbound",
            "sender_profile": sender
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
        fields = ["business_name", "trade", "day_rate", "half_day_rate", "hourly_rate",
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
        sender = profile.get("sender", "")

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

        data = request.json
        history = data.get("history", [])
        message = data.get("message", "")
        image_data = data.get("image", None)
        image_type = data.get("imageType", "image/jpeg")

        if not message and not image_data:
            return jsonify({"error": "No message provided"}), 400

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
            return jsonify(quote_data)
        else:
            return jsonify({"type": "question", "message": reply})

    except Exception as e:
        print("Generate quote AI error: " + str(e))
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
