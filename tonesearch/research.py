"""TONE3000 catalogue access and bounded web research (copied from NAM Mixer's hybrid/services/research.py)."""
from __future__ import annotations

import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from tonesearch import cache, overrides


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


TONE3000_BASE = "https://www.tone3000.com/api/v1"

_METADATA_STOPWORDS = frozenset({
    # Domain filler: generic enough to appear in almost any amp listing or
    # any user request, so matching on them tells you nothing about fit.
    "amp", "amps", "guitar", "model", "pack", "sound", "tone", "capture",
    "captures",
    # General English function/filler words. `rank_query` is often the
    # user's full, conversational request (see tone3000_search's docstring),
    # not a curated search term -- without a broad stopword list, words like
    # "would"/"like"/"way" from ordinary sentences were being counted as
    # genuine metadata matches, drowning out the few words that actually
    # distinguish one capture from another (see
    # test_tone3000_metadata_score_ignores_generic_query_words).
    "and", "are", "for", "from", "have", "into", "its", "need", "over",
    "that", "the", "this", "with", "your", "you", "about", "after", "all",
    "also", "any", "because", "been", "being", "but", "can", "cant",
    "could", "did", "does", "doing", "dont", "down", "each", "get", "gets",
    "getting", "going", "had", "has", "her", "here", "him", "his", "how",
    "ive", "just", "know", "like", "likes", "make", "makes", "many", "may",
    "might", "more", "most", "much", "must", "myself", "not", "now", "off",
    "one", "only", "other", "our", "out", "really", "same", "should",
    "some", "still", "such", "than", "there", "these", "they", "think",
    "those", "through", "too", "try", "trying", "use", "used", "using",
    "very", "want", "wanted", "wants", "was", "way", "well", "were",
    "what", "when", "where", "which", "while", "who", "why", "will",
    "would",
})


def _rank_tone3000_metadata(query: str, result: dict) -> tuple[int, str]:
    """Rank catalogue records from supplied metadata, never invented facts.

    This is the deterministic safety net used before the local model reads the
    shortlist. Scores represent the portion of the meaningful query terms that
    occur in the title or description; a title hit is weighted more heavily.
    """
    terms = {
        term for term in re.findall(r"[a-z0-9]+", query.lower())
        if len(term) >= 3 and term not in _METADATA_STOPWORDS
    }
    title_terms = set(re.findall(r"[a-z0-9]+", result["title"].lower()))
    description_terms = set(re.findall(r"[a-z0-9]+", result["description"].lower()))
    title_hits = sorted(terms & title_terms)
    description_hits = sorted((terms & description_terms) - set(title_hits))
    matched_weight = 2 * len(title_hits) + len(description_hits)
    score = round(100 * matched_weight / (2 * len(terms))) if terms else 0
    reason = "Metadata matches " + ", ".join(title_hits + [term for term in description_hits if term not in title_hits]) if (title_hits or description_hits) else "Limited catalogue metadata; inspect the pack description"
    return score, reason + "."


