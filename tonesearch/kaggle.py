"""Kaggle's web API, called with the visitor's own Kaggle credentials (never the server's).

Cab embed trains on the visitor's Kaggle account: this module pushes one private script kernel, reads its
status and output, and deletes it. It talks to the same endpoints as Kaggle's own SDK (kagglesdk: POST
https://api.kaggle.com/v1/<service>/<method> with a JSON body), using stdlib urlopen, so the server needs no
`kaggle` package and no credentials file. Credentials arrive per request and are never stored or logged.
"""
from __future__ import annotations

import base64
import json
import re
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, build_opener, urlopen

from .research import _NoRedirectHandler

API_BASE = "https://api.kaggle.com/v1"
KERNELS = "kernels.KernelsApiService"
# The builder's Kaggle trainer only ever asks for a T4 (never the P100), and so does this.
ACCELERATOR = "NvidiaTeslaT4"
# Kaggle usernames, and the slugs this module creates (see job_slug in cab.py).
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{3,49}$")
# Kernel output links are signed storage URLs; only Kaggle's and Google's storage hosts are fetched.
_OUTPUT_HOSTS = ("kaggle.com", "kaggleusercontent.com", "googleapis.com", "googleusercontent.com")
MAX_OUTPUT_BYTES = 50_000_000
# Kernel states, without the KERNEL_WORKER_STATUS_ prefix some replies carry.
FINISHED = {"complete", "error", "cancel_acknowledged"}


class KaggleError(RuntimeError):
    """A Kaggle failure with a message a visitor can act on, and the HTTP status when Kaggle sent one."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def credentials(username: str, key: str) -> tuple[str, str]:
    """Checked (username, key). The key is a legacy API key or a newer KGAT_ API token."""
    username, key = (username or "").strip(), (key or "").strip()
    if not USERNAME.match(username):
        raise KaggleError("Enter your Kaggle username (letters, numbers, - and _).")
    if not key or len(key) > 500 or any(c.isspace() for c in key):
        raise KaggleError("Enter your Kaggle API key or token.")
    return username, key


def _auth_header(username: str, key: str) -> str:
    # Kaggle's SDK sends KGAT_ tokens as a bearer token and legacy keys as basic auth with the username.
    if key.startswith("KGAT_"):
        return f"Bearer {key}"
    return "Basic " + base64.b64encode(f"{username}:{key}".encode()).decode()


def _call(method: str, body: dict, username: str, key: str, *, service: str = KERNELS, opener=urlopen,
          timeout: int = 30) -> dict:
    request = Request(f"{API_BASE}/{service}/{method}", data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": _auth_header(username, key),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "TONE-Search cab-embed/1.0",
    })
    try:
        with opener(request, timeout=timeout) as response:
            reply = json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        raise KaggleError(_http_message(exc), exc.code) from exc
    except Exception as exc:
        raise KaggleError(f"Couldn't reach Kaggle: {exc}") from exc
    if not isinstance(reply, dict):
        raise KaggleError("Kaggle sent an unexpected reply.")
    if isinstance(reply.get("code"), int) and reply["code"] >= 400:
        raise KaggleError(f"Kaggle refused the request: {reply.get('message') or reply['code']}")
    return reply


def _http_message(exc: HTTPError) -> str:
    try:
        detail = json.loads(exc.read().decode("utf-8")).get("message")
    except Exception:
        detail = None
    if exc.code in (401, 403):
        return ("Kaggle didn't accept these credentials, or this account can't do that. Check your username and "
                "API key, and that your account is phone-verified (Kaggle requires it for GPUs and internet access)."
                + (f" Kaggle said: {detail}" if detail else ""))
    if exc.code == 404:
        return "Kaggle has no such job on this account. It may have been deleted."
    if exc.code == 429:
        return "Kaggle is limiting requests from this account. Wait a minute and try again."
    return f"Kaggle returned HTTP {exc.code}" + (f": {detail}" if detail else ".")


def push_script(username: str, key: str, *, slug: str, title: str, script: str, dataset: str | None = None,
                opener=urlopen) -> dict:
    """Create and start a private GPU script kernel. Returns {"ref", "url", "version"}."""
    if not SLUG.match(slug):
        raise KaggleError("Invalid job name.")
    reply = _call("SaveKernel", {
        "slug": f"{username}/{slug}",
        "newTitle": title,
        "text": script,
        "language": "python",
        "kernelType": "script",
        "isPrivate": True,
        "enableGpu": True,
        "enableInternet": True,  # pip installs neural-amp-modeler, and the training input may be downloaded
        "machineShape": ACCELERATOR,
        "datasetDataSources": [dataset] if dataset else [],
        "kernelDataSources": [],
        "competitionDataSources": [],
        "modelDataSources": [],
        "categoryIds": [],
    }, username, key, opener=opener, timeout=60)
    if reply.get("error"):
        raise KaggleError(f"Kaggle didn't start the job: {reply['error']}")
    if reply.get("invalidDatasetSources"):
        raise KaggleError("Kaggle couldn't attach the NAM training input dataset. Tell the site owner.")
    return {"ref": f"{username}/{slug}", "url": str(reply.get("url") or f"https://www.kaggle.com/code/{username}/{slug}"),
            "version": reply.get("versionNumber")}


def _ref(ref: str) -> tuple[str, str]:
    owner, _, slug = (ref or "").partition("/")
    if not USERNAME.match(owner) or not SLUG.match(slug):
        raise KaggleError("Invalid job reference.")
    return owner, slug


def status(username: str, key: str, ref: str, *, opener=urlopen) -> dict:
    """{"state": queued|running|complete|error|..., "failure": str|None}."""
    owner, slug = _ref(ref)
    reply = _call("GetKernelSessionStatus", {"userName": owner, "kernelSlug": slug}, username, key, opener=opener)
    state = str(reply.get("status") or "queued").lower().removeprefix("kernel_worker_status_")
    return {"state": state, "failure": reply.get("failureMessage") or None}


def output(username: str, key: str, ref: str, *, opener=urlopen) -> dict:
    """The kernel's output files ({name: url}) and its log text."""
    owner, slug = _ref(ref)
    reply = _call("ListKernelSessionOutput", {"userName": owner, "kernelSlug": slug}, username, key, opener=opener)
    files = {}
    for item in reply.get("files") or []:
        if isinstance(item, dict) and item.get("fileName") and item.get("url"):
            files[str(item["fileName"])] = str(item["url"])
    return {"files": files, "log": str(reply.get("log") or "")}


