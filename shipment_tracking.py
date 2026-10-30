"""17TRACK standard lookups for private support drafts; never writes to Zendesk.

Only the number is transmitted. No email, address, order ID, paid refresh,
translation service, webhook, or browser-provided URL is sent/followed.
"""

import copy
import re
import threading
import time
from datetime import datetime, timezone

import requests


API_BASE = "https://api.17track.net/track/v2.4/"
NUMBER = r"[A-Za-z0-9]{8,40}"
LABEL = re.compile(r"\btracking\s*(?:number|no\.?|id|code|#)?\s*"
                   r"(?:for\s+(?:your|the)\s+(?:replacement|shipment|order)\s*)?"
                   r"(?::|=|\bis\b)?\s*(" + NUMBER + r")(?![\w-])", re.I)
TRACK_LINK = re.compile(r"https://t\.17track\.net/[^\s]*?[#?&]nums=(" + NUMBER + r")(?![\w,;-])", re.I)
SHIPPING_INTENT = re.compile(
    r"\b(?:track|tracking|undelivered|returned|delayed|lost|suivi|livraison|rastreo|seguimiento)\b"
    r"|\b(?:where|when|status|update|not|never|hasn.t|didn.t|haven.t|dove|quando)\b.{0,100}"
    r"\b(?:order|shipment|shipping|delivery|parcel|package|arriv\w*|receiv\w*|spedizione|consegna)\b"
    r"|\b(?:parcel|package|shipment|delivery)\b.{0,100}\b(?:status|update|fail\w*|return\w*|not|never)\b", re.I)
BARE_NUMBER = re.compile(r"\s*(" + NUMBER + r")[.!]?\s*")
_cache = {}
_pending = {}
_lock = threading.Lock()
_last_request = 0.0


class TrackingPending(RuntimeError):
    """Let the normal poll try again before creating a premature holding draft."""


def _valid_number(number):
    return bool(re.fullmatch(NUMBER, number or "") and re.search(r"\d", number))


def tracking_request(comments, requester_id):
    """Find an explicit reference in public text, including quoted shipping emails.

    Most recent public shipping reference wins. Agent announcements are useful
    after a replacement; private drafts and ticket tracking fields are NOT input.
    """
    public = [c for c in comments if c.get("public")]
    customer = next((c for c in reversed(public) if c.get("author_id") == requester_id), {})
    latest_body = customer.get("body") or ""
    # Quoted shipment notifications supply a number, not the current intent.
    current_text = re.split(r"(?im)^\s*(?:On .{0,200}wrote:|From:|-----Original Message-----)",
                            latest_body, maxsplit=1)[0]
    if not SHIPPING_INTENT.search(current_text) and not (
            BARE_NUMBER.fullmatch(latest_body) and len(public) > 1
            and public[-2].get("author_id") != requester_id
            and re.search(r"\btracking\s*(?:number|id|code)\b", public[-2].get("body") or "", re.I)):
        return None
    for index in range(len(public) - 1, -1, -1):
        body = public[index].get("body") or ""
        numbers = [m.group(1).upper() for pat in (LABEL, TRACK_LINK) for m in pat.finditer(body)]
        bare = BARE_NUMBER.fullmatch(body)
        if (not numbers and bare and public[index].get("author_id") == requester_id and index
                and public[index - 1].get("author_id") != requester_id
                and re.search(r"\btracking\s*(?:number|id|code)\b", public[index - 1].get("body") or "", re.I)):
            numbers = [bare.group(1).upper()]
        numbers = list(dict.fromkeys(n for n in numbers if _valid_number(n)))
        if numbers:
            return {"numbers": numbers, "ambiguous": len(numbers) != 1,
                    "source_comment_id": public[index].get("id")}
    return None


