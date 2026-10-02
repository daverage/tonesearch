// TONE Search file tools: inspect a .nam file, check it in NAMCore (wasm), and change its volume or metadata.
// Nothing is uploaded. The edits and facts come from nam-edit.js, ported from NAM Mixer's NAM tools.
import * as nam from "./nam-edit.js";
import { $, RATE, announce, downloadBlob, el, enableDrop, picked, renderOffline, setInfo } from "./tool-ui.js";

const PROBE_SECONDS = 1;
const SKIP = 4096; // past the engine's fade-in
const GEAR_LABELS = { amp: "Amp", pedal: "Pedal", pedal_amp: "Pedal + amp", amp_cab: "Amp + cab", amp_pedal_cab: "Amp + pedal + cab", preamp: "Preamp", studio: "Studio gear" };
const TONE_LABELS = { clean: "Clean", crunch: "Crunch", overdrive: "Overdrive", hi_gain: "High gain", fuzz: "Fuzz" };
const CABINET_LABELS = {
  none: "No cab: the metadata says it's an amp, preamp or pedal on its own",
  included: "Includes a cab, according to its metadata",
  embedded: "A cab IR is built in as a Linear stage (an experimental Sequential file)",
  unknown: "Not stated in the file",
};

const state = { file: null, text: "", data: null, facts: null };
// Values some exporters write for "not set" (TONE3000 writes t3k-unset). Shown as not set, and left alone
// unless the visitor types something new.
const PLACEHOLDER = /^(t3k-unset|unset|unknown|none|n\/a|-)$/i;
const shown = (value) => (value === null || value === undefined || PLACEHOLDER.test(String(value).trim()) ? "" : String(value).trim());

// A fixed, repeatable test signal: an impulse after the fade-in, then quiet noise (like NAM Mixer's inspector).
function probe(rate) {
  const length = Math.round(rate * PROBE_SECONDS);
  const signal = new Float32Array(length);
  let seed = 0x4e414d;
  const random = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 2 ** 32; };
  for (let i = 0; i < length; i += 2) {
    const r = Math.sqrt(-2 * Math.log(random() || 1e-12)), t = 2 * Math.PI * random();
    signal[i] = 0.03 * r * Math.cos(t);
    if (i + 1 < length) signal[i + 1] = 0.03 * r * Math.sin(t);
  }
  signal[SKIP] = 0.2;
  return signal;
}

function levels(output) {
  let peak = 0, energy = 0, finite = true;
  for (let i = SKIP; i < output.length; i++) {
    const v = output[i];
    if (!Number.isFinite(v)) { finite = false; break; }
    peak = Math.max(peak, Math.abs(v));
    energy += v * v;
  }
  const db = (v) => (v > 0 ? 20 * Math.log10(v) : -Infinity);
  return { finite, silent: peak < 1e-6, peakDb: db(peak), rmsDb: db(Math.sqrt(energy / (output.length - SKIP))) };
}

const dbText = (v) => (Number.isFinite(v) ? `${v.toFixed(1)} dB` : "silent");

// ---- Inspecting ----------------------------------------------------------------------------------------

function fact(list, term, value) {
  if (value === null || value === undefined || value === "") return;
  list.append(el("dt", "", term), el("dd", "", value));
}

function showFacts() {
  const f = state.facts, m = f.metadata;
  const about = $("facts-about"), tech = $("facts-tech");
  about.replaceChildren(); tech.replaceChildren();
  fact(about, "Name", shown(f.name) || "Not set");
  fact(about, "Modeled by", shown(m.modeled_by) || "Not set");
  fact(about, "Gear", [shown(m.gear_make), shown(m.gear_model)].filter(Boolean).join(" ") || "Not set");
  fact(about, "Gear type", GEAR_LABELS[f.gear_type] || shown(f.gear_type) || "Not set");
  fact(about, "Tone type", TONE_LABELS[m.tone_type] || shown(m.tone_type) || "Not set");
  fact(about, "Cabinet", CABINET_LABELS[f.cabinet]);
  const format = [f.kind];
  if (f.submodels) format.push(`Full and Lite sizes`);
  if (f.stages.length) format.push(`stages: ${f.stages.join(" → ")}`);
  if (f.version) format.push(`version ${f.version}`);
  fact(tech, "Format", format.join(" · "));
  fact(tech, "Sample rate", f.sample_rate ? `${f.sample_rate / 1000} kHz` : "Not stated (players assume 48 kHz)");
  fact(tech, "Size", `${f.weights.toLocaleString()} weights · ${(state.file.size / 1024).toFixed(0)} KB`);
  fact(tech, "Looks back", f.receptive_field ? `${f.receptive_field_ms} ms (${f.receptive_field.toLocaleString()} samples)` : "Its whole past (a recurrent model)");
  fact(tech, "Loudness", f.loudness_db !== null ? `${f.loudness_db.toFixed(1)} dB` : "Not stated");
  const c = f.calibration;
  fact(tech, "Calibration", c.status === "complete" ? `Input ${c.input_level_dbu} dBu, output ${c.output_level_dbu} dBu`
    : c.status === "input-only" ? `Input ${c.input_level_dbu} dBu (no output level)` : "Not calibrated");
}

