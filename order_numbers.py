"""Shared order-number parsing and format checks; never an order lookup."""

import re


ORDER_PAT = re.compile(
    r"(?<!\d)(\d{3})-[ \t]*(?:\r?\n[ \t]*){0,2}(\d{7})-"
    r"[ \t]*(?:\r?\n[ \t]*){0,2}(\d{7})\b(?!-\d)")
LABELED_ORDER_PAT = re.compile(
    r"\border\s*(?:number|no\.?|id)?\s*(?::|=|\bis\b)?\s*(#?\d{3,9})\b"
    r"(?![ \t]*-[ \t]*(?:\r?\n[ \t]*){0,2}\d)", re.I)
AMAZON_LIKE_PAT = re.compile(
    r"(?<!\d)(\d{2,4})-[ \t]*(?:\r?\n[ \t]*){0,2}(\d{5,9})-"
    r"[ \t]*(?:\r?\n[ \t]*){0,2}(\d{3,9})\b(?!-\d)")


def order_numbers_in_message(text):
    found = ["-".join(m.groups()) for m in ORDER_PAT.finditer(text or "")]
    found.extend(m.group(1) for m in LABELED_ORDER_PAT.finditer(text or ""))
    return list(dict.fromkeys(found))


def malformed_amazon_numbers(text):
    """Recognize an attempted Amazon-shaped number, without repairing it."""
    return list(dict.fromkeys("-".join(m.groups())
                for m in AMAZON_LIKE_PAT.finditer(text or "")
                if tuple(map(len, m.groups())) != (3, 7, 7)))


def customer_order_number(comments, requester_id):
    for comment in reversed(comments):
        if not comment.get("public") or comment.get("author_id") != requester_id:
            continue
        body = comment.get("body") or ""
        numbers = order_numbers_in_message(body)
        malformed = malformed_amazon_numbers(body)
        if numbers or malformed:
            # A later incomplete/corrected identifier must not resurrect an old one.
            return numbers[0] if len(numbers) == 1 and not malformed else None
    return None


def order_number_validation(comments, requester_id, supplied_number=None):
    """Format-valid does NOT mean the order exists or belongs to the customer."""
    supplied_number = str(supplied_number).strip() if supplied_number else None
    malformed = []
    numbers = []
    if supplied_number:
        malformed = malformed_amazon_numbers(supplied_number)
        if not malformed:
            return {"status": "provided_unverified", "number": supplied_number,
                    "requires_customer_correction": False, "malformed_candidates": []}
    if not malformed:
        for comment in reversed(comments):
            if not comment.get("public") or comment.get("author_id") != requester_id:
                continue
            body = comment.get("body") or ""
            numbers = order_numbers_in_message(body)
            malformed = malformed_amazon_numbers(body)
            if numbers or malformed:
                break
    status = ("malformed_amazon" if malformed else
              "ambiguous" if len(numbers) > 1 else
              "provided_unverified" if numbers else "missing")
    return {"status": status, "number": numbers[0] if len(numbers) == 1 and not malformed else None,
            "requires_customer_correction": bool(malformed or len(numbers) > 1),
            "malformed_candidates": malformed,
            "expected_amazon_format": "123-1234567-1234567 (3-7-7 digits)",
            "format_check_is_not_order_verification": True}
