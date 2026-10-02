// TONE Search cab embed page: preview a capture + IR in the browser, train them as one model on Kaggle.
// Audio runs through NeuralAmpModelerCore compiled to WebAssembly (static/vendor/nam-wasm), so files are
// only uploaded when the visitor starts a training, and then only to their own Kaggle account.
import { NamEngine } from "./vendor/nam-wasm/index.js";

const ASSETS = { assetBaseUrl: new URL("./vendor/nam-wasm/", import.meta.url) };
const RATE = 48000; // NAM captures and the training input run at 48 kHz
const IR_TRIM_DB = -40; // leading silence trimmed like the trainer (cab_kernel.LEADING_SILENCE_THRESHOLD_DB)
const COMPARE_SECONDS = 20;
const COMPARE_SKIP = 4096; // the engine fades in for 1024 frames after a model loads
const POLL_MS = 30_000;
const FINISHED = new Set(["complete", "error", "cancel_acknowledged"]);
const JOBS_KEY = "tonesearch-cab-jobs";
const CREDS_KEY = "tonesearch-cab-kaggle";

const $ = (id) => document.getElementById(id);
const announcer = $("announcer");
const announce = (text) => { announcer.textContent = ""; setTimeout(() => { announcer.textContent = text; }, 50); };
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const setInfo = (node, text, isError = false) => { node.textContent = text || ""; node.classList.toggle("is-error", isError); };

const files = { nam: null, ir: null, di: null, trained: null };

// ---- Files -------------------------------------------------------------------------------------------

function describeCapture(json) {
  const meta = json.metadata && typeof json.metadata === "object" ? json.metadata : {};
  const arch = json.architecture;
  const kind = arch === "SlimmableContainer" ? "A2" : arch === "WaveNet" ? "A1 (WaveNet)" : arch || "unknown";
  const rate = json.sample_rate || (arch === "SlimmableContainer" ? json.config?.submodels?.[0]?.model?.sample_rate : null);
  const notes = [];
  let trainable = arch === "WaveNet" || arch === "SlimmableContainer";
  if (arch === "Sequential") notes.push("This file already contains a cabinet stage; use the amp-only capture.");
  else if (!trainable) notes.push(`${kind} captures can be previewed but not trained here; use an A1 or A2 capture.`);
  if (rate && Number(rate) !== RATE) { notes.push(`It runs at ${rate} Hz; training needs 48 kHz.`); trainable = false; }
  if (meta.gear_type === "amp_cab" || meta.gear_type === "amp_pedal_cab") {
    notes.push("Its metadata says it already includes a cab, so the IR would be added on top.");
  }
  return { name: (typeof meta.name === "string" && meta.name.trim()) || null, kind, trainable, notes };
}

// The picker card shows the chosen file's name and whether it can be used.
function picked(inputId, file, state) {
  const card = $(inputId).closest(".file-pick");
  card.dataset.state = file ? state : "empty";
  const name = card.querySelector(".file-pick-name");
  if (!name.dataset.empty) name.dataset.empty = name.textContent;
  name.textContent = file ? file.name : name.dataset.empty;
}

// Files can be dropped on a picker card as well as chosen.
document.querySelectorAll(".file-pick").forEach((card) => {
  const input = card.querySelector(".file-pick-input");
  card.addEventListener("dragover", (event) => { event.preventDefault(); card.classList.add("is-dragging"); });
  card.addEventListener("dragleave", () => card.classList.remove("is-dragging"));
  card.addEventListener("drop", (event) => {
    event.preventDefault();
    card.classList.remove("is-dragging");
    if (!event.dataTransfer.files.length) return;
    input.files = event.dataTransfer.files;
    input.dispatchEvent(new Event("change"));
  });
});

$("nam-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  files.nam = null;
  if (!file) { picked("nam-file", null); return setInfo($("nam-info"), ""); }
  try {
    const text = await file.text();
    const json = JSON.parse(text);
    if (!json || typeof json !== "object" || !json.architecture) throw new Error("not a NAM capture");
    const info = describeCapture(json);
    files.nam = { file, text, info };
    setInfo($("nam-info"), [`${info.name || file.name} · ${info.kind}.`, ...info.notes].join(" "), !info.trainable);
    picked("nam-file", file, info.trainable ? "ok" : "error");
    suggestName();
    player.modelChanged("capture");
  } catch (error) {
    picked("nam-file", file, "error");
    setInfo($("nam-info"), `That file isn't a NAM capture (${error.message}).`, true);
  }
});

