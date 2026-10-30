"""Authenticated submission, validation, and polling."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from fpgen.artifacts import published_dir
from fpgen.jobs import QUEUE_LIMIT, claim_next, enqueue, fetch_job
from fpgen.render import MAX_RENDER_PIXELS
from fpgen.settings import Settings, get_settings
from fpgen.worker import process_job

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


def _broken() -> dict:
    return json.loads((ROOT / "examples" / "broken_layout.json").read_text(encoding="utf-8"))


def _scale(layout: dict, factor: float) -> dict:
    scaled = json.loads(json.dumps(layout))

    def grow(value):
        return round(value * factor, 3)

    scaled["footprint"]["width"] = grow(scaled["footprint"]["width"])
    scaled["footprint"]["depth"] = grow(scaled["footprint"]["depth"])
    for room in scaled["rooms"]:
        for key in ("x", "y", "w", "d"):
            room[key] = grow(room[key])
    for door in scaled.get("doors", []):
        if "width" in door:
            door["width"] = grow(door["width"])
        if "offset" in door:
            door["offset"] = grow(door["offset"])
    for window in scaled.get("windows", []):
        if "width" in window:
            window["width"] = grow(window["width"])
        if "offset" in window:
            window["offset"] = grow(window["offset"])
    return scaled


def _send(api, method: str, path: str, *, auth: bool = True, **kwargs) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}))
    if auth and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {API_KEY}"

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, headers=headers, **kwargs)

    return asyncio.run(run())


def test_api_keys_are_compared_without_using_equality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOORPLAN_API_KEY", API_KEY)
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", str(tmp_path / "data"))
    from fpgen.api import api_keys_match

    source = (ROOT / "fpgen" / "api.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source
    assert "sha256" in source
    assert api_keys_match(API_KEY, API_KEY) is True
    assert api_keys_match("nope", API_KEY) is False
    assert api_keys_match("x" * 500, API_KEY) is False
    assert api_keys_match("", API_KEY) is False


def test_health_stays_open_and_business_routes_require_a_bearer_token(api) -> None:
    schema = api.openapi()
    assert "security" not in schema["paths"]["/health"]["get"]
    assert not schema.get("security")
    assert schema["paths"]["/v1/jobs/generate"]["post"].get("security")

    open_health = _send(api, "GET", "/health", auth=False)
    wrong_health = _send(api, "GET", "/health", auth=False, headers={"Authorization": "Bearer wrong"})
    missing = _send(api, "POST", "/v1/jobs/generate", auth=False, json={"beds": 1})
    wrong = _send(api, "POST", "/v1/jobs/generate", auth=False, headers={"Authorization": "Bearer wrong"}, json={"beds": 1})
    basic = _send(api, "POST", "/v1/layouts/validate", auth=False, headers={"Authorization": "Basic abc"}, json={"layout": {}})

    assert open_health.status_code == 200
    assert open_health.json() == {"status": "ok"}
    assert wrong_health.status_code == 200
    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "unauthorized"
    assert missing.headers["www-authenticate"].lower().startswith("bearer")
    assert wrong.status_code == 401
    assert wrong.json()["detail"]["message"] == "Invalid API key."
    assert basic.status_code == 401


def test_generate_submission_queues_without_drawing(api, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("generation ran during submission")

    monkeypatch.setattr("fpgen.generate.generate_layout", forbidden)
    monkeypatch.setattr("fpgen.render.render_layout", forbidden)

    response = _send(
        api,
        "POST",
        "/v1/jobs/generate",
        json={"beds": 3, "baths": 2.5, "shape": "L", "garage": 2, "garage_side": "left", "laundry": False},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["poll_after_seconds"] == 2
    assert body["status_url"] == f"/v1/jobs/{body['id']}"
    assert response.headers["location"] == body["status_url"]
    stored = fetch_job(__import__("uuid").UUID(body["id"]), settings=settings)
    assert stored is not None
    assert stored.status == "queued"
    assert stored.request_payload["baths"] == 2.5
    assert stored.request_payload["garage"] == 2
    assert stored.request_payload["shape"] == "L"
    assert stored.request_payload["garage_side"] == "left"
    assert stored.request_payload["laundry"] is False
    assert stored.request_payload["sqft"] == 1600
    assert stored.request_payload["seed"] == 1
    assert stored.request_payload["dpi"] == 170
    assert not published_dir(settings, stored.id).exists()


def test_defaults_and_constraints(api, settings: Settings) -> None:
    accepted = _send(api, "POST", "/v1/jobs/generate", json={})
    assert accepted.status_code == 202
    stored = fetch_job(__import__("uuid").UUID(accepted.json()["id"]), settings=settings)
    assert stored.request_payload["beds"] == 3
    assert stored.request_payload["baths"] == 2
    assert stored.request_payload["sqft"] == 1600

    too_small = _send(api, "POST", "/v1/jobs/generate", json={"beds": 3, "baths": 2, "sqft": 100})
    beds = _send(api, "POST", "/v1/jobs/generate", json={"beds": 0})
    garage = _send(api, "POST", "/v1/jobs/generate", json={"garage": 4})
    shape = _send(api, "POST", "/v1/jobs/generate", json={"shape": "U"})
    side = _send(api, "POST", "/v1/jobs/generate", json={"garage_side": "north"})
    dpi = _send(api, "POST", "/v1/jobs/generate", json={"dpi": 71})
    high_dpi = _send(api, "POST", "/v1/jobs/generate", json={"dpi": 301})
    malformed = _send(api, "POST", "/v1/jobs/generate", json={"beds": "three"})
    extra = _send(api, "POST", "/v1/jobs/generate", json={"beds": 3, "stories": 2})
    infinite = _send(
        api,
        "POST",
        "/v1/jobs/generate",
        content=b'{"sqft": 1e309}',
        headers={"Content-Type": "application/json"},
    )

    assert too_small.status_code == 422
    assert too_small.json()["detail"]["code"] == "invalid_request"
    assert "too small" in too_small.json()["detail"]["message"]
    assert beds.status_code == 422
    assert beds.json()["detail"]["message"] == "beds must be between 1 and 6"
    assert garage.json()["detail"]["message"] == "garage must be 0, 1, 2 or 3 cars"
    assert "rect" in shape.json()["detail"]["message"]
    assert "left" in side.json()["detail"]["message"]
    assert dpi.status_code == 422
    assert high_dpi.status_code == 422
    assert malformed.status_code == 422
    assert extra.status_code == 422
    assert infinite.status_code == 422
    assert "finite" in infinite.text
    from fpgen.jobs import QUEUED
    from fpgen.db import session_scope
    from fpgen.models import Job
    from sqlalchemy import func, select

    with session_scope(settings) as session:
        queued = int(session.scalar(select(func.count()).select_from(Job).where(Job.status == QUEUED)) or 0)
    # Only the empty-body submission was queued. The rejected calls were not.
    assert queued == 1


def test_render_submission_rejects_invalid_layouts_and_oversized_drawings(api, settings: Settings) -> None:
    from fpgen.db import session_scope
    from fpgen.jobs import QUEUED
    from fpgen.models import Job
    from sqlalchemy import func, select

    broken = _send(api, "POST", "/v1/jobs/render", json={"layout": _broken(), "dpi": 96})
    huge = _send(api, "POST", "/v1/jobs/render", json={"layout": _scale(_cottage(), 8), "dpi": 170})
    accepted = _send(api, "POST", "/v1/jobs/render", json={"layout": _scale(_cottage(), 8), "dpi": 72, "title": "Wide"})
    cottage = _send(api, "POST", "/v1/jobs/render", json={"layout": _cottage(), "dpi": 300})

    assert broken.status_code == 422
    assert broken.json()["detail"]["code"] == "validation_failed"
    assert any("overlap" in error for error in broken.json()["detail"]["errors"])
    assert huge.status_code == 422
    assert huge.json()["detail"]["code"] == "image_too_large"
    assert str(MAX_RENDER_PIXELS) in huge.json()["detail"]["message"].replace(",", "")
    assert accepted.status_code == 202
    assert cottage.status_code == 202
    with session_scope(settings) as session:
        queued = int(session.scalar(select(func.count()).select_from(Job).where(Job.status == QUEUED)) or 0)
    assert queued == 2
    stored = fetch_job(__import__("uuid").UUID(accepted.json()["id"]), settings=settings)
    assert stored.request_payload["layout"]["rooms"][0]["id"] == "living"
    assert stored.request_payload["title"] == "Wide"
    assert stored.request_payload["dpi"] == 72


def test_validate_reports_errors_without_failing_the_request(api) -> None:
    valid = _send(api, "POST", "/v1/layouts/validate", json={"layout": _cottage()})
    invalid = _send(api, "POST", "/v1/layouts/validate", json={"layout": _broken()})
    malformed = _send(api, "POST", "/v1/layouts/validate", json={"layout": []})

    assert valid.status_code == 200
    assert valid.json()["ok"] is True
    assert valid.json()["errors"] == []
    assert "areas" in valid.json()
    assert invalid.status_code == 200
    assert invalid.json()["ok"] is False
    assert invalid.json()["errors"]
    assert malformed.status_code == 422


def test_polling_follows_a_job_until_it_succeeds_or_fails(api, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import matplotlib.pyplot as plt

    created = _send(api, "POST", "/v1/jobs/generate", json={"beds": 1, "baths": 1, "sqft": 620, "seed": 1})
    assert created.status_code == 202
    job_id = created.json()["id"]
    queued = _send(api, "GET", f"/v1/jobs/{job_id}")
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"
    assert queued.json()["started_at"] is None
    assert "layout" not in queued.json()
    assert "files" not in queued.json()
    assert "error" not in queued.json()

    claimed = claim_next(settings=settings)
    assert claimed is not None
    running = _send(api, "GET", f"/v1/jobs/{job_id}")
    assert running.json()["status"] == "running"
    assert running.json()["started_at"]

    finished = process_job(claimed, settings)
    assert finished.status == "succeeded"
    done = _send(api, "GET", f"/v1/jobs/{job_id}")
    body = done.json()
    assert body["status"] == "succeeded"
    assert body["validation"]["ok"] is True
    assert body["layout"]["rooms"]
    assert body["expires_at"]
    assert body["files"] == {
        "png": f"/v1/jobs/{job_id}/files/png",
        "svg": f"/v1/jobs/{job_id}/files/svg",
        "json": f"/v1/jobs/{job_id}/files/json",
    }
    assert "http://" not in done.text
    assert "https://" not in done.text

    figures = []
    monkeypatch.setattr(plt, "figure", lambda *args, **kwargs: figures.append(1))
    monkeypatch.setattr("fpgen.worker.generate_layout", lambda *args, **kwargs: _broken())
    failed_submit = _send(api, "POST", "/v1/jobs/generate", json={"beds": 1, "baths": 1, "sqft": 620, "seed": 2})
    failed_id = failed_submit.json()["id"]
    failed_job = claim_next(settings=settings)
    assert process_job(failed_job, settings).error_code == "validation_failed"
    failed = _send(api, "GET", f"/v1/jobs/{failed_id}")
    assert failed.json()["status"] == "failed"
    assert failed.json()["error"]["code"] == "validation_failed"
    assert "Layout failed validation." in failed.json()["error"]["message"]
    assert "layout" not in failed.json()
    assert figures == []

    missing = _send(api, "GET", "/v1/jobs/00000000-0000-0000-0000-000000000000")
    bogus = _send(api, "GET", "/v1/jobs/not-a-uuid")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "not_found"
    assert bogus.status_code == 422


def test_worker_rejects_an_oversized_render_before_allocating_a_figure(
    api, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matplotlib.pyplot as plt

    figures = []
    monkeypatch.setattr(plt, "figure", lambda *args, **kwargs: figures.append(1))
    job = enqueue(
        "render",
        {"layout": _scale(_cottage(), 8), "dpi": 170},
        settings=settings,
    )
    claimed = claim_next(settings=settings)
    failed = process_job(claimed, settings)

    assert failed.id == job.id
    assert failed.error_code == "image_too_large"
    assert "25,000,000" in failed.error_message or "25000000" in failed.error_message.replace(",", "")
    assert figures == []
    assert not published_dir(settings, job.id).exists()


def test_render_layout_checks_the_budget_before_creating_a_figure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import matplotlib.pyplot as plt

    from fpgen.render import RenderTooLarge, render_layout

    monkeypatch.setattr(plt, "figure", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("figure")))
    monkeypatch.setattr("fpgen.render.MAX_RENDER_PIXELS", 1)
    with pytest.raises(RenderTooLarge):
        render_layout(_cottage(), str(tmp_path / "plan"), dpi=72)


def test_queue_full_responds_with_retry_after(api, settings: Settings) -> None:
    from sqlalchemy import func, select

    from fpgen.db import session_scope
    from fpgen.jobs import QUEUED
    from fpgen.models import Job

    for number in range(QUEUE_LIMIT):
        enqueue("generate", {"n": number}, settings=settings)

    response = _send(api, "POST", "/v1/jobs/generate", json={"beds": 1, "baths": 1, "sqft": 620})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    assert response.json()["detail"]["code"] == "queue_full"
    assert "id" not in response.json()
    with session_scope(settings) as session:
        queued = int(session.scalar(select(func.count()).select_from(Job).where(Job.status == QUEUED)) or 0)
    assert queued == QUEUE_LIMIT


def test_request_body_limit_is_one_mebibyte(api) -> None:
    from fpgen.api import MAX_REQUEST_BYTES

    exact = _send(
        api,
        "POST",
        "/v1/jobs/generate",
        content=b"x" * MAX_REQUEST_BYTES,
        headers={"Content-Type": "application/json"},
    )
    over = _send(
        api,
        "POST",
        "/v1/jobs/generate",
        content=b"x" * (MAX_REQUEST_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert exact.status_code == 422
    assert "request_too_large" not in exact.text
    assert over.status_code == 422
    assert over.json()["detail"]["code"] == "request_too_large"