def delete(username: str, key: str, ref: str, *, opener=urlopen) -> None:
    owner, slug = _ref(ref)
    _call("DeleteKernel", {"userName": owner, "kernelSlug": slug}, username, key, opener=opener)


def gpu_quota(username: str, key: str, *, opener=urlopen) -> dict:
    """Weekly GPU hours used and allowed, which also proves the credentials work."""
    reply = _call("GetAcceleratorQuotaStatistics", {}, username, key, opener=opener)
    gpu = reply.get("gpuQuota") if isinstance(reply.get("gpuQuota"), dict) else {}

    def hours(value) -> float | None:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(value or ""))
        return round(float(match.group(1)) / 3600, 1) if match else None

    return {"used_hours": hours(gpu.get("timeUsed")), "allowed_hours": hours(gpu.get("totalTimeAllowed")),
            "refreshes": reply.get("quotaRefreshTime")}


def _output_host_ok(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host == h or host.endswith("." + h) for h in _OUTPUT_HOSTS)


def fetch_output(url: str, *, opener=None, limit: int = MAX_OUTPUT_BYTES) -> bytes:
    """Download one output file from its signed link. Redirects are followed by hand, to allowed hosts only."""
    for _ in range(4):
        # Kaggle's links end in the file's own name, spaces and all; urllib refuses those unencoded.
        url = quote(url, safe=":/?#[]@!$&'()*+,;=%~")
        if not _output_host_ok(url):
            raise KaggleError("Kaggle gave an output link on an unexpected host.")
        try:
            with (opener or build_opener(_NoRedirectHandler).open)(Request(url), timeout=60) as response:
                data = response.read(limit + 1)
        except HTTPError as exc:
            location = exc.headers.get("Location") if exc.code in (301, 302, 303, 307, 308) else None
            if location:
                url = urljoin(url, location)
                continue
            raise KaggleError(f"Couldn't download the job's output: HTTP {exc.code}") from exc
        except Exception as exc:
            raise KaggleError(f"Couldn't download the job's output: {exc}") from exc
        if len(data) > limit:
            raise KaggleError("The job's output file is larger than expected.")
        return data
    raise KaggleError("Kaggle redirected the download too many times.")
