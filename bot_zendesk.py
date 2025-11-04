# bot_zendesk.py — Divitize Zendesk Assistant (full)

import os
import re
import time
import threading
import requests
from typing import List, Dict, Any, Optional
from flask import Flask, jsonify
from openai import OpenAI

# ============ ENV ============
Z_SUBDOMAIN   = os.getenv("ZENDESK_SUBDOMAIN", "").strip()
Z_EMAIL       = os.getenv("ZENDESK_EMAIL", "").strip()
Z_API_TOKEN   = os.getenv("ZENDESK_API_TOKEN", "").strip()

OPENAI_APIKEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL  = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()  # tenuto per eventuali futuri usi

BRAND_NAME     = os.getenv("BRAND_NAME", "Divitize").strip()
SIGNATURE_NAME = os.getenv("SIGNATURE_NAME", "Noe").strip()

POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL_SEC", "35"))
DRAFT_TAG      = os.getenv("DRAFT_TAG", "chat_suggested_draft").strip()

# Custom field id per tracking (string ok; es: "29120306162322")
Z_TRACKING_FIELD = os.getenv("Z_TRACKING_FIELD", "").strip()

# Tags fissi
TAG_REPLACEMENT_SENT = "replacement_sent"                 # usato per automazione review
TRACKING_SENT_PREFIX = "tracking_sent_"                   # guard anti-spam per singolo tracking

# Origine — token subject Amazon-QR (deterministico)
AMAZON_SUBJECT_TOKENS = [
    s.strip().lower() for s in os.getenv(
        "AMAZON_SUBJECT_TOKENS",
        "free replacement for my amazon purchase"
    ).split(",") if s.strip()
]

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

# ====== Tags utilities ======
def get_ticket_tags(ticket_id: int) -> List[str]:
    try:
        t = z_get(f"/tickets/{ticket_id}.json").get("ticket", {})
        return list(t.get("tags") or [])
    except Exception:
        return []

def ensure_tags(ticket_id: int, new_tags: List[str]):
    """Unisce e forza i tag nel campo 'tags' del ticket (sidebar/automazioni affidabili)."""
    try:
        existing = get_ticket_tags(ticket_id)
        merged = sorted(set((existing or []) + (new_tags or [])))
        z_put(f"/tickets/{ticket_id}.json", {"ticket": {"tags": merged}})
    except Exception as e:
        print(f"[WARN] ensure_tags failed on {ticket_id}: {e}")

def remove_tags(ticket_id: int, tags_to_remove: List[str]):
    """Rimuove in modo esplicito alcuni tag dal campo 'tags'."""
    if not tags_to_remove:
        return
    try:
        existing = get_ticket_tags(ticket_id)
        target = sorted([t for t in (existing or []) if t not in set(tags_to_remove)])
        z_put(f"/tickets/{ticket_id}.json", {"ticket": {"tags": target}})
    except Exception as e:
        print(f"[WARN] remove_tags failed on {ticket_id}: {e}")

# ====== Helper nome utente ======
def get_user_first_name(user_id: int) -> str:
    try:
        u = z_get(f"/users/{user_id}.json").get("user", {})
        name = (u.get("name") or "").strip()
        return name.split()[0] if name else "there"
    except Exception:
        return "there"

# ====== Campi custom ======
def get_custom_field_value(ticket: Dict[str, Any], field_id: str) -> Optional[str]:
    for f in (ticket.get("custom_fields") or []):
        if str(f.get("id")) == str(field_id):
            v = f.get("value")
            return str(v).strip() if v else None
    return None

# ============ HEURISTICHE DI TESTO ============
ORDER_PAT = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")  # Amazon order
URL_PAT = re.compile(r'https?://[^\s)>\]]+', re.I)
MEASURES_PAT = re.compile(
    r'\b(\d{1,2}(\.\d{1,2})?)\s*[x×]\s*(\d{1,2}(\.\d{1,2})?)\s*[x×]\s*(\d{1,2}(\.\d{1,2})?)\b',
    re.I
)
SHOPIFY_CONTACT_PHRASE = "you received a new message from your online store's contact form"

COLOR_WORDS = {"black","white","brown","dark brown","beige","sienna","red","blue","navy","tan","camel",
               "cream","ivory","pink","green","grey","gray","chocolate","gold","silver"}
