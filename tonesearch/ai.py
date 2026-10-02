"""AI tone brief, pack ranking and pack questions over an OpenAI-compatible chat API.

Providers (NAM_MIXER_AI_PROVIDER): "cloudflare" (Workers AI), "custom" (any OpenAI-compatible
endpoint, including OpenAI itself) or "local" (Ollama on this machine). Variable names match
NAM Mixer's settings so one set of values works in both.
"""
from __future__ import annotations

import ast
import json
import math
import os
import re
import socket
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tonesearch import overrides, research
from tonesearch.research import _is_safe_public_host, _NoRedirectHandler

MAX_RESPONSE_BYTES = 1_000_000

# AI tuning, named as in NAM Mixer: NAM_MIXER_AI_<NAME> or NAM_MIXER_AI_<PROVIDER>_<NAME>.
# (default, minimum, maximum): the caps protect this server, which holds a worker for every AI call,
# even when a visitor brings their own AI.
TUNING_LIMITS = {
    "MAX_TOKENS": (1_800, 64, 8_192),
    "TEMPERATURE": (0.3, 0.0, 2.0),
    "TIMEOUT_SECONDS": (90, 5, 180),
    "HISTORY_MESSAGES": (8, 0, 12),
    "HISTORY_MESSAGE_CHARS": (1_200, 100, 4_000),
    "RESEARCH_CHARS": (7_000, 0, 20_000),
    "MAX_REPLY_CHARS": (1_800, 200, 6_000),
    "MAX_EXPLANATION_CHARS": (900, 200, 4_000),
}


class AiError(RuntimeError):
    """Safe to show to the user."""


@dataclass(frozen=True)
class Tuning:
    max_tokens: int = 1_800
    temperature: float = 0.3
    timeout_seconds: int = 90
    history_messages: int = 8
    history_message_chars: int = 1_200
    research_chars: int = 7_000
    max_reply_chars: int = 1_800
    max_explanation_chars: int = 900


@dataclass(frozen=True)
class AiConfig:
    provider: str
    base_url: str
    model: str
    api_key: Optional[str]
    visitor: bool = False  # came from the visitor's Settings, not the server environment
    tuning: Tuning = Tuning()


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _scoped(provider: str, name: str) -> str:
    return _env(f"NAM_MIXER_AI_{provider.upper()}_{name}") or _env(f"NAM_MIXER_AI_{name}")


def _tuning(read, source: str) -> Tuning:
    """Read each tuning value with `read(NAME)`, clamping it to TUNING_LIMITS."""
    values = {}
    for name, (default, low, high) in TUNING_LIMITS.items():
        raw = read(name)
        if not raw:
            values[name.lower()] = default
            continue
        try:
            number = float(raw)
        except ValueError:
            number = math.nan
        if not math.isfinite(number):
            raise AiError(f"{source}: {name} must be a number")
        value = int(number) if isinstance(default, int) else number
        values[name.lower()] = min(max(value, low), high)
    return Tuning(**values)


def _visitor_config(provider: str) -> AiConfig:
    """Build config only from the visitor's values: never fall back to the server's secrets."""
    model = overrides.get("model")
    api_key = overrides.get("api_key") or None
    if not model:
        raise AiError("Settings: enter the AI model name")
    if provider == "cloudflare":
        account = overrides.get("account_id")
        if not re.fullmatch(r"[A-Fa-f0-9]{32}", account):
            raise AiError("Settings: the Cloudflare account ID must be 32 hexadecimal characters")
        if not api_key:
            raise AiError("Settings: enter your Cloudflare API token")
        return AiConfig(provider, f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1", model, api_key, True,
                        _tuning(lambda n: overrides.get(n.lower()), "Settings"))
    if provider == "custom":
        base_url = overrides.get("base_url").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise AiError("Settings: the AI base URL must start with https://")
        if not _is_safe_public_host(parsed.hostname):
            raise AiError("Settings: the AI base URL must be a public internet address")
        return AiConfig(provider, base_url, model, api_key, True, _tuning(lambda n: overrides.get(n.lower()), "Settings"))
    raise AiError("Settings: the AI provider must be Cloudflare or custom")


def config() -> AiConfig:
    visitor_provider = overrides.get("provider").lower()
    if visitor_provider:
        return _visitor_config(visitor_provider)
    provider = (_env("NAM_MIXER_AI_PROVIDER") or "cloudflare").lower()
    if provider not in {"cloudflare", "custom", "local"}:
        raise AiError("NAM_MIXER_AI_PROVIDER must be cloudflare, custom or local")
    model = _scoped(provider, "MODEL")
    if not model:
        raise AiError(f"No AI model configured (set NAM_MIXER_AI_{provider.upper()}_MODEL)")
    api_key = _scoped(provider, "API_KEY") or None
    if provider == "cloudflare":
        account = _scoped(provider, "ACCOUNT_ID")
        if not re.fullmatch(r"[A-Fa-f0-9]{32}", account):
            raise AiError("Cloudflare account ID must be 32 hexadecimal characters")
        if not api_key:
            raise AiError("Cloudflare API token is not configured")
        return AiConfig(provider, f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1", model, api_key,
                        tuning=_server_tuning(provider))
    base_url = (_scoped(provider, "BASE_URL") or "http://127.0.0.1:11434/v1").rstrip("/")
    parsed = urlparse(base_url)
    if provider == "local":
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise AiError("local AI URL must use http and point to localhost")
        # Local AI runs on the owner's own machine, so the Settings dialog's AI tuning applies to it too.
        tuning = _tuning(lambda name: overrides.get(name.lower()) or _scoped(provider, name), "AI tuning")
        return AiConfig(provider, base_url, model, None, tuning=tuning)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AiError("custom AI URL must use https")
    return AiConfig(provider, base_url, model, api_key, tuning=_server_tuning(provider))


def _server_tuning(provider: str) -> Tuning:
    return _tuning(lambda name: _scoped(provider, name), "Server setting NAM_MIXER_AI_*")


def server_is_local() -> bool:
    """True when this server is set up to use a local AI (NAM_MIXER_AI_PROVIDER=local): the owner's own machine."""
    return (_env("NAM_MIXER_AI_PROVIDER") or "").lower() == "local"