async function check() {
  const box = $("check"), text = $("check-text"), detail = $("check-detail");
  box.dataset.state = "running";
  text.textContent = "Checking in NAMCore…";
  detail.textContent = "";
  const rate = state.facts.sample_rate || RATE;
  const signal = probe(rate);
  const packed = state.facts.architecture === "SlimmableContainer";
  const runs = packed ? [["Full", undefined], ["Lite", 0]] : [["", undefined]];
  const peaks = [], problems = [];
  let normalised = false;
  try {
    for (const [label, slimSize] of runs) {
      const { info, output } = await renderOffline(state.text, signal, { rate, slimSize });
      const l = levels(output);
      const who = label ? `the ${label} size` : "it";
      if (!l.finite) problems.push(`${who} outputs invalid numbers`);
      else if (l.silent) problems.push(`${who} is silent`);
      else peaks.push(`${label ? `${label} ` : ""}${dbText(l.peakDb)}`);
      normalised ||= info.hasLoudness;
    }
  } catch (error) {
    box.dataset.state = "failed";
    text.textContent = "NAMCore can't load this file.";
    detail.textContent = `The engine said: ${error.message}`;
    announce(`${text.textContent} ${detail.textContent}`);
    return;
  }
  const failed = problems.some((p) => p.includes("invalid"));
  box.dataset.state = failed ? "failed" : problems.length ? "warning" : "passed";
  text.textContent = problems.length
    ? `Loads in NAMCore, but ${problems.join(" and ")}.`
    : `Loads and plays in NAMCore${packed ? ", in both its Full and Lite sizes" : ""}.`;
  if (peaks.length) detail.textContent = `Peak on a quiet test signal: ${peaks.join(", ")}${normalised ? ", after the engine evens out loudness to -18 dB" : ""}.`;
  announce(text.textContent);
}

// ---- Editing -------------------------------------------------------------------------------------------

function fillEditor() {
  const f = state.facts;
  for (const key of nam.EDITABLE_METADATA) {
    const input = $(`meta-${key}`);
    input.value = shown(f.metadata[key]);
    if (input.tagName === "INPUT") input.placeholder = PLACEHOLDER.test(String(f.metadata[key] ?? "")) ? `Not set (the file says "${f.metadata[key]}")` : "Not set";
  }
  $("gear-type-help").textContent = `Gear type: ${GEAR_LABELS[f.gear_type] || "not set"}. It describes what was captured, so it can't be changed here.`;
  const unsupported = Boolean(f.volume_unsupported);
  $("volume").disabled = $("volume-slider").disabled = $("btn-volume-zero").disabled = unsupported;
  setVolume(0);
  setInfo($("edit-status"), "");
}

function volumeValue() {
  return $("volume").disabled ? 0 : Number($("volume").value) || 0;
}

// The slider and the number box always show the same value, clamped to ±24 dB in 0.5 dB steps.
function setVolume(value, from) {
  const db = Math.max(-nam.MAX_VOLUME_DB, Math.min(nam.MAX_VOLUME_DB, Math.round((Number(value) || 0) * 2) / 2));
  if (from !== "number") $("volume").value = String(db);
  if (from !== "slider") $("volume-slider").value = String(db);
  const label = db === 0 ? "0 dB, no change" : `${db > 0 ? "+" : ""}${db} dB`;
  $("volume-slider").setAttribute("aria-valuetext", label);
  updateVolumeHelp();
  updateDirty();
}

function updateVolumeHelp() {
  const f = state.facts, help = $("volume-help"), result = $("volume-result");
  if (f.volume_unsupported) { help.textContent = f.volume_unsupported; result.textContent = ""; return; }
  const db = volumeValue();
  const loudness = f.loudness_db !== null ? `loudness ${f.loudness_db.toFixed(1)} dB${db ? ` → ${(f.loudness_db + db).toFixed(1)} dB` : ""}` : "";
  result.textContent = db ? `${Math.abs(db)} dB ${db > 0 ? "louder" : "quieter"}${loudness ? `: ${loudness}` : ""}` : `No change${loudness ? `: ${loudness}` : ""}`;
  result.dataset.warn = String(db > 12);
  help.textContent = db > 12 ? "Large boosts can clip in some players and pedals." : "";
}