CHAIN_WORDS = {"chain","strap","shoulder strap","tracolla","catena","belt"}
SHORT_WORDS = {"short","too short","shorter","più corta","piu corta"}
LONG_WORDS  = {"long","too long","longer","più lunga","piu lunga"}

# --- SIZE: parole intere (senza 'mm')
SIZE_WORDS_BASE  = {"mini","small","medium","large","xl","vanity","pm","gm","bb","nano","micro"}
# --- 'mm' solo come token isolato, case-insensitive
MM_TOKEN_REGEX = re.compile(r'(?<![A-Za-z])mm(?![A-Za-z])', re.I)

PRE_SALE_MATERIAL_TRIGGERS = {"thinner","thin","sottile","silk","nylon","felt","material","soft","morbido","rigido","stiff"}
PRE_SALE_CUSTOM_TRIGGERS   = {"custom"," misura","misure","dimension","size","sizing","fit","fits","su misura","tailor","bespoke"}

POSITIVE_WORDS = {"love","favourite","favorite","amazing","wonderful","best of all time","i adore","grazie mille","thank you so much","your organizers are my favorite"}

# --- RICONOSCIMENTO RINGRAZIAMENTO PURO (no richiesta) ---
THANKS_RE = re.compile(
    r"\b(thank(?: you)?|thanks|appreciate|grateful|many thanks|"
    r"so happy|happy to hear|it fits(?: perfectly)?|fits perfectly|"
    r"works great|received (?:the )?replacement)\b",
    re.I
)

# eccezioni: "would like/want/wanted/just want to thank" = ringraziamento, non richiesta
THANKS_EXCEPTION_RE = re.compile(r"\b(would like|want|wanted|just want)\s+to\s+thank\b", re.I)

# indizi di richiesta/azione (escludiamo se compaiono, salvo eccezione sopra)
REQUEST_CUES_RE = re.compile(
    r"\b(need|needs|need to|want|would like|i'd like|can you|could you|"
    r"please (send|ship|exchange|replace)|replace|exchange|return|refund|"
    r"smaller|larger|different|another|send|ship|arrange|help)\b",
    re.I
)

# indizi di problema/lamentela: se presenti, non è ringraziamento “puro”
NEGATIVE_CUES_RE = re.compile(
    r"\b(too small|too big|pinched|does(?:n'?| no)t fit|did(?:n'?| not) fit|"
    r"wrong|incorrect|issue|problem|damaged|broken|faulty)\b",
    re.I
)

def is_pure_thanks(text: str) -> bool:
    """Vero se il messaggio è di ringraziamento/chiusura (no azioni richieste).
       Gestisce eccezioni 'would like to thank / want to thank' ecc."""
    t = (text or "").strip().lower()
    if not t:
        return False

    # deve esserci un segnale di ringraziamento/fine positiva
    if not THANKS_RE.search(t):
        return False

    # se c'è un trigger di richiesta, vale solo se NON è la forma "to thank"
    if THANKS_EXCEPTION_RE.search(t):
        has_request = False
    else:
        has_request = bool(REQUEST_CUES_RE.search(t))

    if has_request:
        return False

    # niente domande
    if "?" in t:
        return False

    # niente indizi di problema/lamentela
    if NEGATIVE_CUES_RE.search(t):
        return False

    return True

# --- [NUOVO] MATCH A PAROLA INTERA SICURO ---
def has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text or "", flags=re.I) is not None

def contains_any_word(text: str, vocab: set) -> bool:
    return any(has_word(text, w) for w in vocab)

def contains_any(text: str, vocab: set) -> bool:
    """Match 'largo' (sottostringa). Usato dove va bene. Evitare per taglie/colori."""
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

def message_has_photo(comment: Dict[str, Any]) -> bool:
    for a in (comment.get("attachments") or []):
        if (a.get("content_type","").startswith(("image/","application/pdf"))):
            return True
    return False

def detect_chain_case(text: str) -> Optional[Dict[str, Any]]:
    """Usa match a parola intera per evitare falsi positivi."""
    t = (text or "").lower()
    if not contains_any_word(t, CHAIN_WORDS):
        return None
    if contains_any_word(t, LONG_WORDS):
        return {"type":"length","which":"longer"}
    if contains_any_word(t, SHORT_WORDS):
        return {"type":"length","which":"shorter"}
    if has_word(t, "gold") or has_word(t, "oro"):
        return {"type":"color","color":"gold"}
    if has_word(t, "silver") or has_word(t, "argento"):
        return {"type":"color","color":"silver"}
    return {"type":"generic"}

