"""GET /api/v1/file_status?transfer_id={id} - the file-transfer progress poll.

The Flutter file-transfer bubble polls this every 2s (skworld-app
skcomms_client.getFileStatus). The server route was never implemented, so under
the live authz enforce flip an authenticated operator got a fail-closed 403 on
an unmapped path (previously a silent 404). This endpoint serves the transfer
status from the persisted ~/.skchat/transfers/<id>.json in the shape the client
FileTransferStatus.fromJson expects.
"""

import json

from fastapi.testclient import TestClient

from skchat import webui


def _seed(tmp_path, tid, meta):
    d = tmp_path / "transfers"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(json.dumps(meta))


def test_outbound_in_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    _seed(
        tmp_path,
        "tid-1",
        {
            "transfer_id": "tid-1",
            "filename": "report.pdf",
            "file_size": 1000,
            "status": "sending",
            "direction": "outbound",
            "total_chunks": 4,
            "chunks_sent": 2,
        },
    )
    r = TestClient(webui.app).get("/api/v1/file_status", params={"transfer_id": "tid-1"})
    assert r.status_code == 200
    j = r.json()
    assert j["transfer_id"] == "tid-1"
    assert j["status"] == "in_progress"
    assert j["file_name"] == "report.pdf"
    assert j["file_size"] == 1000
    assert j["bytes_transferred"] == 500  # 2/4 of 1000


def test_completed_reports_full_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    _seed(
        tmp_path,
        "tid-2",
        {
            "transfer_id": "tid-2",
            "filename": "a.bin",
            "file_size": 2048,
            "status": "complete",
            "direction": "outbound",
            "total_chunks": 8,
            "chunks_sent": 8,
        },
    )
    j = TestClient(webui.app).get("/api/v1/file_status", params={"transfer_id": "tid-2"}).json()
    assert j["status"] == "completed"
    assert j["bytes_transferred"] == 2048


def test_failed_carries_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    _seed(
        tmp_path,
        "tid-3",
        {
            "transfer_id": "tid-3",
            "filename": "x",
            "file_size": 10,
            "status": "failed",
            "direction": "outbound",
            "error": "chunk hash mismatch",
        },
    )
    j = TestClient(webui.app).get("/api/v1/file_status", params={"transfer_id": "tid-3"}).json()
    assert j["status"] == "failed"
    assert j["error_message"] == "chunk hash mismatch"


def test_unknown_transfer_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    assert (
        TestClient(webui.app)
        .get("/api/v1/file_status", params={"transfer_id": "nope"})
        .status_code
        == 404
    )


def test_missing_param_is_422(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    assert TestClient(webui.app).get("/api/v1/file_status").status_code == 422


def test_path_traversal_transfer_id_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    r = TestClient(webui.app).get(
        "/api/v1/file_status", params={"transfer_id": "../../etc/passwd"}
    )
    assert r.status_code in (404, 422)
