from __future__ import annotations

import base64
import io
import json
import struct
import wave
from urllib.error import HTTPError

import pytest

import app as app_module
from tonesearch import cab, cab_kernel, kaggle, overrides


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    overrides.activate({})
    monkeypatch.delenv(cab.DATASET_ENV, raising=False)
    monkeypatch.delenv(cab.URL_ENV, raising=False)


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def wavenet(**extra) -> dict:
    return {"version": "0.5.4", "architecture": "WaveNet", "sample_rate": 48000, "weights": [],
            "config": {"layers": [{"kernel_size": 3, "dilations": [1, 2, 4]}], "head": None, "head_scale": 0.02},
            "metadata": {"name": "Plexi", "gear_type": "amp"}, **extra}


def a2(child: dict | None = None) -> dict:
    child = child or wavenet()
    return {"version": "0.7.0", "architecture": "SlimmableContainer", "sample_rate": 48000, "weights": [],
            "config": {"submodels": [{"max_value": 0.5, "model": wavenet(metadata={})}, {"max_value": 1.0, "model": child}]},
            "metadata": {"name": "Plexi A2"}}


def pcm_wav(seconds: float = 0.2, rate: int = 48000, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = int(seconds * rate)
        out.writeframes(b"".join(struct.pack("<h", 1000 if i == 0 else 0) * channels for i in range(frames)))
    return buffer.getvalue()


def float_wav(samples: list[float], rate: int = 48000) -> bytes:
    data = struct.pack(f"<{len(samples)}f", *samples)
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data)


# ---- capture and IR checks ----------------------------------------------------------------------------

def test_check_nam_accepts_wavenet_and_a2():
    plain = cab.check_nam(json.dumps(wavenet()).encode())
    assert plain["architecture"] == "WaveNet" and plain["name"] == "Plexi" and not plain["has_cab"]
    packed = cab.check_nam(json.dumps(a2()).encode())
    assert packed["architecture"] == "A2" and packed["name"] == "Plexi A2"


@pytest.mark.parametrize("nam, message", [
    ({"architecture": "LSTM", "config": {}}, "LSTM captures can't"),
    ({"architecture": "Sequential", "config": {"models": []}}, "already has a cabinet"),
    (wavenet(sample_rate=44100), "44100 Hz"),
    (wavenet(config={"layers": [{"kernel_sizes": [3] * 2, "dilations": [4096, 4096]}]}), "looks back"),
])
def test_check_nam_refuses_what_cant_be_trained(nam, message):
    with pytest.raises(cab.CabError, match=message):
        cab.check_nam(json.dumps(nam).encode())


def test_check_nam_refuses_non_json_and_flags_a_built_in_cab():
    with pytest.raises(cab.CabError, match="valid JSON"):
        cab.check_nam(b"\x00not json")
    assert cab.check_nam(json.dumps(wavenet(metadata={"gear_type": "amp_cab"})).encode())["has_cab"]


def test_check_ir_reads_pcm_and_float_wavs():
    assert cab.check_ir(pcm_wav(0.25, 44100, 2)) == {"sample_rate": 44100, "channels": 2, "bits": 16, "seconds": 0.25}
    assert cab.check_ir(float_wav([1.0, 0.5, 0.0, 0.0]))["bits"] == 32


@pytest.mark.parametrize("data, message", [
    (b"", "Choose a cabinet IR"),
    (b"ID3 not a wav file", "isn't a WAV"),
    (pcm_wav(6.0), "6.0 s long"),
])
def test_check_ir_refuses_bad_files(data, message):
    with pytest.raises(cab.CabError, match=message):
        cab.check_ir(data)


def test_model_name_defaults_to_capture_plus_ir():
    assert cab.model_name("", "Plexi", "V30 SM57.wav") == "Plexi + V30 SM57"
    assert cab.model_name("  My   rig ", "Plexi", "x.wav") == "My rig"