def text_has_size_word_strict(t: str) -> bool:
    """True se troviamo una size valida (parola intera) o 'mm' token isolato (qualsiasi casing)."""
    return contains_any_word(t, SIZE_WORDS_BASE) or bool(MM_TOKEN_REGEX.search(t or ""))

def extract_size_keywords_strict(t: str) -> list:
    """Estrae size sicure; 'mm' token isolato viene reso come 'MM' in output."""
    picks = [w for w in SIZE_WORDS_BASE if has_word(t, w)]
    if MM_TOKEN_REGEX.search(t or ""):
        picks.append("MM")
    return picks

def is_explicit_request(text: str) -> bool:
    """Esplicita solo se c’è una frase trigger + una parola intera di size/color/chain."""
    if not text: return False
    t = text.lower()
    triggers = ["i want","please send","replace","i would rather have","instead","can you send","please ship","i prefer","i'd like","i would like"]
    return any(k in t for k in triggers) and (
        text_has_size_word_strict(t) or contains_any_word(t, COLOR_WORDS) or contains_any_word(t, CHAIN_WORDS)
    )

def has_compliment(text: str) -> bool:
    return contains_any(text, POSITIVE_WORDS)

# ============ ORIGINE (Shopify / Amazon-QR / generic) ============
def safe_lower(s: Optional[str]) -> str:
    return (s or "").strip().lower()

def get_ticket_via(ticket: Dict[str, Any]) -> Dict[str, Any]:
    return ticket.get("via") or {}

def classify_origin(ticket: Dict[str, Any], comments: List[Dict[str, Any]]) -> str:
    """
    Ritorna: 'shopify' | 'amazon_qr' | 'generic_email'
    Regole:
      - Shopify: via.source.rel == 'shopify' OPPURE corpo contiene la frase tipica del form.
      - Amazon QR (certo): subject contiene uno dei AMAZON_SUBJECT_TOKENS
                           OPPURE compare un ordine Amazon nel testo.
      - Se si cita Amazon genericamente senza questi indizi -> generic_email (chiedere info e decidere dopo).
    """
    via = get_ticket_via(ticket)
    rel = safe_lower(((via.get("source") or {}).get("rel")))
    if rel == "shopify":
        return "shopify"

    subject = safe_lower(ticket.get("subject"))
    thread_text = " ".join([(c.get("body") or "") for c in comments]).lower()

    if SHOPIFY_CONTACT_PHRASE in thread_text:
        return "shopify"

    if any(tok in subject for tok in AMAZON_SUBJECT_TOKENS):
        return "amazon_qr"
    if ORDER_PAT.search(thread_text.replace("\n", " ")):
        return "amazon_qr"

    return "generic_email"

def tag_origin(ticket_id: int, origin: str):
    if origin == "shopify":
        ensure_tags(ticket_id, ["source_shopify_form"])
    elif origin == "amazon_qr":
        ensure_tags(ticket_id, ["source_amazon_qr"])
    else:
        ensure_tags(ticket_id, ["source_generic_email"])

# ============ OPENAI (placeholder per futuri usi) ============
client = None
try:
    if OPENAI_APIKEY:
        client = OpenAI(api_key=OPENAI_APIKEY)
except Exception as e:
    print(f"[WARN] OpenAI init failed: {e}")
    client = None

# ============ BUILDERS DI MESSAGGIO ============
def greeting(first_name: str) -> str:
    name = first_name or "there"
    return f"Hi {name},"

def build_chain_reply(case: Dict[str, Any], first_name: str) -> str:
    g = greeting(first_name)
    if case.get("type") == "length":
        which = "longer" if case.get("which") == "longer" else "shorter"
        return (
            f"{g}\n\nThanks for letting us know — we’ll send a {which} chain right away so you can get the perfect length. "
            f"{NO_RETURN_SENTENCE}\nWe’ll share the tracking as soon as it’s available.\n\nBest regards,\n{SIGNATURE_NAME}"
        )
    if case.get("type") == "color":
        color = "gold" if case.get("color") == "gold" else "silver"
        return (
            f"{g}\n\nThanks for flagging the color — we’ll send a {color} chain as a replacement right away. "
            f"{NO_RETURN_SENTENCE}\nWe’ll share the tracking as soon as it’s available.\n\nBest regards,\n{SIGNATURE_NAME}"
        )
    return (
        f"{g}\n\nThanks for the details about the chain — we’ll arrange a replacement accordingly. "
        f"{NO_RETURN_SENTENCE}\nWe’ll share the tracking as soon as it’s available.\n\nBest regards,\n{SIGNATURE_NAME}"
    )

