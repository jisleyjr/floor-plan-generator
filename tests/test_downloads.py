"""Authenticated downloads of completed plans."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from fpgen.artifacts import published_dir, resolve_artifact
from fpgen.jobs import claim_next, enqueue
from fpgen.settings import Settings, get_settings
from fpgen.worker import cleanup_expired, process_job

ROOT = Path(__file__).resolve().parents[1]
API_KEY = "test-key"


def _migrate(url: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("DATABASE_URL", "FLOORPLAN_DATA_DIR", "FLOORPLAN_API_KEY"):
        env.pop(name, None)
    env["DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    database = tmp_path / "queue.sqlite"
    url = "sqlite:///" + database.as_posix()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", str(tmp_path / "data"))
    upgraded = _migrate(url)
    assert upgraded.returncode == 0, upgraded.stderr
    return get_settings()


@pytest.fixture
def api(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOORPLAN_API_KEY", API_KEY)
    from fpgen.api import create_app

    application = create_app()
    try:
        yield application
    finally:
        application.state.engine.dispose()


def _cottage() -> dict:
    return json.loads((ROOT / "examples" / "cottage_2bd.json").read_text(encoding="utf-8"))


def _send(api, method: str, path: str, *, auth: bool = True, **kwargs) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}))
    if auth and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {API_KEY}"

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, headers=headers, **kwargs)

    return asyncio.run(run())


def _finish_render(settings: Settings):
    job = enqueue("render", {"layout": _cottage(), "dpi": 72}, settings=settings)
    claimed = claim_next(settings=settings)
    assert claimed is not None
    finished = process_job(claimed, settings)
    assert finished.status == "succeeded"
    return finished


def test_downloads_return_the_published_files(api, settings: Settings) -> None:
    finished = _finish_render(settings)
    status = _send(api, "GET", f"/v1/jobs/{finished.id}")
    body = status.json()
    assert body["files"]["png"].startswith("/")
    assert "http://" not in status.text
    assert status.headers["cache-control"] == "private, no-store"

    png = _send(api, "GET", body["files"]["png"])
    svg = _send(api, "GET", body["files"]["svg"])
    document = _send(api, "GET", body["files"]["json"])

    assert png.status_code == 200
    assert png.headers["content-type"].startswith("image/png")
    assert 'filename="plan.png"' in png.headers["content-disposition"]
    assert png.headers["cache-control"] == "private, no-store"
    assert png.content.startswith(b"\x89PNG")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert 'filename="plan.svg"' in svg.headers["content-disposition"]
    assert b"<svg" in svg.content
    assert document.status_code == 200
    assert document.headers["content-type"].startswith("application/json")
    assert 'filename="layout.json"' in document.headers["content-disposition"]
    assert json.loads(document.content)["rooms"]

    health = _send(api, "GET", "/health", auth=False)
    assert health.status_code == 200
    assert health.headers.get("cache-control") != "private, no-store"


def test_downloads_require_auth_and_a_succeeded_job(api, settings: Settings) -> None:
    queued = enqueue("render", {"layout": _cottage(), "dpi": 72}, settings=settings)
    waiting = _send(api, "GET", f"/v1/jobs/{queued.id}/files/png")
    anonymous = _send(api, "GET", f"/v1/jobs/{queued.id}/files/png", auth=False)
    assert waiting.status_code == 409
    assert waiting.json()["detail"]["code"] == "not_ready"
    assert waiting.json()["detail"]["message"] == "Job is queued."
    assert anonymous.status_code == 401

    claimed = claim_next(settings=settings)
    running = _send(api, "GET", f"/v1/jobs/{queued.id}/files/svg")
    assert running.status_code == 409
    assert running.json()["detail"]["message"] == "Job is running."

    from fpgen.jobs import mark_failed

    mark_failed(claimed.id, "validation_failed", "Layout failed validation.", settings=settings)
    failed = _send(api, "GET", f"/v1/jobs/{queued.id}/files/json")
    assert failed.status_code == 409
    assert failed.json()["detail"]["message"] == "Job is failed."

    missing = _send(api, "GET", f"/v1/jobs/{uuid.uuid4()}/files/png")
    unknown_format = _send(api, "GET", f"/v1/jobs/{queued.id}/files/exe")
    assert missing.status_code == 404
    assert missing.json()["detail"]["message"] == "Job not found."
    assert unknown_format.status_code == 404
    assert unknown_format.json()["detail"]["message"] == "Artifact is not available."


def test_traversal_and_symlinks_cannot_read_other_files(api, settings: Settings, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret-floorplan", encoding="utf-8")
    finished = _finish_render(settings)
    png = published_dir(settings, finished.id) / "plan.png"
    png.unlink()
    png.symlink_to(secret)

    stolen = _send(api, "GET", f"/v1/jobs/{finished.id}/files/png")
    escaped = _send(api, "GET", f"/v1/jobs/{finished.id}/files/..%2F..%2Fsecret.txt")
    dotted = _send(api, "GET", f"/v1/jobs/{finished.id}/files/plan.png")

    assert stolen.status_code == 404
    assert b"top-secret-floorplan" not in stolen.content
    assert escaped.status_code == 404
    assert b"top-secret-floorplan" not in escaped.content
    assert dotted.status_code == 404
    assert resolve_artifact(settings, finished.id, "../secret.txt") is None
    assert resolve_artifact(settings, finished.id, "png") is None
    assert secret.read_text(encoding="utf-8") == "top-secret-floorplan"


def test_missing_and_expired_artifacts_are_not_found(api, settings: Settings) -> None:
    from fpgen.db import session_scope
    from fpgen.models import Job

    finished = _finish_render(settings)
    (published_dir(settings, finished.id) / "plan.svg").unlink()
    missing = _send(api, "GET", f"/v1/jobs/{finished.id}/files/svg")
    assert missing.status_code == 404
    assert missing.json()["detail"]["message"] == "Artifact is not available."
    still_there = _send(api, "GET", f"/v1/jobs/{finished.id}/files/json")
    assert still_there.status_code == 200

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    with session_scope(settings) as session:
        row = session.get(Job, finished.id)
        assert row is not None
        row.expires_at = past
        session.commit()
    removed = cleanup_expired(settings)
    assert [job.id for job in removed] == [finished.id]

    expired = _send(api, "GET", f"/v1/jobs/{finished.id}")
    expired_file = _send(api, "GET", f"/v1/jobs/{finished.id}/files/png")
    assert expired.status_code == 404
    assert expired_file.status_code == 404
    assert not published_dir(settings, finished.id).exists()