def test_build_script_round_trips_through_the_kernel():
    script = cab.build_script(wavenet(), b"RIFFdata", ir_name="dir/V30.wav", name="Plexi + V30", preset="draft")
    assert script.count('PAYLOAD = "') == 1 and 'PAYLOAD = ""' not in script
    packed = script.split('PAYLOAD = "', 1)[1].split('"', 1)[0]
    payload = cab_kernel.decode_payload(packed)
    assert payload["nam"] == wavenet() and base64.b64decode(payload["ir"]) == b"RIFFdata"
    assert payload["ir_name"] == "V30.wav" and payload["preset"] == "draft"
    with pytest.raises(cab.CabError, match="training quality"):
        cab.build_script(wavenet(), b"x", ir_name="x.wav", name="n", preset="huge")


def test_job_slugs_are_valid_kaggle_slugs():
    slug = cab.job_slug()
    assert kaggle.SLUG.match(slug) and slug != cab.job_slug()


def test_progress_reads_the_latest_line_from_a_json_log():
    log = json.dumps([{"stream_name": "stdout", "data": "TONE Search: training (60 epochs)\n"},
                      {"stream_name": "stdout", "data": "TONE Search: epoch 12 of 60\n"}])
    assert cab.progress(log) == "epoch 12 of 60"
    assert cab.progress("") is None


def test_result_summary_never_passes_the_traceback():
    summary = cab.result_summary({"success": False, "error": "no GPU", "traceback": "secret paths",
                                  "training": {"validation_esr": 0.01, "epochs": 60}})
    assert summary["error"] == "no GPU" and summary["validation_esr"] == 0.01
    assert "traceback" not in summary and "secret" not in json.dumps(summary)


def test_input_dataset_must_look_like_owner_slash_slug(monkeypatch):
    monkeypatch.setenv(cab.DATASET_ENV, "daverage/nam-v3-input")
    assert cab.input_dataset() == "daverage/nam-v3-input"
    monkeypatch.setenv(cab.DATASET_ENV, "https://kaggle.com/x")
    assert cab.input_dataset() is None


# ---- Kaggle client ------------------------------------------------------------------------------------

class _Response:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self, _n=None):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def recording_opener(reply):
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        return _Response(reply)
    return opener, calls


def test_push_script_sends_a_private_t4_kernel_with_the_dataset():
    opener, calls = recording_opener({"ref": "/code/ann/x", "url": "https://www.kaggle.com/code/ann/tonesearch-cab-1",
                                      "versionNumber": 1})
    job = kaggle.push_script("ann", "abc123", slug="tonesearch-cab-1a2b", title="tonesearch-cab-1a2b",
                             script="print(1)", dataset="owner/nam-v3", opener=opener)
    request = calls[0]
    body = json.loads(request.data)
    assert request.full_url == "https://api.kaggle.com/v1/kernels.KernelsApiService/SaveKernel"
    assert request.get_header("Authorization") == "Basic " + base64.b64encode(b"ann:abc123").decode()
    assert body["slug"] == "ann/tonesearch-cab-1a2b" and body["isPrivate"] is True
    assert body["enableGpu"] is True and body["machineShape"] == "NvidiaTeslaT4"
    assert body["datasetDataSources"] == ["owner/nam-v3"] and body["text"] == "print(1)"
    assert job["ref"] == "ann/tonesearch-cab-1a2b" and job["version"] == 1


def test_kgat_tokens_are_sent_as_bearer_tokens():
    opener, calls = recording_opener({"status": "KERNEL_WORKER_STATUS_RUNNING"})
    assert kaggle.status("ann", "KGAT_xyz", "ann/tonesearch-cab-1a2b", opener=opener)["state"] == "running"
    assert calls[0].get_header("Authorization") == "Bearer KGAT_xyz"


def test_push_reports_kaggle_errors():
    opener, _ = recording_opener({"error": "Notebook title already in use"})
    with pytest.raises(kaggle.KaggleError, match="title already in use"):
        kaggle.push_script("ann", "k", slug="tonesearch-cab-1a2b", title="t", script="x", dataset="o/d", opener=opener)