// The IR as the trainer prepares it: mono average, leading silence trimmed, 48 kHz (decodeAudioData resamples).
async function prepareIr(bytes) {
  const decoder = new OfflineAudioContext(1, 1, RATE);
  const decoded = await decoder.decodeAudioData(bytes.slice(0));
  const mono = new Float32Array(decoded.length);
  for (let c = 0; c < decoded.numberOfChannels; c++) {
    const data = decoded.getChannelData(c);
    for (let i = 0; i < data.length; i++) mono[i] += data[i] / decoded.numberOfChannels;
  }
  let peak = 0;
  for (const v of mono) peak = Math.max(peak, Math.abs(v));
  if (peak <= 1e-9) throw new Error("the IR is silent");
  const threshold = peak * 10 ** (IR_TRIM_DB / 20);
  const start = Math.max(0, mono.findIndex((v) => Math.abs(v) >= threshold));
  const taps = mono.subarray(start);
  return { taps, seconds: taps.length / RATE, channels: decoded.numberOfChannels };
}

$("ir-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  files.ir = null;
  if (!file) { picked("ir-file", null); return setInfo($("ir-info"), ""); }
  try {
    const bytes = await file.arrayBuffer();
    const prepared = await prepareIr(bytes);
    files.ir = { file, taps: prepared.taps };
    const long = prepared.seconds > 0.13 ? " Longer than an A2 model can hear, so training approximates its tail." : "";
    picked("ir-file", file, "ok");
    setInfo($("ir-info"), `${(prepared.seconds * 1000).toFixed(0)} ms · ${prepared.channels === 1 ? "mono" : `${prepared.channels} channels, averaged to mono`}.${long}`);
    suggestName();
    player.modelChanged("ir");
  } catch (error) {
    picked("ir-file", file, "error");
    setInfo($("ir-info"), `That IR can't be read (${error.message}). Use a WAV file.`, true);
  }
});

$("di-file").addEventListener("change", (event) => {
  files.di = event.target.files[0] || null;
  picked("di-file", files.di, "ok");
  updateCompare();
});

$("trained-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  picked("trained-file", file, "ok");
  loadTrained(file.name, await file.text());
});

function loadTrained(name, text) {
  try { JSON.parse(text); } catch { setInfo($("listen-status"), "That trained file isn't a NAM capture.", true); return; }
  files.trained = { name, text };
  $("trained-option").hidden = false;
  $("chain-help").textContent = `Amp + cab IR is what training learns; Trained capture (${name}) is what it learned. Switch between them to compare.`;
  player.modelChanged("trained");
  updateCompare();
}

function suggestName() {
  const field = $("model-name");
  if (field.dataset.edited) return;
  const capture = files.nam ? files.nam.info.name || files.nam.file.name.replace(/\.nam$/i, "") : null;
  const ir = files.ir ? files.ir.file.name.replace(/\.wav$/i, "") : null;
  field.value = capture && ir ? `${capture} + ${ir}`.slice(0, 100) : "";
}
$("model-name").addEventListener("input", (event) => { event.target.dataset.edited = event.target.value ? "1" : ""; });

// ---- Listening ---------------------------------------------------------------------------------------

const chainValue = () => document.querySelector('input[name="chain"]:checked').value;
const sourceValue = () => document.querySelector('input[name="source"]:checked').value;
const dbToGain = (db) => 10 ** (db / 20);

// For listening, the IR is scaled to unit energy: many IRs add 15-20 dB, which would clip, and training lowers
// the level anyway when the pair peaks above -0.2 dBFS. Measuring the match compares shapes, not levels.
function irBuffer(context, { forListening = false } = {}) {
  let taps = files.ir.taps;
  if (forListening) {
    let energy = 0;
    for (const v of taps) energy += v * v;
    const scale = energy > 0 ? 1 / Math.sqrt(energy) : 1;
    taps = taps.map((v) => v * scale);
  }
  const buffer = context.createBuffer(1, taps.length, RATE);
  buffer.copyToChannel(taps, 0);
  return buffer;
}

async function decodeDi(context) {
  const decoded = await context.decodeAudioData(await files.di.arrayBuffer());
  if (decoded.numberOfChannels === 1) return decoded;
  const mono = context.createBuffer(1, decoded.length, RATE);
  const out = mono.getChannelData(0);
  for (let c = 0; c < decoded.numberOfChannels; c++) {
    const data = decoded.getChannelData(c);
    for (let i = 0; i < data.length; i++) out[i] += data[i] / decoded.numberOfChannels;
  }
  return mono;
}

