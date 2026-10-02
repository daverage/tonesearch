// Safe edits and read-only facts for Neural Amp Modeler (.nam) files, with no DOM: used by tools.js and
// tested with `node --test tests/js`. Ported from NAM Mixer (hybrid-nam-builder): hybrid/training/nam_tools.py
// (volume and metadata), hybrid/core/receptive_field.py and hybrid/core/nam_loader.py (calibration).
//
// Every edit is checked the same way NAM Mixer checks it: the edited file is compared with the original, and
// the edit is refused unless exactly the expected JSON paths changed.

export class NamEditError extends Error {}

// NAM 0.13's UserMetadata. gear_type is a fact about what was captured (amp, or amp + cab), so it's shown but
// not editable; date, training and loudness belong to the trainer (loudness moves only with a volume change).
export const EDITABLE_METADATA = ["name", "modeled_by", "gear_make", "gear_model", "tone_type"];
export const TONE_TYPES = ["clean", "overdrive", "crunch", "hi_gain", "fuzz"];
const TEXT_FIELDS = new Set(["name", "modeled_by", "gear_make", "gear_model"]);
const MAX_TEXT = 200;
export const MAX_VOLUME_DB = 24;

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
const clone = (value) => (typeof structuredClone === "function" ? structuredClone(value) : JSON.parse(JSON.stringify(value)));

export function parseNam(text) {
  let data;
  try { data = JSON.parse(text); } catch { throw new NamEditError("This file isn't a NAM capture (it isn't valid JSON)."); }
  if (!isObject(data) || !data.architecture || !isObject(data.config)) throw new NamEditError("This file isn't a NAM capture.");
  return data;
}

// ---- Facts ---------------------------------------------------------------------------------------------

function layerHistory(layers) {
  const kernels = layers.kernel_sizes ?? layers.kernel_size;
  const dilations = layers.dilations;
  if (!Array.isArray(dilations)) return null;
  const sizes = Array.isArray(kernels) ? kernels : dilations.map(() => kernels);
  return sizes.reduce((sum, k, i) => sum + (Number(k) - 1) * Number(dilations[i]), 0);
}

// Samples of history a model depends on: 1 + sum((kernel - 1) * dilation) over its stacked layer arrays.
export function receptiveField(model) {
  if (!isObject(model)) return null;
  if (model.architecture === "WaveNet") {
    const arrays = model.config?.layers;
    if (!Array.isArray(arrays)) return null;
    let total = 1;
    for (const layers of arrays) {
      const history = layerHistory(layers);
      if (history === null || !Number.isFinite(history)) return null;
      total += history;
    }
    return total;
  }
  if (model.architecture === "SlimmableContainer") {
    const fields = (model.config?.submodels || []).map((entry) => receptiveField(entry?.model));
    return fields.length && fields.every((f) => f !== null) ? Math.max(...fields) : null;
  }
  return null; // LSTM and others depend on their whole past
}

const dig = (data, path) => path.reduce((value, key) => (isObject(value) ? value[key] : undefined), data);
const firstNumber = (data, paths) => paths.map((path) => dig(data, path)).find(isNumber) ?? null;

export function calibration(data) {
  const input = firstNumber(data, [["input_level_dbu"], ["metadata", "input_level_dbu"], ["metadata", "loudness", "input_level_dbu"]]);
  const output = firstNumber(data, [["output_level_dbu"], ["metadata", "output_level_dbu"], ["metadata", "loudness", "output_level_dbu"]]);
  const status = input !== null && output !== null ? "complete" : input !== null ? "input-only" : "absent";
  return { input_level_dbu: input, output_level_dbu: output, status };
}

function countWeights(model) {
  let total = Array.isArray(model?.weights) ? model.weights.length : 0;
  for (const entry of model?.config?.submodels || []) total += countWeights(entry?.model);
  for (const child of model?.config?.models || []) total += countWeights(child);
  return total;
}