def _post(api_key, endpoint, body, post):
    global _last_request
    if endpoint not in {"gettrackinfo", "getquota", "register"}:
        raise ValueError("Tracking endpoint is not permitted")
    with _lock:
        delay = .4 - (time.monotonic() - _last_request)
        if delay > 0:
            time.sleep(delay)
        _last_request = time.monotonic()
    response = post(API_BASE + endpoint, json=body,
                    headers={"17token": api_key, "Content-Type": "application/json"},
                    timeout=(3, 10), allow_redirects=False)
    if response.status_code != 200:
        raise ValueError("Tracking service unavailable")
    result = response.json()
    if not isinstance(result, dict) or result.get("code") != 0 or not isinstance(result.get("data"), dict):
        raise ValueError("Invalid tracking service response")
    return result["data"]


def _matching(data, number):
    entries = data.get("accepted") or []
    rejected = data.get("rejected") or []
    if not isinstance(entries, list) or not isinstance(rejected, list):
        raise ValueError("Invalid tracking entries")
    accepted = [e for e in entries if isinstance(e, dict) and str(e.get("number", "")).upper() == number]
    errors = [e.get("error", {}).get("code") for e in rejected
              if isinstance(e, dict) and str(e.get("number", "")).upper() == number]
    return accepted, errors


def _clean(value, limit=500):
    return re.sub(r"[\x00-\x1f]", " ", str(value or ""))[:limit].strip()


def _summarize(entry, number, checked_at):
    info = entry.get("track_info") or {}
    status = info.get("latest_status") or {}
    event = info.get("latest_event") or {}
    if not isinstance(info, dict) or not isinstance(status, dict) or not isinstance(event, dict):
        raise ValueError("Invalid tracking details")
    raw_time = event.get("time_raw") or {}
    event_time = event.get("time_utc") or event.get("time_iso")
    if not event_time and isinstance(raw_time, dict):
        event_time = " ".join(str(raw_time.get(k) or "") for k in ("date", "time", "timezone")).strip()
    description = _clean(event.get("description"))
    main = _clean(status.get("status"), 80)
    sub = _clean(status.get("sub_status"), 100)
    if not description or not event_time or main == "NotFound":
        return {"outcome": "pending", "number": number, "checked_at": checked_at,
                "verified": False, "reason": "No usable carrier event is available yet"}
    normalized = description.lower().rstrip(" .")
    returned = sub == "Exception_Returned" or normalized == "package return, received by shipper"
    returning = sub == "Exception_Returning"
    outcome = "returned_to_sender" if returned else "returning_to_sender" if returning else "carrier_update"
    if main == "Delivered" and (returned or returning):
        outcome = "conflicting"
    metrics = info.get("time_metrics") or {}
    estimate = metrics.get("estimated_delivery_date") or {} if isinstance(metrics, dict) else {}
    return {"outcome": outcome, "number": number, "verified": outcome != "conflicting",
            "source": "17TRACK standard API / carrier-reported data", "checked_at": checked_at,
            "carrier_code": entry.get("carrier"), "status": main, "sub_status": sub,
            "latest_event": {"description": description, "time": _clean(event_time, 100)},
            "carrier_estimated_delivery": ({k: _clean(estimate.get(k), 100) for k in ("source", "from", "to")}
                                           if isinstance(estimate, dict) else {}),
            "tracking_url": "https://t.17track.net/en#nums=" + number}