def summarize_keywords(text: str) -> str:
    """Estrai parole CHIAVE con match a parola intera; evitiamo falsi positivi (es. 'tan' da 'thanks')."""
    t = (text or "").lower()
    picks = []

    # size sicure (incl. token MM isolato)
    picks.extend(extract_size_keywords_strict(t))

    # color (incluso 'tan' ma solo come parola intera)
    for w in ("dark brown","brown","black","white","gold","silver","beige","sienna","red","blue","navy","tan","camel","cream","ivory","pink","green","grey","gray","chocolate"):
        if has_word(t, w): picks.append(w)

    # chain specifics
    if contains_any_word(t, CHAIN_WORDS): picks.append("chain")
    if contains_any_word(t, LONG_WORDS): picks.append("longer")
    if contains_any_word(t, SHORT_WORDS): picks.append("shorter")

    seen, out = set(), []
    for k in picks:
        if k not in seen:
            out.append(k); seen.add(k)
        if len(out) >= 3: break
    return ", ".join(out)

def build_accept_replacement(first_name: str, echo: str = "") -> str:
    g = greeting(first_name)
    tail = f" ({echo})" if echo else ""
    return (
        f"{g}\n\nThanks for the details — we’ll arrange a replacement right away{tail}. "
        f"{NO_RETURN_SENTENCE}\nWe’ll share the tracking as soon as it’s available.\n\nBest regards,\n{SIGNATURE_NAME}"
    )

def build_need_info_amazon(first_name: str, need_model_link: bool, order_present: bool, photo_present: bool) -> str:
    g = greeting(first_name)
    asks = []
    if not order_present:
        asks.append("your Amazon order number")
    if need_model_link:
        asks.append("the exact model name of your bag or a direct link to the one you own")
    ask_line = " and ".join(asks)
    thanks_photo = "Thanks for the photo — that’s very helpful. " if photo_present else ""
    return (
        f"{g}\n\nThanks for your message! To make sure the fit is perfect, could you please share {ask_line}? "
        f"{thanks_photo}{NO_RETURN_SENTENCE}\n\nBest regards,\n{SIGNATURE_NAME}"
    )

def build_need_info_shopify(first_name: str, need_model_link: bool, order_present: bool, photo_present: bool) -> str:
    g = greeting(first_name)
    asks = []
    if not order_present:
        asks.append("your Shopify order number (e.g. #1234) — if not handy, your full name, checkout email, and shipping postcode work perfectly")
    if need_model_link:
        asks.append("the exact model name of your bag or a direct link to the one you own")
    ask_line = " and ".join(asks)
    thanks_photo = "Thanks for the photo — that’s very helpful. " if photo_present else ""
    return (
        f"{g}\n\nThanks for your message! To make sure the fit is perfect, could you please share {ask_line}? "
        f"{thanks_photo}{NO_RETURN_SENTENCE}\n\nBest regards,\n{SIGNATURE_NAME}"
    )

def build_shopify_materials(first_name: str, bag_model: Optional[str], with_thanks: bool) -> str:
    g = greeting(first_name)
    open_line = "Thank you so much for your kind words — it truly means a lot! " if with_thanks else "Thanks for your message! "
    model_part = f"For your {bag_model}, " if bag_model else "For your bag, "
    return (
        f"{g}\n\n{open_line}{model_part}we can craft the organizer in different materials: "
        "silk (softer and thinner), nylon (lightweight and waterproof), or felt if you’d still like some structure. "
        "Let me know which option you prefer and I’ll arrange it for you.\n\nBest regards,\n{sig}".format(sig=SIGNATURE_NAME)
    )

def build_shopify_custom_size(first_name: str) -> str:
    g = greeting(first_name)
    return (
        f"{g}\n\nAbsolutely — custom sizing is no problem, and it’s the same price and timing as standard (production 48h). "
        "To get the fit just right, could you please share the inside dimensions of your bag (Length × Height × Width)? "
        "If it’s easier, a quick photo of the inside also helps.\n\n"
        f"Once I have this, I’ll confirm the cut for you.\n\nBest regards,\n{SIGNATURE_NAME}"
    )

