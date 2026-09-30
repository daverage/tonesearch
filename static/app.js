// TONE Search page: tone brief -> AI-ranked TONE3000 packs -> per-pack files and questions.
(() => {
  const announcer = document.getElementById("announcer");
  const announce = (text) => { announcer.textContent = ""; setTimeout(() => { announcer.textContent = text; }, 50); };
  const section = document.getElementById("tone3000-ai-section");
  if (!section) return;
  const thread = document.getElementById("t3ai-thread");
  const promptEl = document.getElementById("tone3000-ai-prompt");
  const label = document.getElementById("t3ai-label");
  const research = document.getElementById("tone3000-ai-research");
  const status = document.getElementById("tone3000-ai-status");
  const grid = document.getElementById("tone3000-ai-results");
  const resultsBar = document.getElementById("results-bar");
  const orderSelect = document.getElementById("results-order");
  const searchBtn = document.getElementById("btn-tone3000-ai-search");
  const resetBtn = document.getElementById("btn-t3ai-reset");
  const exportBtn = document.getElementById("btn-export");

  const state = { history: [], goal: "", busy: false, plan: null, searchedFilters: null, log: [], packChats: new Map() };
  const GEAR_LABELS = { amp: "Amp head", "amp-cab": "Full rig", amp_cab: "Full rig", "full-rig": "Full rig", pedal: "Pedal", outboard: "Outboard", ir: "IR" };
  const KIND_LABELS = { amp: "Amps", effect: "Effects", guitar: "Guitars", pickup: "Pickups", cab: "Cabs", other: "Other" };

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };
  // Links that open a new tab say so to screen reader users (WCAG G201).
  const newTab = (link, rel = "noopener noreferrer") => {
    link.target = "_blank"; link.rel = rel;
    if (!link.querySelector(".new-tab-hint")) link.append(el("span", "visually-hidden new-tab-hint", " (opens in a new tab)"));
    return link;
  };
  document.querySelectorAll('a[target="_blank"]').forEach((link) => newTab(link, link.rel || "noopener"));
  // Animates a layout change where supported; instant for reduced motion and older browsers.
  // Resolves once the change has been applied (the browser runs a transition's callback asynchronously).
  // If the browser holds the transition back (e.g. a background tab), the change still lands after 600ms.
  const smoothly = (change) => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!document.startViewTransition || reduced) { change(); return Promise.resolve(); }
    let done = false;
    const once = () => { if (!done) { done = true; change(); } };
    const transition = document.startViewTransition(once);
    transition.ready.catch(() => {}); transition.finished.catch(() => {}); // a skipped animation is not an error
    return new Promise((resolve) => {
      const timer = setTimeout(() => { transition.skipTransition(); once(); resolve(); }, 600);
      transition.updateCallbackDone.then(() => { clearTimeout(timer); resolve(); }, () => { clearTimeout(timer); once(); resolve(); });
    });
  };
  const motion = () => (window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");
  // Visitor's own keys (Settings): kept in this browser only, sent as headers on each request.
  const SETTINGS_KEY = "tonesearch.settings";
  const loadSettings = () => {
    try { return JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}; } catch { return {}; }
  };
  let settings = loadSettings();
  const localModel = document.body.dataset.aiLocal || ""; // set when this server runs a local AI (owner's machine)
  const TUNING_HEADERS = {
    max_tokens: "X-AI-Max-Tokens", temperature: "X-AI-Temperature", timeout_seconds: "X-AI-Timeout-Seconds",
    history_messages: "X-AI-History-Messages", history_message_chars: "X-AI-History-Message-Chars",
    research_chars: "X-AI-Research-Chars", max_reply_chars: "X-AI-Max-Reply-Chars", max_explanation_chars: "X-AI-Max-Explanation-Chars",
  };
  const settingsHeaders = () => {
    const headers = {};
    const provider = settings.provider;
    const sendTuning = () => Object.entries(TUNING_HEADERS).forEach(([key, header]) => {
      const value = (settings.tuning || {})[key];
      if (value) headers[header] = value;
    });
    if (!provider && localModel) sendTuning(); // the local AI honours the tuning too
    if (provider === "cloudflare" || provider === "custom") {
      const ai = settings[provider] || {};
      headers["X-AI-Provider"] = provider;
      if (ai.model) headers["X-AI-Model"] = ai.model;
      if (ai.api_key) headers["X-AI-Key"] = ai.api_key;
      if (provider === "cloudflare" && ai.account_id) headers["X-AI-Account-Id"] = ai.account_id;
      if (provider === "custom" && ai.base_url) headers["X-AI-Base-Url"] = ai.base_url;
      sendTuning();
    }
    if (settings.tone3000_api_key) headers["X-TONE3000-Key"] = settings.tone3000_api_key;
    return headers;
  };
  setupSettings();
  const filters = setupFilters();
  setupWelcome();

  // Google Auto ads, for every visitor. Not on a local run: that is the owner's own machine.
  const adClient = document.body.dataset.adClient;
  if (adClient && !localModel) {
    const script = document.createElement("script");
    script.async = true;
    script.crossOrigin = "anonymous";
    script.src = `https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=${encodeURIComponent(adClient)}`;
    document.head.append(script);
  }

  const compact = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));
  const post = async (url, body) => {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", ...settingsHeaders() }, body: JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  };

  // One request does the whole search, so stages follow typical timings rather than server events.
  const RESEARCH_STAGES = [
    [0, "Researching the tone on the web..."],
    [8, "Research done. Asking the AI to work out the tone and gear..."],
    [35, "Searching TONE3000 for matching captures..."],
    [42, "The AI is ranking the packs..."],
    [75, "Still working. Larger models can take a minute or two..."],
  ];
  const PLAIN_STAGES = RESEARCH_STAGES.slice(1).map(([at, text], i) => [i === 0 ? 0 : at - 6, i === 0 ? "Asking the AI to work out the tone and gear..." : text]);

  function showProgress(stages, target = status) {
    const started = Date.now();
    const text = el("span");
    const clock = el("span", "progress-clock");
    const spinner = el("span", "spinner");
    spinner.setAttribute("aria-hidden", "true");
    clock.setAttribute("aria-hidden", "true");
    target.replaceChildren(spinner, text, clock);
    target.classList.add("is-working");
    const tick = () => {
      const seconds = (Date.now() - started) / 1000;
      const stage = stages.filter(([at]) => seconds >= at).pop()[1];
      if (text.textContent !== stage) text.textContent = stage; // only stage changes reach screen readers
      clock.textContent = ` ${Math.floor(seconds)}s`;
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => { clearInterval(timer); target.classList.remove("is-working"); };
  }

  function setBusy(busy) {
    // aria-disabled and readOnly rather than disabled, so keyboard focus is not lost mid-search.
    state.busy = busy;
    searchBtn.setAttribute("aria-disabled", String(busy));
    promptEl.readOnly = busy;
    section.setAttribute("aria-busy", String(busy));
  }

  // Shows the request at once with a working card; earlier turns shrink to a two-line summary.
  function startTurn(prompt) {
    thread.querySelectorAll(".t3ai-turn.is-failed").forEach((failed) => failed.remove()); // a retry replaces it
    thread.querySelectorAll(".t3ai-turn").forEach((earlier) => earlier.classList.add("is-earlier"));
    const turn = el("div", "t3ai-turn");
    const pending = el("div", "t3ai-brief t3ai-pending");
    pending.setAttribute("role", "status");
    turn.append(el("div", "t3ai-you", prompt), pending);
    thread.append(turn);
    turn.scrollIntoView({ behavior: motion(), block: "start" });
    return { turn, pending };
  }

  function failTurn({ turn, pending }, message) {
    const card = el("div", "t3ai-brief t3ai-failed");
    card.append(el("h3", null, "Search failed"), el("p", "t3ai-warning", message), el("p", "t3ai-meta", "Your request is still in the box below: try again, or change it."));
    pending.replaceWith(card);
    turn.classList.add("is-failed");
  }

  function renderBrief({ turn, pending }, data) {
    const brief = el("article", "t3ai-brief");
    const plan = data.plan;
    const head = el("div", "t3ai-brief-head");
    const refined = thread.querySelectorAll(".t3ai-turn:not(.is-failed)").length > 1;
    head.append(el("h3", null, refined ? "Refined tone brief" : "Tone brief"), el("span", "t3ai-latest", "Latest"));
    const toggle = el("button", "link-btn t3ai-expand", "Show details");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", () => {
      const open = turn.classList.toggle("is-expanded");
      toggle.textContent = open ? "Hide details" : "Show details";
      toggle.setAttribute("aria-expanded", String(open));
    });
    head.append(toggle);
    brief.append(head, el("p", "t3ai-summary", plan.summary));
    if (plan.gear.length) {
      const groups = {};
      plan.gear.forEach((g) => { (groups[g.kind] ||= []).push(g); });
      const gear = el("div", "t3ai-gear");
      Object.keys(KIND_LABELS).filter((k) => groups[k]).forEach((kind) => {
        const row = el("div", "t3ai-gear-row");
        row.append(el("span", "t3ai-gear-kind", KIND_LABELS[kind]));
        groups[kind].forEach((g) => {
          const chip = el("span", `t3ai-chip t3ai-chip-${kind}`, g.name);
          if (g.role) { chip.title = g.role; chip.append(el("span", "visually-hidden", `: ${g.role}`)); } // the tooltip is mouse-only
          if (/uncertain|unconfirmed|not confirmed|possibly|likely|may have|might have|disputed/i.test(g.role || "")) {
            chip.classList.add("is-uncertain");
            chip.append(el("span", "t3ai-chip-note", " (unconfirmed)"));
          }
          row.append(chip);
        });
        gear.append(row);
      });
      brief.append(gear);
    }
    if (plan.advice.length) {
      const details = el("details", "t3ai-advice");
      details.open = state.history.length === 0;
      details.append(el("summary", null, "How to get there"));
      const list = el("ul");
      plan.advice.forEach((tip) => list.append(el("li", null, tip)));
      details.append(list);
      brief.append(details);
    }
    const meta = el("p", "t3ai-meta", `Searched TONE3000 for ${data.queries.map((q) => `"${q}"`).join(", ")}${data.researched ? " after web research" : ""}.${answeredBy(data.ai)}`);
    brief.append(meta);
    if (data.research_notes) brief.append(researchNotes(data.research_notes));
    data.warnings.forEach((w) => brief.append(el("p", "t3ai-warning", w)));
    pending.replaceWith(brief);
  }

  // Names the AI behind a reply when it is local or the visitor's own; this site's own model is not shown.
  function answeredBy(ai) {
    if (!ai || !ai.model) return "";
    return ai.source === "local" ? ` Answered by ${ai.model} (local AI on this computer).` : ` Answered by ${ai.model} (your own AI).`;
  }

  // The web notes the AI was given, one "- title: extract (url)" line per source.
  function researchNotes(notes) {
    const lines = notes.split("\n").filter((line) => line.trim());
    const details = el("details", "t3ai-research");
    details.append(el("summary", null, `Web research notes (${lines.length} source${lines.length === 1 ? "" : "s"})`));
    const list = el("ul");
    lines.forEach((line) => {
      const item = el("li");
      const parts = /^- (.*?): ([\s\S]*) \((https?:\/\/[^\s)]+)\)$/.exec(line);
      if (parts) {
        const link = el("a", null, parts[1] || parts[3]);
        link.href = parts[3]; newTab(link, "noopener noreferrer nofollow");
        item.append(link, el("p", null, parts[2]));
      } else {
        item.textContent = line.replace(/^- /, "");
      }
      list.append(item);
    });
    details.append(list);
    return details;
  }

  let cardCount = 0;
  function packCard(pack) {
    const card = el("article", "t3ai-card");
    const uid = `pack-${++cardCount}`;
    const media = el("div", "t3ai-media");
    if (pack.image) {
      const img = document.createElement("img");
      img.src = pack.image; img.alt = ""; img.loading = "lazy"; img.decoding = "async"; img.referrerPolicy = "no-referrer";
      img.addEventListener("error", () => { img.remove(); media.classList.add("t3ai-media-empty"); });
      media.append(img);
    } else media.classList.add("t3ai-media-empty");
    if (Number.isFinite(pack.ai_fit)) {
      const fit = el("span", "t3ai-fit", `${pack.ai_fit}% fit`);
      fit.dataset.level = pack.ai_fit >= 75 ? "high" : pack.ai_fit >= 50 ? "mid" : "low";
      media.append(fit);
    }
    const body = el("div", "t3ai-card-body");
    const title = el("h3", null, pack.title);
    title.id = `${uid}-title`;
    card.setAttribute("aria-labelledby", title.id);
    body.append(title, el("p", "t3ai-by", `by ${pack.creator}`));
    const facts = el("div", "t3ai-facts");
    if (pack.gear) facts.append(el("span", "t3ai-fact", GEAR_LABELS[pack.gear] || pack.gear));
    const count = fileCount(pack);
    if (count) facts.append(el("span", "t3ai-fact", count));
    // Only show the named model sizes players filter by.
    const sizes = (pack.sizes || []).filter((z) => ["standard", "lite", "feather", "nano"].includes(String(z).toLowerCase()));
    if (sizes.length) facts.append(el("span", "t3ai-fact", sizes.map(titleCase).join(", ")));
    if (Number.isFinite(pack.downloads_count)) facts.append(el("span", "t3ai-fact", `${compact(pack.downloads_count)} downloads`));
    if (pack.year) facts.append(el("span", "t3ai-fact", pack.year));
    if (pack.license) {
      const code = pack.license.toLowerCase();
      const name = code === "t3k" ? "T3K licence" : code.startsWith("cc") ? `${pack.license.toUpperCase().replace(/[-_]/g, " ")} licence` : pack.license;
      const license = el("span", "t3ai-fact", name);
      facts.append(license);
    }
    body.append(facts);
    if (pack.ai_why) body.append(el("p", "t3ai-why", pack.ai_why));
    if (pack.tags && pack.tags.length) {
      const tags = el("div", "t3ai-tags");
      pack.tags.slice(0, 6).forEach((t) => {
        const tag = el("button", "t3ai-tag", t);
        tag.type = "button";
        tag.setAttribute("aria-label", `Filter results by tag ${t}`);
        tag.addEventListener("click", () => filterByTag(t));
        tags.append(tag);
      });
      body.append(tags);
    }
    if (pack.description) {
      const d = el("details", "t3ai-desc");
      d.append(el("summary", null, "Pack description"), el("p", null, pack.description));
      body.append(d);
    }
    const actions = el("div", "t3ai-actions");
    const open = el("button", "btn btn-primary btn-small", "Files & questions");
    open.type = "button";
    open.setAttribute("aria-expanded", "false");
    open.setAttribute("aria-controls", `${uid}-panel`);
    open.setAttribute("aria-describedby", title.id); // tells the many identical buttons apart
    actions.append(open);
    if (pack.url) {
      const link = el("a", "btn btn-secondary btn-small", "Open on TONE3000");
      link.href = pack.url; newTab(link);
      link.setAttribute("aria-describedby", title.id);
      actions.append(link);
    }
    body.append(actions);
    const panel = el("div", "t3ai-panel");
    panel.id = `${uid}-panel`;
    panel.hidden = true;
    card.append(media, body, panel);
    open.addEventListener("click", () => togglePanel(card, panel, open, pack));
    return card;
  }

  async function togglePanel(card, panel, button, pack) {
    if (!panel.hidden) {
      panel.hidden = true; card.classList.remove("is-open"); button.textContent = "Files & questions";
      button.setAttribute("aria-expanded", "false");
      return;
    }
    panel.hidden = false; card.classList.add("is-open"); button.textContent = "Close";
    button.setAttribute("aria-expanded", "true");
    if (panel.dataset.loaded) return;
    panel.replaceChildren(el("p", "info", "Loading the NAM files in this pack..."));
    try {
      const response = await fetch(`api/packs/${encodeURIComponent(pack.id)}/models?architecture=${packArchitecture()}`, { headers: settingsHeaders() });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not load this pack");
      buildPanel(panel, pack, data.models);
      panel.dataset.loaded = "1";
      announce(`${data.models.length} file${data.models.length === 1 ? "" : "s"} loaded for ${pack.title}.`);
    } catch (error) {
      panel.replaceChildren(el("p", "t3ai-warning", error.message));
      announce(error.message);
    }
  }

  // Fetched rather than linked so the visitor's TONE3000 key header goes with it.
  async function downloadModel(pack, model, button, row) {
    button.disabled = true;
    row.querySelector(".t3ai-warning")?.remove();
    try {
      const response = await fetch(`api/packs/${encodeURIComponent(pack.id)}/models/${encodeURIComponent(model.id)}/download?architecture=${packArchitecture()}`, { headers: settingsHeaders() });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || `Download failed (${response.status})`);
      }
      const blob = await response.blob();
      const named = /filename="?([^";]+)"?/i.exec(response.headers.get("Content-Disposition") || "");
      const link = el("a");
      link.href = URL.createObjectURL(blob);
      link.download = named ? named[1] : `${model.name}.nam`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    } catch (error) {
      row.append(el("p", "t3ai-warning", error.message));
      announce(error.message);
    } finally {
      button.disabled = false;
    }
  }

  const titleCase = (text) => text.charAt(0).toUpperCase() + text.slice(1);
  // Files shown for a pack follow the NAM version of the search that found it.
  const packArchitecture = () => {
    const f = state.searchedFilters || {};
    return f.format && f.format !== "nam" ? "any" : (f.architecture || "2");
  };
  function fileCount(pack) {
    const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
    const f = state.searchedFilters || {};
    if (f.format === "ir") return Number.isFinite(pack.irs_count) ? plural(pack.irs_count, "IR") : "";
    if (f.format && f.format !== "nam") return Number.isFinite(pack.models_count) ? plural(pack.models_count, "model") : "";
    if (f.architecture === "1") return Number.isFinite(pack.a1_models_count) ? plural(pack.a1_models_count, "A1 NAM") : "";
    if (f.architecture === "any") return Number.isFinite(pack.models_count) ? plural(pack.models_count, "NAM") : "";
    return Number.isFinite(pack.a2_models_count) ? plural(pack.a2_models_count, "A2 NAM") : "";
  }

  async function downloadPack(pack, button, column) {
    button.disabled = true;
    column.querySelector(":scope > .t3ai-warning")?.remove();
    try {
      button.textContent = "Preparing...";
      const response = await fetch(`api/packs/${encodeURIComponent(pack.id)}/download?architecture=${packArchitecture()}`, { headers: settingsHeaders() });
      const isJson = (response.headers.get("Content-Type") || "").includes("json");
      if (!response.ok) {
        const data = isJson ? await response.json().catch(() => ({})) : {};
        throw new Error(data.error || `Download failed (${response.status})`);
      }
      if (isJson) {
        window.location.href = (await response.json()).url; // a short-lived TONE3000 link, downloaded directly
      } else {
        const link = el("a");
        link.href = URL.createObjectURL(await response.blob());
        link.download = `${pack.title.replace(/[^\w .()-]+/g, "_").trim() || "tone3000-pack"}.zip`;
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      }
      announce(`Downloading ${pack.title}.`);
    } catch (error) {
      column.append(el("p", "t3ai-warning", error.message));
      announce(error.message);
    } finally {
      button.disabled = false;
      button.textContent = "Download all";
    }
  }

  const FILES_PER_PAGE = 12;

  function buildPanel(panel, pack, models) {
    panel.replaceChildren();
    let picks = [];
    let page = 0;
    let filter = "";

    // Files: AI picks pinned on top, then a filterable, paged list.
    const fileColumn = el("section", "t3ai-file-column");
    const fileHead = el("div", "t3ai-panel-head");
    fileHead.append(el("h4", null, `${models.length} file${models.length === 1 ? "" : "s"}`));
    const downloadAll = el("button", "btn btn-secondary btn-small", "Download all");
    downloadAll.type = "button";
    downloadAll.setAttribute("aria-label", `Download all files in ${pack.title}`);
    downloadAll.addEventListener("click", () => downloadPack(pack, downloadAll, fileColumn));
    fileHead.append(downloadAll);
    const search = el("input", "t3ai-file-filter");
    search.type = "search"; search.placeholder = "Filter files";
    search.setAttribute("aria-label", `Filter the files in ${pack.title}`);
    if (models.length > FILES_PER_PAGE) fileHead.append(search);
    const pickList = el("ul", "t3ai-files t3ai-picks");
    const files = el("ul", "t3ai-files");
    const pager = el("div", "t3ai-pager");
    fileColumn.append(fileHead, pickList, files, pager);

    const fileRow = (model) => {
      const row = el("li", "t3ai-file");
      const name = el("span", "t3ai-file-name", model.name);
      if (picks.includes(model.name)) { row.classList.add("is-pick"); name.append(" ", el("span", "t3ai-pick", "AI pick")); }
      const download = el("button", "btn btn-secondary btn-small", "Download");
      download.type = "button";
      download.setAttribute("aria-label", `Download ${model.name}`);
      download.addEventListener("click", () => downloadModel(pack, model, download, row));
      row.append(name, download);
      return row;
    };
    const renderFiles = () => {
      const pinned = models.filter((m) => picks.includes(m.name));
      pickList.hidden = pinned.length === 0;
      pickList.replaceChildren(...(pinned.length ? [el("li", "t3ai-picks-label", "AI picks")] : []), ...pinned.map(fileRow));
      const shown = models.filter((m) => m.name.toLowerCase().includes(filter));
      const pages = Math.max(1, Math.ceil(shown.length / FILES_PER_PAGE));
      page = Math.min(page, pages - 1);
      const from = page * FILES_PER_PAGE;
      files.replaceChildren(...shown.slice(from, from + FILES_PER_PAGE).map(fileRow));
      if (!shown.length) files.append(el("li", "info", "No files match that filter."));
      pager.hidden = pages <= 1;
      const prev = el("button", "btn btn-secondary btn-small", "Previous");
      const next = el("button", "btn btn-secondary btn-small", "Next");
      prev.type = next.type = "button";
      prev.disabled = page === 0; next.disabled = page >= pages - 1;
      // The pager is rebuilt on each page, so focus moves to the new button (or its enabled neighbour).
      const goTo = (step, which) => {
        page += step; renderFiles();
        const [p, n] = pager.querySelectorAll("button");
        const target = which === "next" ? (n.disabled ? p : n) : (p.disabled ? n : p);
        target.focus();
        announce(pager.querySelector(".t3ai-pager-label").textContent.replace("-", " to "));
      };
      prev.addEventListener("click", () => goTo(-1, "prev"));
      next.addEventListener("click", () => goTo(1, "next"));
      pager.replaceChildren(prev, el("span", "t3ai-pager-label", `${from + 1}-${Math.min(from + FILES_PER_PAGE, shown.length)} of ${shown.length}`), next);
    };
    search.addEventListener("input", () => { filter = search.value.trim().toLowerCase(); page = 0; renderFiles(); });
    renderFiles();

    // Chat: scrolling conversation, suggestion chips, composer pinned underneath.
    const chat = el("section", "t3ai-chat");
    const chatHead = el("div", "t3ai-panel-head");
    chatHead.append(el("h4", null, "Ask about this pack"));
    const log = el("div", "t3ai-chat-log"); // replies are read out by the announcer, not a live log
    log.setAttribute("role", "log");
    log.setAttribute("aria-label", `Conversation about ${pack.title}`);
    log.tabIndex = 0; // scrollable, so keyboard users can reach and scroll it
    const empty = el("p", "t3ai-chat-empty", "Ask which file suits your tone, what the file names mean, or how this pack compares.");
    log.append(empty);
    const suggestions = el("div", "t3ai-suggest");
    ["Which file fits my tone best?", "What do the file names mean?", "Which is best for rhythm vs lead?"].forEach((q) => {
      const chip = el("button", "t3ai-chip-btn", q);
      chip.type = "button";
      chip.addEventListener("click", () => { input.value = q; send(); });
      suggestions.append(chip);
    });
    const input = el("textarea", "t3ai-chat-input");
    input.rows = 2; input.maxLength = 600;
    input.placeholder = "Ask about this pack...";
    input.setAttribute("aria-label", `Ask about ${pack.title}`);
    const ask = el("button", "btn btn-primary btn-small", "Ask");
    ask.type = "button";
    const composer = el("div", "t3ai-chat-composer");
    composer.append(input, ask);
    chat.append(chatHead, log, suggestions, composer);

    const scrollLog = () => { log.scrollTop = log.scrollHeight; };
    const history = [];
    async function send() {
      const question = input.value.trim();
      if (!question || ask.disabled) return;
      ask.disabled = true; input.disabled = true;
      empty.remove();
      log.append(el("div", "t3ai-you", question));
      const thinking = el("div", "t3ai-ai is-thinking");
      const spinner = el("span", "spinner");
      spinner.setAttribute("aria-hidden", "true");
      thinking.append(spinner, " Thinking...");
      log.append(thinking);
      scrollLog();
      input.value = "";
      try {
        const data = await post("api/pack_chat", {
          tone_id: pack.id, question, tone_goal: state.goal, history, architecture: packArchitecture(),
          pack: { title: pack.title, creator: pack.creator, description: pack.description, tags: pack.tags || [] },
        });
        thinking.className = "t3ai-ai"; thinking.textContent = data.reply;
        history.push({ role: "user", content: question }, { role: "assistant", content: data.reply });
        if (!state.packChats.has(pack.id)) state.packChats.set(pack.id, { pack, messages: [] });
        state.packChats.get(pack.id).messages.push({ question, reply: data.reply, picks: data.recommended_files, at: new Date() });
        exportBtn.hidden = false;
        picks = data.recommended_files;
        page = 0;
        renderFiles();
        announce(data.reply);
      } catch (error) {
        thinking.className = "t3ai-warning"; thinking.textContent = error.message;
      } finally {
        ask.disabled = false; input.disabled = false; input.focus();
        scrollLog();
      }
    }
    ask.addEventListener("click", send);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
    panel.append(fileColumn, chat);
  }

  async function search() {
    const prompt = promptEl.value.trim();
    if (!prompt || state.busy) {
      if (!prompt) { status.textContent = "Describe the tone you want first."; announce(status.textContent); promptEl.focus(); }
      return;
    }
    setBusy(true);
    status.textContent = "";
    let turn;
    // The intro and About make way for the conversation.
    await smoothly(() => { document.body.classList.add("is-session"); turn = startTurn(prompt); });
    const stop = showProgress(research.checked ? RESEARCH_STAGES : PLAIN_STAGES, turn.pending);
    try {
      const data = await post("api/search", {
        prompt, use_research: research.checked, filters: filters.current(), history: state.history,
      });
      if (!state.goal) state.goal = prompt;
      else state.goal = `${state.goal} / refined: ${prompt}`.slice(-600);
      state.history.push({ role: "user", content: prompt }, { role: "assistant", content: data.plan.summary });
      state.history = state.history.slice(-12);
      state.plan = { summary: data.plan.summary, search_queries: data.queries };
      state.log.push({ kind: "search", at: new Date(), prompt, data });
      exportBtn.hidden = false;
      renderBrief(turn, data);
      renderResults(data);
      announce(`Tone brief ready. ${data.plan.summary} ${status.textContent}`);
      document.title = `${prompt.slice(0, 60)} – TONE Search`;
      promptEl.value = "";
      label.textContent = "Refine the search (e.g. more gain, darker, a different era, a cheaper amp)";
      promptEl.placeholder = "e.g. a bit more gain for solos";
      resetBtn.hidden = false;
    } catch (error) {
      stop();
      failTurn(turn, error.message);
      announce(error.message);
    } finally {
      stop(); setBusy(false);
    }
  }

  // Results: the AI's fit sorts by default; the visitor can re-order instantly. Weak matches fold away.
  const WEAK_FIT = 30;
  const ORDER_FROM_SORT = { "best-match": "fit", "downloads-all-time": "downloads", trending: "catalogue", newest: "newest", oldest: "oldest" };
  const ORDER_NAMES = { fit: "best fit first", downloads: "most downloaded first", newest: "newest first", oldest: "oldest first", catalogue: "in TONE3000's order" };
  const ORDERS = {
    fit: (a, b) => (b.ai_fit ?? -1) - (a.ai_fit ?? -1) || (b.downloads_count || 0) - (a.downloads_count || 0),
    downloads: (a, b) => (b.downloads_count || 0) - (a.downloads_count || 0),
    newest: (a, b) => String(b.published || "").localeCompare(String(a.published || "")),
    oldest: (a, b) => String(a.published || "").localeCompare(String(b.published || "")),
    catalogue: (a, b) => (a.catalog_order ?? 999) - (b.catalog_order ?? 999),
  };

  function renderResults(data) {
    state.searchedFilters = data.filters;
    filters.markApplied();
    state.results = data.results;
    state.cards = new Map(data.results.map((pack) => [pack.id, packCard(pack)])); // reused so open packs survive re-ordering
    state.warning = data.reused_plan && data.warnings.length ? ` ${data.warnings.join(" ")}` : "";
    orderSelect.value = ORDER_FROM_SORT[data.filters?.sort] || "fit";
    drawResults();
  }

  function drawResults() {
    const order = orderSelect.value;
    const sorted = [...state.results].sort(ORDERS[order]);
    const strong = sorted.filter((p) => !Number.isFinite(p.ai_fit) || p.ai_fit >= WEAK_FIT);
    const weak = sorted.filter((p) => Number.isFinite(p.ai_fit) && p.ai_fit < WEAK_FIT);
    const rated = strong.filter((p) => Number.isFinite(p.ai_fit));
    const unrated = strong.filter((p) => !Number.isFinite(p.ai_fit));
    const card = (p) => state.cards.get(p.id);
    grid.replaceChildren(...(order === "fit" ? rated : strong).map(card));
    if (order === "fit" && unrated.length) {
      grid.append(el("h3", "t3ai-grid-heading", rated.length ? "Other catalogue matches (not rated by the AI)" : "Catalogue matches"), ...unrated.map(card));
    }
    if (weak.length) {
      const details = el("details", "t3ai-weak");
      details.append(el("summary", null, `Weak matches (${weak.length}): the AI rated these under ${WEAK_FIT}% fit`));
      const inner = el("div", "t3ai-grid");
      inner.append(...weak.map(card));
      details.append(inner);
      grid.append(details);
    }
    const total = state.results.length;
    grid.hidden = total === 0;
    resultsBar.hidden = total === 0;
    status.textContent = (total
      ? `${strong.length} good match${strong.length === 1 ? "" : "es"}${weak.length ? ` and ${weak.length} weak` : ""}, ${ORDER_NAMES[order]}.`
      : "No TONE3000 packs matched. Try removing some filters, or name a specific amp.") + state.warning;
  }

  orderSelect.addEventListener("change", () => {
    drawResults();
    announce(`Results re-ordered: ${ORDER_NAMES[orderSelect.value]}.`);
  });

  // Re-runs the catalogue search for the latest tone brief with the current filters; no new AI plan.
  async function applyFilters() {
    if (!state.plan || state.busy) return;
    setBusy(true);
    const stop = showProgress([[0, "Searching TONE3000 with your filters..."], [6, "The AI is ranking the packs..."]]);
    announce("Searching TONE3000 with your filters...");
    try {
      const data = await post("api/search", {
        prompt: state.goal.slice(0, 600), use_research: false, filters: filters.current(), reuse_plan: state.plan, history: [],
      });
      stop();
      renderResults(data);
      state.log.push({ kind: "filters", at: new Date(), data });
      announce(status.textContent);
      grid.hidden || grid.scrollIntoView({ behavior: motion(), block: "start" });
    } catch (error) {
      stop();
      status.textContent = error.message;
      announce(error.message);
    } finally {
      stop(); setBusy(false);
    }
  }

  function filterByTag(tag) {
    filters.addToken("tags", tag);
    announce(`Showing packs tagged ${tag}.`);
    applyFilters();
  }

  // Markdown export of the whole session, built in the browser. Keys and settings are never included.
  function exportMarkdown() {
    const md = (text) => String(text ?? "").replace(/([\\`*_[\]<>|])/g, "\\$1").replace(/\s*\n\s*/g, " ").trim();
    // Longer AI text keeps its paragraphs and line breaks.
    const mdBlock = (text) => String(text ?? "").trim().split(/\n\s*\n/).map((para) => para.split("\n").map(md).join("  \n")).join("\n\n");
    const time = (d) => d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    const link = (label, url) => (url ? `[${md(label)}](${url})` : md(label));
    const out = ["# TONE Search conversation", "", `Exported ${time(new Date())} from ${location.origin}${location.pathname}`, ""];
    const packs = (data) => {
      if (!data.results.length) return ["No TONE3000 packs matched.", ""];
      const lines = ["| Fit | Pack | Creator | Details | Why |", "| --- | --- | --- | --- | --- |"];
      data.results.forEach((p) => {
        const details = [p.gear && (GEAR_LABELS[p.gear] || p.gear), fileCount(p), Number.isFinite(p.downloads_count) && `${compact(p.downloads_count)} downloads`]
          .filter(Boolean).join(", ");
        lines.push(`| ${Number.isFinite(p.ai_fit) ? `${p.ai_fit}%` : "not rated"} | ${link(p.title, p.url)} | ${md(p.creator)} | ${md(details)} | ${md(p.ai_why || "")} |`);
      });
      return [...lines, ""];
    };
    let step = 0;
    state.log.forEach((entry) => {
      const { data } = entry;
      if (entry.kind === "filters") {
        out.push(`## Filters applied (${time(entry.at)})`, "", `Filters: ${md(filters.describe(data.filters))}`, "", "### Matching packs", "", ...packs(data));
        return;
      }
      step += 1;
      out.push(`## ${step}. ${step === 1 ? "Request" : "Refinement"} (${time(entry.at)})`, "", `> ${md(entry.prompt)}`, "", "### Tone brief", "", mdBlock(data.plan.summary), "");
      if (data.plan.gear.length) {
        Object.keys(KIND_LABELS).forEach((kind) => {
          const items = data.plan.gear.filter((g) => g.kind === kind);
          if (!items.length) return;
          const names = items.map((g) => `${md(g.name)}${/uncertain|unconfirmed|possibly|likely|may have|might have|disputed/i.test(g.role || "") ? " (unconfirmed)" : ""}${g.role ? `: ${md(g.role)}` : ""}`);
          out.push(`**${KIND_LABELS[kind]}**`, "", ...names.map((n) => `- ${n}`), "");
        });
      }
      if (data.plan.advice.length) out.push("**How to get there**", "", ...data.plan.advice.map((tip) => `- ${md(tip)}`), "");
      out.push(`Searched TONE3000 for ${data.queries.map((q) => `"${md(q)}"`).join(", ")}${data.researched ? " after web research" : ""}. Filters: ${md(filters.describe(data.filters))}.${md(answeredBy(data.ai))}`, "");
      if (data.research_notes) {
        out.push("**Web research sources**", "");
        data.research_notes.split("\n").filter(Boolean).forEach((line) => {
          const parts = /^- (.*?): ([\s\S]*) \((https?:\/\/[^\s)]+)\)$/.exec(line);
          out.push(parts ? `- ${link(parts[1] || parts[3], parts[3])}: ${md(parts[2])}` : `- ${md(line.replace(/^- /, ""))}`);
        });
        out.push("");
      }
      if (data.warnings.length) out.push("**Warnings**", "", ...data.warnings.map((w) => `- ${md(w)}`), "");
      out.push("### Matching packs", "", ...packs(data));
    });
    if (state.packChats.size) {
      out.push("## Questions about packs", "");
      state.packChats.forEach(({ pack, messages }) => {
        out.push(`### ${link(pack.title, pack.url)} by ${md(pack.creator)}`, "");
        messages.forEach((m) => {
          out.push(`**Q:** ${md(m.question)}`, "", `**A:** ${mdBlock(m.reply)}`, "");
          if (m.picks.length) out.push(`AI picks: ${m.picks.map(md).join(", ")}`, "");
        });
      });
    }
    out.push("---", "", "Captures come from TONE3000 and belong to their creators. AI answers can be wrong; trust your ears.", "");
    return out.join("\n");
  }

  function exportConversation() {
    const stamp = new Date().toISOString().slice(0, 16).replace("T", "-").replace(":", "");
    const link = el("a");
    link.href = URL.createObjectURL(new Blob([exportMarkdown()], { type: "text/markdown;charset=utf-8" }));
    link.download = `tone-search-${stamp}.md`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    announce("Conversation exported as a Markdown file.");
  }
  exportBtn.addEventListener("click", exportConversation);

  searchBtn.addEventListener("click", () => (!promptEl.value.trim() && state.plan ? applyFilters() : search()));
  promptEl.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); search(); } });
  resetBtn.addEventListener("click", () => {
    state.history = []; state.goal = ""; state.plan = null; filters.markApplied();
    state.log = []; state.packChats = new Map(); exportBtn.hidden = true;
    thread.replaceChildren(); grid.replaceChildren(); grid.hidden = true; resultsBar.hidden = true; state.results = [];
    status.textContent = ""; resetBtn.hidden = true;
    smoothly(() => document.body.classList.remove("is-session"));
    document.title = "TONE Search: find NAM captures for any guitar or bass tone";
    announce("Started over.");
    label.textContent = "Describe the tone you want";
    promptEl.placeholder = "e.g. Early Stevie Ray Vaughan: fat, bright, on the edge of breakup, digs in when I pick hard";
    promptEl.focus();
  });

  function setupSettings() {
    // Workers AI models suggested in Settings: [id, note, input $/1M, output $/1M, rough searches per free day].
    const CF_MODELS = [
      ["@cf/deepseek-ai/deepseek-r1-distill-qwen-32b", "Good results in testing. Highest output cost here, so the free allowance runs out sooner.", "0.497", "4.881", 13],
      ["@cf/zai-org/glm-4.7-flash", "Promising for search planning. Low cost; guitar advice quality needs testing.", "0.0605", "0.400", 144],
      ["@cf/mistralai/mistral-small-3.1-24b-instruct", "Supports structured JSON. Worth testing for search generation and clear advice.", "0.351", "0.555", 41],
      ["@cf/qwen/qwen3-30b-a3b-fp8", "Lowest cost here. Promising for understanding requests and producing searches.", "0.0509", "0.335", 171],
      ["@cf/openai/gpt-oss-120b", "Worth testing for interpreting research and explaining picks. Moderate allowance use.", "0.350", "0.750", 38],
      ["@cf/google/gemma-4-26b-a4b-it", "Low cost, but repeated timeouts have been seen in this app's testing.", "0.100", "0.300", 122],
      ["@cf/meta/llama-3.3-70b-instruct-fp8-fast", "Fast and conversational, and does not think. Higher cost without a clear quality advantage here.", "0.293", "2.253", 27],
      ["@cf/qwen/qwen3.8-27b", "Adjustable reasoning. High output cost makes it less suited to frequent free use.", "0.450", "3.200", 18],
    ];
    const dialog = document.getElementById("settings-dialog");
    const form = document.getElementById("settings-form");
    const openBtn = document.getElementById("btn-settings");
    const badge = document.getElementById("settings-badge");
    const notice = document.getElementById("server-ai-notice");
    const error = document.getElementById("settings-error");
    const providerEl = form.elements.provider;
    const fields = [...form.querySelectorAll("input[name]")];

    const read = (s, name) => name.split(".").reduce((v, k) => (v || {})[k], s) || "";
    const showProvider = () => {
      form.querySelectorAll("[data-provider]").forEach((g) => { g.hidden = g.dataset.provider !== providerEl.value; });
      form.querySelector("[data-tuning]").hidden = !providerEl.value && !localModel;
    };
    const limitStatus = document.getElementById("settings-limit-status");
    const ownKeys = (s) => {
      const ai = (s.provider === "cloudflare" || s.provider === "custom") && !!(s[s.provider] || {}).api_key;
      return { ai, tone3000: !!s.tone3000_api_key };
    };
    // Mirrors app._brings_own: the limit is skipped only with both an AI key and a TONE3000 key.
    const describeLimit = (s) => {
      if (localModel) return ["ok", `Local AI on this computer (${localModel}): no hourly limit.`];
      const own = ownKeys(s);
      if (own.ai && own.tone3000) return ["ok", "Your AI and TONE3000 keys are set: no hourly limit."];
      if (own.ai) return ["partial", "AI key set. Add a TONE3000 key to remove the hourly limit."];
      if (own.tone3000) return ["partial", "TONE3000 key set. Add your own AI key to remove the hourly limit."];
      return ["none", "Using this site's keys: the hourly limit applies."];
    };
    const showLimit = (s) => { const [state, text] = describeLimit(s); limitStatus.dataset.state = state; limitStatus.textContent = text; };
    const reflect = () => {
      const ownAi = settings.provider === "cloudflare" || settings.provider === "custom";
      const own = ownKeys(settings);
      badge.hidden = !(ownAi || settings.tone3000_api_key || localModel);
      badge.textContent = localModel && !ownAi ? " · local AI" : own.ai && own.tone3000 ? " · your keys, no limit" : " · your keys";
      if (notice) notice.hidden = ownAi;
    };
    const fill = () => {
      providerEl.value = settings.provider || "";
      fields.forEach((input) => { input.value = read(settings, input.name); });
      error.hidden = true;
      showProvider();
      showLimit(settings);
    };
    const collect = () => {
      const next = { provider: providerEl.value };
      fields.forEach((input) => {
        const value = input.value.trim();
        const [group, key] = input.name.includes(".") ? input.name.split(".") : [null, input.name];
        if (group) (next[group] ||= {})[key] = value;
        else next[key] = value;
      });
      return next;
    };
    const problem = (s) => {
      const ai = s[s.provider] || {};
      if (s.provider && !ai.model) return "Enter the AI model name.";
      if (s.provider === "cloudflare" && !/^[0-9a-f]{32}$/i.test(ai.account_id)) return "The Cloudflare account ID must be 32 hexadecimal characters.";
      if (s.provider === "cloudflare" && !ai.api_key) return "Enter your Cloudflare API token.";
      if (s.provider === "custom" && !/^https:\/\/\S+$/i.test(ai.base_url)) return "The base URL must start with https://";
      if (s.tone3000_api_key && !s.tone3000_api_key.startsWith("t3k_cs_")) return "The TONE3000 key must be a secret key starting with t3k_cs_.";
      return "";
    };
    const save = (next) => {
      settings = next;
      try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(next)); } catch { /* private mode: keep for this page only */ }
      reflect();
    };

    // Suggested Workers AI models: suggestions while typing, plus a list with a button to use each one.
    const cfModel = form.elements["cloudflare.model"];
    document.getElementById("cf-models").replaceChildren(...CF_MODELS.map(([id]) => Object.assign(document.createElement("option"), { value: id })));
    document.getElementById("cf-model-list").replaceChildren(...CF_MODELS.map(([id, note, input, output, perDay]) => {
      const item = el("li", "model-item");
      const head = el("div", "model-head");
      const use = el("button", "btn btn-secondary btn-small", "Use this model");
      use.type = "button";
      use.setAttribute("aria-label", `Use this model: ${id}`);
      use.addEventListener("click", () => {
        cfModel.value = id;
        cfModel.dispatchEvent(new Event("input", { bubbles: true }));
        cfModel.focus();
        announce(`Model set to ${id}.`);
      });
      head.append(el("code", "model-id", id), use);
      item.append(head, el("p", "model-note", note),
        el("p", "model-cost", `$${input} in · $${output} out per 1M tokens · about ${perDay} searches per free day`));
      return item;
    }));
    providerEl.addEventListener("change", showProvider);
    form.addEventListener("input", () => showLimit(collect()));
    providerEl.addEventListener("change", () => showLimit(collect()));
    // Older browsers lack <dialog> methods: fall back to toggling the open attribute.
    const openDialog = () => (dialog.showModal ? dialog.showModal() : dialog.setAttribute("open", ""));
    const closeDialog = () => (dialog.close ? dialog.close() : dialog.removeAttribute("open"));
    openBtn.addEventListener("click", () => { fill(); openDialog(); });
    document.getElementById("btn-settings-cancel").addEventListener("click", closeDialog);
    // Clear all asks first: it deletes keys the visitor may not have written down anywhere else.
    const confirmBox = document.getElementById("settings-clear-confirm");
    const mainActions = document.getElementById("settings-main-actions");
    const clearBtn = document.getElementById("btn-settings-clear");
    const askToClear = (show) => {
      confirmBox.hidden = !show;
      mainActions.hidden = show;
      (show ? document.getElementById("btn-clear-keep") : clearBtn).focus(); // the safe choice has focus
    };
    clearBtn.addEventListener("click", () => askToClear(true));
    document.getElementById("btn-clear-keep").addEventListener("click", () => askToClear(false));
    document.getElementById("btn-clear-yes").addEventListener("click", () => {
      save({});
      try { localStorage.removeItem(SETTINGS_KEY); } catch { /* ignore */ }
      fill();
      askToClear(false);
      announce("Settings cleared. Using this site's keys.");
    });
    confirmBox.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); askToClear(false); }
    });
    dialog.addEventListener("close", () => { confirmBox.hidden = true; mainActions.hidden = false; });
    form.addEventListener("submit", (event) => {
      const next = collect();
      const message = problem(next);
      if (message) {
        event.preventDefault();
        error.textContent = message;
        error.hidden = false;
        error.focus();
        return;
      }
      save(next);
      if (!dialog.close) { event.preventDefault(); closeDialog(); }
      announce("Settings saved.");
    });
    reflect();
  }

  // First visit: a welcome dialog explains the site, the free hourly searches and how to add your own keys.
  function setupWelcome() {
    const SEEN_KEY = "tonesearch.welcomed";
    const dialog = document.getElementById("welcome-dialog");
    if (!dialog || !dialog.showModal) return; // very old browsers just see the page and its About section
    const seen = () => { try { return localStorage.getItem(SEEN_KEY) === "1"; } catch { return false; } };
    dialog.addEventListener("close", () => {
      try { localStorage.setItem(SEEN_KEY, "1"); } catch { /* private mode: it may show again next visit */ }
      if (dialog.returnValue !== "settings" && !document.querySelector("dialog[open]")) promptEl.focus();
    });
    document.getElementById("btn-welcome-settings").addEventListener("click", () => {
      dialog.close("settings");
      document.getElementById("btn-settings").click();
    });
    document.getElementById("btn-about").addEventListener("click", () => dialog.showModal());
    if (!seen() && !hasStoredKeys()) dialog.showModal();
  }
  // Visitors who already saved their own keys have clearly been here before.
  function hasStoredKeys() {
    const ai = settings[settings.provider] || {};
    return Boolean(ai.api_key || settings.tone3000_api_key);
  }

  // Search filters: native checkboxes and selects in fieldsets, plus autocomplete token fields.
  function setupFilters() {
    const KEY = "tonesearch.filters";
    const DEFAULTS = { gears: [], sizes: [], makes: [], tags: [], creators: [], format: "nam", architecture: "2",
      sort: "best-match", calibrated: false, verified: false };
    const LABELS = {
      gears: { amp: "Amp head", "amp-cab": "Full rig", pedal: "Pedal", outboard: "Outboard", cab: "Cab", space: "Reverb & delay", experimental: "Experimental" },
      sizes: { standard: "Standard", lite: "Lite", feather: "Feather", nano: "Nano" },
      format: { ir: "IR", "aida-x": "AIDA-X", "aa-snapshot": "AA snapshot", proteus: "Proteus" },
      architecture: { 1: "A1 NAMs", any: "any NAM version" },
      sort: { "downloads-all-time": "most downloaded", trending: "trending", newest: "newest", oldest: "oldest" },
    };
    const panel = document.getElementById("filters-panel");
    const toggle = document.getElementById("btn-filters");
    const countEl = document.getElementById("filters-count");
    const summary = document.getElementById("filters-summary");
    const applyBtn = document.getElementById("btn-filters-apply");
    const archSelect = panel.querySelector('select[name="architecture"]');

    let value = { ...DEFAULTS };
    try { value = { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(KEY)) || {}) }; } catch { /* storage unavailable */ }
    let applied = JSON.stringify(value);

    const tokenFields = {};
    panel.querySelectorAll(".token-field").forEach((container) => {
      tokenFields[container.dataset.kind] = tokenField(container, (kind, tokens) => { value[kind] = tokens; sync(); });
    });

    const activeCount = () => ["gears", "sizes", "makes", "tags", "creators"].reduce((n, k) => n + value[k].length, 0)
      + (value.format !== "nam") + (value.format === "nam" && value.architecture !== "2") + (value.sort !== "best-match")
      + value.calibrated + value.verified;
    const describe = (value) => {
      const parts = [];
      const names = [...value.gears.map((g) => LABELS.gears[g]), ...value.sizes.map((z) => LABELS.sizes[z])];
      if (value.format !== "nam") names.push(LABELS.format[value.format]);
      else if (value.architecture !== "2") names.push(LABELS.architecture[value.architecture]);
      if (value.calibrated) names.push("calibrated");
      if (value.verified) names.push("verified");
      if (names.length) parts.push(names.join(", "));
      if (value.makes.length) parts.push(`Make: ${value.makes.join(", ")}`);
      if (value.tags.length) parts.push(`Tags: ${value.tags.join(", ")}`);
      if (value.creators.length) parts.push(`Creator: ${value.creators.join(", ")}`);
      if (value.sort !== "best-match") parts.push(`Sorted by ${LABELS.sort[value.sort]}`);
      return parts.join(" · ");
    };

    function sync() {
      panel.querySelectorAll('input[type="checkbox"][name]').forEach((box) => {
        box.checked = Array.isArray(value[box.name]) ? value[box.name].includes(box.value) : Boolean(value[box.name]);
      });
      panel.querySelectorAll("select[name]").forEach((select) => { select.value = value[select.name]; });
      archSelect.disabled = value.format !== "nam";
      Object.entries(tokenFields).forEach(([kind, field]) => field.set(value[kind]));
      const count = activeCount();
      countEl.hidden = count === 0;
      countEl.textContent = ` (${count})`;
      const pending = Boolean(state.plan) && JSON.stringify(value) !== applied;
      const describeCurrent = () => describe(value);
      summary.hidden = count === 0 && !pending;
      summary.textContent = `${count ? `Filters: ${describeCurrent()}` : "Filters cleared"}${pending ? " (not applied to these results yet)" : ""}`;
      applyBtn.hidden = !pending;
      try { localStorage.setItem(KEY, JSON.stringify(value)); } catch { /* ignore */ }
    }

    panel.addEventListener("change", (event) => {
      const input = event.target;
      if (!input.name || input.closest(".token-field")) return;
      if (input.type === "checkbox" && Array.isArray(value[input.name])) {
        value[input.name] = input.checked ? [...value[input.name], input.value] : value[input.name].filter((v) => v !== input.value);
      } else if (input.type === "checkbox") {
        value[input.name] = input.checked;
      } else {
        value[input.name] = input.value;
      }
      sync();
    });
    toggle.addEventListener("click", () => {
      const open = panel.hidden;
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (open) panel.querySelector("input, select").focus();
    });
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !event.target.closest(".token-field")) {
        panel.hidden = true; toggle.setAttribute("aria-expanded", "false"); toggle.focus();
      }
    });
    document.getElementById("btn-filters-clear").addEventListener("click", () => {
      value = { ...DEFAULTS, gears: [], sizes: [], makes: [], tags: [], creators: [] };
      sync();
      announce("Filters cleared.");
    });
    applyBtn.addEventListener("click", () => applyFilters());
    sync();

    return {
      current: () => JSON.parse(JSON.stringify(value)),
      addToken: (kind, token) => tokenFields[kind].add(token),
      markApplied: () => { applied = JSON.stringify(value); sync(); },
      describe: (f) => describe({ ...DEFAULTS, ...(f || {}) }) || "none",
    };
  }

  // Accessible autocomplete (ARIA 1.2 combobox + listbox) that collects values as removable tokens.
  function tokenField(container, onChange) {
    const kind = container.dataset.kind;
    const label = container.dataset.label;
    const id = `filter-${kind}`;
    container.classList.add("field");
    const labelEl = el("label", null, label);
    labelEl.htmlFor = `${id}-input`;
    const tokenList = el("ul", "tokens");
    tokenList.setAttribute("aria-label", `Selected ${label.toLowerCase()}`);
    const combo = el("div", "combo");
    const input = el("input");
    Object.assign(input, { id: `${id}-input`, type: "text", autocomplete: "off", placeholder: container.dataset.placeholder || "", maxLength: 60 });
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", `${id}-list`);
    input.setAttribute("aria-describedby", `${id}-hint`);
    const listbox = el("ul", "combo-list");
    listbox.id = `${id}-list`;
    listbox.setAttribute("role", "listbox");
    listbox.setAttribute("aria-label", `${label} suggestions`);
    listbox.hidden = true;
    const hint = el("p", "filter-help", "Type to see suggestions from TONE3000, then press Enter to add.");
    hint.id = `${id}-hint`;
    combo.append(input, listbox);
    container.append(labelEl, tokenList, combo, hint);

    let tokens = [];
    let options = [];
    let active = -1;
    let timer = null;
    let latest = 0;

    const close = () => { listbox.hidden = true; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); active = -1; };
    const renderTokens = () => {
      tokenList.replaceChildren(...tokens.map((token) => {
        const item = el("li");
        const remove = el("button", "token", token);
        remove.type = "button";
        remove.setAttribute("aria-label", `Remove ${label.toLowerCase()} ${token}`);
        remove.append(el("span", "token-x", "×"));
        remove.lastChild.setAttribute("aria-hidden", "true");
        remove.addEventListener("click", () => {
          tokens = tokens.filter((t) => t !== token);
          renderTokens(); onChange(kind, tokens);
          announce(`Removed ${token}.`); input.focus();
        });
        item.append(remove);
        return item;
      }));
      tokenList.hidden = tokens.length === 0;
    };
    const add = (raw) => {
      const token = String(raw || "").trim().slice(0, 60);
      if (!token || /[_,]/.test(token) || tokens.length >= 10 || tokens.some((t) => t.toLowerCase() === token.toLowerCase())) return false;
      tokens = [...tokens, token];
      input.value = "";
      close(); renderTokens(); onChange(kind, tokens);
      announce(`Added ${label.toLowerCase()} ${token}.`);
      return true;
    };
    const highlight = (index) => {
      active = index;
      [...listbox.children].forEach((option, i) => option.setAttribute("aria-selected", String(i === active)));
      if (active >= 0) {
        input.setAttribute("aria-activedescendant", listbox.children[active].id);
        listbox.children[active].scrollIntoView?.({ block: "nearest" });
      } else input.removeAttribute("aria-activedescendant");
    };
    const renderOptions = () => {
      listbox.replaceChildren(...options.map((option, i) => {
        const item = el("li", "combo-option", option.label);
        item.id = `${id}-opt-${i}`;
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", "false");
        if (Number.isFinite(option.count)) item.append(el("span", "visually-hidden", ", "), el("span", "combo-count", `${compact(option.count)} tones`));
        item.addEventListener("mousedown", (event) => { event.preventDefault(); add(option.value); });
        return item;
      }));
      const open = options.length > 0 && document.activeElement === input;
      listbox.hidden = !open;
      input.setAttribute("aria-expanded", String(open));
      active = -1;
      if (open) announce(`${options.length} suggestion${options.length === 1 ? "" : "s"}. Use the arrow keys to choose.`);
    };
    const lookup = async (query) => {
      const ticket = ++latest;
      try {
        const response = await fetch(`api/lookup/${kind}?query=${encodeURIComponent(query)}`, { headers: settingsHeaders() });
        const data = await response.json().catch(() => ({}));
        if (ticket !== latest || !response.ok) return;
        options = (data.suggestions || []).filter((o) => !tokens.includes(o.value));
        renderOptions();
      } catch { /* suggestions are optional: typed values still work */ }
    };

    input.addEventListener("input", () => {
      clearTimeout(timer);
      const query = input.value.trim();
      if (query.length < 2) { options = []; close(); return; }
      timer = setTimeout(() => lookup(query), 250);
    });
    input.addEventListener("keydown", (event) => {
      const open = !listbox.hidden;
      if (event.key === "ArrowDown" && options.length) {
        event.preventDefault();
        if (!open) { listbox.hidden = false; input.setAttribute("aria-expanded", "true"); }
        highlight((active + 1) % options.length);
      } else if (event.key === "ArrowUp" && open) {
        event.preventDefault();
        highlight(active <= 0 ? options.length - 1 : active - 1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        add(open && active >= 0 ? options[active].value : input.value);
      } else if (event.key === "Escape" && open) {
        event.preventDefault(); event.stopPropagation(); close();
      } else if (event.key === "Backspace" && !input.value && tokens.length) {
        const removed = tokens[tokens.length - 1];
        tokens = tokens.slice(0, -1);
        renderTokens(); onChange(kind, tokens); announce(`Removed ${removed}.`);
      }
    });
    input.addEventListener("blur", () => setTimeout(close, 120));
    renderTokens();

    return {
      add,
      set: (values) => { if (values.join("\n") !== tokens.join("\n")) { tokens = [...values]; renderTokens(); } },
    };
  }
})();