function metadataChanges() {
  const changes = {};
  for (const key of nam.EDITABLE_METADATA) {
    const value = $(`meta-${key}`).value.trim();
    const original = state.facts.metadata[key];
    if (value === shown(original)) continue; // unchanged, including a placeholder left blank
    changes[key] = value || null;
  }
  return changes;
}

function updateDirty() {
  if (!state.facts) return;
  const count = Object.keys(metadataChanges()).length + (volumeValue() ? 1 : 0);
  $("btn-reset").disabled = count === 0;
  $("edit-count").textContent = count ? `${count} change${count === 1 ? "" : "s"} ready` : "";
}

// The edited copy must sound the same as the original apart from the volume change.
async function verify(edited, gain) {
  const rate = state.facts.sample_rate || RATE;
  const signal = probe(rate);
  const before = await renderOffline(state.text, signal, { rate });
  const after = await renderOffline(edited, signal, { rate });
  let dot = 0, energy = 0, ref = 0;
  for (let i = SKIP; i < signal.length; i++) {
    dot += before.output[i] * after.output[i]; energy += after.output[i] * after.output[i]; ref += before.output[i] * before.output[i];
  }
  if (!energy || !ref) throw new Error("the edited file is silent in NAMCore");
  const g = dot / energy;
  let error = 0;
  for (let i = SKIP; i < signal.length; i++) { const d = before.output[i] - g * after.output[i]; error += d * d; }
  if (error / ref > 1e-6) throw new Error("the edited file doesn't sound like the original");
  // With loudness in the file, the engine normalises both to -18 dB, so they match exactly; without it, the
  // edited file should be louder by the requested amount.
  const expected = after.info.hasLoudness ? 1 : gain;
  if (Math.abs(1 / g - expected) > 0.01 * expected) throw new Error("the edited file's level isn't what was asked for");
  return after.info.hasLoudness;
}

$("volume").addEventListener("input", (event) => setVolume(event.target.value, "number"));
$("volume").addEventListener("change", (event) => setVolume(event.target.value));
$("volume-slider").addEventListener("input", (event) => setVolume(event.target.value, "slider"));
$("btn-volume-zero").addEventListener("click", () => { setVolume(0); $("volume-slider").focus(); });
for (const key of nam.EDITABLE_METADATA) $(`meta-${key}`).addEventListener("input", updateDirty);

$("btn-reset").addEventListener("click", () => { fillEditor(); announce("Changes undone."); $("volume-slider").focus(); });

$("edit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = $("edit-status"), button = $("btn-save");
  const volumeDb = volumeValue();
  const metadata = metadataChanges();
  let result;
  try {
    result = nam.edit(state.data, { volumeDb, metadata });
  } catch (error) {
    setInfo(status, error.message, true);
    return;
  }
  if (!result.changed.length) { setInfo(status, "Nothing has changed yet."); return; }
  button.disabled = true;
  setInfo(status, "Checking the edited copy in NAMCore…");
  const text = JSON.stringify(result.data);
  try {
    const normalised = await verify(text, result.gain);
    const name = nam.editedFilename(state.file.name, volumeDb, Object.keys(metadata).length > 0);
    downloadBlob(new Blob([text], { type: "application/octet-stream" }), name);
    const level = volumeDb
      ? ` It's ${Math.abs(volumeDb)} dB ${volumeDb > 0 ? "louder" : "quieter"}${normalised ? "; players that normalise loudness will still play it at the same level" : ""}.`
      : "";
    const message = `Downloaded ${name}. Checked in NAMCore: it sounds the same as the original.${level} Changed: ${result.changed.length} value${result.changed.length === 1 ? "" : "s"}.`;
    setInfo(status, message);
    announce(message);
  } catch (error) {
    setInfo(status, `Not downloaded: ${error.message}.`, true);
  }
  button.disabled = false;
});

// ---- Loading -------------------------------------------------------------------------------------------

$("nam-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  $("inspect").hidden = $("editor").hidden = true;
  if (!file) { picked("nam-file", null); setInfo($("nam-info"), ""); return; }
  try {
    const text = await file.text();
    const data = nam.parseNam(text);
    Object.assign(state, { file, text, data, facts: nam.describe(data) });
    picked("nam-file", file, "ok");
    setInfo($("nam-info"), "");
    showFacts();
    fillEditor();
    $("inspect").hidden = $("editor").hidden = false;
    await check();
  } catch (error) {
    picked("nam-file", file, "error");
    setInfo($("nam-info"), error.message, true);
  }
});

enableDrop();