// One model per chain: the capture (with or without the IR) or the trained model.
function modelFor(chain) {
  if (chain === "trained") return files.trained ? { key: `t:${files.trained.name}`, text: files.trained.text } : null;
  return files.nam ? { key: `c:${files.nam.file.name}:${files.nam.file.lastModified}`, text: files.nam.text } : null;
}

const player = {
  context: null, node: null, convolver: null, gain: null, source: null, stream: null, loadedKey: null, playing: false,

  async ensureContext() {
    if (!this.context) {
      this.context = new AudioContext({ sampleRate: RATE, latencyHint: "interactive" });
      const engine = await NamEngine.attach(this.context, ASSETS);
      this.node = await engine.createNode();
      this.gain = this.context.createGain();
      this.gain.gain.value = dbToGain(Number($("out-level").value));
      this.gain.connect(this.context.destination);
    }
    if (this.context.state === "suspended") await this.context.resume();
  },

  async route() {
    const chain = chainValue();
    const model = modelFor(chain);
    if (!model) throw new Error(chain === "trained" ? "Load a trained model first." : "Choose a capture first.");
    if (chain === "pair" && !files.ir) throw new Error("Choose a cabinet IR first, or pick Capture only.");
    if (this.loadedKey !== model.key) {
      const info = await this.node.loadModel(model.text);
      this.loadedKey = model.key;
      if (info.expectedSampleRate > 0 && info.expectedSampleRate !== RATE) {
        setInfo($("listen-status"), `This model expects ${info.expectedSampleRate} Hz; it may sound wrong at 48 kHz.`, true);
      }
    }
    this.node.disconnect();
    if (this.convolver) { this.convolver.disconnect(); this.convolver = null; }
    if (chain === "pair") {
      this.convolver = this.context.createConvolver();
      this.convolver.normalize = false; // scaled by irBuffer instead, the same way every time
      this.convolver.buffer = irBuffer(this.context, { forListening: true });
      this.node.connect(this.convolver).connect(this.gain);
    } else {
      this.node.connect(this.gain);
    }
  },

  async start() {
    await this.ensureContext();
    await this.route();
    if (sourceValue() === "live") {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 },
      });
      this.source = this.context.createMediaStreamSource(this.stream);
    } else {
      if (!files.di) throw new Error("Choose a DI recording, or pick Live input.");
      const buffer = await decodeDi(this.context);
      this.source = this.context.createBufferSource();
      this.source.buffer = buffer;
      this.source.loop = true;
      this.source.start();
    }
    this.source.connect(this.node);
    this.playing = true;
  },

  stop() {
    if (this.source) { try { this.source.stop?.(); } catch { /* already stopped */ } this.source.disconnect(); this.source = null; }
    if (this.stream) { this.stream.getTracks().forEach((track) => track.stop()); this.stream = null; }
    this.playing = false;
  },

  // A file changed: drop a model that no longer matches, and re-route if we're playing.
  async modelChanged() {
    if (!this.playing) return;
    try { await this.route(); } catch (error) { this.stop(); syncButtons(); setInfo($("listen-status"), error.message, true); }
  },
};

function syncButtons() {
  const button = $("btn-play");
  button.textContent = player.playing ? "Stop" : "Play";
  button.dataset.playing = String(player.playing);
}

$("btn-play").addEventListener("click", async () => {
  if (player.playing) { player.stop(); syncButtons(); setInfo($("listen-status"), "Stopped."); return; }
  setInfo($("listen-status"), "Starting…");
  try {
    await player.start();
    setInfo($("listen-status"), sourceValue() === "live" ? "Playing your input live." : "Playing (looping).");
  } catch (error) {
    player.stop();
    setInfo($("listen-status"), error.name === "NotAllowedError" ? "The browser wasn't allowed to use your audio input." : error.message, true);
  }
  syncButtons();
});
document.querySelectorAll('input[name="chain"]').forEach((radio) => radio.addEventListener("change", () => player.modelChanged()));
function showSource() {
  const live = sourceValue() === "live";
  $("di-pick").hidden = live;
  $("live-hint").hidden = !live;
}
document.querySelectorAll('input[name="source"]').forEach((radio) => radio.addEventListener("change", async () => {
  showSource();
  if (!player.playing) return;
  player.stop();
  $("btn-play").click();
}));
$("out-level").addEventListener("input", (event) => {
  $("out-level-value").textContent = `${event.target.value} dB`;
  event.target.setAttribute("aria-valuetext", `${event.target.value} dB`);
  if (player.gain) player.gain.gain.value = dbToGain(Number(event.target.value));
});

