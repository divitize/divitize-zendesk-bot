# bot_zendesk.py — Divitize Zendesk Assistant (full)
import os, re, time, threading, requests
from typing import List, Dict, Any, Optional
from flask import Flask, jsonify
from openai import OpenAI

# ============ ENV ============
Z_SUBDOMAIN   = os.getenv("ZENDESK_SUBDOMAIN", "").strip()
Z_EMAIL       = os.getenv("ZENDESK_EMAIL", "").strip()
Z_API_TOKEN   = os.getenv("ZENDESK_API_TOKEN", "").strip()

OPENAI_APIKEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL  = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

BRAND_NAME     = os.getenv("BRAND_NAME", "Divitize").strip()
SIGNATURE_NAME = os.getenv("SIGNATURE_NAME", "Noe").strip()

POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL_SEC", "35"))
DRAFT_TAG      = os.getenv("DRAFT_TAG", "chat_suggested_draft").strip()

# Custom field id for tracking (string ok)
Z_TRACKING_FIELD = os.getenv("Z_TRACKING_FIELD", "").strip()   # es: "29120306162322"

# Tags
TAG_REPLACEMENT_SENT = "replacement_sent"  # usato anche dalla tua automazione 21/28gg

# ============ MESSAGGI FISSI ============
NO_RETURN_SENTENCE = (
    "Please don’t worry about the current insert—you don’t need to return it. "
    "We’ll take care of everything so you can simply enjoy your upgraded organizer."
)

# ============ ZENDESK REST ============
Z_BASE = f"https://{Z_SUBDOMAIN}.zendesk.com/api/v2"
AUTH   = (f"{Z_EMAIL}/token", Z_API_TOKEN)