def local_model() -> str:
    """The local model's name when this server uses local AI, otherwise ''."""
    return _scoped("local", "MODEL") if server_is_local() else ""


def source() -> Optional[dict]:
    """Which AI answers this request, for labelling replies: local, the visitor's own, or this site's."""
    try:
        cfg = config()
    except AiError:
        return None
    kind = "visitor" if cfg.visitor else "local" if cfg.provider == "local" else "site"
    return {"source": kind, "model": cfg.model if kind != "site" else ""}  # the site's own model stays private


def is_configured() -> bool:
    try:
        config()
        return True
    except AiError:
        return False


# ---- Reading JSON from any model -------------------------------------------------------------
# Only some models support JSON mode, so replies are parsed defensively: JSON is found inside prose,
# code fences and <think> blocks, near-misses are repaired, and fields are normalised before validation.

def _strip_wrappers(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    if "</think>" in text.lower():  # reply that only closes its thinking block
        text = re.split(r"</think>", text, maxsplit=1, flags=re.IGNORECASE)[-1]
    fenced = re.search(r"```(?:json|javascript|js)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced and re.search(r"[\[{]", fenced.group(1)):
        text = fenced.group(1)
    elif text.lstrip().startswith("```"):  # an unclosed fence from a cut-off reply
        text = re.sub(r"^\s*```(?:json)?", "", text, flags=re.IGNORECASE)
    return text.strip()


def _closers(text: str) -> Optional[str]:
    """The brackets (and quote) needed to close a JSON prefix, or None if it is unbalanced the wrong way."""
    stack, in_string, escaped = [], False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack.pop() != char:
                return None
    return ('"' if in_string else "") + "".join(reversed(stack))


def _repair(text: str) -> object:
    """Fix common near-JSON: curly quotes, trailing commas, and replies cut off by the token limit."""
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    text = re.sub(r",\s*([}\]])", r"\1", text)
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON object in the reply")
    text = text[start:]
    cuts = [len(text)] + [m.start() for m in re.finditer(",", text)][::-1][:60]
    for cut in cuts:  # close the whole text, else back off to earlier complete items
        prefix = re.sub(r"[,:\s]+$", "", text[:cut])
        closers = _closers(prefix)
        if closers is None:
            continue
        try:
            return json.loads(prefix + closers)
        except json.JSONDecodeError:
            continue
    try:  # Python-style dicts ('single quotes', True/None)
        return ast.literal_eval(re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", text))))
    except (ValueError, SyntaxError):
        raise ValueError("the reply was not valid JSON") from None


def _decode(content: object) -> object:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        if all(isinstance(part, dict) and "text" in part for part in content):
            content = "".join(str(part.get("text", "")) for part in content)
        else:
            return content
    if not isinstance(content, str):
        raise ValueError("provider content was not text")
    text = _strip_wrappers(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    first = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    for match in re.finditer(r"[\{\[]", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        # An object anywhere, or a list only when it is the outermost value (not a list inside a cut-off object).
        if value and (isinstance(value, dict) or (isinstance(value, list) and match.start() == first)):
            return value
    return _repair(text)


def _example(schema: dict) -> object:
    """A compact example object for models without JSON mode to copy the shape from."""
    if "enum" in schema:
        return "|".join(schema["enum"])
    kind = schema.get("type")
    if kind == "object":
        return {key: _example(value) for key, value in schema.get("properties", {}).items()}
    if kind == "array":
        return [_example(schema.get("items", {}))]
    return 0 if kind == "integer" else "..."


def _unwrap(value: object, keys: set) -> object:
    """{"tone_plan": {...}} -> {...} when the expected keys are one level down."""
    while isinstance(value, dict) and not keys & value.keys() and len(value) == 1:
        inner = next(iter(value.values()))
        if not isinstance(inner, dict):
            break
        value = inner
    return value


def _alias(value: dict, name: str, *others: str) -> None:
    if name not in value:
        for other in others:
            if other in value:
                value[name] = value[other]
                return


def _text_list(items: object, limit: int) -> list:
    if isinstance(items, str):
        items = [part for part in re.split(r"\n+|;\s+|(?:^|\s)[-*•]\s+|\s\d+[.)]\s+", items) if part.strip()]
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, dict):
            item = next((item[k] for k in ("text", "tip", "query", "name", "value") if isinstance(item.get(k), str)), "")
        if isinstance(item, (str, int, float)) and str(item).strip():
            out.append(str(item).strip().lstrip("-*• ").strip())
    return out[:limit]


_KIND_SYNONYMS = {
    "amp": "amp", "amps": "amp", "amplifier": "amp", "amp head": "amp", "head": "amp", "combo": "amp",
    "effect": "effect", "effects": "effect", "pedal": "effect", "pedals": "effect", "fx": "effect", "stompbox": "effect",
    "guitar": "guitar", "guitars": "guitar", "bass": "guitar", "pickup": "pickup", "pickups": "pickup",
    "cab": "cab", "cabinet": "cab", "speaker": "cab", "speakers": "cab",
    "mic": "mic", "mics": "mic", "microphone": "mic", "microphones": "mic", "other": "other",
}


def _score(raw: object) -> Optional[int]:
    if isinstance(raw, str):
        match = re.search(r"-?\d+(?:\.\d+)?", raw)
        if not match:
            return None
        raw = float(match.group())
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    if isinstance(raw, float) and 0 < raw <= 1:  # 0.85 means 85%
        raw *= 100
    return max(0, min(100, round(raw)))


def _normalise(model, value: object) -> object:
    """Map the many shapes models actually return onto the fields each answer needs."""
    if model is _Ranking:
        if isinstance(value, dict):
            value = _unwrap(value, {"ranking"})
            _alias(value, "ranking", "rankings", "results", "packs", "scores", "ranked")
            ranking = value.get("ranking")
            if isinstance(ranking, dict):  # {"123": 85, ...}
                ranking = [{"id": k, "fit": v} for k, v in ranking.items()]
        else:
            ranking = value
        items = []
        for item in ranking if isinstance(ranking, list) else []:
            if not isinstance(item, dict):
                continue
            _alias(item, "id", "pack_id", "tone_id")
            _alias(item, "fit", "score", "match", "fit_score", "rating")
            _alias(item, "why", "reason", "explanation", "note")
            try:
                pack_id = int(str(item.get("id")).strip())
            except ValueError:
                continue
            fit = _score(item.get("fit"))
            if fit is not None:  # one bad entry no longer sinks the whole ranking
                items.append({"id": pack_id, "fit": fit, "why": str(item.get("why") or "")[:300]})
        return {"ranking": items[:40]}
    if not isinstance(value, dict):
        return value
    if model is _Plan:
        value = _unwrap(value, {"summary", "gear", "search_queries"})
        _alias(value, "summary", "tone_summary", "description", "explanation", "brief", "tone")
        _alias(value, "advice", "tips", "how_to", "steps")
        _alias(value, "gear", "equipment", "rig", "products")
        _alias(value, "search_queries", "queries", "searches", "search_terms")
        _alias(value, "aliases", "keywords", "names", "also_known_as", "identifiers")
        gear = []
        for item in value.get("gear") if isinstance(value.get("gear"), list) else []:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                continue
            _alias(item, "name", "model", "product", "title")
            _alias(item, "role", "purpose", "notes", "description", "use")
            name = str(item.get("name") or "").strip()
            if name:
                kind = _KIND_SYNONYMS.get(str(item.get("kind") or item.get("type") or "other").strip().lower(), "other")
                _alias(item, "confidence", "evidence", "certainty", "scope", "evidence_scope")
                gear.append({"kind": kind, "name": name[:80], "role": str(item.get("role") or "")[:240],
                             "confidence": confidence_of(item.get("confidence"))})
        summary = value.get("summary")
        return {**value, "summary": summary if isinstance(summary, str) else "", "gear": gear[:12],
                "advice": _text_list(value.get("advice"), 8), "search_queries": _text_list(value.get("search_queries"), 6),
                "aliases": _text_list(value.get("aliases"), 16),
                "requirements": _text_list(value.get("requirements") or value.get("constraints") or value.get("needs"), 12)}
    if model is _Aliases:
        value = _unwrap(value, {"aliases"})
        _alias(value, "aliases", "keywords", "names", "also_known_as", "identifiers", "other_names")
        return {"aliases": _text_list(value.get("aliases"), 16)}
    if model is _PackAnswer:
        value = _unwrap(value, {"reply"})
        _alias(value, "reply", "answer", "response", "text", "message")
        _alias(value, "recommended_files", "files", "recommended", "picks", "recommendations")
        return {**value, "recommended_files": _text_list(value.get("recommended_files"), 6)}
    return value


_FORMAT_TIERS = ("json_schema", "json_object", "none")
_FORMAT_TIER: dict = {}  # (provider, base_url, model) -> first response format that endpoint accepted

# Thinking models can spend minutes reasoning before they answer. Each hint asks them to think less, in the
# dialect of a different family of servers; an endpoint that rejects a hint just steps to the next one, and the
# first one it accepts is remembered. Models that do not think never get hints, so they are never at risk.
_HINT_TIERS = (
    {"reasoning_effort": "low", "chat_template_kwargs": {"enable_thinking": False}},  # OpenAI-style + vLLM/SGLang
    {"reasoning_effort": "low"},  # OpenAI, gpt-oss, Ollama, most hosted APIs
    {"chat_template_kwargs": {"enable_thinking": False}},  # vLLM/SGLang (Qwen3, GLM, DeepSeek chat templates)
    {},
)
_HINT_TIER: dict = {}  # endpoint key -> index into _HINT_TIERS that the endpoint accepted
_THINKS: set = set()  # endpoint keys whose replies included reasoning
_THINKING_NAMES = re.compile(r"glm-4\.[5-9]|glm-z1|qwen3|qwq|deepseek-r1|r1-distill|gpt-oss|reason|think|magistral|"
                             r"phi-4-reasoning|\bo[134](-mini)?\b|gpt-5|kimi-k2-thinking", re.IGNORECASE)
_NO_STREAM: set = set()  # endpoint keys that refused stream=true


@dataclass(frozen=True)
class _Reply:
    content: object
    reasoning: str
    finish: str


def _thinks(key: tuple) -> bool:
    return key in _THINKS or bool(_THINKING_NAMES.search(key[2]))


def _read_stream(response, *, deadline: Optional[float], started: float) -> _Reply:
    """Collect a streamed (server-sent events) reply. Each read waits at most the socket timeout, so a model that
    keeps producing text never times out; only the overall deadline ends a slow one."""
    content, reasoning, finish, size = [], [], "", 0
    while True:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _OutOfTime(bool(content), bool(reasoning), time.monotonic() - started)
            _limit_wait(response, left)
        try:
            line = response.readline()
        except (TimeoutError, socket.timeout):
            if deadline is not None and time.monotonic() >= deadline - 0.5:
                raise _OutOfTime(bool(content), bool(reasoning), time.monotonic() - started) from None
            raise
        if not line:
            break
        size += len(line)
        if size > MAX_RESPONSE_BYTES:
            raise AiError("AI provider response was too large")
        line = line.decode("utf-8", errors="ignore").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("error"):
            raise AiError(f"The AI provider stopped with an error: {str(event['error'])[:200]}")
        for choice in (event.get("choices") or []) if isinstance(event, dict) else []:
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            thought = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(thought, str):
                reasoning.append(thought)
            finish = str(choice.get("finish_reason") or finish)
    return _Reply("".join(content) or None, "".join(reasoning), finish)


def _limit_wait(response, seconds: float) -> None:
    """Shorten the socket's wait so a stalled stream cannot run past the deadline (best effort: urllib internals)."""
    try:
        sock = response.fp.raw._sock
        current = sock.gettimeout()
        if current is None or seconds < current:
            sock.settimeout(max(0.5, seconds))
    except AttributeError:
        pass


class _OutOfTime(Exception):
    def __init__(self, answering: bool, thinking: bool, seconds: float):
        super().__init__("out of time")
        self.answering, self.thinking, self.seconds = answering, thinking, seconds


def _timeout_message(key: tuple, seconds: float, *, answering: bool = False, thinking: bool = False,
                     silent: bool = False) -> str:
    model = key[2]
    if silent:
        return (f"The AI provider did not respond for {seconds:.0f} seconds ({model}). It may be busy or overloaded: "
                "try again, or choose a smaller or faster model.")
    if thinking and not answering:
        return (f"{model} was still thinking after {seconds:.0f} seconds and had not started its answer. Thinking "
                "models can be very slow; try again, or choose a model that does not think (for example "
                "@cf/meta/llama-3.3-70b-instruct-fp8-fast).")
    return (f"{model} was too slow to finish its answer in {seconds:.0f} seconds. Try again, or choose a faster "
            "model.")


def _chat(cfg: AiConfig, messages: list, *, schema_name: str, schema: dict, max_tokens: int,
          temperature: float, opener=urlopen, deadline: Optional[float] = None) -> _Reply:
    key = (cfg.provider, cfg.base_url, cfg.model)
    if cfg.visitor and opener is urlopen:
        opener = build_opener(_NoRedirectHandler).open  # a visitor's URL must not redirect us inward
    started = time.monotonic()

    def send(tier: str, hints: dict, stream: bool) -> _Reply:
        payload = {"model": cfg.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, **hints}
        if stream:
            payload["stream"] = True
        if tier == "json_schema":
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": False}}
        elif tier == "json_object":
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        request = Request(f"{cfg.base_url}/chat/completions", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        # The socket timeout limits each wait for data, not the whole reply; the deadline limits the whole reply.
        wait = cfg.tuning.timeout_seconds
        if deadline is not None:
            wait = max(1.0, min(wait, deadline - time.monotonic()))
        with opener(request, timeout=wait) as response:
            headers = getattr(response, "headers", None)
            if "event-stream" in str(headers.get("Content-Type", "") if headers is not None else ""):
                return _read_stream(response, deadline=deadline, started=started)
            raw = response.read(MAX_RESPONSE_BYTES + 1)  # the endpoint ignored stream=true, or we did not ask
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AiError("AI provider response was too large")
        choice = json.loads(raw.decode("utf-8"))["choices"][0]
        message = choice.get("message") or {}
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        return _Reply(message.get("content"), reasoning if isinstance(reasoning, str) else json.dumps(reasoning),
                      str(choice.get("finish_reason") or ""))

    tier = _FORMAT_TIER.get(key, 0)
    hint_tier = _HINT_TIER.get(key, 0) if _thinks(key) else len(_HINT_TIERS) - 1
    stream = key not in _NO_STREAM
    while True:
        try:
            reply = send(_FORMAT_TIERS[tier], _HINT_TIERS[hint_tier], stream)
        except HTTPError as exc:
            detail = _error_detail(exc)
            if exc.code in (400, 422):
                said = detail.lower()
                if stream and "stream" in said:
                    stream = False
                    _NO_STREAM.add(key)
                    continue
                hinted = hint_tier < len(_HINT_TIERS) - 1
                about_format = "response_format" in said or "json" in said or "schema" in said
                if hinted and not about_format:  # an unknown thinking hint: try the next dialect, then none
                    hint_tier += 1
                    continue
                if tier < len(_FORMAT_TIERS) - 1:  # format not supported: step down
                    tier += 1
                    continue
                if hinted:
                    hint_tier += 1
                    continue
            raise AiError(_provider_refusal(exc, detail)) from exc
        except _OutOfTime as late:
            raise AiError(_timeout_message(key, late.seconds, answering=late.answering, thinking=late.thinking)) from None
        except (TimeoutError, socket.timeout) as exc:
            raise AiError(_timeout_message(key, time.monotonic() - started, silent=True)) from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise AiError(_timeout_message(key, time.monotonic() - started, silent=True)) from exc
            raise
        _FORMAT_TIER[key] = tier
        if reply.reasoning:
            _THINKS.add(key)
        if _thinks(key):
            _HINT_TIER[key] = hint_tier
        return reply


def _error_detail(exc: HTTPError) -> str:
    """The provider's own explanation from an error response (never includes our key)."""
    try:
        body = json.loads(exc.read(20_000).decode("utf-8", errors="ignore") or "{}")
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message") or "")  # Cloudflare
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            return str(body["error"].get("message") or "")  # OpenAI-style
        if isinstance(body, dict):
            return str(body.get("error") or body.get("message") or "")
    except Exception:
        pass
    return ""


def _provider_refusal(exc: HTTPError, detail: str = "") -> str:
    """A readable error from the provider's own explanation (never includes our key)."""
    hint = {
        401: "The API token was not accepted: check it was copied in full.",
        403: "Check the API token has the Workers AI permission (the 'Workers AI' template), that it belongs to the same "
             "account as the Account ID, and that the model name is right.",
        404: "Check the model name and the account ID or base URL.",
        429: "The provider's rate limit or daily allowance was reached; try again later.",
    }.get(exc.code, "")
    return " ".join(part for part in (f"The AI provider refused the request (HTTP {exc.code}){': ' + detail[:200] if detail else ''}.", hint) if part)


def _answer_text(reply: _Reply) -> object:
    """The model's answer, or a clear error when a thinking model used every token before answering."""
    content = reply.content
    if isinstance(content, str) and _strip_wrappers(content):
        return content
    if content not in (None, "", []):
        return content
    if "{" in reply.reasoning:  # some providers put the final JSON in the reasoning field
        return reply.reasoning
    if reply.reasoning or reply.finish == "length":
        raise AiError("The model used its whole token budget thinking and never answered. Raise Max tokens in "
                      "Settings > AI tuning (or NAM_MIXER_AI_MAX_TOKENS), or choose a model that does not think.")
    raise ValueError("the model returned an empty answer")


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Gear(_Model):
    kind: str = "other"
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(default="", max_length=240)
    confidence: str = "close"


class _Plan(_Model):
    summary: str = Field(min_length=1)  # trimmed to MAX_EXPLANATION_CHARS
    advice: List[str] = Field(default_factory=list, max_length=8)
    gear: List[_Gear] = Field(default_factory=list, max_length=12)
    search_queries: List[str] = Field(default_factory=list, max_length=6)
    aliases: List[str] = Field(default_factory=list, max_length=16)
    requirements: List[str] = Field(default_factory=list, max_length=12)


class _Rank(_Model):
    id: int
    fit: int = Field(ge=0, le=100)
    why: str = Field(default="", max_length=300)


class _Ranking(_Model):
    ranking: List[_Rank] = Field(default_factory=list, max_length=40)


class _PackAnswer(_Model):
    reply: str = Field(min_length=1)  # trimmed to MAX_REPLY_CHARS
    recommended_files: List[str] = Field(default_factory=list, max_length=6)


_GEAR_KINDS = {"amp", "effect", "guitar", "pickup", "cab", "mic", "other"}
# How well this gear answers the request, one scale for every kind of request:
# best = documented for this recording or era, or meets every stated requirement;
# close = the artist's gear from another or unknown era, or misses one requirement;
# alternative = a substitute, modern equivalent, replica, different approach or guess.
# Unlabelled gear, and the older names (confirmed, artist, suggested), map onto it.
CONFIDENCE = ("best", "close", "alternative")
CONFIDENCE_LABELS = {"best": "Best match", "close": "Close match", "alternative": "Alternative"}
# Compatibility only: models that ignore the schema, and older saved plans and library rows, use other words.
_CONFIDENCE_SYNONYMS = {word: level for level, words in {
    "best": ("best", "confirmed", "recording_confirmed", "documented", "high", "certain", "verified",
             "live_era_confirmed", "requirement_match", "confirmed_spec", "meets"),
    "close": ("close", "artist", "artist_general", "artist_era_confirmed", "medium", "likely", "probable",
              "partial", "partial_match"),
    "alternative": ("alternative", "suggested", "speculative", "low", "guess", "modern_equivalent", "genre_typical",
                    "possible", "unconfirmed", "alternative_architecture"),
}.items() for word in words}
# Words that don't show an alias names something in the request.
_COMMON_WORDS = {"the", "and", "of", "a", "an", "in", "on", "tone", "sound", "guitar", "bass", "amp", "rig"}


def confidence_of(value) -> str:
    """A gear item's confidence level, "close" when it's missing or unrecognised."""
    return _CONFIDENCE_SYNONYMS.get(str(value or "").strip().lower(), "close")

_STYLE = (
    "You are an experienced guitar and bass tone advisor for players who use Neural Amp Modeler (NAM) "
    "captures from the TONE3000 catalogue. Be concrete and practical. Never invent facts about a "
    "specific artist's rig: when research notes are supplied, prefer them and say when something is "
    "uncertain. Only mention effects when the research or the well-documented history of the tone "
    "actually involves them. Plain text only inside JSON strings, no markdown."
)
_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "advice": {"type": "array", "items": {"type": "string"}},
        "gear": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": sorted(_GEAR_KINDS)},
            "name": {"type": "string"}, "role": {"type": "string"},
            "confidence": {"type": "string", "enum": list(CONFIDENCE)}}, "required": ["kind", "name", "confidence"]}},
        "search_queries": {"type": "array", "items": {"type": "string"}},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "requirements": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "advice", "gear", "search_queries"],
}
_RANK_SCHEMA = {
    "type": "object",
    "properties": {"ranking": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "integer"}, "fit": {"type": "integer"}, "why": {"type": "string"}},
        "required": ["id", "fit", "why"]}}},
    "required": ["ranking"],
}
_PACK_SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}, "recommended_files": {"type": "array", "items": {"type": "string"}}},
    "required": ["reply", "recommended_files"],
}


