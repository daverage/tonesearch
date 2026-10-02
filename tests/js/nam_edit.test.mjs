// Run with: node --test tests/js
import assert from "node:assert/strict";
import test from "node:test";

import * as nam from "../../static/nam-edit.js";

const wavenet = (extra = {}) => ({
  version: "0.5.4", architecture: "WaveNet", sample_rate: 48000,
  config: { layers: [{ kernel_size: 3, dilations: [1, 2, 4] }, { kernel_size: 3, dilations: [8] }], head: null, head_scale: 0.02 },
  weights: [0.1, -0.2, 0.3, 0.02],
  metadata: { name: "Plexi", modeled_by: "Ann", gear_type: "amp", loudness: -12, input_level_dbu: 12.5 },
  ...extra,
});
const a2 = () => ({
  version: "0.7.0", architecture: "SlimmableContainer", sample_rate: 48000,
  config: { submodels: [0.5, 1.0].map((max_value) => ({
    max_value, model: { ...wavenet({ metadata: { loudness: -10 } }), weights: [0.5, 0.04], config: { ...wavenet().config, head_scale: 0.04 } },
  })) },
  weights: [], metadata: { name: "Plexi A2", gear_type: "amp_cab", loudness: -10 },
});

test("describe reads the facts", () => {
  const facts = nam.describe(wavenet());
  assert.equal(facts.kind, "A1 (WaveNet)");
  assert.equal(facts.receptive_field, 1 + 2 * (1 + 2 + 4) + 2 * 8);
  assert.equal(facts.calibration.status, "input-only");
  assert.equal(facts.cabinet, "none");
  assert.equal(facts.volume_unsupported, null);
  const packed = nam.describe(a2());
  assert.equal(packed.kind, "A2");
  assert.equal(packed.submodels, 2);
  assert.equal(packed.cabinet, "included");
  assert.equal(packed.weights, 4);
});

test("volume scales config.head_scale, the last weight and loudness together", () => {
  const original = wavenet();
  const { data, changed, gain } = nam.edit(original, { volumeDb: 6 });
  assert.ok(Math.abs(gain - 1.9953) < 1e-4);
  assert.ok(Math.abs(data.config.head_scale - 0.02 * gain) < 1e-12);
  assert.equal(data.weights.at(-1), data.config.head_scale);
  assert.equal(data.metadata.loudness, -6);
  assert.deepEqual(changed.sort(), ["config.head_scale", "metadata.loudness", "weights[3]"]);
  assert.equal(original.config.head_scale, 0.02, "the original is untouched");
});

test("A2 volume changes every submodel", () => {
  const { changed, data } = nam.edit(a2(), { volumeDb: -3 });
  assert.equal(changed.length, 2 * 3 + 1);
  assert.equal(data.config.submodels[1].model.metadata.loudness, -13);
  assert.equal(data.metadata.loudness, -13);
});

test("volume is refused when the config and the played scale disagree", () => {
  const file = wavenet();
  file.weights[3] = 0.5;
  assert.throws(() => nam.edit(file, { volumeDb: 3 }), /doesn't match the one NAM plays/);
});

test("volume is refused on architectures without a known output scale", () => {
  const lstm = { architecture: "LSTM", config: { num_layers: 1, hidden_size: 16 }, weights: [1, 2] };
  assert.match(nam.describe(lstm).volume_unsupported, /LSTM/);
  assert.throws(() => nam.edit(lstm, { volumeDb: 3 }), /LSTM/);
  assert.throws(() => nam.edit(wavenet(), { volumeDb: 30 }), /limited/);
});

test("metadata edits touch only the named fields", () => {
  const { data, changed } = nam.edit(wavenet(), { metadata: { name: "  Plexi lead ", gear_make: "Marshall", modeled_by: "" } });
  assert.equal(data.metadata.name, "Plexi lead");
  assert.equal(data.metadata.gear_make, "Marshall");
  assert.ok(!("modeled_by" in data.metadata));
  assert.deepEqual(changed.sort(), ["metadata.gear_make", "metadata.modeled_by", "metadata.name"]);
  assert.throws(() => nam.edit(wavenet(), { metadata: { tone_type: "jazz" } }), /tone type/);
});

test("metadata can be added to a file that has none", () => {
  const bare = wavenet(); delete bare.metadata;
  assert.deepEqual(nam.edit(bare, { metadata: { name: "X" } }).changed, ["metadata"]);
  assert.deepEqual(nam.edit(bare, { metadata: { name: "" } }).changed, []);
});

test("volume and metadata combine in one edit", () => {
  const { changed } = nam.edit(wavenet(), { volumeDb: 2, metadata: { tone_type: "crunch" } });
  assert.deepEqual(changed.sort(), ["config.head_scale", "metadata.loudness", "metadata.tone_type", "weights[3]"]);
});

test("changedPaths finds every difference", () => {
  assert.deepEqual(nam.changedPaths({ a: [1, 2], b: { c: 1 } }, { a: [1, 3], b: { c: 1, d: 2 } }), ["a[1]", "b.d"]);
  assert.deepEqual(nam.changedPaths({ a: 1 }, { a: "1" }), ["a"]);
});

test("edited file names say what changed", () => {
  assert.equal(nam.editedFilename("Plexi.nam", 3, true), "Plexi (+3dB, edited).nam");
  assert.equal(nam.editedFilename("Plexi.nam", -1.5, false), "Plexi (-1.5dB).nam");
});
