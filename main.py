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


def generate_quote_pdf(quote, profile, template):
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
    s_right = ParagraphStyle("right", parent=s_normal, alignment=TA_RIGHT)
    s_center = ParagraphStyle("center", parent=s_normal, alignment=TA_CENTER)

    story = []

    biz_name = (template.get("business_name") if template else None) or profile.get("business_name", "")
    biz_address = (template.get("business_address") if template else None) or ""
    biz_phone = (template.get("business_phone") if template else None) or profile.get("phone", "")
    biz_email = (template.get("business_email") if template else None) or ""
    validity = (template.get("quote_validity_days") if template else None) or profile.get("payment_terms", "30")
    payment_details = (template.get("payment_details") if template else None) or ""
    terms_text = (template.get("terms") if template else None) or ""
    footer_text = (template.get("footer_text") if template else None) or "Thank you for your business."

    # Logo
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
        Paragraph("<font size=14><b>QUOTATION</b></font>", s_right)
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

    today_str = datetime.date.today().strftime("%d %B %Y")
    quote_num = quote.get("quote_number", "QU-001")

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
            "<font size=8 color='grey'>QUOTE NUMBER</font><br/>" + quote_num +
            "<br/><br/><font size=8 color='grey'>DATE</font><br/>" + today_str +
            "<br/><br/><font size=8 color='grey'>VALID FOR</font><br/>" + str(validity) + " days",
            s_right
        )
    ]]
    info_table = Table(info_data, colWidths=[95*mm, 75*mm])
    info_table.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP")]))
    story.append(info_table)
    story.append(Spacer(1, 6*mm))

    client_name = quote.get("client_name", "")
    client_address = quote.get("client_address", "")
    to_text = "<font size=8 color='grey'>PREPARED FOR</font><br/><br/><b>" + client_name + "</b>"
    if client_address:
        to_text += "<br/>" + client_address
    story.append(Paragraph(to_text, s_normal))
    story.append(Spacer(1, 8*mm))

    line_items = quote.get("line_items", [])
    if line_items and len(line_items) > 0:
        table_data = [["Description", "Qty", "Unit Price", "Amount"]]
        subtotal = 0
        for item in line_items:
            desc = item.get("description", "")
            qty = item.get("qty", 1)
            unit_price = item.get("unit_price", 0)
            amount = float(qty) * float(unit_price)
            subtotal += amount
            table_data.append([desc, str(qty), "£{:.2f}".format(float(unit_price)), "£{:.2f}".format(amount)])

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

        totals_data = [["Subtotal", "£{:.2f}".format(subtotal)]]
        if vat_rate > 0:
            totals_data.append(["VAT (20%)", "£{:.2f}".format(vat_amount)])
        totals_data.append(["TOTAL", "£{:.2f}".format(total)])

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
        quote_text = quote.get("quote_text", "")
        if quote_text:
            skip_phrases = ["I can't actually", "Just copy that", "copy and send", "Here's the quote formatted", "---", "Cheers,", "Let me know if", "Reply PDF", "reply pdf"]
            story.append(Paragraph("<b>Quote Details</b>", s_normal))
            story.append(Spacer(1, 3*mm))
            for line in quote_text.split("\n"):
                stripped = line.strip().replace("**", "")
                if not stripped:
                    continue
                skip = False
                for phrase in skip_phrases:
                    if phrase.lower() in stripped.lower():
                        skip = True
                        break
                if not skip and not stripped.startswith("Hi ") and stripped != profile.get("owner_name", ""):
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