def _history(history: Optional[list], tuning: Tuning) -> list:
    recent = (history or [])[-tuning.history_messages:] if tuning.history_messages else []
    return [{"role": item["role"], "content": item["content"][:tuning.history_message_chars]}
            for item in recent
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)]


def _ask(cfg: AiConfig, user: str, schema_name: str, schema: dict, model, *, history: Optional[list] = None,
         opener=urlopen, text_fallback: Optional[str] = None, deadline: Optional[float] = None):
    """Ask for one JSON answer. `text_fallback` names a field that a plain-prose reply can fill instead."""
    tuning = cfg.tuning
    shape = json.dumps(_example(schema), ensure_ascii=False)
    prompt = f"{user}\n\nReply with ONLY one JSON object in exactly this shape, no markdown and no commentary:\n{shape}"
    messages = [{"role": "system", "content": _STYLE}, *_history(history, tuning), {"role": "user", "content": prompt}]
    last_error: Optional[Exception] = None
    for attempt in range(2):
        text = None
        try:
            reply = _chat(cfg, messages, schema_name=schema_name, schema=schema, max_tokens=tuning.max_tokens,
                          temperature=tuning.temperature if attempt == 0 else min(tuning.temperature, 0.1),
                          opener=opener, deadline=deadline)
            text = _answer_text(reply)
            return model.model_validate(_normalise(model, _decode(text)))
        except (ValueError, ValidationError, KeyError, TypeError, IndexError) as exc:
            last_error = exc
            if deadline is not None and deadline - time.monotonic() < 5:
                break  # no time left for a second attempt
            if text_fallback and isinstance(text, str) and len(_strip_wrappers(text)) > 20 and "{" not in text:
                return model.model_validate({text_fallback: _strip_wrappers(text)})  # prose answer: still useful
            messages = [*messages[:-1], {"role": "user", "content": prompt + "\n\nYour previous reply could not be read. "
                                          "Return ONLY the JSON object, starting with { and ending with }."}]
        except AiError:
            raise
        except Exception as exc:
            raise AiError(f"AI provider request failed: {exc}") from exc
    raise AiError(f"The AI returned an answer that could not be read ({last_error}). Please try again.")


