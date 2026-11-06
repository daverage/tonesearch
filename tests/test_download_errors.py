import io
import json
import gzip
from urllib.error import HTTPError

import pytest

from tonesearch import research


def test_model_request_identifies_application_instead_of_urllib_default():
    def opener(request, timeout):
        assert request.get_header("User-agent") == "ToneSearch/1.0 (TONE3000 API client)"
        return io.BytesIO(b"NAM")
    assert research._fetch_file("https://files.tone3000.com/model.nam?verify=test", "token", opener) == b"NAM"


def test_cloudflare_plaintext_code_is_visible_despite_json_content_type():
    error = HTTPError("https://files.tone3000.com/a", 403, "Forbidden",
                      {"Content-Type": "application/json"}, io.BytesIO(b"error code: 1010\n"))
    assert "error code: 1010" in research._download_failure_reason(error, "token")


def test_nested_json_provider_error_is_visible_without_verification_values():
    body = {"detail": {"reason": "Verification failed", "verify": "private-verify"},
            "context": "private-verify", "access_token": "private-oauth"}
    error = HTTPError("https://files.tone3000.com/a?verify=private-verify", 403, "Forbidden",
                      {"Content-Type": "application/json"}, io.BytesIO(json.dumps(body).encode()))
    detail = research._download_failure_reason(error, "private-oauth")
    assert "Verification failed" in detail
    assert "private-verify" not in detail
    assert "private-oauth" not in detail


def test_storage_xml_error_is_readable_and_credentials_are_redacted():
    token = "private-oauth-token"
    body = f"<Error><Code>AccessDenied</Code><Message>Denied {token} https://files.tone3000.com/a?signature=secret</Message></Error>"
    error = HTTPError("https://files.tone3000.com/a", 403, "Forbidden",
                      {"Server": "cloudflare", "Content-Type": "application/xml"}, io.BytesIO(body.encode()))
    detail = research._download_failure_reason(error, token)
    assert "AccessDenied" in detail
    assert "Server: cloudflare" in detail
    assert token not in detail
    assert "signature=secret" not in detail


def test_empty_error_preserves_authentication_response_header():
    error = HTTPError("https://files.tone3000.com/a", 403, "Forbidden",
                      {"WWW-Authenticate": 'Bearer error="insufficient_scope"'}, io.BytesIO(b""))
    assert "insufficient_scope" in research._download_failure_reason(error, "token")


def test_download_failure_reports_query_names_without_signed_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TONESEARCH_DATA_DIR", str(tmp_path))
    url = "https://files.tone3000.com/model.nam?X-Amz-Signature=private-signature"

    def opener(request, timeout):
        assert request.full_url == url
        assert request.get_header("Authorization") == "Bearer private-oauth-token"
        raise HTTPError(url, 403, "Forbidden", {"Server": "cloudflare"}, io.BytesIO(b""))

    with pytest.raises(RuntimeError) as caught:
        research._fetch_file(url, "private-oauth-token", opener)
    message = str(caught.value)
    assert "HTTP 403 from files.tone3000.com" in message
    assert "X-Amz-Signature" in message
    assert "private-signature" not in message
    assert "private-oauth-token" not in message
    assert (tmp_path / "download-errors" / "latest.json").exists()


def test_error_dump_captures_gzip_body_and_redacts_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("TONESEARCH_DATA_DIR", str(tmp_path))
    body = gzip.compress(json.dumps({"detail": "verification rejected", "verify": "private-verification",
                                     "access_token": "private-oauth"}).encode())
    error = HTTPError("https://files.tone3000.com/a?verify=private-verification", 403, "Forbidden",
                      {"Content-Encoding": "gzip", "Set-Cookie": "private-cookie", "CF-Ray": "ray-id"},
                      io.BytesIO(body))
    path = research._dump_download_error(error, "private-oauth", body)
    saved = path.read_text()
    record = json.loads(saved)
    assert record["status"] == 403
    assert record["raw_body_bytes_captured"] == len(body)
    assert record["response_headers"]["CF-Ray"] == "ray-id"
    assert "verification rejected" in record["body_text"]
    for secret in ("private-verification", "private-oauth", "private-cookie"):
        assert secret not in saved
    assert path.stat().st_mode & 0o077 == 0


def test_empty_body_is_recorded_explicitly(tmp_path, monkeypatch):
    monkeypatch.setenv("TONESEARCH_DATA_DIR", str(tmp_path))
    error = HTTPError("https://files.tone3000.com/a", 403, "Forbidden", {}, io.BytesIO(b""))
    record = json.loads(research._dump_download_error(error, "token", b"").read_text())
    assert record["raw_body_bytes_captured"] == 0
    assert record["body_text"] == ""


def _fake_tone3000(seen):
    """Answers the model list and the file, recording the Authorization header each request carried."""
    def opener(request, timeout=None):
        seen.append((request.full_url, request.get_header("Authorization")))
        if "files.tone3000.com" in request.full_url:
            return io.BytesIO(b"NAM")
        return io.BytesIO(json.dumps({"data": [{"id": 7, "name": "Plexi", "model_url": "https://files.tone3000.com/7.nam?verify=v"}],
                                      "total_pages": 1}).encode())
    return opener


def test_download_without_connected_account_uses_the_server_key(monkeypatch):
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server")
    seen = []
    data, name = research.tone3000_model_download(1, 7, opener=_fake_tone3000(seen))
    assert (data, name) == (b"NAM", "Plexi")
    assert seen and all(auth == "Bearer t3k_cs_server" for _, auth in seen)


def test_pack_zip_without_connected_account_uses_the_server_key(monkeypatch):
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server")
    seen = []
    _zip, count = research.tone3000_pack_zip(1, opener=_fake_tone3000(seen))
    assert count == 1
    assert any("files.tone3000.com" in url for url, _ in seen)
    assert all(auth == "Bearer t3k_cs_server" for _, auth in seen)


def test_connected_account_token_wins_over_the_server_key(monkeypatch):
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server")
    seen = []
    research.tone3000_model_download(1, 7, access_token="oauth-token", opener=_fake_tone3000(seen))
    assert all(auth == "Bearer oauth-token" for _, auth in seen)
