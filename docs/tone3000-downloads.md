# TONE3000 downloads: setup and the Cloudflare 1010 fix

## Confirmed cause and fix

On 6 November 2026, TONE Search could list models but downloads from
`files.tone3000.com` failed with HTTP 403. The app reported that upstream failure
as HTTP 503. The complete upstream body was:

```text
error code: 1010
```

Cloudflare was rejecting the HTTP client's signature. Python urllib's default
User-Agent was in use. Sending a descriptive application User-Agent fixed the
download; the user confirmed success with the existing OAuth session.

Use your application's own identity, for example:

```http
User-Agent: MyApp/1.0 (TONE3000 API client)
```

Apply this header to **both API metadata requests and model-file requests**.
Check separate HTTP clients, download helpers, background workers, and pack ZIP
builders: setting a header on the metadata client does not configure another
downloader automatically. A library or worker update can also restore its default
User-Agent unless the header is set explicitly.

This is Cloudflare's documented recommendation for error 1010; a browser-like
User-Agent is unnecessary. See [Cloudflare error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/).

## Applying the fix in another app

Keep the app's working authentication and URL handling. Add a User-Agent to the
existing request headers, preserving its Authorization header and redirect checks.

Python urllib:

```python
headers = {
    "User-Agent": "MyApp/1.0 (TONE3000 API client)",
    "Authorization": f"Bearer {access_token}",
}
request = urllib.request.Request(model_url, headers=headers)
# Send through the app's existing opener with its safe redirect handling.
```

Python requests:

```python
session.headers["User-Agent"] = "MyApp/1.0 (TONE3000 API client)"
# Use this session for metadata AND files. Keep bearer credentials scoped to
# trusted TONE3000 hosts rather than adding them to a general-purpose session.
response = session.get(
    model_url,
    headers={"Authorization": f"Bearer {access_token}"},
    timeout=30,
    allow_redirects=False,  # let the app's safe redirect handler process Location
)
```

Node.js/server-side fetch:

```javascript
const response = await fetch(modelUrl, {
  headers: {
    "User-Agent": "MyApp/1.0 (TONE3000 API client)",
    Authorization: `Bearer ${accessToken}`,
  },
  redirect: "manual", // process redirects with the app's existing safety checks
});
```

These snippets change request headers; they are not complete redirect handlers.
For redirects, validate the destination and omit Authorization when following a
signed storage URL on an external host. Never forward credentials to arbitrary
URLs. Browser JavaScript manages its own User-Agent; make this change in the
server or native HTTP client that actually fetches the file.

Use the **exact `model_url`** returned by TONE3000, including its `verify` query
parameter. Preserve its encoding and query values. Replacing
`files.tone3000.com` with `www.tone3000.com` produced HTTP 404 in this incident.
The API documentation's model example fetches the returned URL directly.

## Authentication and the TONE Search workflow

Cloudflare 1010 did not establish that the secret key had stopped working.
The earlier inference that HTTP 403 proved an OAuth migration was required was
incorrect. TONE3000 still documents `t3k_cs_…` as a server-only Bearer credential
for direct API calls, and recommends OAuth for user-facing integrations. Its
Models examples fetch metadata and model URLs with an OAuth access token.
See [authentication](https://www.tone3000.com/api#authentication) and
[Models](https://www.tone3000.com/api#models).

TONE Search now implements this workflow:

1. Catalogue searches and model display names use the configured secret key:
   `TONE3000_API_KEY` on the server, or the existing visitor setting.
2. In **Settings → TONE3000**, enter the publishable key (`t3k_pub_…`, the OAuth
   `client_id`) and click **Connect TONE3000**.
3. Open the activation link, approve the displayed device code on TONE3000,
   and wait for the connected message. This device flow requires no callback URI.
4. Open a pack's **Files & questions** and download one model or select
   **Download all**.
5. Download operations fetch fresh model metadata and file bytes using the
   connected user's access token. Pack downloads fetch individual models and
   ZIP them locally on the server; they do not use TONE3000's partner-only ZIP
   endpoint. Existing limits are 60 files and 250 MB per pack.

Access and refresh tokens are kept in the current browser tab's session storage
(with an in-memory fallback). Access tokens refresh automatically before expiry;
closing the session requires connecting again. The app sends the access token
to its server as `X-TONE3000-Access-Token`. It is not the publishable key or the
secret API key. A publishable key alone does not authenticate a download.

Other apps with working authentication need the User-Agent fix for this specific
1010 failure; they do not need to copy TONE Search's entire OAuth implementation
solely to resolve that error.

## Diagnosis and validation

After updating, restart every server/worker process that performs downloads.
Deploy the changed backend as well as any UI changes. Confirm a single NAM file
downloads and parses as a NAM JSON document, then try a small pack ZIP. Check that
outgoing API and file requests both use the application User-Agent.

When a download fails, inspect the **upstream body**, not just the app's HTTP
status or the upstream Content-Type. This incident's body was plain text even
though its header said `Content-Type: application/json`.

- `error code: 1010`: client-signature rejection. Check the User-Agent on the
  actual file request. If it persists with an explicit application identity,
  contact TONE3000 support with the UTC timestamp and Cloudflare Ray ID.
- HTTP 401 or a token-specific error: inspect token expiry and refresh handling.
- HTTP 403 without 1010: inspect the provider's actual message before deciding
  whether it concerns authorization, verification URLs, or another security rule.
- HTTP 404: check the exact model URL and fresh metadata; do not rewrite hosts
  based only on the HTTP status.

TONE Search saves the latest failed download to
`$TONESEARCH_DATA_DIR/download-errors/latest.json` (default:
`data/download-errors/latest.json`). It records the status, response headers,
decoded body, captured byte count, truncation status, and timestamp. Gzip bodies
are decoded. The capture is capped at 1 MB and each failure replaces the previous
dump. Credentials and signed URL values are redacted; files are created with
owner-only permissions. The default `data/` directory is ignored by Git.

Implementation references:

- `tonesearch/research.py`: `TONE3000_USER_AGENT`, `_tone3000_get`, `_fetch_file`,
  `_dump_download_error`.
- `static/app.js`: device authorization, token refresh, download headers.
- `tonesearch/overrides.py` and `app.py`: per-request access-token forwarding.
- `tests/test_download_errors.py`: application identity and diagnostic regressions.