# Pedals whose names small models mistake for amps, usually because the brand also makes amps. This only
# relabels an item, never removes one; the prompt says the same, but small models still file a Shredmaster as an amp.
_KNOWN_PEDALS = re.compile(
    r"shred ?master|guv'?nor|drive ?master|jackhammer|\brat\b|"
    r"tube ?screamer|\bts ?-?(808|9|10)\b|\bds-?[12]\b|\bsd-?1\b|big ?muff|fuzz ?face|klon|centaur|"
    r"\bpedal\b|stomp", re.IGNORECASE)
# Electric guitars aren't captured on TONE3000, so searches for gear the model calls a guitar are skipped unless the
# player asks for guitar captures.
_WANTS_GUITAR = re.compile(r"acoustic|classical|nylon|guitar (models?|captures?|sims?)|piezo|\bdi\b|direct input",
                           re.IGNORECASE)
# A category is not a product: "Compressor", "Overdrive pedal" or "fuzz distortion" gives the player nothing to find
# or search for. A name is generic when every word is one of these; any other word ("Fuzz Factory") makes it a product.
_GENERIC_WORDS = set(
    "a an the of with and or clean high low gain tube valve solid state vintage modern british american bass guitar "
    "electric amp amps amplifier amplifiers head combo cab cabinet speaker speakers compressor overdrive "
    "distortion fuzz boost booster delay reverb chorus flanger phaser tremolo vibrato wah eq equaliser equalizer noise "
    "gate octave pitch shifter looper tuner preamp di box pedal pedals unit device effect effects model modeler "
    "modeller style type channel channels mic mics microphone microphones condenser dynamic ribbon".split())
