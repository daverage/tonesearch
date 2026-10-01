"""TONE3000 catalogue access and bounded web research (copied from NAM Mixer's hybrid/services/research.py)."""
from __future__ import annotations

import io
import ipaddress
import json
import logging
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


def _page_title(html: str) -> str:
    """Best-effort HTML <title> for topic detection; Trafilatura handles the actual page content."""
    match = re.search(r"<title\b[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = re.sub(r"<[^>]+>", " ", match.group(1))
    return re.sub(r"\s+", " ", title).strip()


class _PageText(HTMLParser):
    """Extract readable page text without executing or trusting page markup: the fallback when trafilatura is
    missing, fails or finds no main text (it skips short pages).

    Page chrome (navigation, headers, footers, forms) is dropped, and block elements end a line,
    so menus never run together into one long fake "sentence".
    """

    IGNORED = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button", "select", "menu", "template"}
    # The title says what the page is about (see `title`), but it is not evidence: it is kept out of `parts`.
    BLOCKS = {"p", "li", "div", "section", "article", "br", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "dd", "dt"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._ignored = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self.IGNORED:
            self._ignored += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.IGNORED and self._ignored:
            self._ignored -= 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._ignored:
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
# Generic equipment words. Makes and models are recognised by their shape instead (_MODEL, _names), so research
# works for gear nobody has listed here: "a Zorblax QX-57 amplifier" is evidence without "Zorblax" in Python.
_GEAR_WORDS = {
    "amp", "amps", "amplifier", "amplifiers", "combo", "cab", "cabinet", "speaker", "speakers", "pickup", "pickups",
    "humbucker", "humbuckers", "pedal", "pedals", "stompbox", "bass", "preamp", "gear",
    "fuzz", "overdrive", "distortion", "boost", "booster", "delay", "echo", "reverb", "chorus", "phaser", "flanger",
    "tremolo", "vibrato", "wah", "compressor", "octave", "valve", "valves", "tube", "tubes", "modeler", "modeller",
    # and the words of a spec sheet
    "effects", "loop", "watt", "watts", "stereo", "mono", "headroom",
}
# Not "guitar" ("Tom Quayle took up the guitar at 15"), "head" ("head to this page") or "channel" ("YouTube channel"):
# too common outside gear talk to show a sentence is about gear.
# Every request is about instruments, gear or peripherals, so a page must mention one of these somewhere to be
# evidence: "AC30" also finds a Panasonic AG-AC30 camcorder, and song titles find lyrics pages. Words that other
# products use too ("stereo", "audio", "chorus", "keyboard", "instrument") are left out on purpose.
_MUSIC_WORDS = {
    "guitar", "guitars", "guitarist", "guitarists", "bass", "basses", "bassist", "bassists", "amp", "amps",
    "amplifier", "amplifiers", "combo", "pedal", "pedals", "pedalboard", "stompbox", "pickup", "pickups", "humbucker",
    "humbuckers", "cab", "cabinet", "preamp", "fuzz", "overdrive", "distortion", "reverb", "tremolo", "wah", "synth",
    "synthesizer", "synthesiser", "drums", "drummer", "piano", "musician", "musicians", "frets", "fretboard",
}


_PLAYING = {"played", "plays", "playing", "plugged", "tracked", "strummed"}


def _about_music(*texts: str, short: bool = False) -> bool:
    """True when the text mentions an instrument or gear; a short snippet may instead say someone played it."""
    words = set(re.findall(r"[a-z]+", " ".join(texts).lower()))
    return bool(words & _MUSIC_WORDS or (short and words & _PLAYING))


# A model-like token mixes letters and digits ("VH4", "JC-40", "AC30", "DS-1", "2x10", "40W"), but decades ("1970s")
# and ordinals ("2nd") don't count...
_MODEL = re.compile(r"(?<![\w-])(?=[\w-]*\d)(?=[\w-]*[a-z])[a-z0-9]+(?:-[a-z0-9]+)*(?![\w-])", re.IGNORECASE)
_NOT_MODEL = re.compile(r"^\d+(s|st|nd|rd|th|x)$|\d[A-Z]?[a-z]{3,}|^[A-G](#|b)?(m|maj|min|sus|add|dim|aug)\d*$")
# ...and not chords ("Gadd9", "Am7") or chord charts that run into lyrics ("Gadd9fail").
# A spec or rig-list line: "Amplifier: Diezel VH4", "JC-40: 40W stereo 2x10 combo".
_LABEL = re.compile(r"^[A-Z][\w /&()-]{0,30}:\s+\S")
# Words that say gear was actually used rather than just discussed ("recorded through", "plugged into").
_USE_WORDS = {"used", "uses", "using", "played", "plays", "recorded", "recording", "tracked", "plugged", "runs", "ran",
              "paired", "equipped", "fitted", "studio", "session", "sessions", "rig", "cranked", "designed", "developed"}
# A hint for what to search, not an intent classifier: two or more of these ask for gear meeting needs (search
# specs and reviews), one makes the request ambiguous (search one of each), none means a player's or a song's rig.
_NEEDS = re.compile(r"\b(best|recommend\w*|suggest\w*|gigg?ing|gigs?|stereo|headroom|watts?|combo|budget|cheap|"
                    r"affordable|under [£$€]?\d+|lightweight|portable|pedal platform|practice|home use|bedroom|"
                    r"loud enough|small venues?)\b", re.IGNORECASE)
_BASS = re.compile(r"\bbass(ist|ists)?\b", re.IGNORECASE)
_FILLER_WORDS = {
    "i", "im", "i'm", "id", "i'd", "would", "like", "want", "wanted", "need", "looking", "look", "for", "find",
    "a", "an", "the", "tone", "tones", "sound", "sounds", "sounding", "that", "which", "to", "of", "on", "in",
    "with", "from", "had", "has", "have", "used", "uses", "use", "get", "got", "match", "matches", "matching",
    "similar", "please", "me", "my", "some", "is", "was", "be", "it", "and", "or", "he", "she", "they", "their",
    "his", "her", "what", "how", "can", "you", "just", "really", "exact", "exactly", "same", "as", "at", "by",
}
# Sources are skipped for three different reasons, kept apart so each can be reviewed on its own terms.
# Unreadable: video, social and streaming pages need a browser or a login, so extraction finds nothing there.
_UNREADABLE_HOSTS = ("tiktok.com", "youtube.com", "youtu.be", "instagram.com", "facebook.com", "pinterest.",
                     "twitter.com", "x.com", "spotify.com", "apple.com")
# Low value: preset-sharing pages for a maker's own modellers give settings to recreate a sound, never what an
# artist used. (Line 6's product pages are kept: only its CustomTone presets are skipped, by path.)
_PRESET_HOSTS = ("tone.fender.com",)
_PRESET_PATH = re.compile(r"/customtone\b", re.IGNORECASE)
# Commercial noise: shops, marketplaces and classifieds list gear for sale.
_SHOP_HOSTS = ("amazon.", "ebay.", "reverb.com", "gumtree.", "craigslist.", "preloved.", "for-sale.", "gear4music.",
               "thomann.", "sweetwater.com", "guitarcenter.com", "musiciansfriend.com", "andertons.co.uk",
               "bassbros.co.uk", "pmtonline.co.uk", "dv247.", "zzounds.com", "kijiji.", "marktplaats.", "olx.",
               "etsy.com", "walmart.com")
_SKIP_HOSTS = _UNREADABLE_HOSTS + _PRESET_HOSTS + _SHOP_HOSTS
# Bass forums, skipped only for requests that don't mention bass: a guitar amp request found a bass-amp thread and
# the AI suggested a bass head and a bass cab.
_BASS_SITES = ("talkbass.com", "basschat.co.uk")
# Listing pages on any site: "/for-sale/", "/shop/", "/classifieds/". Not "/products/": makers' spec pages
# (roland.com/global/products/jc-40/) live there, and known shops are skipped by host instead.
_LISTING_PATH = re.compile(r"/(for-?sale|shop|store|classifieds?|buy|cart|category|categories|listings?)(/|$|\?|-)",
                           re.IGNORECASE)
# Words too common to tell one request from another: "bass amps for sale" mustn't count as evidence about
# "Periphery bass", so evidence needs the request's distinctive words (artist, song, album).
_NOT_TOPIC = {"bass", "guitar", "guitars", "amp", "amps", "tone", "tones", "sound", "sounds", "rig", "gear",
              "pedal", "pedals", "solo", "riff", "live", "studio", "album", "song", "band", "the", "and"}
MAX_SOURCES = 4
EVIDENCE_CHARS = 1_400  # coherent local context per source; raise AI RESEARCH_CHARS to about 7,000 alongside this
EVIDENCE_BLOCKS = 3
MAX_EXTRACTED_CHARS = 120_000  # bound Trafilatura output before relevance scoring
_REFERS_BACK = re.compile(r"(he|she|they|their|his|her|it|its|this|these|that|those|both)\b", re.IGNORECASE)


def _topic(query: str) -> str:
    """The artist/song/gear words of a request, without conversational filler. The library and saved answers are
    keyed on these words (knowledge.topic_words), so changing this changes which saved work a request finds."""
    words = re.findall(r"[\w'’.-]+", query)
    kept = [w for w in words if w.lower().strip(".'’") not in _FILLER_WORDS]
    return " ".join(kept) or query.strip()


def _search_words(query: str) -> str:
    """The request as a web search: like _topic, but short requests ("Tom Quayle", "Knights of Cydonia") stay whole,
    as do small words inside a capitalised name ("Knights of Cydonia by Muse"), which search engines match better."""
    words = re.findall(r"[\w'’.-]+", query)
    if len(words) <= 3:
        return " ".join(words) or query.strip()
    kept = []
    for i, word in enumerate(words):
        inside_name = (0 < i < len(words) - 1 and word.islower()
                       and words[i - 1][:1].isupper() and words[i + 1][:1].isupper())
        if inside_name or word.lower().strip(".'’") not in _FILLER_WORDS:
            kept.append(word)
    return " ".join(kept) or query.strip()


def _topic_words(topic: str) -> set:
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9']{3,}", topic)}
    return (words - _NOT_TOPIC) or words  # "bass tone" alone has nothing more distinctive to go on


def _skip_source(href: str) -> bool:
    parsed = urlparse(href)
    host = (parsed.hostname or "").lower()
    if _LISTING_PATH.search(parsed.path) or _PRESET_PATH.search(parsed.path):
        return True
    return not host or any(host == pattern or host.endswith("." + pattern) or (pattern.endswith(".") and pattern in host)
                           for pattern in _SKIP_HOSTS)


# Sentences that change what nearby evidence means. These score highly on their own: without them the AI takes a
# site's suggestions and later-era gear as what was used on the record.
_QUALIFIER = re.compile(r"modern equivalent|replica|recreat|reissue|stand-?in|substitut|instead of|our (catalog|catalogue)|"
                        r"\bonly (two|three|\d+) (songs|tracks|tones)\b|not (confirmed|known|documented|the original)|"
                        r"unconfirmed|\bunknown\b|\bunclear\b|\blater\b (era|years|tours?)|\bera\b", re.IGNORECASE)
# Weaker hedges and dates: kept when they sit next to chosen evidence, not chosen for themselves.
_HEDGE = re.compile(r"\b(likely|probably|possibly|reportedly|rumou?red|later|introduced|currently|unclear)\b", re.IGNORECASE)
_DATES = re.compile(r"\b(19[4-9]\d|20[0-4]\d)\b|\b(recorded|released|tracked) (in|at|during)\b", re.IGNORECASE)


_CATEGORY_WORDS = _GEAR_WORDS | _MUSIC_WORDS | {"microphone", "microphones", "instrument", "instruments", "effect",
                                                 "accessories"}


def _names(sentence: str, topic_words: set) -> int:
    """Runs of capitalised words after the first word ("Diezel", "Bad Cat Hot Cat"), other than the topic's own and
    capitalised categories ("Guitars, Amplifiers, Effects Pedals" in a site's menu copy)."""
    runs, current = 0, []
    for word in re.findall(r"[A-Za-z][\w'’-]*", sentence)[1:] + [""]:
        if word[:1].isupper() and word not in ("I", "I'm", "I've"):
            current.append(word.lower())
            continue
        if current and not set(current) <= topic_words | _CATEGORY_WORDS:
            runs += 1
        current = []
    return runs


def _specific(sentence: str, topic_words: set) -> bool:
    """A model, a name other than the topic's, or a sign gear was used: more than generic gear words."""
    return bool(any(not _NOT_MODEL.search(m) for m in _MODEL.findall(sentence)) or _names(sentence, topic_words)
                or set(re.findall(r"[a-z']+", sentence.lower())) & _USE_WORDS)


def _sentence_score(sentence: str, topic_words: set, page_on_topic: bool = False, *, thread_on_topic: bool = False) -> int:
    """0 for menus and boilerplate; otherwise how much the sentence says about the topic's gear.

    Gear is recognised by shape, not by a list of makes: generic equipment words, model-like tokens ("VH4") and
    capitalised names. A sentence needs the topic, unless the page is about the topic (a maker's spec page or rig
    rundown) and it names gear twice, or it is on a forum thread about the topic and says someone used the gear.
    """
    words = re.findall(r"[a-z0-9']+", sentence.lower())
    models = {m.lower() for m in _MODEL.findall(sentence) if not _NOT_MODEL.search(m)}
    spec_line = bool(_LABEL.match(sentence)) and len(words) <= 12
    if len(words) < (3 if models or spec_line else 7) or len(sentence) > 450:
        return 0
    if sentence[-1:] not in ".!?\"”)" and not (models or spec_line):
        return 0  # a cut-off fragment; spec lines ("Amplifier: Diezel VH4") often have no full stop
    if sentence.rstrip("\"”)").endswith("?"):
        return 0  # a question ("Does anyone know the gear...?") is not evidence
    if {"you", "your", "you're", "yours"} & set(words) and not (page_on_topic and models):
        return 0  # sales copy aimed at the reader ("adapt it to your rig"); a spec page's "gives you 40W" still counts
    if "→" in sentence or "»" in sentence or sentence.count("·") >= 2 or sentence.count("|") >= 2:
        return 0  # "related links" strips: arrows and dot/pipe separators
    capitalised = sum(1 for w in re.findall(r"[A-Za-z][\w']*", sentence) if w[0].isupper())
    if capitalised > len(words) * 0.5 and not spec_line:  # Title Case runs are menus, headings and tag lists
        return 0
    topic = len(set(words) & topic_words)
    gear = len(set(words) & _GEAR_WORDS) + len(models) + bool(re.search(r"\bDI\b", sentence))  # not Italian "di"
    names = _names(sentence, topic_words)
    used = len(set(words) & _USE_WORDS)
    if _QUALIFIER.search(sentence) and (topic or page_on_topic):
        return 6 + topic * 2 + gear
    if topic:
        # An album intro names the artist without gear; a name only counts as gear when something was used.
        enough = gear or (names and used)
    elif page_on_topic or (thread_on_topic and used):
        # "Their go-to gear: a Mesa Dual Rectifier and a Bad Cat", "JC-40: 40W stereo 2x10 combo". Names alone are
        # not enough: lyrics, album credits and band histories are full of them.
        enough = gear and gear + names >= 2
    else:
        enough = False
    return topic * 2 + min(gear, 4) + min(names, 2) + used if enough else 0


# Forum threads about an artist are mostly members describing their own rigs ("my SVT into an 8x10"), so on a
# forum a sentence only counts when it names the artist, and never when it's about the poster's own gear.
_FORUM = re.compile(r"(^|\.)(talkbass|thegearpage|gearspace|reddit|ultimate-guitar|seymourduncan|rig-talk|"
                    r"sevenstring|harmony-central|basschat|musicplayers|fractalaudio|line6)\.|^forums?\.|"
                    r"/(forums?|threads?|showthread|viewtopic|comments|community)\b", re.IGNORECASE)
_OWN_RIG = re.compile(r"\b(my|mine|i use|i run|i play|i've got|i have)\b", re.IGNORECASE)


def _is_forum(href: str) -> bool:
    parsed = urlparse(href)
    return bool(_FORUM.search((parsed.hostname or "").lower()) or _FORUM.search(parsed.path))


_TRAFILATURA = None  # trafilatura.extract once loaded, False when it isn't installed


def _trafilatura():
    """trafilatura's extract function, or None when the package is missing (research then uses _PageText).

    Imported on first use, like ddgs, so a server without it still starts and serves everything else."""
    global _TRAFILATURA
    if _TRAFILATURA is None:
        try:
            from trafilatura import extract
        except ImportError:
            logging.getLogger(__name__).warning(
                "trafilatura is not installed: web research uses the basic page parser (pip install -r requirements.txt)")
            extract = False
        _TRAFILATURA = extract
    return _TRAFILATURA or None


def _main_text(html: str, url: str = "", forum: bool = False) -> tuple[str, str]:
    """The page's main text, one block per line, and which extractor produced it ("trafilatura" or "parser")."""
    extract = _trafilatura()
    if extract:
        try:
            # Forum posts are what trafilatura calls comments: on a thread, the replies are the evidence.
            text = extract(html, url=url or None, output_format="txt", include_comments=forum, include_tables=True,
                           include_links=False, favor_recall=True) or ""
        except Exception:  # one odd page must not stop the research
            text = ""
        if text.strip():
            return text[:MAX_EXTRACTED_CHARS], "trafilatura"
    parser = _PageText()
    parser.feed(html)
    return "".join(parser.parts)[:MAX_EXTRACTED_CHARS], "parser"


def _split_sentences(block: str) -> list[str]:
    """Sentence-ish pieces for scoring only; returned evidence stays as coherent blocks."""
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", block) if part.strip()]
    return parts or ([block.strip()] if block.strip() else [])


def _content_blocks(text: str) -> list[str]:
    """Useful paragraph/list/spec blocks from the page's main text.

    We keep the extractor's line/paragraph boundaries instead of flattening the page into unrelated sentences.
    Sentences already seen are dropped, also inside a block (pages repeat a summary box; trafilatura repeats a
    forum reply it reads as both post and comment). Very long lines are broken into sentence windows so one giant
    block cannot consume the whole evidence budget.
    """
    blocks: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"\n+", text):
        block = re.sub(r"\s+", " ", raw).strip(" \t-*•")
        sentences = []
        for sentence in _split_sentences(block):
            if sentence.lower() not in seen:
                seen.add(sentence.lower())
                sentences.append(sentence)
        if not sentences:
            continue
        block = " ".join(sentences)
        if len(block) <= 900:
            blocks.append(block)
            continue
        chunk = ""
        for sentence in sentences:
            candidate = f"{chunk} {sentence}".strip()
            if chunk and len(candidate) > 700:
                blocks.append(chunk)
                chunk = sentence
            else:
                chunk = candidate
        if chunk:
            blocks.append(chunk)
    return blocks


def _scores(block: str, topic_words: set, page_on_topic: bool, thread_on_topic: bool, forum: bool) -> list[tuple[str, int]]:
    """Each sentence of a block with its score; on forums a poster's own rig scores 0."""
    return [(sentence, 0 if forum and _OWN_RIG.search(sentence) else
             _sentence_score(sentence, topic_words, page_on_topic, thread_on_topic=thread_on_topic))
            for sentence in _split_sentences(block)]


def _block_score(block: str, topic_words: set, page_on_topic: bool, *, thread_on_topic: bool = False,
                 forum: bool = False) -> int:
    """Relevance of one coherent content block, using sentence scores as signals rather than output units."""
    useful = sorted((score for _, score in _scores(block, topic_words, page_on_topic, thread_on_topic, forum) if score),
                    reverse=True)
    if not useful and not (forum and _OWN_RIG.search(block)):
        # Short structured lines can contain several specs separated by punctuation the extractor normalises away.
        direct = _sentence_score(block, topic_words, page_on_topic, thread_on_topic=thread_on_topic)
        useful = [direct] if direct else []
    if not useful:
        return 0
    score = useful[0] + sum(useful[1:3]) // 2
    if _QUALIFIER.search(block):
        score += 3
    if _DATES.search(block):
        score += 1
    return score


def _evidence_part(block: str, topic_words: set, page_on_topic: bool, thread_on_topic: bool, forum: bool) -> str:
    """The part of a chosen block worth quoting: its scoring sentences and those they depend on (who "he" is, a
    date before, a hedge after). Trafilatura can merge paragraphs, newsletter sign-ups and all, into one block."""
    scored = _scores(block, topic_words, page_on_topic, thread_on_topic, forum)
    sentences = [sentence for sentence, _ in scored]
    keep = {i for i, (_, score) in enumerate(scored) if score}
    if len(sentences) <= 1 or not keep:
        return block  # a single line, or a spec line that only scores as a whole
    for i in list(keep):
        if i > 0 and (_REFERS_BACK.match(sentences[i]) or _DATES.search(sentences[i - 1])):
            keep.add(i - 1)
        if i + 1 < len(sentences) and (_QUALIFIER.search(sentences[i + 1]) or _HEDGE.search(sentences[i + 1])):
            keep.add(i + 1)
    return " ".join(sentences[i] for i in sorted(keep) if not (forum and _OWN_RIG.search(sentences[i])))


def _context_part(block: str, forum: bool, *, before: bool) -> str:
    """From a neighbouring block, only what dates, qualifies or hedges the evidence; before it, else the sentence
    a "he" refers back to."""
    sentences = [s for s in _split_sentences(block) if not (forum and _OWN_RIG.search(s))]
    picked = [s for s in sentences if _DATES.search(s) or _QUALIFIER.search(s) or _HEDGE.search(s)]
    return " ".join(picked or (sentences[-1:] if before else []))


def _block_context(index: int, blocks: list[str], forum: bool) -> list[int]:
    """Neighbouring blocks that materially qualify or identify a selected block."""
    chosen: list[int] = []
    if index > 0:
        previous = blocks[index - 1]
        if not (forum and _OWN_RIG.search(previous)) and (
            _DATES.search(previous) or _QUALIFIER.search(previous) or _REFERS_BACK.match(blocks[index])
        ):
            chosen.append(index - 1)
    if index + 1 < len(blocks):
        following = blocks[index + 1]
        if not (forum and _OWN_RIG.search(following)) and (
            _QUALIFIER.search(following) or _HEDGE.search(following) or _DATES.search(following)
        ):
            chosen.append(index + 1)
    return chosen


def _extract_evidence(html: str, topic: str, forum: bool = False, url: str = "") -> str:
    """A few coherent, relevant passages of a page, on one line (research notes are one line per source).

    trafilatura decides what is main-page content (_main_text). This function only decides which local passages
    are useful for this request; the AI planner still decides what the evidence means.
    """
    text, _ = _main_text(html, url, forum)
    if not text.strip() or not _about_music(_page_title(html), text):
        return ""
    topic_words = _topic_words(topic)
    opening = set(re.findall(r"[a-z0-9']+", (_page_title(html) + " " + text[:3000]).lower()))
    on_topic = len(opening & topic_words) >= min(2, len(topic_words))
    page_on_topic, thread_on_topic = on_topic and not forum, forum and on_topic
    blocks = _content_blocks(text)
    scored = [(score, i) for i, block in enumerate(blocks)
              if (score := _block_score(block, topic_words, page_on_topic, thread_on_topic=thread_on_topic, forum=forum))]

    # Strongest passages first, returned in document order. A selected block may bring the date or qualifier from a
    # neighbouring block with it. This preserves local meaning without feeding whole articles to the model.
    parts: dict[int, str] = {}

    def size(extra: dict) -> int:
        return sum(len(part) + 1 for part in {**parts, **extra}.values())

    taken = 0
    for _, index in sorted(scored, key=lambda item: (item[0], -item[1]), reverse=True)[: EVIDENCE_BLOCKS * 2]:
        core = {index: _evidence_part(blocks[index], topic_words, page_on_topic, thread_on_topic, forum)}
        context = {j: part for j in _block_context(index, blocks, forum) if j not in parts
                   and (part := _context_part(blocks[j], forum, before=j < index))}
        if size({**context, **core}) <= EVIDENCE_CHARS:
            parts.update({**context, **core})
        elif size(core) <= EVIDENCE_CHARS:
            parts.update(core)  # keep the evidence even when its context doesn't fit
        else:
            continue
        taken += 1
        if taken >= EVIDENCE_BLOCKS:
            break
    return " ".join(parts[i] for i in sorted(parts))


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
    return _extract_evidence(html, topic, forum=_is_forum(href), url=href)


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
    bass = bool(_BASS.search(query))
    player = "bassist bass" if bass else "guitarist"  # a bass request must not find guitar rigs
    words = _search_words(query)
    instrument = " bass" if bass else ""  # not " guitar": "...pedal platform gigging guitar" found only pedalboards
    rig, specs = f"{words} {player} amp pedals gear used", f"{words}{instrument} specifications"
    needs = len(_NEEDS.findall(query))
    if needs >= 2:  # "a stereo combo for gigging": what products do, not whose rig
        queries = (specs, f"{words}{instrument} review")
    elif needs or any(not _NOT_MODEL.search(m) for m in _MODEL.findall(query)):  # "AC30": a rig or a product
        # "AC30 specifications" alone found a Panasonic AG-AC30 camcorder; "guitar" is safe here, unlike in the
        # needs search above.
        queries = (rig, f"{words}{instrument or ' guitar'} specifications")
    else:
        queries = (rig, f"{words} {player} interview amplifier gear")
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
            bass_site = any(host == site or host.endswith("." + site) for site in _BASS_SITES)
            if href and href not in seen and host not in hosts and not _skip_source(href) and (bass or not bass_site):
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
        snippet = re.sub(r"^[^·]{1,30}·\s*", "", snippet)  # the engine's date stamp: "1 day ago · ", "Jul 21, 2026 · "
        extract = re.sub(r"\s+", " ", extract or "").strip()  # one line per source: the AI and the page split on lines
        # A snippet is often a site's own blurb ("a community-built gear list for Gavin Rossdale"), so one of its
        # sentences must name something specific, not just gear words.
        if not extract and _about_music(title, snippet, short=True) and any(
                _sentence_score(part if part[-1:] in ".!?" else part + ".", topic_words) and _specific(part, topic_words)
                for part in _split_sentences(snippet)):
            extract = f"Search snippet only: {snippet}"  # the page itself couldn't be read: weaker evidence
        text = extract
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
    tones = cache.remember(cache_db, "search:" + json.dumps(params, sort_keys=True), fetch, cache.CATALOGUE_SECONDS,
                           event="catalogue")
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