def build_shade_photo_request(first_name: str) -> str:
    g = greeting(first_name)
    return (
        f"{g}\n\nHappy to help with the shade. Could you please send a quick photo of the organizer inside the bag, in good light? "
        "That way I can recommend the best matching shade right away.\n\nThanks!\nNoe"
    )

def build_confirmation_after_info(first_name: str, keywords: str) -> str:
    g = greeting(first_name)
    return (
        f"{g}\n\nThanks for the info! We’ll send your replacement according to your instructions: {keywords}. "
        "We’ll share the tracking shortly.\n\nBest regards,\n{sig}".format(sig=SIGNATURE_NAME)
    )

def build_public_tracking_message(tracking: str, first_name: Optional[str] = None) -> str:
    hello = f"Hi {first_name}!" if first_name else "Hi again!"
    return (
        f"{hello}\n\n"
        f"Here is the tracking number for your replacement: {tracking}\n"
        "You can follow the updates of your package by clicking on the link below:\n"
        f"https://t.17track.net/en#nums={tracking}\n\n"
        "Feel free to reach out if you have any questions or concerns along the way. "
        "Wishing you a smooth delivery experience!\n\n"
        f"Warm regards,\n{SIGNATURE_NAME}"
    )

def build_public_tracking_correction_message(tracking: str, first_name: Optional[str]) -> str:
    hello = f"Hi {first_name}!" if first_name else "Hi there!"
    return (
        f"{hello} Please disregard our previous message.\n\n"
        "Here is the correct tracking number for your replacement:\n"
        f"{tracking}\n\n"
        "You can follow the updates of your package by clicking on the link below:\n"
        f"https://t.17track.net/en#nums={tracking}\n\n"
        "Feel free to reach out if you have any questions or concerns along the way. "
        "Wishing you a smooth delivery experience!\n\n"
        f"Warm regards,\n{SIGNATURE_NAME}"
    )

def build_ack_review_request(first_name: str) -> str:
    name = first_name or "there"
    return (
        f"Hi again {name}!\n\n"
        "I’m so happy to hear that your replacement fits perfectly in your bag! "
        "Thank you for sharing this with me — it truly makes my day.\n\n"
        "If you ever feel like sharing your experience, we’d be so grateful for a quick review with a photo of your organizer inside your bag. "
        "It really helps other customers and means a lot to our small business.\n\n"
        "However thanks again for your kind words and support!\n\n"
        f"Warm regards,\n{SIGNATURE_NAME}"
    )

# ============ RETURN POLICY AUTO REPLY ============
RETURN_REQUEST_RE = re.compile(
    r"\b("
    r"do i need to (return|send back|ship back|mail back|send this|send it back)|"
    r"should i (return|send back|ship back|mail this|ship this)|"
    r"must i return|"
    r"do you need (me to return|me to send it back)|"
    r"need to (return|send back)|"
    r"will you (send a label|need the old one back)|"
    r"do you want (me to send|me to return)|"
    r"want me to return|"
    r"do you want it back|"
    r"send (it|this) back to you|"
    r"send the old one|"
    r"return the old one"
    r")\b",
    re.I
)

def build_return_only(first_name: str) -> str:
    g = greeting(first_name)
    return (
        f"{g}\n\n"
        "No need to send it back — we’ll take care of everything on our end. "
        "You can keep it and use it to protect your bag while waiting for the new one, "
        "or dispose of it if you don’t need it anymore.\n\n"
        "Wishing you a wonderful day,\n"
        f"{SIGNATURE_NAME}"
    )

def build_return_appendix() -> str:
    return (
        "No need to send the old one back — we’ll take care of everything on our end. "
        "You can keep it and use it to protect your bag while waiting for the new one, "
        "or dispose of it if you don’t need it anymore."
    )

# ============ SUPPORTO FLUSSO ============
def last_is_end_user_public(ticket: Dict[str,Any], comments: List[Dict[str,Any]]) -> bool:
    if not comments: return False
    last = comments[-1]
    return last.get("public") and last.get("author_id") == ticket.get("requester_id")

def user_wrote_after_last_internal(comments: List[Dict[str,Any]], requester_id: int) -> bool:
    last_user_idx = -1
    last_internal_idx = -1
    for i, c in enumerate(comments):
        if c.get("public") and c.get("author_id") == requester_id:
            last_user_idx = i
        if not c.get("public"):
            last_internal_idx = i
    return last_user_idx > last_internal_idx

