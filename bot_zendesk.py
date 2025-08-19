import os, re, time, threading, requests
from typing import List, Dict, Any
from flask import Flask, jsonify
from openai import OpenAI

# ====== ENV ======
Z_SUBDOMAIN   = os.getenv("ZENDESK_SUBDOMAIN")         # es: divitize
Z_EMAIL       = os.getenv("ZENDESK_EMAIL")             # es: divitize.info@gmail.com
Z_API_TOKEN   = os.getenv("ZENDESK_API_TOKEN")         # token Zendesk
OPENAI_APIKEY = os.getenv("OPENAI_API_KEY")            # sk-...
OPENAI_MODEL  = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

BRAND_NAME     = os.getenv("BRAND_NAME", "Divitize")
SIGNATURE_NAME = os.getenv("SIGNATURE_NAME", "Noe")
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL_SEC", "35"))
DRAFT_TAG      = os.getenv("DRAFT_TAG", "chat_suggested_draft")

RETURN_SENTENCE = os.getenv(
    "RETURN_SENTENCE",
    "Please don’t worry about the current insert—you don’t need to return it. We’ll take care of everything so you can simply enjoy your upgraded organizer."
)

# ====== ZENDESK REST ======
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

def list_recent_tickets(limit=40):
    # Gli updated più recenti per essere “attenti” ai nuovi messaggi
    return z_get("/tickets.json", {
        "sort_by": "updated_at",
        "sort_order": "desc",
        "per_page": limit
    }).get("tickets", [])

def get_ticket_comments(ticket_id: int):
    return z_get(f"/tickets/{ticket_id}/comments.json").get("comments", [])

def add_internal_note_and_tag(ticket_id: int, note: str, tag: str):
    payload = {"ticket": {"comment": {"public": False, "body": note}, "additional_tags": [tag]}}
    z_put(f"/tickets/{ticket_id}.json", payload)

# ====== HEURISTICS ======
ORDER_PAT = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")

def extract_order_number(text: str) -> str | None:
    if not text: return None
    m = ORDER_PAT.search(text.replace("\n"," "))
    return m.group(0) if m else None

def last_is_end_user_public(ticket: Dict[str,Any], comments: List[Dict[str,Any]]) -> bool:
    if not comments: return False
    last = comments[-1]
    return last.get("public") and last.get("author_id") == ticket.get("requester_id")

def message_has_photo(comment: Dict[str,Any]) -> bool:
    return any((a.get("content_type","").startswith(("image/","application/pdf"))) for a in (comment.get("attachments") or []))

def is_request_explicit(text: str) -> bool:
    if not text: return False
    t = text.lower()
    triggers = ["i want","please send","replace with","i would rather have","instead"]
    colors   = ["black","white","brown","dark brown","beige","sienna","red","blue","navy","tan","camel","cream","ivory","pink","green","grey","gray","chocolate"]
    sizes    = ["mini","small","medium","large","xl","vanity","pm","mm","gm","bb","nano","micro"]
    return (any(k in t for k in triggers) and (any(c in t for c in colors) or any(s in t for s in sizes)))

# ====== PROMPT ======
SYSTEM_RULES = """
You are {sig}, the customer service agent for {brand}.
Tone: warm, polite, professional, crystal-clear. Always sign as '{sig}'.
Write in the customer’s language if obvious; otherwise use English.

Rules:
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
- Include this reassurance when appropriate: "{no_return}"
- Never overpromise; say you'll share the tracking as soon as available.
- Sign as '{sig}'.
""".format(sig=SIGNATURE_NAME, brand=BRAND_NAME, no_return=RETURN_SENTENCE)

def classify_intent(client: OpenAI, model: str, text: str) -> str:
    try:
        r = client.chat.completions.create(
            model=model, temperature=0,
            messages=[
                {"role":"system","content":"Classify to one label only."},
                {"role":"user","content":
                 "Labels: Size issue | Color preference | Color shade/match | Material issue | Generic/Other.\n"
                 "Message:\n"+(text or "")}
            ]
        )
        return r.choices[0].message.content.strip()
    except Exception:
        return "Generic/Other"

