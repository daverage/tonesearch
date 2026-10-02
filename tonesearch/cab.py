"""Cab embed jobs: check the visitor's capture and IR, then build the Kaggle script that trains them.

The training itself runs on the visitor's own Kaggle account (tonesearch/cab_kernel.py, pushed by
tonesearch/kaggle.py). These checks run first so a bad file fails here in a second, not on Kaggle after
the visitor has waited for a GPU.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import secrets
import struct
from pathlib import Path
from urllib.parse import urlparse

KERNEL_SOURCE = Path(__file__).with_name("cab_kernel.py")
PAYLOAD_LINE = 'PAYLOAD = ""'
PRESETS = ("draft", "standard", "high_def")  # cab_kernel.EPOCH_PRESETS: 20, 60 and 120 epochs
SAMPLE_RATE = 48000
# The A2 model neural-amp-modeler 0.13 trains has a 6332-sample receptive field (NAM Mixer's
# receptive_field.py). The kernel recomputes it from the installed trainer; this only fails early.
A2_RECEPTIVE_FIELD = 6332
MAX_NAM_BYTES = 8_000_000
MAX_IR_BYTES = 5_000_000
MAX_IR_SECONDS = 5.0
MAX_SCRIPT_BYTES = 9_000_000
# Where the Kaggle notebook gets NAM's official V3 training input: a public Kaggle dataset ("owner/dataset-slug")
# attached to the notebook, or an https URL it downloads from. Either turns training on; with both, the dataset
# is tried first. The notebook checks the file's MD5 either way.
DATASET_ENV = "TONESEARCH_NAM_INPUT_DATASET"
URL_ENV = "TONESEARCH_NAM_INPUT_URL"
_DATASET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}/[a-z0-9][a-z0-9-]{2,49}$")
_PROGRESS = re.compile(r"TONE Search: ([^\"\\\n]{1,200})")


class CabError(ValueError):
    """A file or setting that can't be used, with a message for the visitor."""


def input_dataset() -> str | None:
    value = os.environ.get(DATASET_ENV, "").strip()
    return value if _DATASET.match(value) else None


def input_url() -> str | None:
    value = os.environ.get(URL_ENV, "").strip()
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.hostname and len(value) <= 500 else None


def training_ready() -> bool:
    return bool(input_dataset() or input_url())


def _receptive_field(model: dict) -> int:
    total = 1
    for layers in (model.get("config") or {}).get("layers") or []:
        kernels = layers.get("kernel_sizes", layers.get("kernel_size"))
        dilations = layers.get("dilations")
        if not isinstance(dilations, list):
            raise CabError("This capture's WaveNet layers are missing their dilations.")
        if not isinstance(kernels, list):
            kernels = [kernels] * len(dilations)
        try:
            total += sum((int(k) - 1) * int(d) for k, d in zip(kernels, dilations))
        except (TypeError, ValueError) as exc:
            raise CabError("This capture's WaveNet layers can't be read.") from exc
    return total


def check_nam(data: bytes) -> dict:
    """The parsed capture and a summary, or CabError saying why it can't be cab embedded."""
    if not data:
        raise CabError("Choose a .nam capture.")
    if len(data) > MAX_NAM_BYTES:
        raise CabError(f"That .nam file is over {MAX_NAM_BYTES // 1_000_000} MB, too large for a capture.")
    try:
        nam = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CabError("That file isn't a NAM capture (it isn't valid JSON).") from exc
    if not isinstance(nam, dict) or not isinstance(nam.get("config"), dict):
        raise CabError("That file isn't a NAM capture.")
    architecture = nam.get("architecture")
    if architecture == "WaveNet":
        child = nam
    elif architecture == "SlimmableContainer":
        submodels = nam["config"].get("submodels")
        try:
            child = max(submodels, key=lambda item: float(item["max_value"]))["model"]
        except (TypeError, ValueError, KeyError) as exc:
            raise CabError("This A2 capture has no usable Full model.") from exc
        if not isinstance(child, dict) or child.get("architecture") != "WaveNet":
            raise CabError("This A2 capture's Full model isn't a WaveNet.")
    elif architecture == "Sequential":
        raise CabError("This capture already has a cabinet stage (a Sequential file). Use the amp-only capture.")
    else:
        raise CabError(f"{architecture or 'This'} captures can't be cab embedded yet. Use a WaveNet (A1) or A2 capture.")
    rate = child.get("sample_rate", nam.get("sample_rate"))
    if rate is not None and rate != SAMPLE_RATE:
        raise CabError(f"This capture runs at {rate} Hz. Cab embed trains at {SAMPLE_RATE} Hz, like NAM's trainer.")
    history = _receptive_field(child)
    if history > A2_RECEPTIVE_FIELD:
        raise CabError(f"This capture looks back {history} samples, more than an A2 model can ({A2_RECEPTIVE_FIELD}).")
    meta = nam.get("metadata") if isinstance(nam.get("metadata"), dict) else {}
    gear_type = meta.get("gear_type")
    return {
        "nam": nam,
        "name": str(meta.get("name") or "").strip()[:100] or None,
        "architecture": "A2" if architecture == "SlimmableContainer" else "WaveNet",
        "gear_type": gear_type,
        "has_cab": gear_type in ("amp_cab", "amp_pedal_cab"),
    }


