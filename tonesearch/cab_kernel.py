#!/usr/bin/env python3
"""TONE Search cab embed: learn an amp capture plus a cabinet IR as one ordinary NAM model.

This file runs INSIDE a private Kaggle script kernel on the visitor's own Kaggle account, never on the
TONE Search server. tonesearch/cab.py fills in PAYLOAD (the visitor's .nam and IR, gzipped JSON in base64)
and pushes the result with tonesearch/kaggle.py. It must stay self-contained: Kaggle runs this one file.

What it does, following NAM Mixer's learned cab embed (hybrid-nam-builder:
hybrid/modes/cab_embed_training_target.py and cloud/kaggle/train_a2_cloud.py):

1. Installs the pinned neural-amp-modeler and checks for a GPU.
2. Finds NAM's official V3 training input in the attached dataset, by its MD5.
3. Renders that input through the visitor's capture (the Full model of an A2 file).
4. Prepares the IR like NAM Mixer (mono average, leading silence trimmed at -40 dB relative to its peak,
   resampled to 48 kHz) and convolves: causal, truncated to the input's length.
5. Lowers the target's level only if its peak is above -0.2 dBFS (one fixed gain, never a limiter).
6. Trains an A2 model on input -> target with the pinned trainer and exports it with the source's metadata.
7. Writes <name>.nam and training_result.json to /kaggle/working, which become the kernel's output.

Lines starting "TONE Search:" are progress the website reads from the log.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

PAYLOAD = ""

NEURAL_AMP_MODELER_VERSION = "0.13.0"
OFFICIAL_V3_INPUT_MD5 = "36cd1af62985c2fac3e654333e36431e"
SAMPLE_RATE = 48000
TARGET_PEAK_CEILING_DBFS = -0.2  # NAM Mixer's A2_TARGET_PEAK_CEILING_DBFS
LEADING_SILENCE_THRESHOLD_DB = -40.0  # NAM Mixer's LEADING_SILENCE_THRESHOLD_RELATIVE_DB
EPOCH_PRESETS = {"draft": 20, "standard": 60, "high_def": 120}  # NAM Mixer's presets
TRAINING_SETTINGS = {"batch_size": 16, "ny": 8192, "seed": 0, "latency": 0}
RENDER_CHUNK = 1 << 18

# Overridable for a local test run (see tests/test_cab.py and README "Cab embed"); Kaggle uses the defaults.
OUTPUT_DIR = Path(os.environ.get("TONESEARCH_OUTPUT_DIR", "/kaggle/working"))
WORK_DIR = Path(os.environ.get("TONESEARCH_WORK_DIR", "/tmp/tonesearch-cab"))
INPUT_ROOT = Path(os.environ.get("TONESEARCH_INPUT_ROOT", "/kaggle/input"))
ALLOW_CPU = os.environ.get("TONESEARCH_ALLOW_CPU") == "1"
FAST_DEV_RUN = os.environ.get("TONESEARCH_FAST_DEV_RUN") == "1"


class CabJobError(RuntimeError):
    pass


def say(message: str) -> None:
    print(f"TONE Search: {message}", file=sys.__stdout__ or sys.stdout, flush=True)


def decode_payload(text: str) -> dict:
    if not text:
        raise CabJobError("this script has no payload; TONE Search fills it in before pushing it to Kaggle")
    payload = json.loads(gzip.decompress(base64.b64decode(text)).decode("utf-8"))
    for key in ("nam", "ir", "ir_name", "model_name", "preset"):
        if key not in payload:
            raise CabJobError(f"payload is missing {key!r}")
    if payload["preset"] not in EPOCH_PRESETS:
        raise CabJobError(f"unknown quality preset {payload['preset']!r}")
    return payload


def full_model(nam: dict) -> dict:
    """The model to render: a WaveNet as is, or the Full (highest max_value) WaveNet of an A2 file."""
    architecture = nam.get("architecture")
    if architecture == "WaveNet":
        child = nam
    elif architecture == "SlimmableContainer":
        submodels = (nam.get("config") or {}).get("submodels") or []
        if not submodels:
            raise CabJobError("this A2 file has no submodels")
        child = max(submodels, key=lambda item: float(item["max_value"]))["model"]
        if child.get("architecture") != "WaveNet":
            raise CabJobError("this A2 file's Full model is not a WaveNet")
    else:
        raise CabJobError(f"{architecture} captures can't be cab embedded; use a WaveNet (A1) or A2 capture")
    rate = child.get("sample_rate", nam.get("sample_rate", SAMPLE_RATE))
    if rate is not None and int(rate) != SAMPLE_RATE:
        raise CabJobError(f"the capture runs at {rate} Hz; cab embed needs a {SAMPLE_RATE} Hz capture")
    return child


def modern_wavenet(model: dict) -> dict:
    """The same WaveNet in the layout neural-amp-modeler 0.13's PyTorch loader reads.

    Classic A1 files (written by NAM trainers before A2, and most of TONE3000's A1 captures) give each layer
    array head_size, head_bias and a gated flag; 0.13 wants a head object and per-layer gating. The weights
    are in the same order either way: plain and gated A1 files rendered this way matched NAMCore's native
    renderer to an error-to-signal ratio below 1e-12.
    """
    layers = (model.get("config") or {}).get("layers") or []
    if not any("head" not in layer and "head_size" in layer for layer in layers):
        return model
    converted = []
    for layer in layers:
        layer = dict(layer)
        if "head" not in layer and "head_size" in layer:
            layer["head"] = {"out_channels": layer.pop("head_size"), "kernel_size": 1, "bias": bool(layer.pop("head_bias", False))}
            if layer.pop("gated", False):  # tanh(a) * sigmoid(b), as NAMCore runs a gated layer
                count = len(layer["dilations"])
                activation = layer["activation"]
                layer["activation"] = [activation] * count if isinstance(activation, (str, dict)) else activation
                layer["gating_mode"] = ["gated"] * count
                layer["secondary_activation"] = ["Sigmoid"] * count
        converted.append(layer)
    return {**model, "config": {**model["config"], "layers": converted}}


def wavenet_receptive_field(model: dict) -> int:
    """1 + sum((kernel - 1) * dilation) over the stacked layer arrays (NAM Mixer's receptive_field.py)."""
    total = 1
    for layers in (model.get("config") or {}).get("layers") or []:
        kernels = layers.get("kernel_sizes", layers.get("kernel_size"))
        dilations = layers["dilations"]
        if not isinstance(kernels, list):
            kernels = [kernels] * len(dilations)
        total += sum((int(k) - 1) * int(d) for k, d in zip(kernels, dilations))
    return total


def a2_receptive_field() -> int:
    import importlib.resources

    resource = importlib.resources.files("nam.train._resources").joinpath("config_model_packed.json")
    config = json.loads(resource.read_text(encoding="utf-8"))
    best = 0
    for entry in config["net"]["config"]["submodels"]:
        total = 1
        for layers in entry["config"].get("layers_configs", entry["config"].get("layers")):
            total += sum((int(k) - 1) * int(d) for k, d in zip(layers["kernel_sizes"], layers["dilations"]))
        best = max(best, total)
    return best


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def find_training_input(url: str | None = None, root: Path = None) -> Path:
    """NAM's official V3 input: from an attached dataset if there is one, else downloaded from `url`."""
    root = root or INPUT_ROOT
    for path in sorted(root.rglob("*.wav")) if root.is_dir() else []:
        if md5_file(path) == OFFICIAL_V3_INPUT_MD5:
            return path
    if not url:
        raise CabJobError(f"NAM's official V3 training input wasn't found under {root}, and no download link was given")
    from urllib.request import Request, urlopen

    say("downloading NAM's training input")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    path = WORK_DIR / "v3_0_0.wav"
    try:
        with urlopen(Request(url, headers={"User-Agent": "TONE-Search cab-embed/1.0"}), timeout=120) as response, \
                open(path, "wb") as out:
            shutil.copyfileobj(response, out)
    except Exception as exc:
        raise CabJobError(f"couldn't download NAM's training input from {url}: {exc}") from exc
    if md5_file(path) != OFFICIAL_V3_INPUT_MD5:
        raise CabJobError(f"the file at {url} isn't NAM's official V3 training input (its MD5 doesn't match)")
    return path


def prepare_ir(data: bytes, sample_rate: int = SAMPLE_RATE):
    """Mono float32 taps at sample_rate, plus what was done to them."""
    from math import gcd

    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    try:
        raw, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception as exc:
        raise CabJobError(f"the IR isn't a readable WAV file: {exc}") from exc
    if raw.shape[0] == 0:
        raise CabJobError("the IR file is empty")
    mono = raw.mean(axis=1).astype(np.float32) if raw.shape[1] > 1 else raw[:, 0].astype(np.float32)
    if not np.all(np.isfinite(mono)):
        raise CabJobError("the IR contains NaN or infinite samples")
    peak = float(np.max(np.abs(mono)))
    if peak <= 1e-9:
        raise CabJobError("the IR is silent")
    above = np.nonzero(np.abs(mono) >= peak * 10.0 ** (LEADING_SILENCE_THRESHOLD_DB / 20.0))[0]
    trimmed = int(above[0]) if len(above) else 0
    mono = mono[trimmed:]
    if int(rate) != sample_rate:
        g = gcd(int(rate), sample_rate)
        mono = resample_poly(mono, sample_rate // g, int(rate) // g).astype(np.float32)
    return mono, {"original_sample_rate": int(rate), "original_channels": int(raw.shape[1]),
                  "leading_samples_trimmed": trimmed, "taps": int(len(mono))}


def render(model_config: dict, audio, *, device: str):
    """Play `audio` through the capture with NAM's PyTorch model, in chunks that share their history."""
    import numpy as np
    import torch
    from nam.models import init_from_nam

    model = init_from_nam(modern_wavenet(model_config)).to(device).eval()
    history = model.receptive_field - 1
    padded = np.concatenate([np.zeros(history, dtype=np.float32), audio.astype(np.float32)])
    out = np.empty(len(audio), dtype=np.float32)
    with torch.no_grad():
        for start in range(0, len(audio), RENDER_CHUNK):
            stop = min(start + RENDER_CHUNK, len(audio))
            segment = torch.from_numpy(padded[start:stop + history]).to(device)
            out[start:stop] = model(segment, pad_start=False).detach().cpu().numpy()[: stop - start]
    return out


def peak_ceiling(audio, ceiling_dbfs: float = TARGET_PEAK_CEILING_DBFS):
    """One fixed gain reduction if the peak is above the ceiling; never makeup gain, never a limiter."""
    import numpy as np

    peak = float(np.max(np.abs(audio)))
    peak_dbfs = 20.0 * np.log10(peak) if peak > 0 else float("-inf")
    if peak_dbfs <= ceiling_dbfs:
        return audio, peak_dbfs, 0.0
    reduction = peak_dbfs - ceiling_dbfs
    return (audio * 10.0 ** (-reduction / 20.0)).astype(np.float32), peak_dbfs, reduction


def build_target(source: dict, ir_taps, input_audio, *, device: str):
    import numpy as np
    from scipy.signal import fftconvolve

    say("rendering the training input through your capture")
    rendered = render(full_model(source), input_audio, device=device)
    say("applying your cabinet IR")
    target = fftconvolve(rendered.astype(np.float64), ir_taps.astype(np.float64))[: len(input_audio)].astype(np.float32)
    if not np.all(np.isfinite(target)):
        raise CabJobError("the capture plus IR produced NaN or infinite samples")
    if float(np.max(np.abs(target))) < 1e-6:
        raise CabJobError("the capture plus IR is silent; check the capture and the IR")
    return peak_ceiling(target)


def user_metadata_kwargs(source: dict, model_name: str) -> dict:
    """The new file keeps the source's credits, make, model, tone type and input calibration."""
    meta = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}

    def text(key: str):
        value = meta.get(key)
        return value.strip()[:200] if isinstance(value, str) and value.strip() else None

    tone = meta.get("tone_type") if meta.get("tone_type") in {"clean", "overdrive", "crunch", "hi_gain", "fuzz"} else None
    level = meta.get("input_level_dbu")
    return {
        "name": model_name,
        "modeled_by": text("modeled_by"),
        "gear_make": text("gear_make"),
        "gear_model": text("gear_model"),
        "tone_type": tone,
        # The target is the source's own output, so the source's input calibration still holds.
        "input_level_dbu": float(level) if isinstance(level, (int, float)) else None,
    }


def install_epoch_progress(core) -> None:
    """Print "TONE Search: epoch N of M" as each epoch starts (Lightning's bar prints nothing to a log)."""
    official = getattr(core, "get_callbacks", None)
    try:
        import pytorch_lightning as pl
    except ImportError:
        return
    if official is None:
        return

    class EpochProgress(pl.Callback):
        def on_train_epoch_start(self, trainer, pl_module):
            say(f"epoch {trainer.current_epoch + 1} of {trainer.max_epochs or '?'}")

    core.get_callbacks = lambda *args, **kwargs: [*official(*args, **kwargs), EpochProgress()]


def ensure_nam_installed() -> str:
    try:
        import nam
        if getattr(nam, "__version__", None) == NEURAL_AMP_MODELER_VERSION:
            return nam.__version__
    except ImportError:
        pass
    say(f"installing neural-amp-modeler {NEURAL_AMP_MODELER_VERSION}")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    f"neural-amp-modeler=={NEURAL_AMP_MODELER_VERSION}"], check=True)
    import importlib

    import nam
    importlib.reload(nam)
    return getattr(nam, "__version__", NEURAL_AMP_MODELER_VERSION)