export function describe(data) {
  const meta = isObject(data.metadata) ? data.metadata : {};
  const arch = data.architecture;
  const submodels = arch === "SlimmableContainer" && Array.isArray(data.config.submodels) ? data.config.submodels : [];
  const stages = arch === "Sequential" && Array.isArray(data.config.models) ? data.config.models.map((m) => m?.architecture) : [];
  const rate = isNumber(data.sample_rate) ? data.sample_rate : isNumber(submodels[0]?.model?.sample_rate) ? submodels[0].model.sample_rate : null;
  const history = receptiveField(data);
  let cabinet = "unknown";
  if (stages.includes("Linear")) cabinet = "embedded";
  else if (meta.gear_type === "amp_cab" || meta.gear_type === "amp_pedal_cab") cabinet = "included";
  else if (meta.gear_type === "amp" || meta.gear_type === "preamp" || meta.gear_type === "pedal" || meta.gear_type === "pedal_amp") cabinet = "none";
  let volume = null;
  try { findOutputScalers(data); } catch (error) { volume = error.message; }
  const loudness = isNumber(meta.loudness) ? meta.loudness : isNumber(meta.loudness?.db) ? meta.loudness.db : null;
  return {
    name: typeof meta.name === "string" && meta.name.trim() ? meta.name.trim() : null,
    metadata: Object.fromEntries(EDITABLE_METADATA.map((key) => [key, meta[key] ?? null])),
    gear_type: meta.gear_type ?? null,
    architecture: arch,
    kind: arch === "SlimmableContainer" ? "A2" : arch === "WaveNet" ? "A1 (WaveNet)" : arch,
    version: typeof data.version === "string" ? data.version : null,
    sample_rate: rate,
    submodels: submodels.length,
    stages,
    receptive_field: history,
    receptive_field_ms: history && rate ? Math.round((history * 1000 * 10) / rate) / 10 : null,
    weights: countWeights(data),
    loudness_db: loudness,
    calibration: calibration(data),
    cabinet,
    volume_unsupported: volume,
  };
}

// ---- Volume --------------------------------------------------------------------------------------------

export function gainFor(db) {
  if (!isNumber(db)) throw new NamEditError("Enter the volume change in dB.");
  if (Math.abs(db) > MAX_VOLUME_DB) throw new NamEditError(`Volume changes are limited to ±${MAX_VOLUME_DB} dB.`);
  return 10 ** (db / 20);
}

// NAMCore's WaveNet loader plays the LAST value of the flat weights array as head_scale and ignores
// config.head_scale. The trainer writes both; if they disagree, refuse rather than guess which is real.
function runtimeHeadScale(owner, config, label) {
  const weights = owner.weights;
  if (!Array.isArray(weights) || !weights.length || !isNumber(weights.at(-1))) {
    throw new NamEditError(`${label} has no weights ending in its output scale.`);
  }
  const stored = weights.at(-1), declared = config.head_scale;
  if (Math.abs(stored - declared) > Math.max(1e-12, 1e-6 * Math.abs(declared))) {
    throw new NamEditError(`${label}'s output scale in its config doesn't match the one NAM plays; it may have been edited by an older tool. Use the original file.`);
  }
  return weights.length - 1;
}

// Every place the final audio output is scaled: [configPath, config, weightPath, weights]. Only the documented
// output scales are touched, never arbitrary head_scale keys, which may belong to controls or conditioning.
export function findOutputScalers(data) {
  const config = data.config;
  if (!isObject(config)) throw new NamEditError("This capture has no config.");
  if (data.architecture === "SlimmableContainer") {
    const submodels = config.submodels;
    if (!Array.isArray(submodels) || !submodels.length) throw new NamEditError("This A2 capture has no submodels.");
    return submodels.map((entry, index) => {
      const model = entry?.model, modelConfig = model?.config;
      if (!isObject(modelConfig) || !isNumber(modelConfig.head_scale)) {
        throw new NamEditError(`This A2 capture's submodel ${index + 1} has no output scale.`);
      }
      const last = runtimeHeadScale(model, modelConfig, `Submodel ${index + 1}`);
      const prefix = `config.submodels[${index}].model`;
      return [`${prefix}.config.head_scale`, modelConfig, `${prefix}.weights[${last}]`, model.weights, index];
    });
  }
  if (isNumber(config.head_scale)) {
    const last = runtimeHeadScale(data, config, "This capture");
    return [["config.head_scale", config, `weights[${last}]`, data.weights, null]];
  }
  throw new NamEditError(`Volume can't be changed safely on ${data.architecture || "this"} captures: there's no known output scale.`);
}

// ---- Change checking -----------------------------------------------------------------------------------