def test_rejected_credentials_explain_phone_verification():
    def opener(request, timeout=None):
        raise HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b'{"message": "Unauthenticated"}'))
    with pytest.raises(kaggle.KaggleError, match="phone-verified") as caught:
        kaggle.gpu_quota("ann", "k", opener=opener)
    assert caught.value.code == 401


def test_gpu_quota_converts_durations_to_hours():
    opener, _ = recording_opener({"gpuQuota": {"timeUsed": "5400s", "totalTimeAllowed": "108000.5s"}})
    assert kaggle.gpu_quota("ann", "k", opener=opener) == {"used_hours": 1.5, "allowed_hours": 30.0, "refreshes": None}


def test_credentials_and_refs_are_checked():
    with pytest.raises(kaggle.KaggleError, match="username"):
        kaggle.credentials("bad name", "k")
    with pytest.raises(kaggle.KaggleError, match="Invalid job"):
        kaggle.status("ann", "k", "ann/../../etc", opener=recording_opener({})[0])


def test_fetch_output_only_from_kaggle_or_google_storage():
    with pytest.raises(kaggle.KaggleError, match="unexpected host"):
        kaggle.fetch_output("https://evil.example/x.nam", opener=recording_opener(b"x")[0])
    with pytest.raises(kaggle.KaggleError, match="unexpected host"):
        kaggle.fetch_output("http://www.kaggleusercontent.com/x", opener=recording_opener(b"x")[0])

    seen = []

    def opener(request, timeout=None):
        seen.append(request.full_url)
        if len(seen) == 1:
            raise HTTPError(request.full_url, 302, "moved", {"Location": "https://storage.googleapis.com/o/x.nam"}, None)
        return _Response(b"model")
    assert kaggle.fetch_output("https://www.kaggleusercontent.com/kf/1/x.nam", opener=opener) == b"model"
    assert seen[-1] == "https://storage.googleapis.com/o/x.nam"


# ---- routes -------------------------------------------------------------------------------------------

HEADERS = {"X-Kaggle-Username": "ann", "X-Kaggle-Key": "abc123"}


def test_cab_page_renders_without_ads(client):
    page = client.get("/cab").get_data(as_text=True)
    assert "Cab embed" in page and f"static/v{app_module.ASSET_VERSION}/cab.js" in page
    assert "adsbygoogle" not in page and "/app.js" not in page
    assert "isn't set up on this site yet" in page


def test_start_needs_the_training_dataset(client):
    reply = client.post("/api/cab/jobs", headers=HEADERS)
    assert reply.status_code == 503 and cab.URL_ENV in reply.get_json()["error"]


def _upload(client, nam: bytes, ir: bytes, **form):
    data = {"nam": (io.BytesIO(nam), "plexi.nam"), "ir": (io.BytesIO(ir), "V30.wav"), "preset": "draft", **form}
    return client.post("/api/cab/jobs", headers=HEADERS, data=data, content_type="multipart/form-data")


def test_start_pushes_a_job_with_the_visitors_credentials(client, monkeypatch):
    monkeypatch.setenv(cab.DATASET_ENV, "owner/nam-v3")
    pushed = {}

    def push(username, key, **kwargs):
        pushed.update(username=username, key=key, **kwargs)
        return {"ref": f"{username}/{kwargs['slug']}", "url": "https://www.kaggle.com/code/x", "version": 1}
    monkeypatch.setattr(kaggle, "push_script", push)
    reply = _upload(client, json.dumps(wavenet(metadata={"name": "Plexi", "gear_type": "amp_cab"})).encode(), pcm_wav())
    body = reply.get_json()
    assert reply.status_code == 200, body
    assert pushed["username"] == "ann" and pushed["key"] == "abc123" and pushed["dataset"] == "owner/nam-v3"
    assert pushed["slug"] == pushed["title"] and kaggle.SLUG.match(pushed["slug"])
    assert body["name"] == "Plexi + V30" and "already includes a cabinet" in body["warning"]
    payload = cab_kernel.decode_payload(pushed["script"].split('PAYLOAD = "', 1)[1].split('"', 1)[0])
    assert payload["preset"] == "draft" and payload["nam"]["metadata"]["name"] == "Plexi"