// ---- Measuring the match -----------------------------------------------------------------------------

function updateCompare() {
  $("btn-compare").disabled = !(files.trained && files.nam && files.ir && files.di);
}

async function renderOffline(text, withIr, length, diBuffer) {
  const context = new OfflineAudioContext(1, length, RATE);
  const engine = await NamEngine.attach(context, ASSETS);
  const node = await engine.createNode();
  await node.loadModel(text);
  const source = context.createBufferSource();
  source.buffer = diBuffer;
  let tail = node;
  source.connect(node);
  if (withIr) {
    const convolver = context.createConvolver();
    convolver.normalize = false;
    convolver.buffer = irBuffer(context);
    tail = node.connect(convolver);
  }
  tail.connect(context.destination);
  source.start();
  const rendered = await context.startRendering();
  await node.dispose();
  return rendered.getChannelData(0);
}

// Error-to-signal ratio after matching levels: training lowers the level when the pair would clip.
function compare(reference, candidate) {
  let dot = 0, energy = 0, refEnergy = 0;
  for (let i = COMPARE_SKIP; i < reference.length; i++) {
    dot += reference[i] * candidate[i]; energy += candidate[i] * candidate[i]; refEnergy += reference[i] * reference[i];
  }
  if (!energy || !refEnergy) return null;
  const gain = dot / energy;
  let error = 0;
  for (let i = COMPARE_SKIP; i < reference.length; i++) { const d = reference[i] - gain * candidate[i]; error += d * d; }
  return { esr: error / refEnergy, levelDb: 20 * Math.log10(Math.abs(gain)) };
}

$("btn-compare").addEventListener("click", async () => {
  const button = $("btn-compare");
  button.disabled = true;
  setInfo($("listen-status"), "Rendering both versions…");
  try {
    const decoder = new OfflineAudioContext(1, 1, RATE);
    const full = await decodeDi(decoder);
    const length = Math.min(full.length, COMPARE_SECONDS * RATE);
    if (length <= COMPARE_SKIP * 4) throw new Error("The DI recording is too short to compare.");
    const reference = await renderOffline(files.nam.text, true, length, full);
    const candidate = await renderOffline(files.trained.text, false, length, full);
    const result = compare(reference, candidate);
    if (!result) throw new Error("One of the versions was silent.");
    const level = Math.abs(result.levelDb) < 0.5 ? "at the same level"
      : `${Math.abs(result.levelDb).toFixed(1)} dB ${result.levelDb > 0 ? "quieter" : "louder"} than the pair`;
    const text = `Error-to-signal ratio ${result.esr.toFixed(4)} once levels are matched (lower is closer; around 0.01 or less is a very close match). The trained model plays ${level}.`;
    setInfo($("listen-status"), text);
    announce(text);
  } catch (error) {
    setInfo($("listen-status"), `Couldn't compare: ${error.message}`, true);
  }
  updateCompare();
});

// ---- Kaggle credentials ------------------------------------------------------------------------------

function loadCreds() {
  for (const store of [sessionStorage, localStorage]) {
    try {
      const saved = JSON.parse(store.getItem(CREDS_KEY));
      if (saved && saved.username) {
        $("kaggle-username").value = saved.username;
        $("kaggle-key").value = saved.key || "";
        $("kaggle-remember").checked = store === localStorage;
        return;
      }
    } catch { /* storage unavailable */ }
  }
}

function saveCreds() {
  const value = JSON.stringify({ username: $("kaggle-username").value.trim(), key: $("kaggle-key").value.trim() });
  try {
    if ($("kaggle-remember").checked) { localStorage.setItem(CREDS_KEY, value); sessionStorage.removeItem(CREDS_KEY); }
    else { sessionStorage.setItem(CREDS_KEY, value); localStorage.removeItem(CREDS_KEY); }
  } catch { /* private mode: kept for this page only */ }
}
["kaggle-username", "kaggle-key", "kaggle-remember"].forEach((id) => $(id).addEventListener("change", saveCreds));

