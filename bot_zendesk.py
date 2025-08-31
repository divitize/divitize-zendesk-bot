# bot_zendesk.py
import os
import re
import time
import threading
from typing import List, Dict, Any

import requests
from flask import Flask
from openai import OpenAI

# =========================
# ENV
# =========================
Z_SUBDOMAIN   = os.getenv("ZENDESK_SUBDOMAIN", "").strip()
Z_EMAIL       = os.getenv("ZENDESK_EMAIL", "").strip()
Z_API_TOKEN   = os.getenv("ZENDESK_API_TOKEN", "").strip()

OPENAI_APIKEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL  = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

BRAND_NAME     = os.getenv("BRAND_NAME", "Divitize").strip()
SIGNATURE_NAME = os.getenv("SIGNATURE_NAME", "Noe").strip()

POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL_SEC", "35"))
DRAFT_TAG      = os.getenv("DRAFT_TAG", "chat_suggested_draft").strip()

# Custom field id for tracking (string is ok)
Z_TRACKING_FIELD = os.getenv("Z_TRACKING_FIELD", "").strip()

# Tag to avoid re-sending tracking
TRACKING_SENT_TAG = "tracking_sent"

# =========================
# ZENDESK REST
# =========================
Z_BASE = f"https://{Z_SUBDOMAIN}.zendesk.com/api/v2"
AUTH   = (f"{Z_EMAIL}/token", Z_API_TOKEN)