def test_start_refuses_bad_files_before_calling_kaggle(client, monkeypatch):
    monkeypatch.setenv(cab.DATASET_ENV, "owner/nam-v3")
    monkeypatch.setattr(kaggle, "push_script", lambda *a, **k: pytest.fail("Kaggle must not be called"))
    reply = _upload(client, json.dumps({"architecture": "LSTM", "config": {}}).encode(), pcm_wav())
    assert reply.status_code == 400 and "LSTM" in reply.get_json()["error"]
    reply = _upload(client, json.dumps(wavenet()).encode(), b"not a wav")
    assert reply.status_code == 400 and "WAV" in reply.get_json()["error"]


def test_status_reports_progress_and_the_result(client, monkeypatch):
    monkeypatch.setattr(kaggle, "status", lambda u, k, ref: {"state": "complete", "failure": None})
    monkeypatch.setattr(kaggle, "output", lambda u, k, ref: {
        "files": {"training_result.json": "https://www.kaggleusercontent.com/r", "Plexi.nam": "https://www.kaggleusercontent.com/n"},
        "log": "TONE Search: done"})
    monkeypatch.setattr(kaggle, "fetch_output", lambda url, **k: json.dumps(
        {"success": True, "output": {"filename": "Plexi.nam"}, "training": {"validation_esr": 0.02, "epochs": 20}}).encode())
    body = client.get("/api/cab/jobs/ann/tonesearch-cab-1a2b", headers=HEADERS).get_json()
    assert body["state"] == "complete" and body["progress"] == "done"
    assert body["result"]["filename"] == "Plexi.nam" and body["result"]["validation_esr"] == 0.02


def test_status_of_a_job_kaggle_hasnt_listed_yet(client, monkeypatch):
    def missing(*a, **k):
        raise kaggle.KaggleError("no such job", 404)
    monkeypatch.setattr(kaggle, "status", missing)
    body = client.get("/api/cab/jobs/ann/tonesearch-cab-1a2b", headers=HEADERS).get_json()
    assert body["state"] == "missing"


def test_jobs_on_another_account_are_refused(client):
    reply = client.get("/api/cab/jobs/bob/tonesearch-cab-1a2b", headers=HEADERS)
    assert reply.status_code == 503 and "different Kaggle account" in reply.get_json()["error"]


def test_download_serves_the_trained_model(client, monkeypatch):
    monkeypatch.setattr(kaggle, "output", lambda u, k, ref: {
        "files": {"training_result.json": "https://www.kaggleusercontent.com/r", "Plexi + V30.nam": "https://www.kaggleusercontent.com/n"},
        "log": ""})
    monkeypatch.setattr(kaggle, "fetch_output", lambda url, **k: (
        json.dumps({"success": True, "output": {"filename": "Plexi + V30.nam"}}).encode() if url.endswith("/r") else b'{"model": 1}'))
    reply = client.get("/api/cab/jobs/ann/tonesearch-cab-1a2b/download", headers=HEADERS)
    assert reply.status_code == 200 and reply.data == b'{"model": 1}'
    assert "attachment" in reply.headers["Content-Disposition"]