def extract_first_name_from_shopify_body(comments: List[Dict[str,Any]]) -> Optional[str]:
    """
    Cerca riga 'Name:' nel corpo del form Shopify e restituisce il primo nome.
    """
    for c in comments:
        body = (c.get("body") or "")
        if SHOPIFY_CONTACT_PHRASE in body.lower():
            for line in body.splitlines():
                if line.lower().startswith("name:"):
                    name = line.split(":",1)[1].strip()
                    if name:
                        return name.split()[0]
    return None

def current_sent_tracking_tag(tags: List[str]) -> Optional[str]:
    """
    Ritorna il tag tracking_sent_<...> se presente (il primo trovato).
    """
    for t in tags or []:
        if t.startswith(TRACKING_SENT_PREFIX):
            return t
    return None

def extract_tracking_from_guard(tag: str) -> str:
    # guard è tracking_sent_<normalized>
    return tag[len(TRACKING_SENT_PREFIX):] if tag and tag.startswith(TRACKING_SENT_PREFIX) else ""

def normalize_tag(s: str) -> str:
    return re.sub(r'[^a-z0-9_]', '', (s or '').lower())

def last_public_comment_contains_tracking(comments: List[Dict[str,Any]], tracking: str) -> bool:
    if not comments: return False
    key = normalize_tag(tracking)
    for c in reversed(comments):
        if c.get("public"):
            body = (c.get("body") or "")
            if key in normalize_tag(body):
                return True
            if f"nums={normalize_tag(tracking)}" in normalize_tag(body):
                return True
            return False
    return False

# ============ COMPOSIZIONE BOZZE ============
def compose_draft(ticket: Dict[str,Any], comments: List[Dict[str,Any]]) -> str:
    last = comments[-1]
    text = (last.get("body") or "").strip()
    requester_id = ticket.get("requester_id")

    # Origine (per tono e richieste)
    origin = classify_origin(ticket, comments)

    # Nome: preferisci "Name:" del form Shopify, altrimenti requester
    shopify_first_name = extract_first_name_from_shopify_body(comments) if origin == "shopify" else None
    first_name = shopify_first_name or get_user_first_name(requester_id)

    # ---------- RINGRAZIAMENTO PURO → proposta recensione (bozza interna) ----------
    thread_text = " ".join([c.get("body","") or "" for c in comments] + [ticket.get("subject","") or ""])
    if is_pure_thanks(text) or is_pure_thanks(thread_text):
        return "[Suggested reply by ChatGPT — please review and send]\n\n" + build_ack_review_request(first_name)
    # ---------------------------------------------------------------------------------

        # ----------- RECENSIONE (GIÀ LASCIATA O FUTURA) -----------
    REVIEW_LEFT_RE = re.compile(
        r"\b(i\s*(have|had|just|already)?\s*(left|posted|wrote|submitted|gave|made|sent|shared|uploaded)\b.*\b(review|feedback|rating|5\s*stars?)\b)"
        r"|\b(i\s*(have|had|just|already)?\s*(reviewed|rated))\b"
        r"|\b(thank you for letting me leave a review)\b",
        re.I
    )

    REVIEW_FUTURE_RE = re.compile(
        r"\b(i\s*(will|am going to|plan to|want to|would like to|intend to)\s*(leave|write|post|submit|make|give|send|share|upload)\b.*\b(review|feedback|rating|5\s*stars?)\b)"
        r"|\b(i’ll\s*(write|post|leave|give)\s*(you\s+\s+)?(nice|good)?\s*(review|feedback|rating))\b",
        re.I
    )

    REVIEW_CONDITIONAL_RE = re.compile(
        r"\b(if\s+\s*(leave|write|give|post|submit|make|send|share|upload)\b.*\b(review|feedback|rating|5\s*stars?))\b",
        re.I
    )

    # Caso: recensione già lasciata
    if REVIEW_LEFT_RE.search(text) and not REVIEW_CONDITIONAL_RE.search(text):
        msg = (
            f"Oh wow, what a wonderful news! Thank you so much from the bottom of our hearts for your incredible support.\n\n"
            f"We feel genuinely lucky and honored to have a customer like you, {first_name}, and we really hope to see you again in the future.\n\n"
            "Wishing you a lovely rest of the week,\n"
            "Noe"
        )
        return "[Suggested reply by ChatGPT — please review and send]\n\n" + msg

    # Caso: recensione promessa ma non ancora lasciata
    elif REVIEW_FUTURE_RE.search(text) and not REVIEW_CONDITIONAL_RE.search(text):
        msg = (
            f"That’s so kind of you, {first_name}! We honestly can’t wait to read your review once it’s published.\n\n"
            "It truly means a lot to us to know you’re planning to share your experience — thank you for being so thoughtful.\n\n"
            "Wishing you a wonderful day ahead,\n"
            "Noe"
        )
        return "[Suggested reply by ChatGPT — please review and send]\n\n" + msg

