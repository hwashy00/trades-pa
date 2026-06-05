"""
vanoffice_intelligence.py
─────────────────────────
Two upgrades that make VanOffice feel like a real PA:

  1. send_intelligent_briefing()  — a daily 7am WhatsApp that is WRITTEN by Claude
     from the day's real data (diary, weather at the job, unpaid invoices, quiet
     quotes, new enquiries). Reads like a sharp office manager, not a list.

  2. get_weather_for_location()   — keyless forecast (Open-Meteo) used by the
     briefing to flag rain on outdoor jobs.

This module is self-contained. main.py wires it in with three lines (see PATCH notes
at the bottom of this file). It deliberately reuses the objects main.py already has
(supabase, anthropic client, Twilio) by receiving them as arguments — no globals,
nothing to break.
"""

import os
import json
import datetime
import urllib.request
import urllib.parse


# ──────────────────────────────────────────────────────────────────────────
# WEATHER  (keyless — Open-Meteo)
# ──────────────────────────────────────────────────────────────────────────

# Trades that care about the forecast. Used to decide whether to mention weather.
_OUTDOOR_TRADES = {
    "roofer", "roofing", "scaffolder", "scaffolding", "groundworker",
    "groundworks", "landscaper", "landscaping", "bricklayer", "brickwork",
    "fencer", "fencing", "decking", "paver", "paving", "driveway", "render",
    "renderer", "rendering", "painter", "decorator", "guttering", "window",
    "conservatory", "extension", "builder", "building",
}

_WEATHER_CODE = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    80: "rain showers", 81: "rain showers", 82: "heavy showers",
    95: "thunderstorms", 96: "thunderstorms", 99: "thunderstorms",
}