export function changedPaths(before, after, path = "") {
  const here = path || "<root>";
  if (Array.isArray(before) !== Array.isArray(after) || typeof before !== typeof after || (before === null) !== (after === null)) return [here];
  if (Array.isArray(before)) {
    if (before.length !== after.length) return [here];
    return before.flatMap((item, i) => changedPaths(item, after[i], `${path}[${i}]`));
  }
  if (isObject(before)) {
    const keys = [...Object.keys(before), ...Object.keys(after).filter((k) => !(k in before))];
    return keys.flatMap((key) => {
      const child = path ? `${path}.${key}` : key;
      if (!(key in before) || !(key in after)) return [child];
      return changedPaths(before[key], after[key], child);
    });
  }
  return Object.is(before, after) ? [] : [here];
}

function checkChanges(original, edited, expected) {
  const changed = new Set(changedPaths(original, edited));
  const wanted = new Set(expected);
  const unexpected = [...changed].filter((p) => !wanted.has(p)).concat([...wanted].filter((p) => !changed.has(p)));
  if (unexpected.length) throw new NamEditError(`Edit refused: it would change ${unexpected.join(", ")}.`);
}

// ---- Edits ---------------------------------------------------------------------------------------------

export function cleanMetadata(updates) {
  const clean = {};
  for (const key of EDITABLE_METADATA) {
    if (!(key in updates)) continue;
    let value = updates[key];
    if (typeof value === "string") value = value.trim();
    if (value === "" || value === undefined) value = null;
    if (value !== null && TEXT_FIELDS.has(key)) {
      if (typeof value !== "string") throw new NamEditError(`${key} must be text.`);
      if (value.length > MAX_TEXT) throw new NamEditError(`${key} is longer than ${MAX_TEXT} characters.`);
    }
    if (key === "tone_type" && value !== null && !TONE_TYPES.includes(value)) throw new NamEditError("That isn't a NAM tone type.");
    clean[key] = value;
  }
  return clean;
}

// One edited copy with a volume change (dB, 0 for none) and metadata changes ({field: value or null}).
// Returns {data, changed, gain}; throws NamEditError if the edit can't be made safely.
export function edit(original, { volumeDb = 0, metadata = {} } = {}) {
  const data = clone(original);
  const expected = [];
  const gain = gainFor(volumeDb);
  if (volumeDb !== 0) {
    for (const [configPath, config, weightPath, weights, index] of findOutputScalers(data)) {
      const beforeScale = config.head_scale;
      config.head_scale *= gain;
      if (config.head_scale !== beforeScale) expected.push(configPath);
      const beforeWeight = weights.at(-1);
      weights[weights.length - 1] *= gain;
      if (weights.at(-1) !== beforeWeight) expected.push(weightPath);
      if (index !== null) {
        const meta = data.config.submodels[index].model.metadata;
        if (isObject(meta) && isNumber(meta.loudness)) {
          meta.loudness += volumeDb;
          expected.push(`config.submodels[${index}].model.metadata.loudness`);
        }
      }
    }
    if (isObject(data.metadata) && isNumber(data.metadata.loudness)) {
      data.metadata.loudness += volumeDb;
      expected.push("metadata.loudness");
    }
  }
  const updates = cleanMetadata(metadata);
  if (Object.keys(updates).length) {
    if ("metadata" in data && !isObject(data.metadata)) throw new NamEditError("This file's metadata isn't an object.");
    const hadMetadata = isObject(original.metadata);
    data.metadata = isObject(data.metadata) ? data.metadata : {};
    for (const [key, value] of Object.entries(updates)) {
      if (value === null) delete data.metadata[key]; else data.metadata[key] = value;
    }
    if (!hadMetadata) {
      if (Object.keys(data.metadata).length) expected.push("metadata"); else delete data.metadata;
    } else {
      for (const key of Object.keys(updates)) {
        if (!Object.is(original.metadata[key], data.metadata[key]) && !(original.metadata[key] === undefined && !(key in data.metadata))) {
          expected.push(`metadata.${key}`);
        }
      }
    }
  }
  checkChanges(original, data, expected);
  return { data, changed: expected, gain };
}

// The edited file's name: the original stem plus what changed.
export function editedFilename(filename, volumeDb, metadataChanged) {
  const stem = String(filename || "capture.nam").replace(/\.nam$/i, "");
  const parts = [];
  if (volumeDb) parts.push(`${volumeDb > 0 ? "+" : ""}${volumeDb}dB`);
  if (metadataChanged) parts.push("edited");
  return `${stem}${parts.length ? ` (${parts.join(", ")})` : ""}.nam`;
}