def compose_draft(client: OpenAI, model: str, ticket: Dict[str,Any], comments: List[Dict[str,Any]]) -> str:
    last = comments[-1]
    text = (last.get("body") or "").strip()
    thread = " ".join([c.get("body","") for c in comments] + [ticket.get("subject","")])
    order_present = bool(extract_order_number(thread))
    photo_present = message_has_photo(last)
    explicit      = is_request_explicit(text)
    intent        = classify_intent(client, model, text)

    context = {
        "subject": ticket.get("subject") or "(no subject)",
        "customer_last_message": text,
        "intent": intent,
        "explicit_request": explicit,
        "order_number_present": order_present,
        "photo_present_in_last_message": photo_present
    }

    r = client.chat.completions.create(
        model=model, temperature=0.2,
        messages=[
            {"role":"system","content": SYSTEM_RULES},
            {"role":"user","content":
             "Write the final reply for the customer now. "
             "If explicit_request=True keep it short (confirm + reassurance + tracking). "
             "Else ask only minimal missing info. "
             f"Context:\n{context}\nSign as {SIGNATURE_NAME}."}
        ]
    )
    msg = r.choices[0].message.content.strip()
    if SIGNATURE_NAME not in msg:
        msg += f"\n\nBest regards,\n{SIGNATURE_NAME}"
    return "[Suggested reply by ChatGPT — please review and send]\n\n" + msg

# ====== SKIP LOGIC MIGLIORATA ======
def last_internal_after_last_user(comments: List[Dict[str, Any]], requester_id: int) -> bool:
    last_user_idx = -1
    last_internal_idx = -1
    for i, c in enumerate(comments):
        if c.get("public") and c.get("author_id") == requester_id:
            last_user_idx = i
        if not c.get("public"):
            last_internal_idx = i
    return last_internal_idx > last_user_idx

def why_skip(ticket: Dict[str, Any], comments: List[Dict[str, Any]], draft_tag: str) -> str | None:
    if ticket.get("status") in ("solved", "closed"):
        return "status solved/closed"
    if not comments:
        return "no comments"
    last = comments[-1]
    if not (last.get("public") and last.get("author_id") == ticket.get("requester_id")):
        return "last is not end-user public"
    # se c'è il tag MA il cliente ha scritto dopo l'ultima nota interna => NON skip
    has_tag = draft_tag in (ticket.get("tags") or [])
    if has_tag and not last_internal_after_last_user(comments, ticket.get("requester_id")):
        return f"tag '{draft_tag}' still present and last internal after last user"
    return None

# ====== POLLING LOOP ======
client = OpenAI(api_key=OPENAI_APIKEY)

def process_once():
    for t in list_recent_tickets():
        try:
            comments = get_ticket_comments(t["id"])
        except Exception as e:
            print(f"[ERROR] get_comments {t.get('id')}: {e}")
            continue

        skip_reason = why_skip(t, comments, DRAFT_TAG)
        if skip_reason:
            print(f"[SKIP] Ticket {t['id']}: {skip_reason}")
            continue

        try:
            draft = compose_draft(client, OPENAI_MODEL, t, comments)
            add_internal_note_and_tag(t["id"], draft, DRAFT_TAG)
            print(f"[OK] Draft created for ticket {t['id']}")
        except Exception as e:
            print(f"[ERROR] {t['id']}: {e}")

def poll_forever():
    print(f"[BG] polling thread started (interval {POLL_INTERVAL}s)")
    while True:
        try:
            print("[BG] tick")
            process_once()
        except Exception as e:
            print(f"[BG][ERROR] {e}")
        time.sleep(POLL_INTERVAL)

# ====== FLASK (health) + AVVIO THREAD ======
app = Flask(__name__)

@app.route("/healthz")
def healthz():
    return jsonify(ok=True, service="divitize-zendesk-bot")

# Avvia il thread al boot del processo
thread_started = False

def start_background_once():
    global thread_started
    if not thread_started:
        th = threading.Thread(target=poll_forever, daemon=True)
        th.start()
        thread_started = True

# Render/Flask entrypoint
@app.before_first_request
def _before_first_request():
    start_background_once()

# In alcuni ambienti è utile far partire subito
start_background_once()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    print(f"{BRAND_NAME} — Draft Assistant running as {SIGNATURE_NAME} (poll {POLL_INTERVAL}s)")
    app.run(host="0.0.0.0", port=port, debug=False)