# Not "acoustic simulator": that is a kind of capture TONE3000 has (Boss AC-3, acoustic sims), so a fair search.


def _generic(name: str) -> bool:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return all(w in _GENERIC_WORDS for w in words)  # also true for an empty name


_FORMAT_WORDS = {"amp", "amplifier", "head", "combo", "the"}
# An item's role that calls it a stand-in: the label can't then say best or close.
_STAND_IN = re.compile(r"\b(alternative|substitute|stand-in|modern equivalent|replica|instead of)\b", re.IGNORECASE)


def _same_gear(a: str, b: str) -> bool:
    """True when one name is the other with words left out: 'Deluxe Reverb' and 'Fender Deluxe Reverb', not
    'Fender Twin Reverb'."""
    words_a, words_b = (set(re.findall(r"[a-z0-9]+", n.lower())) - _FORMAT_WORDS for n in (a, b))
    return bool(words_a and words_b) and (words_a <= words_b or words_b <= words_a)


_ALIAS_RULE = (
    "ONLY if the request names an artist, band, song or album: up to 5 other names someone might type to ask for THIS "
    "same rig: the player's name and nickname, the band, the song, the album, the era or year (e.g. for 'Periphery "
    "bass': 'Nolly', 'Adam Getgood', 'Periphery'). Only names that research or well-known facts support. Never "
    "bandmates, other players, or a label, church or collective the player was part of ('Bethel Music'): they have "
    "their own rigs. Never genres or styles ('djent', 'fusion', 'progressive metal'), sound descriptions or gear.")