# --------------------------------------------------------------

    # Casi speciali catena/strap
    chain_case = detect_chain_case(text)
    if chain_case:
        return "[Suggested reply by ChatGPT — please review and send]\n\n" + build_chain_reply(chain_case, first_name)

    # Valuta informazioni presenti
    info_text = thread_text
    info_sufficient = has_link(info_text) or (thread_has_any_photo(comments) and has_measurements(info_text))

    # Ordini
    amazon_order_present = bool(extract_order_number(info_text))
    photo_present = message_has_photo(last)
    explicit = is_explicit_request(text)
    compliment = has_compliment(info_text)

    # Shopify pre-vendita (materiale/custom) — naturale, niente "headers" robotici
    if origin == "shopify" and not amazon_order_present:
        if contains_any(text, PRE_SALE_MATERIAL_TRIGGERS) and not explicit:
            bag_model = (ticket.get("subject") or "").strip() or None
            msg = build_shopify_materials(first_name, bag_model, with_thanks=compliment)
            return "[Suggested reply by ChatGPT — please review and send]\n\n" + msg

        if contains_any(text, PRE_SALE_CUSTOM_TRIGGERS) and not explicit:
            msg = build_shopify_custom_size(first_name)
            return "[Suggested reply by ChatGPT — please review and send]\n\n" + msg

    # Shade / match: chiedi foto inside solo se serve
    if "shade" in text.lower() or "match" in text.lower():
        if not thread_has_any_photo(comments):
            return "[Suggested reply by ChatGPT — please review and send]\n\n" + build_shade_photo_request(first_name)

    # Replacement esplicito (qualsiasi origine): diretto, breve — echo parole chiave (max 2)
    if explicit:
        kw_raw = summarize_keywords(text)
        kws = [k.strip() for k in kw_raw.split(",") if k.strip()]
        if len(kws) > 2:
            kws = kws[:2]
        kw = ", ".join(kws)

        base_msg = build_accept_replacement(first_name, kw)

        # Se nel messaggio c’è domanda di reso, aggiungi la parte Return alla fine del messaggio principale
        if RETURN_REQUEST_RE.search(text):
            base_msg = base_msg.replace(
                "We’ll share the tracking as soon as it’s available.",
                build_return_appendix() + "\nWe’ll share the tracking as soon as it’s available."
            )

        return "[Suggested reply by ChatGPT — please review and send]\n\n" + base_msg
    
        # Caso: solo domanda sul reso (no replacement esplicito)
    if RETURN_REQUEST_RE.search(text) and not explicit:
        return "[Suggested reply by ChatGPT — please review and send]\n\n" + build_return_only(first_name)

    # Informazioni minime mancanti (se non è esplicito NON facciamo eco keyword)
    if origin == "shopify":
        msg = build_need_info_shopify(first_name, need_model_link=True, order_present=False, photo_present=photo_present)
    elif origin == "amazon_qr":
        msg = build_need_info_amazon(first_name, need_model_link=True, order_present=amazon_order_present, photo_present=photo_present)
    else:
        if "amazon" in info_text.lower():
            msg = build_need_info_amazon(first_name, need_model_link=True, order_present=amazon_order_present, photo_present=photo_present)
        else:
            msg = (
                f"{greeting(first_name)}\n\nThanks for your message! "
                "To make sure the fit is perfect, could you please share the exact model name of your bag or a direct link to the one you own? "
                f"{'Thanks for the photo — that’s very helpful. ' if photo_present else ''}"
                "If you’ve already placed an order, feel free to include your order number as well.\n\n"
                f"Best regards,\n{SIGNATURE_NAME}"
            )

    return "[Suggested reply by ChatGPT — please review and send]\n\n" + msg