def generate_invoice_pdf(invoice, profile, template):
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
                        "trade": data.get("trade", ""), "day_rate": "0", "half_day_rate": "0",
                        "hourly_rate": "0", "materials_markup": "20", "payment_terms": "30",
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

    conversation_history[sender].append({"role": "user", "content": incoming_msg})

    today = datetime.date.today().strftime("%A %d %B %Y")

    system_prompt = "You are a PA for a trades business. Today is " + today + ".\n"
    system_prompt += "The user details are:\n"
    system_prompt += "Business: " + str(profile.get("business_name")) + "\n"
    system_prompt += "Name: " + str(profile.get("owner_name")) + "\n"
    system_prompt += "Trade: " + str(profile.get("trade")) + "\n"
    system_prompt += "Day rate: " + str(profile.get("day_rate")) + "\n"
    system_prompt += "Half day rate: " + str(profile.get("half_day_rate")) + "\n"
    system_prompt += "Hourly rate: " + str(profile.get("hourly_rate")) + "\n"
    system_prompt += "Materials markup: " + str(profile.get("materials_markup")) + "% (apply this silently - never show the markup to the customer)\n"
    system_prompt += "Payment terms: " + str(profile.get("payment_terms")) + " days\n"
    system_prompt += "VAT registered: " + str(profile.get("vat_registered")) + "\n\n"
    system_prompt += "You help with:\n"
    system_prompt += "1. Logging job enquiries - extract client name, address, job type, urgency\n"
    system_prompt += "2. Generating quotes - use their exact rates above. Present the quote clearly to the user on WhatsApp.\n"
    system_prompt += "3. Booking in jobs - extract client, job type, location, date, time, duration\n"
    system_prompt += "4. Tracking outstanding jobs and payments\n"
    system_prompt += "5. General scheduling and reminders\n\n"
    system_prompt += "Always be concise - this is WhatsApp. Do NOT say you cannot send messages. Just present the quote directly.\n\n"
    system_prompt += "When generating a QUOTE, present it on WhatsApp like this:\n"
    system_prompt += "Quote for [client name]\n"
    system_prompt += "[job description] at [address]\n\n"
    system_prompt += "Labour: [X] days @ [rate] = [amount]\n"
    system_prompt += "Materials: [total amount after markup already applied - never show the markup percentage to the customer]\n"
    system_prompt += "TOTAL: [amount]\n"
    system_prompt += "Payment due within [X] days\n\n"
    system_prompt += "Reply PDF to get this as a professional PDF document.\n\n"
    system_prompt += "Then end your reply with this data tag on a new line:\n"
    system_prompt += "LOG:name=<client name>|job=<job type>|location=<location>|status=quoted\n"
    system_prompt += "QUOTEDATA:" + json.dumps({"items": [{"description": "example", "qty": 1, "unit_price": 0}], "subtotal": "0", "total": "0"}) + "\n"
    system_prompt += "Replace the QUOTEDATA with the actual quote line items as valid JSON.\n\n"
    system_prompt += "If logging an enquiry (not a quote) end your reply with:\n"
    system_prompt += "LOG:name=<client name>|job=<job type>|location=<location>|status=new\n\n"
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
    if "QUOTEDATA:" in reply:
        try:
            qd_line = reply.split("QUOTEDATA:")[1].strip().split("\n")[0]
            qd = json.loads(qd_line)
            quote_items = qd.get("items", [])
            quote_subtotal = str(qd.get("subtotal", "0"))
            quote_total = str(qd.get("total", "0"))
        except Exception as e:
            print("QUOTEDATA parse error: " + str(e))

    if "LOG:" in reply:
        try:
            log_line = reply.split("LOG:")[1].strip().split("\n")[0]
            parts = dict(p.split("=") for p in log_line.split("|"))
            status = parts.get("status", "new")
            supabase.table("enquiries").insert({
                "sender": sender, "message": incoming_msg,
                "summary": reply.split("LOG:")[0].strip(),
                "client_name": parts.get("name", ""),
                "job_type": parts.get("job", ""),
                "location": parts.get("location", ""),
                "status": status
            }).execute()

            if status == "quoted":
                quote_count = supabase.table("quotes").select("id").eq("sender", sender).execute()
                quote_num = "QU-" + str(len(quote_count.data) + 1).zfill(3)
                clean_text = reply.split("LOG:")[0].split("QUOTEDATA:")[0].strip()
                supabase.table("quotes").insert({
                    "sender": sender,
                    "client_name": parts.get("name", ""),
                    "client_address": parts.get("location", ""),
                    "job_description": parts.get("job", ""),
                    "line_items": quote_items,
                    "subtotal": quote_subtotal,
                    "vat": "0",
                    "total": quote_total,
                    "status": "sent",
                    "quote_number": quote_num,
                    "quote_text": clean_text
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
                "due_date": parts.get("due", "")
            }).execute()
        except Exception as e:
            print("Invoice logging error: " + str(e))

    clean_reply = reply.split("LOG:")[0].split("BOOK:")[0].split("QUOTEDATA:")[0].split("INV:")[0].split("INVOICEDATA:")[0].strip()
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
        gmail_connected = bool(profile.get("gmail_token"))
        outlook_connected = bool(profile.get("outlook_token"))
        return jsonify({
            "enquiries": len(new_enq), "unpaid": len(quoted), "missed": len(missed),
            "recent": recent, "bookings": bookings, "profile": profile, "template": template,
            "emails": emails, "invoices": invoices, "gmail_connected": gmail_connected, "outlook_connected": outlook_connected
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
