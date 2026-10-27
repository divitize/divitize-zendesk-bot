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
SHOPIFY_ERP_PREFIX = "3325573-"
SHOPIFY_ERP_PAT = re.compile(r"(?<![\w-])3325573-([0-9]{4,6})(?![\w-])")
SHORT_ORDER_PAT = re.compile(r"#?([0-9]{4,6})")
BARE_ORDER_PAT = re.compile(r"\s*#?([0-9]{4,6})[.!]?\s*")
ORDER_REQUEST_PAT = re.compile(r"\border\s*(?:number|no\.?|id)\b", re.I)
REQUEST_CUE_PAT = re.compile(r"\b(?:send|share|provide|resend|confirm|what|have|need)\b|\?", re.I)
ORDER_LIST_TAIL_PAT = re.compile(
    r"\s*(?:[,/&]|\bor\b|\band\b)\s*(#?[0-9]{4,6})(?![\w.-])", re.I)


def shopify_erp_number(number):
    """Map the 4-6 digit storefront reference to ERP format, exactly once."""
    number = str(number or "").strip()
    if SHOPIFY_ERP_PAT.fullmatch(number):
        return number
    match = SHORT_ORDER_PAT.fullmatch(number)
    return SHOPIFY_ERP_PREFIX + match.group(1) if match else None


def order_numbers_in_message(text):
    text = text or ""
    found = ["-".join(m.groups()) for m in ORDER_PAT.finditer(text)]
    erp_matches = list(SHOPIFY_ERP_PAT.finditer(text))
    labeled_matches = list(LABELED_ORDER_PAT.finditer(text))
    found.extend(m.group(0) for m in erp_matches)
    found.extend(m.group(1) for m in labeled_matches)
    # Do not choose the first item from "Order #1893 or #1894".
    for match in erp_matches + labeled_matches:
        number = match.group(0) if match.re is SHOPIFY_ERP_PAT else match.group(1)
        if not shopify_erp_number(number):
            continue
        end = match.end()
        while True:
            following = ORDER_LIST_TAIL_PAT.match(text, end)
            if not following:
                break
            found.append(following.group(1))
            end = following.end()
    # Short and already-prefixed references to the SAME order are not ambiguous.
    unique = {}
    for number in found:
        unique.setdefault(shopify_erp_number(number) or number, number)
    return list(unique.values())


def malformed_amazon_numbers(text):
    """Recognize an attempted Amazon-shaped number, without repairing it."""
    return list(dict.fromkeys("-".join(m.groups())
                for m in AMAZON_LIKE_PAT.finditer(text or "")
                if tuple(map(len, m.groups())) != (3, 7, 7)))


def _customer_order_candidates(comments, requester_id):
    public = [c for c in comments if c.get("public")]
    for index in range(len(public) - 1, -1, -1):
        comment = public[index]
        if comment.get("author_id") != requester_id:
            continue
        body = comment.get("body") or ""
        numbers = order_numbers_in_message(body)
        malformed = malformed_amazon_numbers(body)
        previous = public[index - 1] if index else {}
        prior_body = previous.get("body") or ""
        bare = BARE_ORDER_PAT.fullmatch(body)
        if not numbers and bare and (body.strip().startswith("#") or (
                previous.get("author_id") != requester_id
                and ORDER_REQUEST_PAT.search(prior_body)
                and REQUEST_CUE_PAT.search(prior_body))):
            numbers = [bare.group(1)]
        # A short reference explicitly presented as Amazon is NOT a Shopify order.
        if numbers and all(SHORT_ORDER_PAT.fullmatch(n) for n in numbers) and (
                re.search(r"\bamazon\b", body, re.I) or (
                    bare and re.search(r"\bamazon\b", prior_body, re.I))):
            malformed.extend(numbers)
            numbers = []
        if numbers or malformed:
            # A later incomplete/corrected identifier must not resurrect an old one.
            return numbers, malformed
    return [], []


def customer_order_number(comments, requester_id):
    numbers, malformed = _customer_order_candidates(comments, requester_id)
    return numbers[0] if len(numbers) == 1 and not malformed else None


def customer_order_field_number(comments, requester_id):
    number = customer_order_number(comments, requester_id)
    return shopify_erp_number(number) or number


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
        numbers, malformed = _customer_order_candidates(comments, requester_id)
    status = ("malformed_amazon" if malformed else
              "ambiguous" if len(numbers) > 1 else
              "provided_unverified" if numbers else "missing")
    return {"status": status, "number": numbers[0] if len(numbers) == 1 and not malformed else None,
            "requires_customer_correction": bool(malformed or len(numbers) > 1),
            "malformed_candidates": malformed,
            "expected_amazon_format": "123-1234567-1234567 (3-7-7 digits)",
            "format_check_is_not_order_verification": True}
