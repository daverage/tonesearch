# TONE Search

Describe a guitar or bass tone in plain words, and TONE Search finds [Neural Amp Modeler](https://www.neuralampmodeler.com/) (NAM) captures on [TONE3000](https://www.tone3000.com/) that match it.

> "How do I get Mick Ronson's lead tone on Ziggy Stardust?"

The AI explains the tone and the gear behind it (amps, pedals, guitars and pickups), gives practical tips, then searches TONE3000 and ranks the matching packs by how well they fit. You can open any pack to download its `.nam` files, or ask the AI which file suits rhythm or lead.

TONE Search is a small Flask app with no build step. It runs on a laptop or on ordinary shared hosting (cPanel / Passenger).

---

## Contents

- [How it works](#how-it-works)
- [What you need](#what-you-need)
- [Run it locally](#run-it-locally)
  - [Option A: Cloudflare Workers AI (free tier)](#option-a-cloudflare-workers-ai-free-tier)
  - [Option B: A local model with Ollama](#option-b-a-local-model-with-ollama)
  - [Option C: Any OpenAI-compatible API](#option-c-any-openai-compatible-api)
- [Using the app](#using-the-app)
- [Configuration reference](#configuration-reference)
- [Choosing a model](#choosing-a-model)
- [Deploying](#deploying)
- [Privacy and security](#privacy-and-security)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Credits and disclaimers](#credits-and-disclaimers)
- [Licence](#licence)

---

## How it works

One search runs this pipeline on the server:

```
 your description
        │
        ▼
 1. Web research (optional) ── DuckDuckGo: two gear-focused searches (each in its own process), up to 8 pages read,
        │                      the most gear-relevant sentences kept as notes
        ▼
 2. Tone plan (AI) ─────────── a summary of the tone, the gear behind it, tips,
        │                      and 1–3 short TONE3000 search queries
        ▼
 3. TONE3000 search ────────── each query is searched with your filters; results are de-duplicated
        │
        ▼
 4. Shortlist ──────────────── sorted by catalogue score and cut to 12
        │                      (small models stop scoring partway through long lists)
        ▼
 5. Ranking (AI) ───────────── every pack gets a 0–100 fit score and a one-line reason
        │
        ▼
 tone brief + ranked packs
```

- **Only the tone plan is essential.** If web research, the TONE3000 search or ranking fails, you still get results, with a warning. If the plan fails, the search returns an error.
- **Refining:** you can refine in plain words ("more gain", "darker", "a cheaper amp"). The conversation so far is sent with each refinement.
- **Filters:** changing filters or clicking a tag reuses the last plan and skips the AI planning step.
- **Pack questions:** when you open a pack, the AI answers questions about it using the pack's description and file names, and can pin the files it recommends.

### Working with many kinds of AI model

The AI code (`tonesearch/ai.py`) is built to cope with a wide range of models, including small local ones:

- **Response formats:** it asks for structured JSON. If a provider doesn't support that, it falls back to plain JSON mode, then to no format at all, and remembers what each model accepts.
- **Messy replies:** code fences, `<think>` blocks, trailing commas, Python-style literals and cut-off answers are all repaired before the reply is read.
- **Streaming:** replies are streamed, so the timeout means "no progress for N seconds", not "not finished in N seconds". Slow but working models finish.
- **Thinking models:** models that reason before answering (GLM, Qwen3, DeepSeek-R1, gpt-oss and others) are asked to think less. Several different ways of asking are tried, and one the provider doesn't recognise is dropped automatically. If none is accepted, the model is used as normal.
- **Time budget:** each search and each pack question has an overall budget (85 seconds by default), so it finishes before a proxy such as Cloudflare cuts it off at 100 seconds. If time runs short, ranking is skipped and packs are shown in catalogue order, with a warning.

---

## What you need

- **Python 3.10 or newer.**
- **A TONE3000 API key (free):** [register for an account](https://www.tone3000.com/), then copy the **secret key** (it starts with `t3k_cs_`) from your [TONE3000 settings](https://www.tone3000.com/settings). Without it, you get the AI tone brief but no packs.
- **An AI model**, from one of these:
  - **Cloudflare Workers AI:** has a free daily allowance. Setup is slightly technical.
  - **A local model with Ollama:** free and private, but needs a reasonably capable computer.
  - **Any OpenAI-compatible API:** for example OpenAI, OpenRouter, Groq, Together, or a vLLM/LM Studio server reachable over HTTPS.

---

## Run it locally

```bash
git clone <this repository's URL> tonesearch
cd tonesearch
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

Then choose an AI provider below and start the app with its settings. The app reads **environment variables**; it does not load a `.env` file. Put the variables in front of the command as shown, or `export` them first.

Open **http://127.0.0.1:5090** once the server says it is running.

> If you set the variables on lines by themselves without `export`, then run `python3 app.py` separately, the app won't see them. Put the variables and the command together, as in the examples below.

### Option A: Cloudflare Workers AI (free tier)

1. Create a free Cloudflare account.
2. Follow Cloudflare's [REST API guide](https://developers.cloudflare.com/workers-ai/get-started/rest-api/) to create an API token. Use the **Workers AI** template.
3. Note your **Account ID** from the dashboard. It is 32 hexadecimal characters.
4. Pick a model from the [model catalogue](https://developers.cloudflare.com/workers-ai/models/). See [Choosing a model](#choosing-a-model).

Then start the app:

```bash
NAM_MIXER_AI_PROVIDER=cloudflare \
NAM_MIXER_AI_CLOUDFLARE_ACCOUNT_ID=your32characteraccountid000000000 \
NAM_MIXER_AI_CLOUDFLARE_API_KEY=your_cloudflare_api_token \
NAM_MIXER_AI_CLOUDFLARE_MODEL=@cf/zai-org/glm-4.7-flash \
TONE3000_API_KEY=t3k_cs_your_key \
python3 app.py
```

### Option B: A local model with Ollama

1. Install [Ollama](https://ollama.com/) and pull a model, for example `ollama pull gemma4:e4b`.
2. Check Ollama is running: `curl http://127.0.0.1:11434/v1/models` should list your models.

Then start the app:

```bash
NAM_MIXER_AI_PROVIDER=local \
NAM_MIXER_AI_LOCAL_BASE_URL=http://127.0.0.1:11434/v1 \
NAM_MIXER_AI_LOCAL_MODEL=gemma4:e4b \
TONE3000_API_KEY=t3k_cs_your_key \
python3 app.py
```

With local AI, the app treats the machine as the owner's own:

- There are no hourly limits and no ads.
- The **AI tuning** fields in Settings apply to the local model.
- The page says it is using local AI: the Settings button reads "Settings · local AI", and each tone brief ends with "Answered by gemma4:e4b (local AI on this computer)".

To confirm the model is really being used, run `ollama ps` in another terminal during a search. It should list the model.

For security, local mode only accepts a plain `http://` address on this machine (`127.0.0.1`, `localhost` or `::1`). Other local servers that speak the OpenAI API, such as LM Studio (`http://127.0.0.1:1234/v1`), work the same way.

### Option C: Any OpenAI-compatible API

```bash
NAM_MIXER_AI_PROVIDER=custom \
NAM_MIXER_AI_CUSTOM_BASE_URL=https://api.openai.com/v1 \
NAM_MIXER_AI_CUSTOM_API_KEY=your_api_key \
NAM_MIXER_AI_CUSTOM_MODEL=gpt-4o-mini \
TONE3000_API_KEY=t3k_cs_your_key \
python3 app.py
```

The base URL must use `https://`. The app calls `<base URL>/chat/completions`.

### Without any server keys

You can also start the app with no settings at all (`python3 app.py`) and enter your keys in the **Settings** dialog in the browser:

- You can use a Cloudflare or custom provider, plus your TONE3000 key.
- Keys entered there are saved only in that browser.
- The page will warn that the server's own AI isn't configured. That is expected.

---

## Using the app

1. **Describe it.** Name a song, an artist or an album, or say what you hear: "glassy clean with a bit of hair", "tight modern metal rhythm".
2. **Read the brief.** The AI sums up the tone, lists the gear, and gives tips. With **Web research** on, it checks real sources first. Web research is slower, but better for songs and artists.
3. **Pick a capture.** Packs are ranked by fit, and weak matches are folded away. Use **Files & questions** to see a pack's NAM files, download one or all of them, or ask the AI about the pack.
4. **Refine** in plain words, or use **Filters**:
   - gear type
   - format (NAM, IR, AIDA-X…)
   - NAM version (A1/A2)
   - model size
   - make, tags, creator
   - calibrated/verified only
5. **Export** saves the whole conversation, with the ranked packs, as a Markdown file. Your keys are never included.

**Keyboard and accessibility:**

- The app aims to meet WCAG 2.2 AA.
- Press Ctrl/Cmd+Enter in the search box to search.
- The filter suggestions work with the arrow keys and Enter.
- Progress and results are announced to screen readers.
- The layout works down to 320px wide.

---

## Configuration reference

All settings are environment variables.

### AI provider

The variable names are shared with NAM Mixer, so one set of values works in both. Every AI setting can be scoped to a provider, such as `NAM_MIXER_AI_CLOUDFLARE_MODEL`, or left unscoped, such as `NAM_MIXER_AI_MODEL`, which is used as a fallback.

| Variable | Meaning |
|---|---|
| `NAM_MIXER_AI_PROVIDER` | `cloudflare` (default), `custom` or `local` |
| `NAM_MIXER_AI_<PROVIDER>_MODEL` | Model ID, e.g. `@cf/zai-org/glm-4.7-flash`, `gpt-4o-mini`, `gemma4:e4b` |
| `NAM_MIXER_AI_CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID (32 hex characters) |
| `NAM_MIXER_AI_<PROVIDER>_API_KEY` | API token or key (Cloudflare and custom) |
| `NAM_MIXER_AI_<PROVIDER>_BASE_URL` | For `custom` (https only) and `local` (http on localhost only; default `http://127.0.0.1:11434/v1`) |

### AI tuning

Each value is clamped to the range shown.

| Variable | Default | Range | Meaning |
|---|---:|---|---|
| `NAM_MIXER_AI_MAX_TOKENS` | 1800 | 64–8192 | Tokens per AI reply. Raise it for thinking models that run out |
| `NAM_MIXER_AI_TEMPERATURE` | 0.3 | 0–2 | Randomness |
| `NAM_MIXER_AI_TIMEOUT_SECONDS` | 90 | 5–180 | Longest wait with **no progress** from the provider |
| `NAM_MIXER_AI_HISTORY_MESSAGES` | 8 | 0–12 | Earlier messages sent with a refinement |
| `NAM_MIXER_AI_HISTORY_MESSAGE_CHARS` | 1200 | 100–4000 | Characters kept per earlier message |
| `NAM_MIXER_AI_RESEARCH_CHARS` | 5000 | 0–20000 | Web research notes passed to the AI |
| `NAM_MIXER_AI_MAX_REPLY_CHARS` | 1800 | 200–6000 | Longest pack-question answer shown |
| `NAM_MIXER_AI_MAX_EXPLANATION_CHARS` | 900 | 200–4000 | Longest tone summary shown |

Visitors using their own AI provider can set the same values under **Settings → AI tuning**.

### TONE3000, limits and hosting

| Variable | Default | Meaning |
|---|---:|---|
| `TONE3000_API_KEY` | none | The server's TONE3000 secret key (`t3k_cs_…`) |
| `TONESEARCH_SEARCHES_PER_HOUR` | 12 | Searches per visitor per hour (0 = no limit) |
| `TONESEARCH_CHATS_PER_HOUR` | 40 | Pack questions per visitor per hour |
| `TONESEARCH_FILE_REQUESTS_PER_HOUR` | 120 | File lists and downloads per visitor per hour |
| `TONESEARCH_LOOKUPS_PER_HOUR` | 600 | Filter autocomplete lookups per visitor per hour |
| `TONESEARCH_REQUEST_BUDGET_SECONDS` | 85 | Overall time allowed for one search or pack question |
| `TONESEARCH_DATA_DIR` | `data/` | Where the rate-limit database is kept |
| `TONESEARCH_ADSENSE_CLIENT` | none (no ads) | Your Google AdSense publisher ID (`ca-pub-…`). Ads load only when this is set. |
| `PORT` | 5090 | Port for `python3 app.py` |
| `FLASK_DEBUG` | off | Set to `1` for Flask's debug mode (local development only) |

**How the limits work:**

- Visitors are identified by their IP address, taken from the first `X-Forwarded-For` entry.
- Each request is recorded in `data/limits.sqlite3` and deleted after an hour.
- Visitors who add **both** their own AI key and their own TONE3000 key in Settings skip the search and chat limits.
- A server running local AI has no limits at all.

---

## Choosing a model

Non-thinking models are fastest. Thinking models can give better answers, but take longer and use more of a free allowance.

The Settings dialog lists these Workers AI models, each with a button to use it:

| Model | Notes |
|---|---|
| `@cf/zai-org/glm-4.7-flash` | Low cost; promising for search planning |
| `@cf/qwen/qwen3-30b-a3b-fp8` | Lowest cost; good at turning requests into searches |
| `@cf/mistralai/mistral-small-3.1-24b-instruct` | Supports structured JSON |
| `@cf/openai/gpt-oss-120b` | Good at interpreting research; moderate cost |
| `@cf/deepseek-ai/deepseek-r1-distill-qwen-32b` | Good results in testing; highest output cost |
| `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | Fast and does not think; higher cost |
| `@cf/google/gemma-4-26b-a4b-it` | Low cost, but timeouts have been seen |

Prices and free allowances change. Check Cloudflare's [pricing page](https://developers.cloudflare.com/workers-ai/platform/pricing/).

For Ollama, small models such as `gemma4:e4b` work, but give thinner gear lists than larger models. The first search after starting is slower while Ollama loads the model.

---

## Deploying

### cPanel / Passenger

1. In cPanel, open **Setup Python App** and create an app. Use Python 3.10 or newer.
2. Set the **startup file** to `passenger_wsgi.py` and the **entry point** to `application`.
3. Upload the repository, then install the requirements from the app's virtual environment: `pip install -r requirements.txt`.
4. Add the environment variables from the [Configuration reference](#configuration-reference) in the app's settings.
5. Restart the app after every upload.

Serving under a sub-path such as `example.com/tonesearch` works: the page sets its own `<base href>`. Links to the CSS and JavaScript carry a version number based on when the files last changed, so browsers and Cloudflare fetch fresh copies after a restart.

### Behind Cloudflare

- Cloudflare's proxy ends requests after about 100 seconds. Keep `TONESEARCH_REQUEST_BUDGET_SECONDS` below that (the default is 85).
- Upstream failures return HTTP **503**, not 502, because Cloudflare replaces the body of a 502 with its own error page. That would hide the app's error message.

### Ads

Ads are off by default. To show Google AdSense Auto ads, set `TONESEARCH_ADSENSE_CLIENT` to your own publisher ID (`ca-pub-…`). Don't paste Google's ad snippet into the template: the app adds it itself, so it can keep ads away from visitors' keys.

- Ads are never loaded for visitors who save their own keys in Settings, or on a server running local AI. See [Privacy and security](#privacy-and-security).
- In the UK and EU, publish a consent message in AdSense under **Privacy & messaging → European regulations**.

---

## Privacy and security

- **Visitors' keys stay in their browser.**
  - Keys entered in Settings are saved only in the visitor's browser, in `localStorage`.
  - They are sent over HTTPS with each request, as `X-AI-*` and `X-TONE3000-Key` headers.
  - The server uses them for that one request, then forgets them. They are never stored or logged, and never included in exports.
  - A visitor who sets their own AI provider never falls back to the server's secret keys.
- **Keys and ads never share a page.** Any script on the page can read `localStorage`, so the ad script is not loaded once a visitor has saved keys. Saving keys while ads are running reloads the page first.
- **The same site means shared browser storage.** Browsers share `localStorage` across every page on the same domain, not just this app's folder. If other pages on your domain run third-party scripts, host TONE Search on its own subdomain.
- **Visitors' AI URLs are restricted.** A visitor's custom AI URL must be a public `https://` host, and redirects are refused. Visitors cannot choose `local`. This stops the server from being used to reach private addresses.
- **Web research page fetches are restricted too.** Pages are only fetched from public hosts, without following redirects.
- **The rate limiter stores IP addresses briefly.** It keeps each visitor's IP address and request times for up to an hour.

---

## Development

```bash
pip install -r requirements-dev.txt
pytest                                   # all tests
pytest tests/test_app.py::test_name      # one test
FLASK_DEBUG=1 python3 app.py             # auto-reload while editing
```

**Project layout:**

```
app.py                  Flask routes, validation, hourly limits, time budget
passenger_wsgi.py       WSGI entry point for cPanel/Passenger
tonesearch/ai.py        AI calls: prompts, JSON repair, streaming, thinking hints, deadlines
tonesearch/research.py  TONE3000 search, file lists and downloads; DuckDuckGo web research
tonesearch/overrides.py Per-request settings from the visitor's Settings dialog (headers)
templates/index.html    The single page
static/app.js           Front end (no build step)
static/style.css        Styles (light and dark)
tests/test_app.py       Tests; they never touch the network
```

**Conventions:**

- **Network access is injectable.** Every network function takes an `opener=urlopen` parameter, so tests can use fakes.
- **Errors are user-facing.** Services raise `RuntimeError` or `AiError` with readable messages, and routes turn them into JSON errors.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| "The AI provider is not configured on this server yet" | The AI environment variables didn't reach the app. Put them on the same command as `python3 app.py`, or `export` them first. |
| A tone brief but no packs, with a TONE3000 warning | No TONE3000 key. Set `TONE3000_API_KEY`, or add your key in Settings. |
| "… was still thinking after N seconds" | A thinking model took too long. Try again, choose a faster model, or raise the time budget if you are not behind Cloudflare. |
| "The model used its whole token budget thinking" | Raise **Max tokens** (Settings → AI tuning or `NAM_MIXER_AI_MAX_TOKENS`), or choose a model that doesn't think. |
| "Settings: the AI base URL must be a public internet address" | A visitor-entered URL can't point at `127.0.0.1` or a private network. To use a local model, run the app yourself with `NAM_MIXER_AI_PROVIDER=local`. |
| Connection refused with local AI | Check the port: Ollama's default is `11434`. `curl http://127.0.0.1:11434/v1/models` should list your models. |
| `ollama ps` stays empty during a search | The app isn't reaching Ollama. Check the base URL and model name. If the page itself doesn't load, see the next row. |
| The local server stops responding (even the home page) | Older versions could freeze during web research on macOS (a bug in the `ddgs` search library's HTTP client). Update to the latest code, then stop the frozen server with `kill -9 <pid>` (Ctrl+C can't stop it) and start it again. |
| "Web research unavailable" | Install the requirements (`ddgs`). DuckDuckGo may also be rate-limiting; searches still work without research. |
| Changes don't show after deploying | Restart the app, then hard-refresh (Cmd/Ctrl+Shift+R). |

---

## Credits and disclaimers

- **Captures belong to their creators.** Every pack, file and image comes from [TONE3000](https://www.tone3000.com/) and is used under its creator's licence, which is shown on each pack. Check the licence before using a capture in your own work.
- **Not affiliated.** TONE Search is an independent project. It is not affiliated with or endorsed by TONE3000, Neural Amp Modeler, Cloudflare, or any artist, band or gear maker mentioned. Product names, trademarks and artist names belong to their owners and are used only to describe tones.
- **AI can be wrong.** Tone briefs, gear lists, tips and fit scores are AI suggestions, not facts. Trust your ears.
- **No warranty.** The software is provided as is, without warranty of any kind.

## Licence

Copyright © 2026 Andrzej Marczewski. TONE Search is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE.md):

- **Allowed:** you may use, change and share the code for any **non-commercial** purpose. That includes personal use, hobby projects, study and research, and use by charities, schools and public bodies.
- **Needs permission:** commercial use, such as selling the software, offering it as a paid service, or using it in a business's work. Contact the author for a separate licence.
- **Keep the notice:** anyone you share the code with must also receive the licence terms, including the `Required Notice` line.