def test_delete_removes_the_kaggle_job(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(kaggle, "delete", lambda u, k, ref: deleted.append(ref))
    reply = client.delete("/api/cab/jobs/ann/tonesearch-cab-1a2b", headers=HEADERS)
    assert reply.status_code == 200 and deleted == ["ann/tonesearch-cab-1a2b"]


def test_check_shows_gpu_hours(client, monkeypatch):
    monkeypatch.setattr(kaggle, "gpu_quota", lambda u, k: {"used_hours": 2.0, "allowed_hours": 30.0, "refreshes": None})
    assert client.post("/api/cab/check", headers=HEADERS).get_json()["allowed_hours"] == 30.0
    assert client.post("/api/cab/check", headers={"X-Kaggle-Username": "ann"}).status_code == 503


# ---- the kernel's audio steps (need numpy, scipy and soundfile, which Kaggle has) --------------------

def test_kernel_prepares_the_ir_like_nam_mixer():
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    sf = pytest.importorskip("soundfile")
    stereo = np.zeros((100, 2), dtype=np.float32)
    stereo[10] = [1.0, 0.5]  # 10 samples of leading silence, then the peak
    stereo[11] = [0.001, 0.001]  # below -40 dB, but after the start, so kept
    buffer = io.BytesIO()
    sf.write(buffer, stereo, 48000, format="WAV", subtype="FLOAT")
    taps, info = cab_kernel.prepare_ir(buffer.getvalue())
    assert info["leading_samples_trimmed"] == 10 and info["original_channels"] == 2
    assert taps[0] == pytest.approx(0.75) and len(taps) == 90

    buffer = io.BytesIO()
    sf.write(buffer, np.ones(441, dtype=np.float32), 44100, format="WAV", subtype="FLOAT")
    taps, info = cab_kernel.prepare_ir(buffer.getvalue())
    assert info["original_sample_rate"] == 44100 and len(taps) == 480


def test_kernel_peak_ceiling_only_turns_down():
    np = pytest.importorskip("numpy")
    loud = np.array([0.0, 4.0, -2.0], dtype=np.float32)
    out, peak, reduction = cab_kernel.peak_ceiling(loud)
    assert peak == pytest.approx(12.04, abs=0.01) and reduction == pytest.approx(12.24, abs=0.01)
    assert 20 * np.log10(np.max(np.abs(out))) == pytest.approx(-0.2, abs=1e-4)
    quiet = np.array([0.1, -0.2], dtype=np.float32)
    same, _, none = cab_kernel.peak_ceiling(quiet)
    assert none == 0.0 and same is quiet


def test_kernel_picks_the_full_model_and_reads_its_history():
    model = cab_kernel.full_model(a2(wavenet(metadata={"name": "full"})))
    assert model["metadata"] == {"name": "full"}
    assert cab_kernel.wavenet_receptive_field(model) == 15
    with pytest.raises(cab_kernel.CabJobError, match="LSTM"):
        cab_kernel.full_model({"architecture": "LSTM"})


def test_kernel_metadata_keeps_the_sources_credits():
    source = wavenet(metadata={"modeled_by": " Ann ", "gear_make": "Marshall", "tone_type": "crunch",
                               "input_level_dbu": 12.5, "gear_type": "amp"})
    assert cab_kernel.user_metadata_kwargs(source, "Plexi + V30") == {
        "name": "Plexi + V30", "modeled_by": "Ann", "gear_make": "Marshall", "gear_model": None,
        "tone_type": "crunch", "input_level_dbu": 12.5}


def test_kernel_converts_classic_a1_layers_for_the_pytorch_loader():
    classic = {"architecture": "WaveNet", "config": {"head": None, "head_scale": 0.02, "layers": [
        {"input_size": 1, "condition_size": 1, "head_size": 8, "channels": 16, "kernel_size": 3,
         "dilations": [1, 2], "activation": "Tanh", "gated": False, "head_bias": False},
        {"input_size": 16, "condition_size": 1, "head_size": 1, "channels": 8, "kernel_size": 3,
         "dilations": [1, 2], "activation": "Tanh", "gated": True, "head_bias": True}]}}
    plain, gated = cab_kernel.modern_wavenet(classic)["config"]["layers"]
    assert plain["head"] == {"out_channels": 8, "kernel_size": 1, "bias": False}
    assert "head_size" not in plain and "gated" not in plain and "gating_mode" not in plain
    assert gated["head"]["bias"] is True and gated["gating_mode"] == ["gated", "gated"]
    assert gated["secondary_activation"] == ["Sigmoid", "Sigmoid"] and gated["activation"] == ["Tanh", "Tanh"]
    modern = cab_kernel.modern_wavenet(classic)
    assert cab_kernel.modern_wavenet(modern) == modern  # files already in the new layout are left alone


def test_input_url_must_be_https(monkeypatch):
    monkeypatch.setenv(cab.URL_ENV, "https://marczewski.me.uk/tonesearch/cab/static/v3_0_0.wav")
    assert cab.input_url().endswith("v3_0_0.wav") and cab.training_ready()
    monkeypatch.setenv(cab.URL_ENV, "http://example.com/v3_0_0.wav")
    assert cab.input_url() is None and not cab.training_ready()


def test_start_with_only_a_download_url(client, monkeypatch):
    url = "https://marczewski.me.uk/tonesearch/cab/static/v3_0_0.wav"
    monkeypatch.setenv(cab.URL_ENV, url)
    pushed = {}
    monkeypatch.setattr(kaggle, "push_script", lambda u, k, **kw: pushed.update(kw) or {"ref": f"{u}/{kw['slug']}", "url": "x", "version": 1})
    assert _upload(client, json.dumps(wavenet()).encode(), pcm_wav()).status_code == 200
    assert pushed["dataset"] is None
    payload = cab_kernel.decode_payload(pushed["script"].split('PAYLOAD = "', 1)[1].split('"', 1)[0])
    assert payload["input_url"] == url
    assert "isn't set up" not in client.get("/cab").get_data(as_text=True)


def test_push_without_a_dataset_attaches_none():
    opener, calls = recording_opener({"url": "x"})
    kaggle.push_script("ann", "k", slug="tonesearch-cab-1a2b", title="t", script="x", opener=opener)
    assert json.loads(calls[0].data)["datasetDataSources"] == []


def test_kernel_downloads_and_checks_the_training_input(tmp_path, monkeypatch):
    import urllib.request
    good = b"official input"
    monkeypatch.setattr(cab_kernel, "OFFICIAL_V3_INPUT_MD5", __import__("hashlib").md5(good).hexdigest())
    monkeypatch.setattr(cab_kernel, "WORK_DIR", tmp_path / "work")
    served = {"body": good}
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: io.BytesIO(served["body"]))
    empty = tmp_path / "no-dataset"
    path = cab_kernel.find_training_input("https://example.com/v3_0_0.wav", root=empty)
    assert path.read_bytes() == good
    served["body"] = b"something else"
    with pytest.raises(cab_kernel.CabJobError, match="MD5"):
        cab_kernel.find_training_input("https://example.com/v3_0_0.wav", root=empty)
    with pytest.raises(cab_kernel.CabJobError, match="no download link"):
        cab_kernel.find_training_input(None, root=empty)


def test_assets_are_served_under_versioned_paths(client):
    """Pages link to static/v<version>/..., since Cloudflare may ignore ?v= and serve an old file."""
    page = client.get("/").get_data(as_text=True)
    assert f"static/v{app_module.ASSET_VERSION}/style.css" in page and "?v=" not in page
    reply = client.get("/static/v123/vendor/nam-wasm/index.js")
    assert reply.status_code == 200 and "immutable" in reply.headers["Cache-Control"]
    assert client.get("/static/v123/../app.py").status_code == 404


def test_fetch_output_encodes_spaces_in_kaggle_links():
    opener, calls = recording_opener(b"model")
    url = "https://www.kaggleusercontent.com/kf/1/eyJ..abc/SLASH_AFD_2_Head + Modern Boutique 4x12.nam"
    assert kaggle.fetch_output(url, opener=opener) == b"model"
    assert calls[0].full_url == "https://www.kaggleusercontent.com/kf/1/eyJ..abc/SLASH_AFD_2_Head%20+%20Modern%20Boutique%204x12.nam"