# ============ TRACKING: INVIO, CORREZIONE, ANTI-SPAM ============
def handle_tracking_if_any(ticket: Dict[str,Any]) -> bool:
    """
    Gestione tracking idempotente + correzione:
      - Se il campo tracking è vuoto → STOP.
      - Se esiste un tag tracking_sent_<X>:
          * se <X> == normalize(current) → già inviato → STOP.
          * se <X> != normalize(current) → INVIA messaggio di CORREZIONE (disregard previous),
            rimuove tracking_sent_<X>, aggiunge tracking_sent_<current>, forza TAG replacement_sent, set SOLVED.
      - Se NON esiste alcun tracking_sent_:
          * Se l’ultimo pubblico contiene già il tracking corrente → retro-tag (replacement_sent + guard corrente) e STOP.
          * Altrimenti INVIA messaggio tracking standard, aggiunge (replacement_sent + guard), set SOLVED.
    Ritorna True se ha pubblicato o retro-taggato; False se non ha fatto nulla.
    """
    ticket_id = ticket["id"]
    requester_id = ticket.get("requester_id")
    first_name = get_user_first_name(requester_id)
    tags = ticket.get("tags") or []
    current_tracking = get_custom_field_value(ticket, Z_TRACKING_FIELD) if Z_TRACKING_FIELD else None
    if not current_tracking:
        return False

    current_guard = TRACKING_SENT_PREFIX + normalize_tag(current_tracking)
    existing_guard = current_sent_tracking_tag(tags)  # es. tracking_sent_uk4246...

    comments = get_ticket_comments(ticket_id)

    # Caso: esiste già un guard ma è DIVERSO -> correzione tracking
    if existing_guard and existing_guard != current_guard:
        # Se abbiamo già inviato questa correzione (cioè il nuovo guard è presente), non ripetere
        if current_guard in tags or last_public_comment_contains_tracking(comments, current_tracking):
            ensure_tags(ticket_id, [TAG_REPLACEMENT_SENT, current_guard])
            remove_tags(ticket_id, [existing_guard])
            print(f"[OK] Tracking correction retro-tagged on ticket {ticket_id}")
            try:
                z_put(f"/tickets/{ticket_id}.json", {"ticket": {"status": "solved"}})
            except Exception:
                pass
            return True

        # Invia messaggio di correzione
        body = build_public_tracking_correction_message(current_tracking, first_name)
        add_public_reply_and_tags(ticket_id, body, [TAG_REPLACEMENT_SENT, current_guard], set_status="solved")
        ensure_tags(ticket_id, [TAG_REPLACEMENT_SENT, current_guard])
        remove_tags(ticket_id, [existing_guard])
        print(f"[OK] Tracking CORRECTED and solved for ticket {ticket_id}")
        return True

    # Caso: esiste guard uguale → già inviato, non fare nulla
    if existing_guard == current_guard:
        return False

    # Caso: nessun guard presente -> prima comunicazione
    if last_public_comment_contains_tracking(comments, current_tracking):
        ensure_tags(ticket_id, [TAG_REPLACEMENT_SENT, current_guard])
        try:
            z_put(f"/tickets/{ticket_id}.json", {"ticket": {"status": "solved"}})
        except Exception:
            pass
        print(f"[OK] Retro-tag guard + replacement_sent on ticket {ticket_id}")
        return True

    # invio standard
    body = build_public_tracking_message(current_tracking, first_name)
    add_public_reply_and_tags(ticket_id, body, [TAG_REPLACEMENT_SENT, current_guard], set_status="solved")
    ensure_tags(ticket_id, [TAG_REPLACEMENT_SENT, current_guard])
    print(f"[OK] Tracking published + solved + tags forced for ticket {ticket_id}")
    return True

# ============ CICLO PRINCIPALE ============
def process_once():
    # 0) Tagga origine (Shopify / Amazon QR / generic) su tutti i ticket non-closed
    for t in list_recent_tickets():
        if t.get("status") == "closed":
            continue
        try:
            full = fetch_ticket(t["id"])           # via/subject affidabili
            comments = get_ticket_comments(t["id"])
            origin = classify_origin(full, comments)
            tag_origin(full["id"], origin)
        except Exception as e:
            print(f"[WARN] origin tag fail {t.get('id')}: {e}")

    # 1) Tracking prima di tutto (idempotente + correzione)
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

# ============ VALIDAZIONE ENV ============
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