def z_get(path: str, params=None) -> Dict[str, Any]:
    r = requests.get(f"{Z_BASE}{path}", params=params or {}, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()

def z_put(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.put(f"{Z_BASE}{path}", json=payload, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()

def list_recent_tickets(limit=60) -> List[Dict[str, Any]]:
    return z_get("/tickets.json", {"sort_by":"updated_at","sort_order":"desc","per_page":limit}).get("tickets", [])

def fetch_ticket(ticket_id: int) -> Dict[str, Any]:
    return z_get(f"/tickets/{ticket_id}.json").get("ticket", {})

def get_ticket_comments(ticket_id: int) -> List[Dict[str, Any]]:
    return z_get(f"/tickets/{ticket_id}/comments.json").get("comments", [])

def add_internal_note_and_tags(ticket_id: int, note: str, tags: List[str]):
    payload = {"ticket": {"comment": {"public": False, "body": note}, "additional_tags": tags}}
    z_put(f"/tickets/{ticket_id}.json", payload)

def add_public_reply_and_tags(ticket_id: int, body: str, tags: List[str], set_status: Optional[str] = None):
    t: Dict[str, Any] = {"comment": {"public": True, "body": body}, "additional_tags": tags}
    if set_status:
        t["status"] = set_status
    z_put(f"/tickets/{ticket_id}.json", {"ticket": t})

def get_user_first_name(user_id: int) -> str:
    try:
        u = z_get(f"/users/{user_id}.json").get("user", {})
        name = (u.get("name") or "").strip()
        return name.split()[0] if name else "there"
    except Exception:
        return "there"

def get_custom_field_value(ticket: Dict[str, Any], field_id: str) -> Optional[str]:
    for f in (ticket.get("custom_fields") or []):
        if str(f.get("id")) == str(field_id):
            v = f.get("value")
            return str(v).strip() if v else None
    return None

# ============ HEURISTICHE ============
ORDER_PAT = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
URL_PAT = re.compile(r'https?://[^\s)>\]]+', re.I)
MEASURES_PAT = re.compile(
    r'\b(\d{1,2}(\.\d{1,2})?)\s*[x×]\s*(\d{1,2}(\.\d{1,2})?)\s*[x×]\s*(\d{1,2}(\.\d{1,2})?)\b',
    re.I
)

COLOR_WORDS = {"black","white","brown","dark brown","beige","sienna","red","blue","navy","tan","camel",
               "cream","ivory","pink","green","grey","gray","chocolate","gold","silver"}
CHAIN_WORDS = {"chain","strap","shoulder strap","tracolla","catena","belt"}
SHORT_WORDS = {"short","too short","shorter","più corta","piu corta"}
LONG_WORDS  = {"long","too long","longer","più lunga","piu lunga"}
SIZE_WORDS  = {"mini","small","medium","large","xl","vanity","pm","mm","gm","bb","nano","micro"}

def contains_any(text: str, vocab: set) -> bool:
    t = (text or "").lower()
    return any(w in t for w in vocab)

def extract_order_number(text: str) -> Optional[str]:
    if not text: return None
    m = ORDER_PAT.search(text.replace("\n"," "))
    return m.group(0) if m else None

def has_link(text: str) -> bool:
    return bool(URL_PAT.search(text or ""))

def has_measurements(text: str) -> bool:
    return bool(MEASURES_PAT.search((text or "").replace("inches","").replace("inch","")))

def thread_has_any_photo(comments: List[Dict[str, Any]]) -> bool:
    for c in comments:
        for a in (c.get("attachments") or []):
            if (a.get("content_type","").startswith(("image/","application/pdf"))):
                return True
    return False

def detect_chain_case(text: str) -> Optional[Dict[str, Any]]:
    t = (text or "").lower()
    if not contains_any(t, CHAIN_WORDS):
        return None
    if contains_any(t, LONG_WORDS):
        return {"type":"length","which":"longer"}
    if contains_any(t, SHORT_WORDS):
        return {"type":"length","which":"shorter"}
    if "gold" in t or "oro" in t:
        return {"type":"color","color":"gold"}
    if "silver" in t or "argento" in t:
        return {"type":"color","color":"silver"}
    return {"type":"generic"}

def is_explicit_request(text: str) -> bool:
    if not text: return False
    t = text.lower()
    triggers = ["i want","please send","replace","i would rather have","instead","can you send","please ship"]
    return any(k in t for k in triggers) and (
        contains_any(t, SIZE_WORDS) or contains_any(t, COLOR_WORDS) or contains_any(t, CHAIN_WORDS)
    )

def last_is_end_user_public(ticket: Dict[str,Any], comments: List[Dict[str,Any]]) -> bool:
    if not comments: return False
    last = comments[-1]
    return last.get("public") and last.get("author_id") == ticket.get("requester_id")

def user_wrote_after_last_internal(comments: List[Dict[str,Any]], requester_id: int) -> bool:
    """True se l'ultimo messaggio del cliente (pubblico) è successivo all'ultima nota interna."""
    last_user_idx = -1
    last_internal_idx = -1
    for i, c in enumerate(comments):
        if c.get("public") and c.get("author_id") == requester_id:
            last_user_idx = i
        if not c.get("public"):
            last_internal_idx = i
    return last_user_idx > last_internal_idx

# ============ OPENAI ============
client = OpenAI(api_key=OPENAI_APIKEY)

SYSTEM_RULES = f"""
You are {SIGNATURE_NAME}, the customer service agent for {BRAND_NAME}.
Tone: warm, polite, professional, crystal-clear. Always sign as '{SIGNATURE_NAME}'.
Write in the customer’s language if obvious; otherwise use English.

Rules:
- Always start with "Hi <first_name>," (use provided name or 'there').
- Ask for the Amazon order number ONLY if it is NOT present in the thread.
- If photos are already attached, do NOT ask again; thank them for the picture.
- Size issues are unusual: show empathy and say it's unusual; request only what’s needed (bag model/link; photo only if not provided).
- Material: if felt too stiff → offer nylon/silk. If nylon/silk too soft → offer felt. Never propose the same material again.
- Color:
  * If customer clearly prefers another color, do NOT ask for photos. Confirm replacement and mention tracking. Echo color/bag if known.
  * If it’s a shade/match question, ask for a picture of the organizer inside the bag to pick a better shade.
- If request is explicit (specific color/size), be brief and direct (no long preamble). Include reassurance about no return and mention tracking.
- Otherwise, ask ONLY minimal missing info:
  - "Could you please confirm the exact model name of your bag, or share a direct link to the one you own?"
  - If missing: "May I kindly ask you to provide your Amazon order number so I can quickly locate your purchase?"
- Include this reassurance when appropriate: "{NO_RETURN_SENTENCE}"
- Never overpromise; say you'll share the tracking as soon as available.
- Sign as '{SIGNATURE_NAME}'.
"""

def summarize_keywords(text: str) -> str:
    t = (text or "").lower()
    picks = []
    for w in SIZE_WORDS:
        if w in t: picks.append(w)
    for w in ("dark brown","brown","black","white","gold","silver","beige","sienna","red","blue","navy","tan","camel","cream","ivory","pink","green","grey","gray","chocolate"):
        if w in t: picks.append(w)
    if contains_any(t, CHAIN_WORDS): picks.append("chain")
    if contains_any(t, LONG_WORDS): picks.append("longer")
    if contains_any(t, SHORT_WORDS): picks.append("shorter")
    seen, out = set(), []
    for k in picks:
        if k not in seen:
            out.append(k)
            seen.add(k)
        if len(out) >= 3: break
    return ", ".join(out)

def build_chain_reply(case: Dict[str, Any], first_name: str) -> str:
    g = f"Hi {first_name},"
    if case.get("type") == "length":
        which = "longer" if case.get("which") == "longer" else "shorter"
        return (
            f"{g}\n\nThanks for letting us know! We’ll send a {which} chain right away so you can get the perfect length. "
            f"{NO_RETURN_SENTENCE}\nWe’ll share the tracking as soon as it’s available.\n\nBest regards,\n{SIGNATURE_NAME}"
        )
    if case.get("type") == "color":
        color = "gold" if case.get("color") == "gold" else "silver"
        return (
            f"{g}\n\nThanks for flagging the color. We’ll send a {color} chain as a replacement right away. "
            f"{NO_RETURN_SENTENCE}\nWe’ll share the tracking as soon as it’s available.\n\nBest regards,\n{SIGNATURE_NAME}"
        )
    return (
        f"{g}\n\nThanks for the details about the chain. We’ll arrange a replacement accordingly. "
        f"{NO_RETURN_SENTENCE}\nWe’ll share the tracking as soon as it’s available.\n\nBest regards,\n{SIGNATURE_NAME}"
    )

def compose_draft(ticket: Dict[str,Any], comments: List[Dict[str,Any]]) -> str:
    last = comments[-1]
    text = (last.get("body") or "").strip()
    requester_id = ticket.get("requester_id")
    first_name = get_user_first_name(requester_id)

    # catena/chain: replacement diretto
    chain_case = detect_chain_case(text)
    if chain_case:
        return "[Suggested reply by ChatGPT — please review and send]\n\n" + build_chain_reply(chain_case, first_name)

    # info già sufficienti? (link oppure misure + foto in thread)
    thread_text = " ".join([c.get("body","") or "" for c in comments] + [ticket.get("subject","") or ""])
    info_sufficient = has_link(thread_text) or (thread_has_any_photo(comments) and has_measurements(thread_text))

    order_present = bool(extract_order_number(thread_text))
    photo_present = message_has_photo(last)
    explicit = is_explicit_request(text)

    if info_sufficient or explicit:
        kw = summarize_keywords(text) if explicit else ""
        g = f"Hi {first_name},"
        tail = f" ({kw})" if kw else ""
        msg = (
            f"{g}\n\nThanks for the details — we’ll arrange a replacement right away{tail}. "
            f"{NO_RETURN_SENTENCE}\nWe’ll share the tracking as soon as it’s available.\n\nBest regards,\n{SIGNATURE_NAME}"
        )
        return "[Suggested reply by ChatGPT — please review and send]\n\n" + msg

    # altrimenti, chiedi solo il minimo
    ask_bits = []
    if not order_present:
        ask_bits.append("your Amazon order number")
    # sempre utile avere conferma modello/link per fit perfetto se non sufficiente
    ask_bits.append("the exact model name of your bag or a direct link to the one you own")
    ask_line = " and ".join(ask_bits)

    g = f"Hi {first_name},"
    ask_msg = (
        f"{g}\n\nThanks for your message! To make sure the fit is perfect, could you please share {ask_line}? "
        f"{'Thanks for the picture — that’s very helpful. ' if photo_present else ''}"
        f"{NO_RETURN_SENTENCE}\n\nBest regards,\n{SIGNATURE_NAME}"
    )
    return "[Suggested reply by ChatGPT — please review and send]\n\n" + ask_msg

# ============ ANTI-SPAM TRACKING ============
def normalize_tag(s: str) -> str:
    return re.sub(r'[^a-z0-9_]', '', (s or '').lower())

def tracking_guard_tag(tracking: str) -> str:
    return f"tracking_sent_{normalize_tag(tracking)}"

def last_public_comment_contains_tracking(comments: List[Dict[str,Any]], tracking: str) -> bool:
    if not comments: return False
    key = normalize_tag(tracking)
    for c in reversed(comments):
        if c.get("public"):
            body = (c.get("body") or "")
            return key in normalize_tag(body)
    return False

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

def handle_tracking_if_any(ticket: Dict[str,Any]) -> bool:
    """
    Se c'è tracking:
      - Se l'ultimo pubblico contiene già il tracking → retro-tag (replacement_sent + guard) e STOP.
      - Se c'è già il guard tag → STOP.
      - Altrimenti invia messaggio pubblico + status Solved + aggiunge (replacement_sent + guard).
    Ritorna True se ha fatto qualcosa (pubblicato o retro-taggato).
    """
    ticket_id = ticket["id"]
    tags = ticket.get("tags") or []
    tracking = get_custom_field_value(ticket, Z_TRACKING_FIELD) if Z_TRACKING_FIELD else None
    if not tracking:
        return False

    guard = tracking_guard_tag(tracking)

    # Se già presente il messaggio (inviato manualmente o prima): retro-tag e fine
    comments = get_ticket_comments(ticket_id)
    if last_public_comment_contains_tracking(comments, tracking):
        if guard not in tags or TAG_REPLACEMENT_SENT not in tags:
            try:
                z_put(f"/tickets/{ticket_id}.json", {"ticket": {"additional_tags": list({TAG_REPLACEMENT_SENT, guard})}})
                print(f"[OK] Retro-tag guard added on ticket {ticket_id}")
            except Exception as e:
                print(f"[WARN] retro-tag {ticket_id}: {e}")
        return True

    # Se già taggato per questo tracking → non spammare
    if guard in tags:
        return False

    # Invia una sola volta
    body = build_public_tracking_message(tracking)
    add_public_reply_and_tags(ticket_id, body, [TAG_REPLACEMENT_SENT, guard], set_status="solved")
    print(f"[OK] Tracking published + solved for ticket {ticket_id}")
    return True

# ============ CICLO PRINCIPALE ============
def process_once():
    # 1) Tracking prima di tutto (idempotente)
    for t in list_recent_tickets():
        if t.get("status") == "closed":
            continue
        try:
            full = fetch_ticket(t["id"])
            _ = handle_tracking_if_any(full)
        except Exception as e:
            print(f"[ERROR tracking] {t.get('id')}: {e}")

    # 2) Bozze interne (risposte) solo quando serve
    for t in list_recent_tickets():
        ticket_id = t["id"]
        status = t.get("status")
        if status in ("solved","closed"):
            continue

        try:
            comments = get_ticket_comments(ticket_id)
        except Exception as e:
            print(f"[ERROR comments] {ticket_id}: {e}")
            continue
        if not comments:
            continue

        # ultimo è pubblico del cliente?
        if not last_is_end_user_public(t, comments):
            continue

        # se c'è già un draft tag e l'utente NON ha scritto dopo la tua ultima nota → skip
        tags = (t.get("tags") or [])
        if DRAFT_TAG in tags and not user_wrote_after_last_internal(comments, t.get("requester_id")):
            continue

        try:
            full = fetch_ticket(ticket_id)
            draft = compose_draft(full, comments)
            add_internal_note_and_tags(ticket_id, draft, [DRAFT_TAG])
            print(f"[OK] Draft created for ticket {ticket_id}")
        except Exception as e:
            print(f"[ERROR draft] {ticket_id}: {e}")

def validate_env():
    missing = [k for k,v in {
        "ZENDESK_SUBDOMAIN":Z_SUBDOMAIN,
        "ZENDESK_EMAIL":Z_EMAIL,
        "ZENDESK_API_TOKEN":Z_API_TOKEN,
        "OPENAI_API_KEY":OPENAI_APIKEY,
        "Z_TRACKING_FIELD":Z_TRACKING_FIELD
    }.items() if not v]
    if missing:
        raise SystemExit("Missing env vars: " + ", ".join(missing))

# ============ Flask keep-alive (Render) ============
app = Flask(__name__)

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})

def background_loop():
    validate_env()
    print(f"{BRAND_NAME} — Zendesk Assistant running as {SIGNATURE_NAME} (poll {POLL_INTERVAL}s)")
    while True:
        try:
            process_once()
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