def safe_stem(name: str) -> str:
    return (re.sub(r"[^\w .()+\[\]-]+", "_", name).strip(" ._") or "cab-embed")[:120]


def run(payload: dict, result: dict) -> None:
    import numpy as np
    import soundfile as sf
    import torch

    result["python"] = platform.python_version()
    result["torch"] = torch.__version__
    if torch.cuda.is_available():
        device = "cuda"
        result["gpu"] = torch.cuda.get_device_name(0)
    elif ALLOW_CPU:
        device = "cpu"
    else:
        raise CabJobError("this Kaggle session has no GPU; TONE Search asks for a T4, so check your GPU quota")
    result["neural_amp_modeler"] = ensure_nam_installed()

    source = payload["nam"]
    child = full_model(source)
    source_rf, a2_rf = wavenet_receptive_field(child), a2_receptive_field()
    if source_rf > a2_rf:
        raise CabJobError(f"the capture needs {source_rf} samples of history, more than an A2 model has ({a2_rf})")

    input_path = find_training_input(payload.get("input_url"))
    input_audio, input_rate = sf.read(input_path, dtype="float32")
    if input_rate != SAMPLE_RATE or input_audio.ndim != 1:
        raise CabJobError("the training input must be mono 48 kHz")

    ir_taps, ir_info = prepare_ir(base64.b64decode(payload["ir"]))
    ir_history = max(0, ir_info["taps"] - 1)
    result["ir"] = {**ir_info, "name": payload["ir_name"]}
    result["receptive_field"] = {
        "source_samples": source_rf, "a2_samples": a2_rf, "cab_history_samples": ir_history,
        # Past this, the A2 model approximates the cab's tail rather than reproducing it exactly.
        "cab_approximated": source_rf + ir_history > a2_rf,
    }

    target, raw_peak, reduction = build_target(source, ir_taps, input_audio, device=device)
    result["target"] = {"raw_peak_dbfs": round(raw_peak, 2), "gain_reduction_db": round(reduction, 2)}
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    data_input, data_target = WORK_DIR / "input.wav", WORK_DIR / "target.wav"
    shutil.copyfile(input_path, data_input)
    sf.write(data_target, np.asarray(target, dtype=np.float32), SAMPLE_RATE, subtype="FLOAT")

    import nam.train.core as core
    from nam.models.metadata import GearType, ToneType, UserMetadata
    from nam.train.metadata import TRAINING_KEY

    epochs = 1 if FAST_DEV_RUN else EPOCH_PRESETS[payload["preset"]]
    install_epoch_progress(core)
    say(f"training ({epochs} epochs)")
    started = time.time()
    trained = core.train(
        input_path=str(data_input), output_path=str(data_target), train_path=str(WORK_DIR / "train"),
        epochs=epochs, ignore_checks=False, silent=True, modelname="model", fast_dev_run=FAST_DEV_RUN,
        **TRAINING_SETTINGS,
    )
    if trained is None or trained.model is None:
        raise CabJobError("NAM's trainer refused the training data. Its checks need the capture plus IR to sound the "
                          "same each time the test signal repeats; a very long IR, or a capture that is silent or very "
                          "noisy, can fail them. The log on Kaggle has the details")
    result["training"] = {"epochs": epochs, "preset": payload["preset"], "seconds": round(time.time() - started),
                          "validation_esr": trained.metadata.validation_esr}

    kwargs = user_metadata_kwargs(source, payload["model_name"])
    tone = kwargs.pop("tone_type")
    metadata = UserMetadata(gear_type=GearType.AMP_CAB, tone_type=ToneType(tone) if tone else None, **kwargs)
    stem = safe_stem(payload["model_name"])
    export_dir = WORK_DIR / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    trained.model.net.export(export_dir, basename=stem, user_metadata=metadata,
                             other_metadata={TRAINING_KEY: trained.metadata.model_dump()})
    exported = export_dir / f"{stem}.nam"
    if not exported.is_file():
        raise CabJobError("the trainer didn't write the exported model")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    final = OUTPUT_DIR / exported.name
    shutil.copyfile(exported, final)
    result["output"] = {"filename": final.name, "bytes": final.stat().st_size}
    say("done")


def main() -> int:
    result: dict = {"success": False, "started": time.time()}
    try:
        payload = decode_payload(PAYLOAD)
        result["model_name"] = payload["model_name"]
        run(payload, result)
        result["success"] = True
    except Exception as exc:  # noqa: BLE001 -- every failure is reported in training_result.json
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()
        say(f"failed: {exc}")
    finally:
        result["finished"] = time.time()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / "training_result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
