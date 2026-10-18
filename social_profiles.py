"""Conservative, offline extraction of creator-supplied social profile links."""

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit


PROFILE_URL = re.compile(
    r"(?<![\w@./-])(?:https?://)?(?:(?:www|m)\.)?"
    r"(?:instagram\.com|tiktok\.com)/[^\s<>\"']+", re.IGNORECASE)
LABELED_HANDLE = re.compile(
    r"\b(instagram|ig|tik\s?tok)\s*(?:profile|account|handle)?\s*"
    r"(?:is\s+|[:=\-]\s*)?@([a-zA-Z0-9_][a-zA-Z0-9_.]{0,63})(?![\w.])",
    re.IGNORECASE)
INSTAGRAM_NON_PROFILES = {
    "p", "reel", "reels", "stories", "explore", "accounts", "direct", "about",
    "developer", "developers", "legal", "privacy", "help", "challenge", "share",
    "sharer", "tv", "web", "oauth", "directory", "emails", "static",
}


class ProfileLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.links.extend(value for key, value in attrs
                              if key.lower() == "href" and isinstance(value, str))


def normalize_profile(url):
    """No network access or redirects; accept only direct profile URLs."""
    url = unescape(url).strip().rstrip(".,!?;:)]}’”")
    parsed = urlsplit(url if "://" in url else "https://" + url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in ("http", "https") or parsed.username or parsed.password:
        return None
    if parsed.netloc.lower() != host:  # Reject ports, credentials and lookalike hosts.
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 1:
        return None
    handle = parts[0]
    if host in ("instagram.com", "www.instagram.com", "m.instagram.com"):
        if (not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.]{0,29}", handle)
                or handle.lower() in INSTAGRAM_NON_PROFILES):
            return None
        return "instagram", "https://www.instagram.com/" + handle.lower() + "/"
    if host in ("tiktok.com", "www.tiktok.com", "m.tiktok.com"):
        if not re.fullmatch(r"@[a-zA-Z0-9_][a-zA-Z0-9_.]{0,63}", handle):
            return None
        return "tiktok", "https://www.tiktok.com/@" + handle[1:].lower()
    return None


def customer_social_profiles(comments, requester_id):
    """Only public requester messages, never internal drafts or agent guesses.

    Multiple different profiles on one platform are ambiguous: leave that field
    blank for a human. A clearly labelled @handle also counts as an explicit link.
    """
    candidates = {"instagram": set(), "tiktok": set()}
    for comment in comments:
        if not comment.get("public") or comment.get("author_id") != requester_id:
            continue
        body = unescape(comment.get("body") or "")
        parser = ProfileLinks()
        parser.feed(comment.get("html_body") or "")
        urls = PROFILE_URL.findall(body) + parser.links
        for platform, handle in LABELED_HANDLE.findall(body):
            urls.append(("https://www.tiktok.com/@" if platform.lower().replace(" ", "") == "tiktok"
                         else "https://www.instagram.com/") + handle)
        for url in urls:
            try:
                profile = normalize_profile(url)
            except ValueError:  # Malformed URLs cannot cause a field update.
                continue
            if profile:
                platform, canonical_url = profile
                candidates[platform].add(canonical_url)
    return {platform: next(iter(urls)) for platform, urls in candidates.items() if len(urls) == 1}