def check_ir(data: bytes) -> dict:
    """WAV header facts for the IR, or CabError. The kernel decodes and prepares the samples."""
    if not data:
        raise CabError("Choose a cabinet IR (.wav).")
    if len(data) > MAX_IR_BYTES:
        raise CabError(f"That IR is over {MAX_IR_BYTES // 1_000_000} MB. Cabinet IRs are usually well under 1 MB.")
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise CabError("That IR isn't a WAV file.")
    fmt, frames_bytes, pos = None, None, 12
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if chunk == b"fmt " and len(body) >= 16:
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif chunk == b"data":
            frames_bytes = min(size, len(data) - pos - 8)
        pos += 8 + size + (size & 1)
    if fmt is None or frames_bytes is None:
        raise CabError("That WAV file has no audio format or no audio.")
    codec, channels, rate, _, block, bits = fmt
    if codec not in (1, 3, 0xFFFE) or not channels or not rate or not block:
        raise CabError("That WAV file uses a format IR loaders don't read. Save it as PCM or 32-bit float.")
    seconds = frames_bytes / block / rate
    if seconds <= 0:
        raise CabError("That IR has no samples.")
    if seconds > MAX_IR_SECONDS:
        raise CabError(f"That IR is {seconds:.1f} s long. Cabinet IRs are usually under a second (limit {MAX_IR_SECONDS:g} s).")
    return {"sample_rate": rate, "channels": channels, "bits": bits, "seconds": round(seconds, 3)}


def model_name(requested: str, capture_name: str | None, ir_filename: str) -> str:
    name = " ".join((requested or "").split())[:100]
    if name:
        return name
    ir = Path(ir_filename or "cab").stem.strip()[:50] or "cab"
    return f"{capture_name or 'Capture'} + {ir}"[:100]


def job_slug() -> str:
    """A fresh kernel slug. Never built from visitor text, so it's always a valid Kaggle slug and title."""
    return f"tonesearch-cab-{secrets.token_hex(6)}"


def build_script(nam: dict, ir: bytes, *, ir_name: str, name: str, preset: str, input_url: str | None = None) -> str:
    if preset not in PRESETS:
        raise CabError("Choose a training quality.")
    payload = {"nam": nam, "ir": base64.b64encode(ir).decode("ascii"), "ir_name": Path(ir_name or "cab.wav").name[:120],
               "model_name": name, "preset": preset, "input_url": input_url}
    packed = base64.b64encode(gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))).decode("ascii")
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    if source.count(PAYLOAD_LINE) != 1:
        raise RuntimeError("cab_kernel.py must contain exactly one PAYLOAD line")
    script = source.replace(PAYLOAD_LINE, f'PAYLOAD = "{packed}"', 1)
    if len(script) > MAX_SCRIPT_BYTES:
        raise CabError("The capture and IR are too large to send to Kaggle together.")
    return script


def progress(log: str) -> str | None:
    """The kernel's latest "TONE Search: ..." line, such as "epoch 12 of 60"."""
    found = _PROGRESS.findall(log or "")
    return found[-1].strip() if found else None


def result_summary(result: dict) -> dict:
    """The parts of training_result.json the page shows. Never passes the traceback through."""
    training = result.get("training") if isinstance(result.get("training"), dict) else {}
    receptive = result.get("receptive_field") if isinstance(result.get("receptive_field"), dict) else {}
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    esr = training.get("validation_esr")
    return {
        "success": bool(result.get("success")),
        "error": str(result.get("error"))[:500] if result.get("error") else None,
        "model_name": str(result.get("model_name") or "")[:100] or None,
        "filename": str(output.get("filename") or "")[:200] or None,
        "validation_esr": esr if isinstance(esr, (int, float)) else None,
        "epochs": training.get("epochs") if isinstance(training.get("epochs"), int) else None,
        "training_seconds": training.get("seconds") if isinstance(training.get("seconds"), int) else None,
        "cab_approximated": bool(receptive.get("cab_approximated")),
        "gpu": str(result.get("gpu") or "")[:80] or None,
    }