def filter_aliases(request: str, aliases: list, *, evidence: str = "", gear: list = (), needs: bool = False) -> list:
    """The aliases worth filing research under: each must share a word with the request or be named in the evidence
    (research notes and the plan's own words), and must not be gear or a category. A request for needs keeps none
    unless one names something in it.

    Aliases file this research under other names for later visitors, so a wrong one spreads. Don't require them to
    repeat the request: the prompt asks for other names ('Muse', 'Matt Bellamy' for "Knights of Cydonia")."""
    asked = set(re.findall(r"[a-z0-9]+", request.lower()))
    items = [(g.get("name", ""), g.get("role", "")) if isinstance(g, dict) else (g.name, g.role) for g in gear]
    known = set(re.findall(r"[a-z0-9]+", " ".join([evidence, *(f"{n} {r}" for n, r in items)]).lower()))

    def in_request(alias: str) -> bool:
        return bool(set(re.findall(r"[a-z0-9]+", alias.lower())) & asked - _COMMON_WORDS)

    def supported(alias: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", alias.lower())) - {"the", "and", "of", "a", "an"}
        return in_request(alias) or bool(words) and words <= known

    def names_gear(alias: str) -> bool:
        # 'Ibanez TQM2' or 'Telecaster' would hand this answer to a search for that gear; the player's name in their
        # own signature product ('Nolly' in 'Dingwall NG-3 Nolly Signature') stays.
        words = set(re.findall(r"[a-z0-9]+", alias.lower()))
        for name, role in items:
            if not name or not words <= set(re.findall(r"[a-z0-9]+", name.lower())):
                continue
            if re.search(r"\d", alias) or name.split()[0].lower() in words:
                return True  # a model ('AC30') or the maker's own name for it ('Ibanez TQM2')
            if not (in_request(alias) or "signature" in f"{name} {role}".lower()):
                return True  # 'Telecaster'; but the player's name in their own gear ('Eric Clapton') stays
        return False

    if needs and not any(in_request(a) for a in aliases):
        return []  # for "clean pedal platform amp cab gigging" a model listed "beatles"
    kept = [a.strip()[:60] for a in aliases if isinstance(a, str) and a.strip()]
    return list(dict.fromkeys(a for a in kept if not _generic(a) and not names_gear(a) and supported(a)))[:5]


class _Aliases(_Model):
    aliases: List[str] = Field(default_factory=list, max_length=16)


_ALIASES_SCHEMA = {"type": "object", "properties": {"aliases": {"type": "array", "items": {"type": "string"}}},
                   "required": ["aliases"]}


def suggest_aliases(topic: str, notes: str, gear: list, *, opener=urlopen, deadline: Optional[float] = None) -> list:
    """Other names for a saved research topic, for entries saved without them (scripts/backfill_aliases.py)."""
    cfg = config()
    notes = _whole_lines(notes, cfg.tuning.research_chars)
    gear_lines = "\n".join(f"- {g.get('name', '')}: {g.get('role', '')}" for g in gear if isinstance(g, dict))
    user = (f"Player request: {topic.strip()}\n\n"
            + (f"Web research notes (may be partial or noisy):\n{notes}\n\n" if notes else "")
            + (f"Gear found for it:\n{gear_lines}\n\n" if gear_lines else "")
            + f"Return JSON with aliases: {_ALIAS_RULE} If the request names no artist, band, song or album, return [].")
    answer = _ask(cfg, user, "aliases", _ALIASES_SCHEMA, _Aliases, opener=opener, deadline=deadline)
    return filter_aliases(topic, answer.aliases, evidence=notes, gear=gear,
                          needs=len(research._NEEDS.findall(topic)) >= 2)


def _whole_lines(text: str, limit: int) -> str:
    """`text` cut to `limit` characters at a line break, so no source loses its link mid-line."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit + 1)
    return text[:cut] if cut > 0 else text[:limit]


def plan_tone(prompt: str, *, research_notes: str = "", history: Optional[list] = None, opener=urlopen,
              deadline: Optional[float] = None) -> dict:
    cfg = config()
    research_notes = _whole_lines(research_notes, cfg.tuning.research_chars)
    user = (
        f"Player request: {prompt.strip()}\n\n"
        + (f"Web research notes (may be partial or noisy):\n{research_notes}\n\n" if research_notes else "")
        + "Return JSON with:\n"
        "- summary: 2-4 sentences describing the tone in plain words (gain, EQ, feel, era).\n"
        "- advice: 3-6 short practical tips (amp settings, playing, guitar/pickup choice, and effects only if relevant).\n"
        "If the request is about a BASS tone, describe the bass sound and list only the bassist's gear (bass amps, "
        "bass preamps, DIs and pedals such as a Darkglass or SansAmp, the bass itself): never the band's guitar rig. "
        "A bass sound described as distorted highs over clean lows usually means a split or parallel chain: say so.\n"
        "- requirements: if the request states needs rather than (or as well as) a sound, list the explicit and "
        "implied ones, hard ones first (e.g. 'combo with built-in speakers', 'stereo', 'loud enough to gig', 'clean "
        "headroom for pedals'); else []. Then gear is the candidate products: check each against every requirement "
        "and leave out any that breaks a hard one (a head when a combo was asked for, mono when stereo is required). "
        "A 'best amps' list is not evidence that a product meets a requirement; a maker's specs are. Don't list "
        "pedals, speakers or guitars unless the request asks for them.\n"
        "- gear: the specific products that define this tone, each with kind, name, a short role and confidence:\n"
        "  'best' = a source documents it for this recording, album or era, or it meets every requirement; 'close' = "
        "the player's gear from another era or with no era given (including a site's whole-career gear list), or it "
        "misses one requirement (say which in its role); 'alternative' = a modern equivalent, a replica, typical of "
        "the genre, a different approach (an amp simulator instead of a combo) or your inference. Never call gear "
        "'best' because it is famous, and never turn a site's 'modern equivalent' into the original. A maker's page "
        "for a signature product proves the partnership, not that it was used on recordings made before it existed. "
        "List only gear that helps: two well-supported items beat six weak ones.\n"
        "  Name only as much as the source supports: 'Vox AC30', not an AC30 variant the source doesn't name, and never "
        "build a name from separate facts ('Laney Lionheart' and '60 watts' is not a 'Laney Lionheart 60-watt'). Don't "
        "name a pedal model just because the tone has that effect: put 'dotted-eighth delay' or 'compressor' in advice, "
        "not gear. Use real "
        "make and model names (for example 'Fender Vibroverb', 'Ibanez TS808 Tube Screamer', 'Fender Stratocaster'), "
        "never generic categories like 'tube amplifier' or 'overdrive pedal'. If the request names an artist, song "
        "or album, list that player's documented gear for it.\n"
        "  Classify kind by what the product IS, not by its brand: a stompbox is an effect even from an amp maker "
        "(Marshall Shredmaster, Marshall Guv'nor, Marshall Bluesbreaker pedal, Boss DS-1, ProCo RAT, Ibanez Tube Screamer); "
        "a microphone is 'mic'. "
        "Only list gear a source says THIS artist used: a forum member describing their own rig, or a site "
        "recommending gear, is not the artist's gear.\n  "
        "Research notes can disagree: trust what the player said in an interview or a documented rig rundown over "
        "a tone-settings site's catalogue claims or averaged EQ settings, and include every amp the player names.\n  "
        "When research names a specific product, use exactly that product and never swap in a different, better-known "
        "one or a category ('ZVEX Fuzz Factory', not 'distortion device'); if it is uncertain, still name the researched "
        "one and say so in its role. Ignore gear that a tone-settings site recommends for recreating the sound today "
        "(modelling or practice amps such as a Fender Mustang, Boss Katana or Positive Grid Spark) unless the player asks "
        "for budget or modern gear; but a modeller a source says the artist plays (an Axe-Fx, Kemper or Helix rig) is "
        "their real gear.\n"
        f"- aliases: {_ALIAS_RULE}\n"
        "- search_queries: 1-3 SHORT TONE3000 catalogue searches for the kind of capture the player wants. Usually that "
        "is the amp (make/model or amp family, e.g. 'Marshall JCM800', 'Fender Deluxe Reverb', 'Vox AC30'), matching "
        "the amps in gear. But TONE3000 also has captures of pedals, preamps, outboard gear, bass rigs and acoustic "
        "guitars/acoustic simulators: when the player asks for one of those (for example 'acoustic guitar models rather "
        "than amps', 'just the pedal', 'bass DI'), search for that instead (e.g. 'Martin D-28', 'acoustic simulator', "
        "'Boss AC-3', 'Ampeg SVT') and do NOT search for amps they did not ask for. When the player wants to change how "
        "a signal sounds (a DI or piezo guitar to sound miked, an electric to sound acoustic), search for captures of the "
        "result: the instrument it should sound like ('classical guitar', 'nylon', 'Takamine DH90') or 'acoustic "
        "simulator'. Microphones, interfaces and recording gear are what captures are made WITH, never what they capture: "
        "never search for them, even when the request names one. Always include the model, never a "
        "bare brand like 'Marshall'. No adjectives, no artist names. Add an effect search only when that pedal is what "
        "creates the core sound."
    )
    plan = _ask(cfg, user, "tone_plan", _PLAN_SCHEMA, _Plan, history=history, opener=opener, deadline=deadline)
    for gear in plan.gear:
        gear.confidence = confidence_of(gear.confidence)
        if gear.confidence != "alternative" and _STAND_IN.search(gear.role or ""):
            gear.confidence = "alternative"  # its own role says so: "Alternative high-end studio microphone" | best
        if gear.kind == "amp" and _KNOWN_PEDALS.search(gear.name):
            gear.kind = "effect"
    plan.gear = [g for g in plan.gear if not _generic(g.name)]
    queries = [q.strip()[:80] for q in plan.search_queries if q and not _generic(q)]
    # Recording gear is never what a capture captures; a guitar only is when the player wants guitar captures.
    uncapturable = ("mic",) if _WANTS_GUITAR.search(prompt) else ("mic", "guitar", "pickup")
    unsearched = [g.name for g in plan.gear if g.kind in uncapturable]
    queries = [q for q in queries if not any(_same_gear(q, name) for name in unsearched)]
    # Every query stays eligible, but searches for alternatives go last, so the limit drops those first.
    alternatives = [g.name for g in plan.gear if g.confidence == "alternative"]
    stand_ins = [q for q in queries if any(_same_gear(q, name) for name in alternatives)]
    queries = [q for q in queries if q not in stand_ins]
    # Spare slots go to the best and close amps that no query covers yet (alternatives only when nothing else).
    amps = [g for g in plan.gear if g.kind == "amp"]
    for amp in [g for g in amps if g.confidence != "alternative"] or amps:
        if not any(_same_gear(amp.name, q) for q in queries + stand_ins):
            queries.append(amp.name.strip()[:80])
    queries = list(dict.fromkeys(queries + stand_ins))
    aliases = filter_aliases(prompt, plan.aliases, gear=plan.gear, needs=bool(plan.requirements),
                             evidence=" ".join([research_notes, plan.summary, *plan.advice]))
    return {
        "summary": plan.summary.strip()[:cfg.tuning.max_explanation_chars],
        "advice": [tip.strip() for tip in plan.advice if tip.strip()][:6],
        "gear": [{**g.model_dump(), "kind": g.kind if g.kind in _GEAR_KINDS else "other"} for g in plan.gear][:10],
        "search_queries": queries[:3],
        "aliases": aliases,
        "requirements": [r.strip()[:120] for r in plan.requirements if r.strip()][:8],
    }


def gear_summary(plan: dict) -> str:
    """The plan's summary plus its gear by confidence, so ranking favours captures of the best matches."""
    groups = {level: [g["name"] for g in plan.get("gear") or [] if confidence_of(g.get("confidence")) == level]
              for level in CONFIDENCE}
    lines = [f"{CONFIDENCE_LABELS[level]}: {', '.join(names)}." for level, names in groups.items() if names]
    if plan.get("requirements"):
        lines.insert(0, "Requirements for the real gear: " + "; ".join(plan["requirements"]) + ". A capture is only "
                        "the sound, so ignore requirements about the physical box (combo or head, built-in or stereo "
                        "speakers, weight, size, portability): score packs below 40 only when they break one that "
                        "changes the sound (clean headroom, gain range, voicing, bass or guitar).")
    return " ".join([plan.get("summary", ""), *lines]).strip()


def rank_packs(prompt: str, summary: str, packs: list, *, opener=urlopen, deadline: Optional[float] = None) -> dict:
    if not packs:
        return {}
    lines = [json.dumps({"id": p["id"], "title": p.get("title", ""), "gear": p.get("gear"),
                         "tags": ", ".join(p.get("tags") or []), "description": (p.get("description") or "")[:280]})
             for p in packs[:12]]
    user = (
        f"Player request: {prompt.strip()}\nTone summary: {summary}\n\nCandidate TONE3000 packs (one JSON per line):\n"
        + "\n".join(lines)
        + "\n\nScore EVERY candidate with fit 0-100 using this scale, and give why in one short sentence that names "
        "the deciding detail:\n"
        "Prefer captures of the best-match gear, then close matches; alternatives are only stand-ins.\n"
        "Judge what each pack CAPTURES (its gear type, title and makes), not every product its description mentions: "
        "the microphone, interface or guitar used to make a capture is not what was captured, so a speaker cabinet "
        "recorded with the requested microphone is still a speaker cabinet.\n"
        "- 90-100: the same product (or the exact rig) the request calls for.\n"
        "- 70-89: the same model family, or a very close substitute that will get the player there.\n"
        "- 40-69: a plausible alternative that needs tweaking.\n"
        "- 10-39: related but clearly different (wrong era, gain range or voicing).\n"
        "- 0-9: the wrong kind of capture entirely (for example an electric amp when they asked for an acoustic "
        "guitar, or a pedal when they asked for a full rig).\n"
        "Use the whole scale (e.g. 83, 64), not just round numbers. Use only the ids given."
    )
    ranking = _ask(config(), user, "pack_ranking", _RANK_SCHEMA, _Ranking, opener=opener, deadline=deadline)
    known = {p["id"] for p in packs}
    return {r.id: {"fit": r.fit, "why": r.why.strip()} for r in ranking.ranking if r.id in known}


def match_file(recommended: str, file_names: list) -> Optional[str]:
    """Exact name, or a shortened one ('EDGY') that identifies exactly one real file."""
    wanted = recommended.strip().lower()
    if not wanted:
        return None
    exact = [n for n in file_names if n.lower() == wanted]
    if exact:
        return exact[0]
    partial = [n for n in file_names if wanted in n.lower()]
    return partial[0] if len(partial) == 1 else None


def ask_about_pack(question: str, pack: dict, file_names: list, *, tone_goal: str = "",
                   history: Optional[list] = None, opener=urlopen, deadline: Optional[float] = None) -> dict:
    files = "\n".join(f"- {name}" for name in file_names[:80])
    user = (
        (f"The player is looking for this tone: {tone_goal.strip()}\n\n" if tone_goal.strip() else "")
        + f"TONE3000 pack: {pack.get('title', '')} by {pack.get('creator', '')}\n"
        f"Tags: {', '.join(pack.get('tags') or [])}\nDescription: {(pack.get('description') or '')[:600]}\n"
        f"NAM files in this pack:\n{files}\n\nQuestion: {question.strip()}\n\n"
        "Answer the question using the pack information. File names often encode gain, channel or mic "
        "settings; explain what they suggest and say when you are inferring. In recommended_files list "
        "only exact file names from the list above that you recommend (can be empty)."
    )
    cfg = config()
    answer = _ask(cfg, user, "pack_answer", _PACK_SCHEMA, _PackAnswer, history=history, opener=opener, text_fallback="reply",
                  deadline=deadline)
    picks = [n for n in (match_file(r, file_names) for r in answer.recommended_files) if n]
    return {"reply": answer.reply.strip()[:cfg.tuning.max_reply_chars], "recommended_files": list(dict.fromkeys(picks))}