def lookup_tracking(number, api_key, post=None, now=None):
    """Register only if absent, then read standard results. No paid refresh route.

    Server registration is idempotent for the same number/carrier. After a timeout
    a subsequent request reads first, rather than blindly registering again.
    """
    now = now or time.time
    number = str(number or "").strip().upper()
    if not _valid_number(number):
        return {"outcome": "unavailable", "verified": False, "reason": "Invalid tracking number"}
    if not api_key:
        return {"outcome": "unavailable", "number": number, "verified": False,
                "reason": "17TRACK API is not configured; agent must check manually"}
    post = post or requests.post
    cache_key = (api_key, number)  # Key stays in process memory; never in notes/logs/model data.
    cached = _cache.get(cache_key)
    if cached and now() - cached[0] < (30 if cached[1]["outcome"] == "pending" else 600):
        result = copy.deepcopy(cached[1])
    else:
        checked_at = datetime.fromtimestamp(now(), timezone.utc).isoformat()
        try:
            data = _post(api_key, "gettrackinfo", [{"number": number}], post)
            accepted, errors = _matching(data, number)
            if not accepted and errors == [-18019902]:
                quota = _post(api_key, "getquota", [], post).get("quota_remain")
                if not isinstance(quota, int) or isinstance(quota, bool) or quota < 1:
                    result = {"outcome": "unavailable", "number": number, "verified": False,
                              "reason": "No usable 17TRACK quota; no new shipment was registered"}
                else:
                    registered = _post(api_key, "register", [{"number": number}], post)
                    registrations, registration_errors = _matching(registered, number)
                    if len(registrations) == 1 and not registration_errors:
                        query = {"number": number, "carrier": registrations[0]["carrier"]}
                        accepted, errors = _matching(_post(api_key, "gettrackinfo", [query], post), number)
                        result = None
                    elif registration_errors == [-18019901]:
                        accepted, errors = _matching(_post(api_key, "gettrackinfo", [{"number": number}], post), number)
                        result = None
                    else:
                        result = {"outcome": "unavailable", "number": number, "verified": False,
                                  "reason": "Carrier detection or registration needs manual review"}
            else:
                result = None
            if result is None:
                if len(accepted) == 1 and not errors:
                    result = _summarize(accepted[0], number, checked_at)
                elif len(accepted) > 1:
                    result = {"outcome": "ambiguous", "number": number, "verified": False,
                              "reason": "Multiple carriers matched; agent must identify the correct shipment"}
                else:
                    result = {"outcome": "unavailable", "number": number, "verified": False,
                              "reason": "No tracking details returned; agent must check manually"}
        except Exception:
            # Provider/HTTP exceptions may contain headers or echoed credentials.
            result = {"outcome": "unavailable", "number": number, "verified": False,
                      "reason": "17TRACK lookup failed; agent must check manually"}
        if len(_cache) >= 256:
            _cache.pop(next(iter(_cache)))
        _cache[cache_key] = (now(), copy.deepcopy(result))
    if result["outcome"] == "pending":
        if len(_pending) >= 256 and cache_key not in _pending:
            _pending.pop(next(iter(_pending)))
        started = _pending.setdefault(cache_key, now())
        if now() - started < 300:
            raise TrackingPending("Waiting for the initial carrier result; no draft posted yet")
    else:
        _pending.pop(cache_key, None)
    return result


def tracking_context(comments, requester_id, enabled=False, api_key=""):
    candidate = tracking_request(comments, requester_id)
    if not candidate:
        return None
    if candidate["ambiguous"]:
        return {"outcome": "ambiguous", "verified": False, **candidate,
                "reason": "Multiple explicit tracking numbers; ask which shipment is relevant"}
    number = candidate["numbers"][0]
    result = lookup_tracking(number, api_key if enabled else "")
    return {**result, "source_comment_id": candidate["source_comment_id"]}


def tracking_agent_note(context):
    if not context:
        return ""
    number = context.get("number") or ", ".join(context.get("numbers") or [])
    note = "Shipment lookup (reference only; NEVER populate replacement tracking): " + number
    if context.get("verified"):
        event = context["latest_event"]
        note += (f"\n17TRACK carrier report: {context['status']} / {context['sub_status']}; "
                 f"{event['time']} — {event['description']}"
                 f"\nRetrieved: {context['checked_at']}. This is the latest reported event, not a live delivery guarantee.")
    else:
        note += "\nNot verified: " + context.get("reason", context.get("outcome", "unavailable"))
    return note