def z_get(path, params=None):
    r = requests.get(f"{Z_BASE}{path}", params=params or {}, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()

def z_put(path, payload):
    r = requests.put(f"{Z_BASE}{path}", json=payload, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()

def z_post(path, payload):
    r = requests.post(f"{Z_BASE}{path}", json=payload, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()

def list_recent_tickets(limit=40) -> List[Dict[str, Any]]:
    """
    Fetch a page of recently updated tickets.
    """
    data = z_get("/tickets.json", {"sort_by":"updated_at","sort_order":"desc","per_page":limit})
    return data.get("tickets", [])

def get_ticket(ticket_id: int) -> Dict[str, Any]:
    return z_get(f"/tickets/{ticket_id}.json").get("ticket", {})

def get_ticket_comments(ticket_id: int) -> List[Dict[str, Any]]:
    return z_get(f"/tickets/{ticket_id}/comments.json").get("comments", [])

def add_private_note_and_tag(ticket_id: int, body: str, tag: str):
    """
    Adds a **private** (internal) note and appends a tag.
    """
    payload = {
        "ticket": {
            "comment": {"public": False, "body": body},
            "additional_tags": [tag]
        }
    }
    z_put(f"/tickets/{ticket_id}.json", payload)

def add_public_reply_and_tag(ticket_id: int, body: str, tag: str):
    """
    Adds a **public** reply and appends a tag.
    """
    payload = {
        "ticket": {
            "comment": {"public": True, "body": body},
            "additional_tags": [tag]
        }
    }
    z_put(f"/tickets/{ticket_id}.json", payload)

def get_user_first_name(user_id: int) -> str:
    try:
        u = z_get(f"/users/{user_id}.json").get("user", {})
        name = (u.get("name") or "").strip()
        if not name:
            return "there"
        return name.split()[0]
    except Exception:
        return "there"

def get_custom_field_value(ticket: Dict[str, Any], field_id: str) -> str:
    """
    ticket['custom_fields'] is a list of {id, value}
    field_id is string; Zendesk ids are ints but we'll compare as strings
    """
    for f in (ticket.get("custom_fields") or []):
        if str(f.get("id")) == str(field_id):
            v = f.get("value")
            return "" if v is None else str(v).strip()
    return ""

# =========================
# HEURISTICS
# =========================
ORDER_PAT = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
COLOR_WORDS = {"gold", "golden", "silver", "argent", "oro", "argento"}
CHAIN_WORDS = {"chain", "strap", "shoulder strap", "tracolla", "catena", "belt"}
SHORT_WORDS = {"short", "too short", "più corta", "shorter"}
LONG_WORDS  = {"long", "too long", "più lunga", "longer"}

SIZE_WORDS  = {
    "mini","small","medium","large","xl","vanity","pm","mm","gm",
    "bb","nano","micro"
}

def contains_any(text: str, vocab: set) -> bool:
    t = text.lower()
    return any(w in t for w in vocab)

def extract_order_number(text: str) -> str | None:
    if not text:
        return None
    m = ORDER_PAT.search(text.replace("\n", " "))
    return m.group(0) if m else None

def detect_chain_case(text: str) -> Dict[str, Any] | None:
    """
    Returns dict with keys:
      {"type": "length", "which": "longer"/"shorter"} or
      {"type": "color", "color": "gold"/"silver"}
    or None if not a chain case.
    """
    t = text.lower()
    if contains_any(t, CHAIN_WORDS):
        # length
        if contains_any(t, LONG_WORDS):
            return {"type": "length", "which": "longer"}
        if contains_any(t, SHORT_WORDS):
            return {"type": "length", "which": "shorter"}
        # color
        if "gold" in t or "oro" in t:
            return {"type": "color", "color": "gold"}
        if "silver" in t or "argento" in t or "argent" in t:
            return {"type": "color", "color": "silver"}
    return None

def is_explicit_request(text: str) -> bool:
    """
    Very simple heuristic: if it looks like 'please send/replace' + size/color keywords.
    """
    if not text:
        return False
    t = text.lower()
    triggers = ["i want", "please send", "replace", "i would rather have", "instead", "can you send", "please ship"]
    if any(k in t for k in triggers):
        if contains_any(t, SIZE_WORDS) or contains_any(t, COLOR_WORDS) or contains_any(t, CHAIN_WORDS):
            return True
    return False

# =========================
# OPENAI
# =========================
client = OpenAI(api_key=OPENAI_APIKEY)

def ask_openai(prompt_messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
    r = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=temperature,
        messages=prompt_messages
    )
    return r.choices[0].message.content.strip()

# =========================
# MESSAGE BUILDERS
# =========================
def greeting(first_name: str) -> str:
    name = first_name or "there"
    return f"Hi {name},"

NO_RETURN_SENTENCE = (
    "Please don’t worry about the current insert—you don’t need to return it. "
    "We’ll take care of everything so you can simply enjoy your upgraded organizer."
)

def build_chain_reply(case: Dict[str, Any], first_name: str) -> str:
    g = greeting(first_name)
    if case["type"] == "length":
        which = "longer" if case["which"] == "longer" else "shorter"
        body = (
            f"{g}\n\nThanks for letting us know! We’ll send a {which} chain right away so you can get the perfect length. "
            f"{NO_RETURN_SENTENCE}\n\nBest regards,\n{SIGNATURE_NAME}"
        )
    else:
        color = "gold" if case["color"] == "gold" else "silver"
        body = (
            f"{g}\n\nThanks for flagging the color. We’ll send a {color} chain as a replacement right away. "
            f"{NO_RETURN_SENTENCE}\n\nBest regards,\n{SIGNATURE_NAME}"
        )
    return body

def build_accept_replacement(first_name: str, summary_keywords: str = "") -> str:
    g = greeting(first_name)
    tail = f" ({summary_keywords})" if summary_keywords else ""
    return (
        f"{g}\n\nThanks for the details — we’ll arrange a replacement right away{tail}. "
        f"{NO_RETURN_SENTENCE}\nWe’ll share the tracking as soon as it’s available.\n\nBest regards,\n{SIGNATURE_NAME}"
    )

def build_need_info(first_name: str, ask_order: bool, ask_model_link: bool) -> str:
    g = greeting(first_name)
    asks = []
    if ask_model_link:
        asks.append("the exact model name of your bag or a direct link to the one you own")
    if ask_order:
        asks.append("your Amazon order number")
    ask_line = " and ".join(asks)
    return (
        f"{g}\n\nThanks for your message! To make sure the fit is perfect, could you please share "
        f"{ask_line}?\n\n{NO_RETURN_SENTENCE}\n\nBest regards,\n{SIGNATURE_NAME}"
    )

def build_confirmation_after_info(first_name: str, keywords: str) -> str:
    g = greeting(first_name)
    return (
        f"{g}\n\nThanks for the info! We’ll send your replacement according to your instructions: {keywords}. "
        f"We’ll share the tracking shortly.\n\nBest regards,\n{SIGNATURE_NAME}"
    )

def build_public_tracking_message(tracking: str) -> str:
    return (
        "Hi again!\n\n"
        f"Here is the tracking number for your replacement: {tracking}\n"
        "You can follow the updates of your package by clicking on the link below:\n"
        f"https://t.17track.net/en#nums={tracking}\n\n"
        "Feel free to reach out if you have any questions or concerns along the way. "
        "Wishing you a smooth delivery experience!\n\n"
        f"Warm regards,\n{SIGNATURE_NAME}"
    )

def summarize_keywords(text: str) -> str:
    """
    Quick heuristic to pull 2–3 short keywords to echo back.
    """
    t = text.lower()
    picks = []
    # prefer size
    for w in SIZE_WORDS:
        if w in t: picks.append(w)
    # color
    for w in ("dark brown","brown","black","white","gold","silver","beige","sienna","red","blue","navy","tan","camel","cream","ivory","pink","green","grey","gray","chocolate"):
        if w in t: picks.append(w)
    # chain specifics
    if contains_any(t, CHAIN_WORDS): picks.append("chain")
    if contains_any(t, LONG_WORDS): picks.append("longer")
    if contains_any(t, SHORT_WORDS): picks.append("shorter")
    # keep 3 unique
    seen, out = set(), []
    for k in picks:
        if k not in seen:
            out.append(k)
            seen.add(k)
        if len(out) >= 3: break
    return ", ".join(out)

# =========================
# CORE LOGIC
# =========================
def last_is_end_user_public(ticket: Dict[str,Any], comments: List[Dict[str,Any]]) -> bool:
    if not comments:
        return False
    last = comments[-1]
    return last.get("public") and last.get("author_id") == ticket.get("requester_id")

def compose_private_note(ticket: Dict[str, Any], comments: List[Dict[str, Any]]) -> str | None:
    """
    Build the internal note (first or second response).
    - Chain/strap cases -> accept replacement immediately.
    - If explicit request -> accept replacement.
    - Else ask minimal info (model/link and/or order).
    """
    last = comments[-1]
    text = (last.get("body") or "").strip()
    requester_id = ticket.get("requester_id")
    first_name = get_user_first_name(requester_id)

    # Special chain/strap handling
    chain_case = detect_chain_case(text)
    if chain_case:
        return build_chain_reply(chain_case, first_name)

    # Decide explicit / info ask
    explicit = is_explicit_request(text)

    # Determine whether we have an order number already in the thread
    whole_thread_text = " ".join([c.get("body","") or "" for c in comments] + [ticket.get("subject","") or ""])
    order_present = bool(extract_order_number(whole_thread_text))

    # If explicit request, accept and echo keywords
    if explicit:
        kw = summarize_keywords(text)
        return build_accept_replacement(first_name, kw)

    # Otherwise, ask only what we still need
    ask_order = not order_present
    ask_model_link = True  # safe minimal ask to ensure perfect sizing
    return build_need_info(first_name, ask_order=ask_order, ask_model_link=ask_model_link)

def maybe_post_public_tracking(ticket: Dict[str, Any]) -> bool:
    """
    If the tracking custom field has a value and we haven't sent it yet,
    send a **public reply** with the tracking and add the tracking_sent tag.
    Returns True if we posted; False otherwise.
    """
    ticket_id = ticket["id"]
    tags = ticket.get("tags") or []
    if TRACKING_SENT_TAG in tags:
        return False

    tracking = ""
    try:
        tracking = get_custom_field_value(ticket, Z_TRACKING_FIELD)
    except Exception:
        tracking = ""

    if not tracking:
        return False

    # Build public body
    body = build_public_tracking_message(tracking)
    try:
        add_public_reply_and_tag(ticket_id, body, TRACKING_SENT_TAG)
        print(f"[TRACKING] Sent to ticket {ticket_id}: {tracking}")
        return True
    except Exception as e:
        print(f"[ERROR tracking] ticket {ticket_id}: {e}")
        return False

def process_once():
    # 1) Look for tracking opportunities first (always safe)
    for t in list_recent_tickets():
        try:
            _ = maybe_post_public_tracking(get_ticket(t["id"]))
        except Exception as e:
            print(f"[ERROR tracking scan] {t.get('id')}: {e}")

    # 2) Create private note drafts where customer just wrote publicly
    for t in list_recent_tickets():
        try:
            if t.get("status") in ("solved", "closed"):
                continue
            if DRAFT_TAG in (t.get("tags") or []):
                continue

            comments = get_ticket_comments(t["id"])
            if not comments or not last_is_end_user_public(t, comments):
                continue

            body = compose_private_note(t, comments)
            if not body:
                continue

            # All bot-generated non-tracking replies are private notes
            add_private_note_and_tag(t["id"], body, DRAFT_TAG)
            print(f"[OK] Draft (private) created for ticket {t['id']}")
        except Exception as e:
            print(f"[ERROR draft] {t.get('id')}: {e}")

# =========================
# STARTUP VALIDATION
# =========================
def validate_env():
    missing = [k for k,v in {
        "ZENDESK_SUBDOMAIN": Z_SUBDOMAIN,
        "ZENDESK_EMAIL": Z_EMAIL,
        "ZENDESK_API_TOKEN": Z_API_TOKEN,
        "OPENAI_API_KEY": OPENAI_APIKEY,
        "Z_TRACKING_FIELD": Z_TRACKING_FIELD
    }.items() if not v]
    if missing:
        raise SystemExit("Missing env vars: " + ", ".join(missing))

# =========================
# FLASK + BACKGROUND LOOP
# =========================
app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

def background_loop():
    validate_env()
    print(f"{BRAND_NAME} — Draft Assistant running as {SIGNATURE_NAME} (poll {POLL_INTERVAL}s)")
    while True:
        try:
            process_once()
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
        time.sleep(POLL_INTERVAL)

def main():
    # run polling in background thread so Flask can bind a port for Render
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    # Flask app (Render will bind $PORT automatically)
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    main()