const creds = () => ({ username: $("kaggle-username").value.trim(), key: $("kaggle-key").value.trim() });
const haveCreds = () => Boolean(creds().username && creds().key);
const kaggleHeaders = () => ({ "X-Kaggle-Username": creds().username, "X-Kaggle-Key": creds().key });

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...kaggleHeaders(), ...(options.headers || {}) } });
  let data = {};
  try { data = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) throw new Error(data.error || `The server returned HTTP ${response.status}.`);
  return data;
}

$("btn-check").addEventListener("click", async () => {
  const status = $("check-status");
  if (!haveCreds()) { setInfo(status, "Enter your Kaggle username and key first.", true); $("kaggle-username").focus(); return; }
  setInfo(status, "Checking…");
  try {
    const quota = await api("api/cab/check", { method: "POST" });
    const hours = quota.allowed_hours != null
      ? `GPU this week: ${quota.used_hours ?? 0} of ${quota.allowed_hours} hours used.` : "GPU hours not reported.";
    setInfo(status, `Connected. ${hours}`);
  } catch (error) {
    setInfo(status, error.message, true);
  }
});

// ---- Training jobs -----------------------------------------------------------------------------------

const PRESET_LABELS = { draft: "Draft", standard: "Standard", high_def: "High" };
const STATE_LABELS = {
  queued: "Waiting for a GPU", running: "Training", complete: "Finished", error: "Failed", new_script: "Starting",
  cancel_requested: "Stopping", cancel_acknowledged: "Stopped", missing: "Starting", gone: "Not found on Kaggle",
};

let jobs = [];
try { jobs = JSON.parse(localStorage.getItem(JOBS_KEY)) || []; } catch { jobs = []; }
const saveJobs = () => { try { localStorage.setItem(JOBS_KEY, JSON.stringify(jobs)); } catch { /* ignore */ } };

function renderJobs() {
  const list = $("jobs");
  list.replaceChildren();
  $("jobs-empty").hidden = jobs.length > 0;
  for (const job of jobs) list.append(jobItem(job));
}

function jobItem(job) {
  const item = el("li", "cab-job");
  item.dataset.state = job.state || "queued";
  const head = el("div", "cab-job-head");
  head.append(el("strong", "", job.name));
  const state = el("span", "cab-state", STATE_LABELS[job.state] || job.state || "Starting");
  state.dataset.state = job.state || "queued";
  if (!FINISHED.has(job.state) && job.state !== "gone") { const spin = el("span", "spinner"); spin.setAttribute("aria-hidden", "true"); state.prepend(spin); }
  head.append(state);
  item.append(head);
  const when = new Date(job.created).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const facts = [`Started ${when}`, PRESET_LABELS[job.preset] || job.preset];
  if (job.progress && !FINISHED.has(job.state)) facts.push(job.progress);
  item.append(el("p", "info", facts.join(" · ")));
  const result = job.result;
  if (result && result.success) {
    const bits = [];
    if (result.validation_esr != null) bits.push(`Training ESR ${Number(result.validation_esr).toFixed(4)}`);
    if (result.training_seconds) bits.push(`${Math.round(result.training_seconds / 60)} min on ${result.gpu || "GPU"}`);
    if (result.cab_approximated) bits.push("the IR's tail is approximated (longer than an A2 model hears)");
    if (bits.length) item.append(el("p", "info", bits.join(" · ")));
  } else if (job.state === "error" || (result && result.error)) {
    item.append(el("p", "t3ai-warning", (result && result.error) || job.failure || "Kaggle reported an error. Open the job on Kaggle to see its log."));
  } else if (job.state === "gone") {
    item.append(el("p", "t3ai-warning", "Kaggle has no job with this name any more."));
  }
  if (job.warning) item.append(el("p", "info", job.warning));

  const actions = el("div", "t3ai-actions");
  const open = el("a", "btn btn-secondary btn-small", "Open on Kaggle");
  open.href = job.url; open.target = "_blank"; open.rel = "noopener noreferrer";
  open.append(el("span", "visually-hidden", " (opens in a new tab)"));
  actions.append(open);
  if (job.state === "complete" && result && result.success) {
    const download = el("button", "btn btn-primary btn-small", "Download");
    download.type = "button";
    download.addEventListener("click", () => downloadJob(job, download));
    actions.append(download);
  }
  const del = el("button", "btn btn-secondary btn-small", "Delete from Kaggle");
  del.type = "button";
  del.addEventListener("click", () => deleteJob(job, del));
  actions.append(del);
  const forget = el("button", "link-btn btn-small-link", "Remove from this list");
  forget.type = "button";
  forget.addEventListener("click", () => { jobs = jobs.filter((j) => j.ref !== job.ref); saveJobs(); renderJobs(); announce("Removed from the list."); });
  actions.append(forget);
  item.append(actions);
  return item;
}