class _PageText(HTMLParser):
    """Extract readable page text without executing or trusting page markup.

    Page chrome (navigation, headers, footers, forms) is dropped, and block elements end a line,
    so menus never run together into one long fake "sentence".
    """

    IGNORED = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button", "select", "menu", "template"}
    BLOCKS = {"p", "li", "div", "section", "article", "br", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "dd", "dt"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.IGNORED:
            self._ignored += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.IGNORED and self._ignored:
            self._ignored -= 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._ignored:
            self.parts.append(data)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects instead of silently re-fetching an unvalidated host.

    A search-engine result is third-party, untrusted data; if we validated
    the original hostname but then transparently followed a redirect, a
    malicious or compromised page could point us at an internal address
    (SSRF) after the safety check already passed.
    """

    def redirect_request(self, *args, **kwargs):
        return None


def _is_safe_public_host(hostname: str) -> bool:
    """Reject loopback/private/link-local/reserved targets to guard against SSRF.

    `href` values come from third-party search results, not from a trusted
    catalogue API -- unlike the TONE3000 calls in this module, which only
    ever hit a fixed, known-safe base URL.
    """
    if not hostname:
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
    except OSError:
        return False
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            return False
        if (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast or address.is_unspecified
        ):
            return False
    return True


# Words that make a research sentence about the actual rig rather than about the web page.
_GEAR_WORDS = {
    "amp", "amps", "amplifier", "amplifiers", "head", "combo", "cabinet", "cab", "speaker", "speakers", "celestion",
    "jensen", "marshall", "fender", "vox", "mesa", "boogie", "hiwatt", "orange", "peavey", "ampeg", "dumble", "plexi",
    "twin", "deluxe", "champ", "bassman", "princeton", "stratocaster", "strat", "telecaster", "tele", "gibson",
    "sg", "explorer", "flying", "humbucker", "humbuckers", "pickup", "pickups", "p90", "pedal", "pedals", "fuzz",
    "overdrive", "distortion", "wah", "screamer", "rangemaster", "booster", "univibe", "leslie", "reverb",
    "tremolo", "echo", "delay", "echoplex", "valve", "tube", "tubes", "rectifier", "jcm800", "soldano", "engl",
    "bogner", "friedman", "laney", "matchless", "muff", "klon", "prs", "ibanez", "schecter", "compressor", "chorus",
    "flanger", "phaser", "darkglass", "sansamp", "svt", "preamp", "bass", "di",
}
_BASS = re.compile(r"\bbass(ist|ists)?\b", re.IGNORECASE)
_GEAR_PHRASES = ("les paul", "bad cat", "pro co rat", "big muff", "tube screamer", "dual rectifier")
# Words that support gear evidence but don't name any equipment ("recorded in the studio").
_CONTEXT_WORDS = {"recorded", "recording", "studio", "session", "played", "plugged", "used", "cranked", "gain", "rig"}
_FILLER_WORDS = {
    "i", "im", "i'm", "id", "i'd", "would", "like", "want", "wanted", "need", "looking", "look", "for", "find",
    "a", "an", "the", "tone", "tones", "sound", "sounds", "sounding", "that", "which", "to", "of", "on", "in",
    "with", "from", "had", "has", "have", "used", "uses", "use", "get", "got", "match", "matches", "matching",
    "similar", "please", "me", "my", "some", "is", "was", "be", "it", "and", "or", "he", "she", "they", "their",
    "his", "her", "what", "how", "can", "you", "just", "really", "exact", "exactly", "same", "as", "at", "by",
}
# Social, video and preset-sharing sites rarely say what the artist actually used.
_SKIP_HOSTS = ("tiktok.com", "youtube.com", "youtu.be", "instagram.com", "facebook.com", "pinterest.", "twitter.com",
               "x.com", "tone.fender.com", "line6.com", "spotify.com", "apple.com", "amazon.", "ebay.", "reverb.com",
               # Shops and classifieds list gear for sale, never what an artist used.
               "gumtree.", "craigslist.", "preloved.", "for-sale.", "gear4music.", "thomann.", "sweetwater.com",
               "guitarcenter.com", "musiciansfriend.com", "andertons.co.uk", "bassbros.co.uk", "pmtonline.co.uk",
               "dv247.", "zzounds.com", "kijiji.", "marktplaats.", "olx.", "etsy.com", "walmart.com")
# Listing and product pages on any site: "/for-sale/", "/shop/", "/products/", "/classifieds/".
_LISTING_PATH = re.compile(r"/(for-?sale|shop|store|products?|classifieds?|buy|cart|category|categories|listings?)(/|$|\?|-)",
                           re.IGNORECASE)
# Words too common to tell one request from another: "bass amps for sale" mustn't count as evidence about
# "Periphery bass", so evidence needs the request's distinctive words (artist, song, album).
_NOT_TOPIC = {"bass", "guitar", "guitars", "amp", "amps", "tone", "tones", "sound", "sounds", "rig", "gear",
              "pedal", "pedals", "solo", "riff", "live", "studio", "album", "song", "band", "the", "and"}
MAX_SOURCES = 4
EVIDENCE_CHARS = 700


def _topic(query: str) -> str:
    """The artist/song/gear words of a request, without conversational filler."""
    words = re.findall(r"[\w'’.-]+", query)
    kept = [w for w in words if w.lower().strip(".'’") not in _FILLER_WORDS]
    return " ".join(kept) or query.strip()


def _topic_words(topic: str) -> set:
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9']{3,}", topic)}
    return (words - _NOT_TOPIC) or words  # "bass tone" alone has nothing more distinctive to go on


def _skip_source(href: str) -> bool:
    parsed = urlparse(href)
    host = (parsed.hostname or "").lower()
    if _LISTING_PATH.search(parsed.path):
        return True
    return not host or any(host == pattern or host.endswith("." + pattern) or (pattern.endswith(".") and pattern in host)
                           for pattern in _SKIP_HOSTS)


_QUALIFIER = re.compile(r"modern equivalent|replica|recreat|reissue|stand-?in|substitut|our (catalog|catalogue)|"
                        r"\bonly (two|three|\d+) (songs|tracks|tones)\b|not (confirmed|known|documented)|unconfirmed|"
                        r"\bunknown\b|\blater\b (era|years|tours?)|\bera\b", re.IGNORECASE)


def _sentence_score(sentence: str, topic_words: set, page_on_topic: bool = False) -> int:
    """0 for menus and boilerplate; otherwise topic hits (weighted) plus distinct gear words."""
    words = re.findall(r"[a-z0-9']+", sentence.lower())
    if len(words) < 7 or len(sentence) > 450:
        return 0
    if sentence.rstrip("\"”)").endswith("?"):
        return 0  # a question ("Does anyone know the gear...?") is not evidence
    if {"you", "your", "you're", "yours"} & set(words):
        return 0  # sales copy aimed at the reader ("adapt it to your rig"), not what the artist used
    if "→" in sentence or "»" in sentence or sentence.count("·") >= 2 or sentence.count("|") >= 2:
        return 0  # "related links" strips: arrows and dot/pipe separators
    capitalised = sum(1 for w in re.findall(r"[A-Za-z][\w']*", sentence) if w[0].isupper())
    if capitalised > len(words) * 0.5:  # Title Case runs are menus, headings and tag lists
        return 0
    gear = len(set(words) & _GEAR_WORDS) + sum(phrase in sentence.lower() for phrase in _GEAR_PHRASES)
    topic = len(set(words) & topic_words)
    if _QUALIFIER.search(sentence) and (topic or page_on_topic):
        # "A modern equivalent", "our catalogue only has two songs": without these the AI takes a site's
        # suggestions and later-era gear as what was used on the record.
        return 6 + topic * 2 + gear
    # A product page lists gear without the artist and an album intro names the artist without gear, so a
    # sentence needs both, unless the page is about the topic and the sentence names gear twice ("their go-to
    # gear: a Dual Rectifier and a Bad Cat").
    if not gear or not (topic or (page_on_topic and gear >= 2)):
        return 0
    return topic * 2 + gear + len(set(words) & _CONTEXT_WORDS)


# Forum threads about an artist are mostly members describing their own rigs ("my SVT into an 8x10"), so on a
# forum a sentence only counts when it names the artist, and never when it's about the poster's own gear.
_FORUM = re.compile(r"(^|\.)(talkbass|thegearpage|gearspace|reddit|ultimate-guitar|seymourduncan|rig-talk|"
                    r"sevenstring|harmony-central|basschat|musicplayers|fractalaudio|line6)\.|^forums?\.|"
                    r"/(forums?|threads?|showthread|viewtopic|comments|community)\b", re.IGNORECASE)
_OWN_RIG = re.compile(r"\b(my|mine|i use|i run|i play|i've got|i have)\b", re.IGNORECASE)


def _is_forum(href: str) -> bool:
    parsed = urlparse(href)
    return bool(_FORUM.search((parsed.hostname or "").lower()) or _FORUM.search(parsed.path))


def _extract_evidence(html: str, topic: str, forum: bool = False) -> str:
    """The best few sentences of a page about the requested rig, in page order."""
    parser = _PageText()
    parser.feed(html)
    topic_words = _topic_words(topic)
    text = "".join(parser.parts)
    opening = set(re.findall(r"[a-z0-9']+", text[:3000].lower()))  # title and first lines say what a page is about
    page_on_topic = not forum and len(opening & topic_words) >= min(2, len(topic_words))
    candidates, seen = [], set()
    for block in text.split("\n"):
        block = re.sub(r"\s+", " ", block).strip()
        for sentence in re.split(r"(?<=[.!?])\s+", block):
            sentence = sentence.strip()
            if sentence.lower() in seen:  # pages often repeat a line (summary box + body)
                continue
            if forum and _OWN_RIG.search(sentence):
                continue
            if sentence[-1:] in ".!?\"”)" and (score := _sentence_score(sentence, topic_words, page_on_topic)):
                seen.add(sentence.lower())
                candidates.append((score, len(candidates), sentence))
    best = sorted(sorted(candidates, reverse=True)[:3], key=lambda c: c[1])
    return " ".join(c[2] for c in best)[:EVIDENCE_CHARS]


def _page_evidence(href: str, topic: str) -> str:
    """Fetch a public page (no redirects, bounded size) and extract rig evidence from it."""
    parsed = urlparse(href)
    if parsed.scheme not in ("https", "http") or not _is_safe_public_host(parsed.hostname or ""):
        return ""
    try:
        request = Request(href, headers={"User-Agent": "TONE-Search research/1.0"})
        opener = build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=8) as response:
            if "html" not in response.headers.get("Content-Type", ""):
                return ""
            html = response.read(750_000).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    return _extract_evidence(html, topic, forum=_is_forum(href))


# ddgs's native HTTP client (primp) can block forever while holding Python's GIL: on macOS, two searches in
# threads at once never return, and every other thread in the server stops with them. Each search therefore
# runs in its own short-lived process, which can always be killed when it overruns.
_DDGS_CHILD = r"""
import json, sys
args = json.load(sys.stdin)
try:
    from ddgs import DDGS
except ImportError:
    print(json.dumps({"missing": True}))
    sys.exit()
try:
    with DDGS() as search:
        found = search.text(args["query"], max_results=args["max_results"], backend=args["backend"],
                            timeout=args["timeout"]) or []
    print(json.dumps({"results": found}))
except Exception as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]}))
"""
_MISSING_DDGS = "Web research needs the 'ddgs' package (pip install -r requirements.txt)."


def _ddgs_search(query: str, max_results: int, *, backend: str = "auto", timeout: int = 10, run=subprocess.run) -> list:
    request = json.dumps({"query": query, "max_results": max_results, "backend": backend, "timeout": timeout})
    try:
        child = run([sys.executable, "-c", _DDGS_CHILD], input=request, capture_output=True, text=True,
                    timeout=timeout + 5)  # a little longer than ddgs's own timeout, for start-up
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"web search did not finish in {timeout + 5} seconds") from None
    try:
        reply = json.loads(child.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(f"web search failed ({(child.stderr or 'no output').strip()[-200:]})") from None
    if reply.get("missing"):
        raise RuntimeError(_MISSING_DDGS)
    if reply.get("error"):
        raise RuntimeError(reply["error"])
    return [r for r in reply.get("results") or [] if isinstance(r, dict)]


# Search engines often rate-limit a shared server IP for a moment, which ddgs reports as "No results found".
# Each attempt: (engines, pause before it). "auto" rotates ddgs's engines; the second attempt asks
# engines that block shared servers less often.
SEARCH_ATTEMPTS = (("auto", 0.0), ("brave,mojeek,yahoo", 1.5))
TOPIC_ATTEMPTS = (("brave,mojeek,yahoo", 0.0), ("auto", 1.5))
RESEARCH_SEARCH_SECONDS = 20


def _search_retrying(search, query: str, deadline: float, attempts=SEARCH_ATTEMPTS) -> tuple[list, Exception | None]:
    """Results for one query, trying other engines after a failure, never past the deadline."""
    last_error = None
    for backend, pause in attempts:
        remaining = deadline - time.monotonic() - pause
        if remaining < 2:
            break
        time.sleep(pause)
        try:
            results = search(query, 10, backend=backend, timeout=max(2, min(10, int(remaining))))
        except RuntimeError as exc:
            if str(exc) == _MISSING_DDGS:  # the package is missing: retrying cannot help
                raise
            last_error = exc
            continue
        except Exception as exc:  # ddgs exposes provider-specific exception types.
            last_error = exc
            continue
        if results:
            return results, None
    return [], last_error


def web_notes(query: str, *, search=_ddgs_search, evidence=_page_evidence) -> str:
    """Return up to MAX_SOURCES notes of documented gear for the request, one "- title: text (url)" line each."""
    topic = _topic(query)
    deadline = time.monotonic() + RESEARCH_SEARCH_SECONDS
    # Tested on real prompts: "guitar rig" matched the Guitar Rig software and "equipboard" pulled in
    # unrelated shops; these two found forums, interviews and gear write-ups instead.
    player = "bassist bass" if _BASS.search(query) else "guitarist"  # a bass request must not find guitar rigs
    queries = (f"{topic} {player} amp pedals gear used", f"{topic} {player} interview amplifier gear")
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:  # one failing search no longer stops the other
        outcomes = list(pool.map(lambda q: _search_retrying(search, q, deadline), queries))
    if not any(found for found, _ in outcomes):
        outcomes.append(_search_retrying(search, topic, deadline, TOPIC_ATTEMPTS))  # last try: just the topic words
    results, seen, hosts = [], set(), set()
    for found, _ in outcomes:
        for result in found:
            href = str(result.get("href", "")).strip()
            host = (urlparse(href).hostname or "").lower().removeprefix("www.")
            # One page per site: otherwise one tone-settings site can fill every note.
            if href and href not in seen and host not in hosts and not _skip_source(href):
                seen.add(href)
                hosts.add(host)
                results.append(result)
    if not results:
        errors = [str(error) for _, error in outcomes if error]
        raise RuntimeError("the search engines returned nothing"
                           + (f" ({errors[-1]}); they may be limiting this server for a moment" if errors else " for this request")
                           + ". The search continued without it.")

    candidates = results[:10]  # several pages have no usable text (blocked or built by JavaScript)
    with ThreadPoolExecutor(max_workers=len(candidates) or 1) as pool:
        extracts = list(pool.map(lambda r: evidence(str(r.get("href", "")).strip(), topic), candidates))
    topic_words = _topic_words(topic)
    notes = []
    for result, extract in zip(candidates, extracts):
        title = str(result.get("title", "")).strip()
        snippet = re.sub(r"\s+", " ", str(result.get("body", ""))).strip()
        text = extract or (snippet if _sentence_score(snippet if snippet[-1:] in ".!?" else snippet + ".", topic_words) else "")
        if text:
            notes.append(f"- {title[:120]}: {text[:EVIDENCE_CHARS]} ({result['href']})")  # URL last and intact
        if len(notes) == MAX_SOURCES:
            break
    if not notes:
        raise RuntimeError("Web research found no pages describing this rig.")
    return "\n".join(notes)


def _require_tone3000_api_key(*, for_action: str) -> str:
    """Return a validated TONE3000 secret key, never logging its value.

    The visitor's own key from Settings wins; otherwise the server environment
    (cPanel: Setup Python App > Environment variables).
    """
    visitor_key = overrides.get("tone3000_api_key")
    if visitor_key:
        if not visitor_key.startswith("t3k_cs_"):
            raise RuntimeError("Settings: your TONE3000 key must be a secret key starting with 't3k_cs_'.")
        return visitor_key
    api_key = _env("TONE3000_API_KEY")
    if not api_key:
        raise RuntimeError(f"TONE3000 {for_action} needs a server-side TONE3000_API_KEY secret key (t3k_cs_…).")
    if not api_key.startswith("t3k_cs_"):
        raise RuntimeError(
            f"TONE3000 {for_action} found a TONE3000_API_KEY, but it does not start with the expected "
            "'t3k_cs_' secret-key prefix. Set the server's TONE3000_API_KEY environment variable to a secret key."
        )
    return api_key


_TONE3000_LINK_PREFIXES = ("https://api.tone3000.com/", "https://www.tone3000.com/")


def _tone3000_link(value: object) -> str | None:
    text = str(value or "")
    return text if text.startswith(_TONE3000_LINK_PREFIXES) else None


def _tone3000_card_fields(tone: dict) -> dict:
    """Display-only pack metadata; links are kept only when they point at TONE3000 itself."""
    def count(key: str) -> int | None:
        value = tone.get(key)
        return value if isinstance(value, int) and value >= 0 else None

    images = tone.get("images") if isinstance(tone.get("images"), list) else []
    tags = tone.get("tags") if isinstance(tone.get("tags"), list) else []
    makes = tone.get("makes") if isinstance(tone.get("makes"), list) else []
    return {
        "image": next((link for link in map(_tone3000_link, images) if link), None),
        "tags": [str(tag.get("name"))[:40] for tag in tags if isinstance(tag, dict) and tag.get("name")][:12],
        "makes": [str(make.get("name") if isinstance(make, dict) else make)[:40] for make in makes if make][:6],
        "gear": str(tone.get("gear") or "")[:30] or None,
        "a2_models_count": count("a2_models_count"),
        "a1_models_count": count("a1_models_count"),
        "models_count": count("models_count"),
        "irs_count": count("irs_count"),
        "format": str(tone.get("format") or "")[:20] or None,
        "sizes": [str(size.get("name") if isinstance(size, dict) else size)[:20]
                  for size in (tone.get("sizes") if isinstance(tone.get("sizes"), list) else []) if size][:5],
        "license": str((tone.get("license") or {}).get("name") if isinstance(tone.get("license"), dict)
                       else tone.get("license") or "")[:40] or None,
        "year": str(tone.get("published_at") or tone.get("created_at") or "")[:4] or None,
        "published": str(tone.get("published_at") or tone.get("created_at") or "")[:25] or None,
        "downloads_count": count("downloads_count"),
        "favorites_count": count("favorites_count"),
        "url": _tone3000_link(tone.get("url")),
    }


# Search filters the TONE3000 API accepts (https://www.tone3000.com/api); validated in app.py.
GEARS = ("amp", "amp-cab", "pedal", "outboard", "cab", "space", "experimental")
SIZES = ("standard", "lite", "feather", "nano", "custom")
FORMATS = ("nam", "ir", "aida-x", "aa-snapshot", "proteus")
ARCHITECTURES = ("2", "1", "any")
SORTS = ("best-match", "downloads-all-time", "trending", "newest", "oldest")
DEFAULT_FILTERS = {"gears": [], "sizes": [], "makes": [], "tags": [], "creators": [], "format": "nam",
                   "architecture": "2", "sort": "best-match", "calibrated": False, "verified": False}


def validate_filters(raw) -> dict | None:
    """Validated TONE3000 filters from the page or an MCP client, or None when anything is malformed."""
    if raw is None:
        return dict(DEFAULT_FILTERS)
    if not isinstance(raw, dict):
        return None
    filters = dict(DEFAULT_FILTERS)
    choices = {"gears": GEARS, "sizes": SIZES}
    for name in ("gears", "sizes", "makes", "tags", "creators"):
        values = raw.get(name, [])
        if not isinstance(values, list) or len(values) > 10 or not all(isinstance(v, str) and 0 < len(v.strip()) <= 60 for v in values):
            return None
        values = list(dict.fromkeys(v.strip() for v in values))
        if name in choices and not set(values) <= set(choices[name]):
            return None
        if any(sep in v for v in values for sep in ("_", ",")):  # the API's list separators
            return None
        filters[name] = values
    for name, allowed in (("format", FORMATS), ("architecture", ARCHITECTURES), ("sort", SORTS)):
        value = raw.get(name, filters[name])
        if value not in allowed:
            return None
        filters[name] = value
    for name in ("calibrated", "verified"):
        value = raw.get(name, False)
        if not isinstance(value, bool):
            return None
        filters[name] = value
    return filters


def _search_params(query: str, filters: dict) -> dict:
    params = {"query": query, "page": 1, "page_size": 20, "sort": filters["sort"], "format": filters["format"]}
    if filters["format"] == "nam" and filters["architecture"] != "any":
        params["architecture"] = filters["architecture"]
    for name, separator in (("gears", "_"), ("tags", "_"), ("makes", "_"), ("sizes", "-"), ("creators", ",")):
        if filters[name]:
            params[name] = separator.join(filters[name])
    for flag in ("calibrated", "verified"):
        if filters[flag]:
            params[flag] = "true"
    return params


def tone3000_search(query: str, *, filters: dict | None = None, rank_query: str = "", opener=urlopen,
                    cache_db=None) -> list[dict]:
    """Search public TONE3000 metadata; captures themselves are never downloaded.

    `query` is the narrow amp-family term sent to the catalogue API, which
    already filters for relevance -- scoring every result against that same
    short term is nearly always 100% and tells the user nothing. `rank_query`
    is used for match scoring instead so results are actually differentiated;
    it defaults to `query` when the caller has nothing richer to offer.
    `filters` (see DEFAULT_FILTERS) is passed to the API, so creators, makes and
    tags are matched across the whole catalogue, not just one page of results.
    """
    api_key = _require_tone3000_api_key(for_action="search")
    params = _search_params(query, {**DEFAULT_FILTERS, **(filters or {})})
    request = Request(
        f"{TONE3000_BASE}/tones/search?{urlencode(params)}",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    def fetch():
        try:
            with opener(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"TONE3000 search failed: {exc}") from exc
        tones = payload.get("data", []) if isinstance(payload, dict) else []
        return [tone for tone in tones if isinstance(tone, dict)]
    # Public catalogue data, the same for every key, so it's shared between visitors and the MCP server.
    tones = cache.remember(cache_db, "search:" + json.dumps(params, sort_keys=True), fetch, cache.CATALOGUE_SECONDS)
    results = []
    for position, tone in enumerate(tones):
        # An id-less entry can never be turned into a working discuss/download
        # link downstream (both key off this id), so surfacing it would just
        # be a dead result the player can click and get nothing from.
        if not isinstance(tone.get("id"), int):
            continue
        user = tone.get("user") or {}
        creator = str(user.get("display_name") or user.get("username") or "")
        title = str(tone.get("title") or tone.get("name") or "Untitled capture")
        description = str(tone.get("description") or "").replace("\n", " ")
        result = {
            "id": tone.get("id"),
            "title": title,
            "creator": creator or "unknown creator",
            "description": description[:600],
            **_tone3000_card_fields(tone),
        }
        result["catalog_rank"] = position  # TONE3000's own order for this search (best match, trending...)
        result["match_score"], result["match_reason"] = _rank_tone3000_metadata(rank_query or query, result)
        results.append(result)
    if not results:
        filtered = any(params.get(name) for name in ("gears", "tags", "makes", "sizes", "creators", "calibrated", "verified"))
        raise RuntimeError(f"TONE3000 returned no matches for \"{query}\"" +
                           (" with these filters. Try removing some filters." if filtered else "."))
    # The catalogue order is useful, but it is not the user's request-specific
    # ranking.  Rank the complete API page before shortening it: taking the
    # first eight first could discard the best AC30/JCM result simply because
    # it appeared later in TONE3000's broad best-match response.
    return sorted(
        results,
        key=lambda result: (result["match_score"], result["title"].lower()),
        reverse=True,
    )[:8]


MODELS_PAGE_SIZE = 300  # the API maximum: nearly every pack arrives in one request
MAX_MODELS = 600


def _models_params(tone_id: int, architecture: str, page: int) -> dict:
    params = {"tone_id": tone_id, "page": page, "page_size": MODELS_PAGE_SIZE}
    if architecture in ("1", "2"):
        params["architecture"] = architecture
    return params


def _tone3000_models_payload(tone_id: int, *, architecture: str = "2", opener=urlopen) -> list[dict]:
    """Every A2 model in a pack: the API pages its results, so keep asking until a short page."""
    api_key = _require_tone3000_api_key(for_action="downloads")
    models: list[dict] = []
    page = 1
    while len(models) < MAX_MODELS:
        request = Request(
            f"{TONE3000_BASE}/models?{urlencode(_models_params(tone_id, architecture, page))}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with opener(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            if models:  # keep what we have rather than failing the whole pack on a later page
                break
            raise RuntimeError(f"TONE3000 pack details failed: {exc}") from exc
        batch = payload.get("data", []) if isinstance(payload, dict) else []
        models.extend(batch)
        total_pages = payload.get("total_pages") if isinstance(payload, dict) else None
        if not batch or (isinstance(total_pages, int) and page >= total_pages) or (total_pages is None and len(batch) < MODELS_PAGE_SIZE):
            break
        page += 1
    return models[:MAX_MODELS]


def tone3000_models(tone_id: int, *, architecture: str = "2", opener=urlopen, cache_db=None) -> list[dict]:
    """Return model names for one public TONE3000 tone pack, never credentials or download links."""
    key = f"models:{tone_id}:{architecture}"
    saved = cache.get(cache_db, key, cache.REFERENCE_SECONDS)
    if saved:
        return saved
    models = _tone3000_model_names(tone_id, architecture, opener)
    cache.put(cache_db, key, models)  # names only: download links may be signed and short-lived
    return models


def _tone3000_model_names(tone_id: int, architecture: str, opener) -> list[dict]:
    raw_models = _tone3000_models_payload(tone_id, architecture=architecture, opener=opener)
    models = []
    for model in raw_models:
        url = str(model.get("model_url") or "")
        if not url.startswith("https://"):
            continue
        models.append({
            "id": model.get("id"),
            "name": str(model.get("name") or "Unnamed file"),
            "architecture": model.get("architecture_version"),
        })
    if not models:
        wanted = {"1": "A1 NAM files", "2": "A2 NAM files"}.get(architecture, "downloadable files")
        raise RuntimeError(f"TONE3000 returned no {wanted} in this pack. Try NAM version 'Any' in Filters.")
    return models


MAX_FILE_BYTES = 50_000_000


def _fetch_file(url: str, api_key: str, opener=None) -> bytes:
    """Download one capture file without ever sending the API key off TONE3000.

    TONE3000 API links get the key; anything else (for example a signed storage link, or where an API
    link redirects to) is fetched without it, and only from a public https host. Redirects are followed
    by hand so urllib cannot forward the Authorization header to another host.
    """
    for _ in range(3):
        parsed = urlparse(url)
        if url.startswith(f"{TONE3000_BASE}/"):
            headers = {"Authorization": f"Bearer {api_key}"}
        elif parsed.scheme == "https" and _is_safe_public_host(parsed.hostname or ""):
            headers = {}
        else:
            raise RuntimeError(f"TONE3000 gave a download link on an unexpected host ({parsed.hostname or 'none'}).")
        request = Request(url, headers=headers)
        try:
            with (opener or build_opener(_NoRedirectHandler).open)(request, timeout=30) as response:
                data = response.read(MAX_FILE_BYTES + 1)
        except HTTPError as exc:
            location = exc.headers.get("Location") if exc.code in (301, 302, 303, 307, 308) else None
            if location:
                url = urljoin(url, location)
                continue
            raise RuntimeError(f"TONE3000 download failed: HTTP {exc.code}") from exc
        except Exception as exc:
            raise RuntimeError(f"TONE3000 download failed: {exc}") from exc
        if len(data) > MAX_FILE_BYTES:
            raise RuntimeError("That file is too large to download here; open the pack on TONE3000 instead.")
        if not data:
            raise RuntimeError("TONE3000 returned an empty download.")
        return data
    raise RuntimeError("TONE3000 redirected the download too many times.")


def _model_link(model: dict) -> str:
    return str(next((model.get(k) for k in ("model_url", "download_url", "file_url", "url") if model.get(k)), "") or "")


def _find_model(tone_id: int, model_id: int, architecture: str, opener=None) -> dict | None:
    """Look a file up the way the pack list found it, then under the other NAM versions, then directly."""
    kwargs = {"opener": opener} if opener else {}
    for version in dict.fromkeys((architecture, "2", "1", "any")):
        model = next((m for m in _tone3000_models_payload(tone_id, architecture=version, **kwargs) if m.get("id") == model_id), None)
        if model and _model_link(model):
            return model
    api_key = _require_tone3000_api_key(for_action="downloads")
    request = Request(f"{TONE3000_BASE}/models/{model_id}", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with (opener or urlopen)(request, timeout=20) as response:
            model = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    model = model.get("data", model) if isinstance(model, dict) else None
    if isinstance(model, dict) and model.get("tone_id") not in (None, tone_id):
        return None  # never serve a file from a different pack than the one shown
    return model if isinstance(model, dict) and _model_link(model) else None


def tone3000_model_download(tone_id: int, model_id: int, *, architecture: str = "2", opener=None) -> tuple[bytes, str]:
    """Download a selected pack member server-side, keeping the API key private."""
    model = _find_model(tone_id, model_id, architecture, opener)
    if not model:
        raise RuntimeError("TONE3000 has no download link for that file.")
    api_key = _require_tone3000_api_key(for_action="downloads")
    return _fetch_file(_model_link(model), api_key, opener), str(model.get("name") or f"tone3000-{model_id}")


MAX_ZIP_FILES = 60


def tone3000_pack_zip(tone_id: int, *, architecture: str = "any", opener=None) -> tuple[bytes, int]:
    """Zip a pack's files on this server (TONE3000's whole-pack link is for approved partners with a user sign-in, not a secret key)."""
    kwargs = {"opener": opener} if opener else {}
    models = [m for m in _tone3000_models_payload(tone_id, architecture=architecture, **kwargs) if _model_link(m)]
    if not models:
        raise RuntimeError("TONE3000 returned no downloadable files in this pack.")
    if len(models) > MAX_ZIP_FILES:
        raise RuntimeError(f"This pack has {len(models)} files; packs over {MAX_ZIP_FILES} are best downloaded on TONE3000.")
    api_key = _require_tone3000_api_key(for_action="downloads")
    with ThreadPoolExecutor(max_workers=6) as pool:
        files = list(pool.map(lambda m: _fetch_file(_model_link(m), api_key, opener), models))
    buffer = io.BytesIO()
    used: set = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for model, data in zip(models, files):
            base = re.sub(r"[^\w .()\[\]-]+", "_", str(model.get("name") or model.get("id"))).strip() or str(model.get("id"))
            ext = "" if re.search(r"\.(nam|wav|json)$", base, re.IGNORECASE) else ".nam"
            name, n = f"{base}{ext}", 2
            while name.lower() in used:
                name, n = f"{base} ({n}){ext}", n + 1
            used.add(name.lower())
            archive.writestr(name, data)
    return buffer.getvalue(), len(models)


LOOKUP_KINDS = {"makes": "makes", "tags": "tags", "creators": "users"}


def tone3000_lookup(kind: str, query: str, *, opener=urlopen, cache_db=None) -> list[dict]:
    """Catalogue suggestions for the filter fields: [{"value", "label", "count"}], most used first."""
    if cache_db is not None:
        key = f"lookup:{kind}:{query.lower()}"
        saved = cache.get(cache_db, key, cache.REFERENCE_SECONDS)
        if saved is None:
            saved = _tone3000_lookup(kind, query, opener=opener)
            cache.put(cache_db, key, saved)
        return saved
    return _tone3000_lookup(kind, query, opener=opener)


def _tone3000_lookup(kind: str, query: str, *, opener=urlopen) -> list[dict]:
    """Catalogue suggestions for the filter fields: [{"value", "label", "count"}], most used first."""
    endpoint = LOOKUP_KINDS[kind]
    api_key = _require_tone3000_api_key(for_action="search")
    params = {"query": query, "page": 1, "page_size": 10}
    params["sort"] = "tones"
    request = Request(f"{TONE3000_BASE}/{endpoint}?{urlencode(params)}",
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with opener(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"TONE3000 suggestions failed: {exc}") from exc
    items = payload.get("data", []) if isinstance(payload, dict) else []
    suggestions = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if kind == "creators":
            value = str(item.get("username") or "").strip()
            label = str(item.get("display_name") or value).strip()
            count = item.get("tones_count")
        else:
            value = str(item.get("slug") or item.get("name") or "").strip()
            label = str(item.get("name") or value).strip()
            count = item.get("tones_count", item.get("count"))
        if value:
            suggestions.append({"value": value[:60], "label": label[:60], "count": count if isinstance(count, int) else None})
    return suggestions