_WET_CODES = {51, 53, 55, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def _http_json(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "VanOffice/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _geocode(place):
    """Free-text place (a job location/postcode) -> (lat, lon, label) or None."""
    if not place or not place.strip():
        return None
    try:
        q = urllib.parse.quote(place.strip())
        url = ("https://geocoding-api.open-meteo.com/v1/search"
               f"?name={q}&count=1&language=en&country=GB")
        data = _http_json(url)
        res = (data.get("results") or [])
        if not res:
            return None
        r = res[0]
        return (r["latitude"], r["longitude"], r.get("name", place))
    except Exception as e:
        print("geocode error:", e)
        return None


def get_weather_for_location(location, on_date=None):
    """
    Returns a small dict describing the day's weather at a job location, or None.
      { 'summary': 'rain from 2pm', 'wet': True, 'max_c': 12, 'min_c': 7,
        'wet_from': '14:00', 'label': 'Exeter' }
    Keyless. Safe to call in a scheduler — never raises.
    """
    geo = _geocode(location)
    if not geo:
        return None
    lat, lon, label = geo
    on_date = on_date or datetime.date.today()
    try:
        url = ("https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               "&hourly=weathercode,precipitation_probability"
               "&daily=weathercode,temperature_2m_max,temperature_2m_min"
               "&timezone=auto"
               f"&start_date={on_date.isoformat()}&end_date={on_date.isoformat()}")
        data = _http_json(url)
        daily = data.get("daily", {})
        hourly = data.get("hourly", {})
        code = (daily.get("weathercode") or [0])[0]
        max_c = (daily.get("temperature_2m_max") or [None])[0]
        min_c = (daily.get("temperature_2m_min") or [None])[0]

        # Find the first wet hour during working hours (07:00–18:00).
        wet_from = None
        times = hourly.get("time", [])
        codes = hourly.get("weathercode", [])
        for t, c in zip(times, codes):
            hh = int(t[11:13]) if len(t) >= 13 else 0
            if 7 <= hh <= 18 and c in _WET_CODES:
                wet_from = t[11:16]
                break

        desc = _WEATHER_CODE.get(code, "mixed")
        wet = code in _WET_CODES or wet_from is not None
        if wet and wet_from:
            summary = f"{desc}, wet from {wet_from}"
        elif wet:
            summary = desc
        else:
            summary = desc
        return {
            "summary": summary, "wet": wet, "wet_from": wet_from,
            "max_c": round(max_c) if max_c is not None else None,
            "min_c": round(min_c) if min_c is not None else None,
            "label": label,
        }
    except Exception as e:
        print("weather error:", e)
        return None


# ──────────────────────────────────────────────────────────────────────────
# DATA SNAPSHOT  — pulls the day's real numbers for one tradesperson
# ──────────────────────────────────────────────────────────────────────────

def _num(v):
    try:
        return float(str(v).replace("\u00a3", "").replace(",", "") or 0)
    except Exception:
        return 0.0


def _days_since(iso_ts):
    try:
        d = datetime.date.fromisoformat(str(iso_ts)[:10])
        return (datetime.date.today() - d).days
    except Exception:
        return None


def build_day_snapshot(supabase, profile, include_weather=True):
    """Collect everything the briefing might mention, for one profile."""
    sender = profile.get("sender", "")
    today = datetime.date.today().isoformat()
    trade = (profile.get("trade") or "").lower()
    is_outdoor = any(w in trade for w in _OUTDOOR_TRADES)

    bookings = supabase.table("bookings").select("*").eq("sender", sender).execute().data or []
    today_jobs = sorted(
        [b for b in bookings if str(b.get("date", "")).startswith(today)],
        key=lambda b: (b.get("time", "") or "")
    )

    invoices = supabase.table("invoices").select("*").eq("sender", sender).execute().data or []
    unpaid = [i for i in invoices if i.get("status") != "paid"]
    overdue = [i for i in unpaid if i.get("due_date") and str(i["due_date"]) < today]
    unpaid_total = sum(_num(i.get("total", 0)) for i in unpaid)

    quotes = supabase.table("quotes").select("*").eq("sender", sender).execute().data or []
    # "Quiet" quotes = sent, no acceptance, 3+ days old.
    quiet_quotes = []
    for q in quotes:
        if q.get("status") == "sent":
            age = _days_since(q.get("created_at"))
            if age is not None and age >= 3:
                quiet_quotes.append({
                    "client": q.get("client_name", "a client"),
                    "total": q.get("total", ""),
                    "age": age,
                    "job": q.get("job_description", ""),
                })

    new_enq = supabase.table("enquiries").select("*").eq("status", "new").eq("sender", sender).execute().data or []

    # Weather for the first outdoor job today
    weather = None
    if include_weather and is_outdoor and today_jobs:
        loc = today_jobs[0].get("location") or today_jobs[0].get("description") or ""
        if loc:
            weather = get_weather_for_location(loc)

    return {
        "owner": (profile.get("owner_name") or profile.get("business_name") or "").split(" ")[0],
        "today_jobs": [
            {"client": b.get("client_name", ""), "time": b.get("time", ""),
             "location": b.get("location", ""),
             "job": b.get("description") or b.get("job_type") or "job"}
            for b in today_jobs
        ],
        "overdue": [
            {"client": i.get("client_name", ""), "total": i.get("total", ""),
             "number": i.get("invoice_number", ""),
             "days_overdue": _days_since(i.get("due_date"))}
            for i in overdue
        ],
        "unpaid_count": len(unpaid),
        "unpaid_total": int(unpaid_total),
        "quiet_quotes": quiet_quotes,
        "new_enquiries": [
            {"client": e.get("client_name", ""), "job": e.get("job_type") or e.get("summary") or "enquiry"}
            for e in new_enq
        ],
        "weather": weather,
        "is_outdoor": is_outdoor,
    }


# ──────────────────────────────────────────────────────────────────────────
# THE BRIEFING — Claude writes it from the snapshot
# ──────────────────────────────────────────────────────────────────────────

_BRIEFING_SYSTEM = (
    "You are the personal assistant for a UK trades business, writing the owner's "
    "morning WhatsApp briefing. You are given a JSON snapshot of their real day. "
    "Write 2-4 short sentences, warm and direct, like a sharp office manager who has "
    "already glanced at everything. British English, pounds. Lead with what matters most. "
    "Rules: "
    "- If there's a job today, mention the first one (client + time). "
    "- If weather is present AND wet, work it into advice naturally (e.g. get outdoor work done before the rain). "
    "- If an invoice is overdue, offer to chase it. "
    "- If a quote has gone quiet (3+ days), offer to follow up, naming the client. "
    "- Mention new enquiries to look at if any. "
    "- If the day is genuinely clear, say so briefly and warmly — don't invent tasks. "
    "Never use bullet points, headers or emojis. Never list more than the 2-3 most important things. "
    "Do not greet with 'Good morning' more than once. End with a light, natural offer to act if there's an obvious next step."
)


def compose_briefing_text(anthropic_client, snapshot):
    """Ask Claude to write the human briefing. Falls back to a simple line on error."""
    try:
        msg = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            system=_BRIEFING_SYSTEM,
            messages=[{
                "role": "user",
                "content": "Here is today's snapshot. Write the briefing.\n\n" + json.dumps(snapshot, ensure_ascii=False)
            }],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        return text or _fallback_briefing(snapshot)
    except Exception as e:
        print("briefing compose error:", e)
        return _fallback_briefing(snapshot)


def _fallback_briefing(s):
    part = "Morning" + ((" " + s["owner"]) if s.get("owner") else "") + "."
    bits = [part]
    if s["today_jobs"]:
        j = s["today_jobs"][0]
        bits.append("First job today" + ((" — " + j["client"]) if j["client"] else "") +
                    ((" at " + j["time"]) if j["time"] else "") + ".")
        if s.get("weather") and s["weather"].get("wet"):
            bits.append("Heads up: " + s["weather"]["summary"] + " where you're working.")
    else:
        bits.append("Nothing in the diary today.")
    if s["overdue"]:
        bits.append(str(len(s["overdue"])) + " invoice(s) overdue — want me to chase?")
    elif s["quiet_quotes"]:
        q = s["quiet_quotes"][0]
        bits.append(q["client"] + "'s quote has gone quiet — shall I follow up?")
    if s["new_enquiries"]:
        bits.append(str(len(s["new_enquiries"])) + " new enquiry/enquiries to look at.")
    return " ".join(bits)


def _wa(n):
    return n if str(n).startswith("whatsapp:") else "whatsapp:" + str(n)


def send_intelligent_briefing(supabase, anthropic_client, twilio_client_factory):
    """
    Scheduler entry point. Sends each tradesperson a Claude-written briefing on WhatsApp.

      twilio_client_factory: a zero-arg callable returning a Twilio Client
                             (lets main.py pass its own credentials/setup).
    """
    try:
        from_number = os.environ.get("TWILIO_NUMBER")
        profiles = supabase.table("profiles").select("*").execute().data or []
        if not profiles:
            return
        tc = twilio_client_factory()
        for profile in profiles:
            try:
                # Owner's WhatsApp = their onboarding 'sender' (already whatsapp:+44...).
                to = profile.get("sender", "")
                if not to:
                    continue
                snapshot = build_day_snapshot(supabase, profile)
                # Skip totally empty days for brand-new accounts with nothing at all.
                has_anything = (snapshot["today_jobs"] or snapshot["overdue"] or
                                snapshot["quiet_quotes"] or snapshot["new_enquiries"] or
                                snapshot["unpaid_count"])
                text = compose_briefing_text(anthropic_client, snapshot)
                if not text:
                    continue
                tc.messages.create(body=text, from_=_wa(from_number), to=_wa(to))
                print("Briefing sent to", to)
            except Exception as e:
                print("briefing send error for", profile.get("sender", "?"), ":", e)
    except Exception as e:
        print("send_intelligent_briefing error:", e)


# ──────────────────────────────────────────────────────────────────────────
# PATCH NOTES — how main.py wires this in (3 edits, all near the scheduler):
#
#   from vanoffice_intelligence import send_intelligent_briefing
#
#   def _twilio_factory():
#       return TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"),
#                           os.environ.get("TWILIO_AUTH_TOKEN"))
#
#   # replace the old morning summary job:
#   scheduler.add_job(lambda: send_intelligent_briefing(supabase, client, _twilio_factory),
#                     "cron", hour=7, minute=0)
# ──────────────────────────────────────────────────────────────────────────