async function refreshJob(job) {
  const [owner, slug] = job.ref.split("/");
  const reply = await api(`api/cab/jobs/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}`);
  let state = reply.state;
  if (state === "missing" && Date.now() - job.created > 15 * 60_000) state = "gone";
  const changed = state !== job.state;
  Object.assign(job, { state, failure: reply.failure, progress: reply.progress || job.progress, result: reply.result || job.result });
  if (changed && FINISHED.has(state)) announce(`${job.name}: ${STATE_LABELS[state]}.`);
}

async function pollJobs() {
  if (document.hidden || !haveCreds()) return;
  const open = jobs.filter((job) => !FINISHED.has(job.state) && job.state !== "gone");
  for (const job of open) {
    try { await refreshJob(job); } catch (error) { job.failure = error.message; }
  }
  if (open.length) { saveJobs(); renderJobs(); }
}

async function downloadJob(job, button) {
  if (!haveCreds()) { $("train-status").hidden = false; setInfo($("train-status"), "Enter your Kaggle username and key to download.", true); return; }
  button.disabled = true;
  const [owner, slug] = job.ref.split("/");
  try {
    const response = await fetch(`api/cab/jobs/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/download`, { headers: kaggleHeaders() });
    if (!response.ok) {
      let data = {};
      try { data = await response.json(); } catch { /* not JSON */ }
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const name = (job.result && job.result.filename) || `${job.name}.nam`;
    const link = el("a");
    link.href = URL.createObjectURL(blob);
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
    loadTrained(name, await blob.text());
    announce(`Downloaded ${name}. It's also loaded under Listen as the trained model.`);
  } catch (error) {
    job.failure = `Download failed: ${error.message}`;
    renderJobs();
  }
  button.disabled = false;
}

async function deleteJob(job, button) {
  if (button.dataset.confirm !== "1") {
    button.dataset.confirm = "1";
    button.textContent = "Yes, delete it on Kaggle";
    button.classList.add("btn-danger");
    return;
  }
  button.disabled = true;
  const [owner, slug] = job.ref.split("/");
  try {
    await api(`api/cab/jobs/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}`, { method: "DELETE" });
    jobs = jobs.filter((j) => j.ref !== job.ref);
    saveJobs();
    renderJobs();
    announce("Deleted on Kaggle.");
  } catch (error) {
    job.failure = error.message;
    renderJobs();
  }
}

$("train-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = $("train-status");
  const fail = (message, focus) => { status.hidden = false; status.textContent = message; (focus || status).focus(); };
  status.hidden = true;
  if (!files.nam) return fail("Choose an amp capture first.", $("nam-file"));
  if (!files.nam.info.trainable) return fail("This capture can't be trained here; see the note under it.", $("nam-file"));
  if (!files.ir) return fail("Choose a cabinet IR first.", $("ir-file"));
  if (!haveCreds()) return fail("Enter your Kaggle username and API key.", $(creds().username ? "kaggle-key" : "kaggle-username"));
  saveCreds();
  const button = $("btn-train");
  button.disabled = true;
  button.textContent = "Sending to Kaggle…";
  const form = new FormData();
  form.append("nam", files.nam.file);
  form.append("ir", files.ir.file);
  form.append("name", $("model-name").value.trim());
  form.append("preset", $("preset").value);
  try {
    const job = await api("api/cab/jobs", { method: "POST", body: form });
    jobs.unshift({ ref: job.ref, url: job.url, name: job.name, preset: job.preset, created: Date.now(), state: "missing", warning: job.warning });
    saveJobs();
    renderJobs();
    announce(`Training started on Kaggle: ${job.name}. It's listed under Your trainings.`);
    $("jobs-title").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    fail(error.message);
  }
  button.disabled = false;
  button.textContent = "Start training";
});

loadCreds();
renderJobs();
syncButtons();
showSource();
pollJobs();
setInterval(pollJobs, POLL_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollJobs(); });
