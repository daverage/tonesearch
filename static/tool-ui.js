// Shared pieces of the NAM tool pages (tools.html, cab.html): file pickers, status text and NAMCore in wasm.
import { NamEngine } from "./vendor/nam-wasm/index.js";

export const ASSETS = { assetBaseUrl: new URL("./vendor/nam-wasm/", import.meta.url) };
export const RATE = 48000; // NAM captures and the training input run at 48 kHz
export { NamEngine };

export const $ = (id) => document.getElementById(id);

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

const announcer = document.getElementById("announcer");
export function announce(text) {
  announcer.textContent = "";
  setTimeout(() => { announcer.textContent = text; }, 50);
}

export function setInfo(node, text, isError = false) {
  node.textContent = text || "";
  node.classList.toggle("is-error", isError);
}

// The picker card shows the chosen file's name and whether it can be used (state "ok" or "error").
export function picked(inputId, file, state) {
  const card = $(inputId).closest(".file-pick");
  card.dataset.state = file ? state : "empty";
  const name = card.querySelector(".file-pick-name");
  if (!name.dataset.empty) name.dataset.empty = name.textContent;
  name.textContent = file ? file.name : name.dataset.empty;
}

// Files can be dropped on any picker card as well as chosen.
export function enableDrop() {
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
}

export function downloadBlob(blob, filename) {
  const link = el("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
}

// Play `input` (mono Float32Array) through a model offline in NAMCore. Returns {info, output}.
// slimSize picks an A2 file's Lite model (0) instead of its Full one (undefined). The engine fades in over
// its first 1024 frames, and normalises output to -18 dB when the file reports its loudness.
export async function renderOffline(text, input, { rate = RATE, slimSize } = {}) {
  const context = new OfflineAudioContext(1, input.length, rate);
  const engine = await NamEngine.attach(context, ASSETS);
  const node = await engine.createNode();
  try {
    const info = await node.loadModel(text, slimSize === undefined ? {} : { slimSize });
    const buffer = context.createBuffer(1, input.length, rate);
    buffer.copyToChannel(input, 0);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(node).connect(context.destination);
    source.start();
    const rendered = await context.startRendering();
    return { info, output: rendered.getChannelData(0) };
  } finally {
    await node.dispose();
  }
}
