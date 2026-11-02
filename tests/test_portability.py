"""The same service flow on SQLite, MySQL 8.4, and MariaDB 11.4."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from PIL import Image

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
def service(backend_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", backend_url)
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FLOORPLAN_API_KEY", API_KEY)
    upgraded = _migrate(backend_url)
    assert upgraded.returncode == 0, upgraded.stderr
    from fpgen.api import create_app
    from fpgen.settings import get_settings

    application = create_app()
    try:
        yield application, get_settings()
    finally:
        application.state.engine.dispose()


def _send(api, method: str, path: str, **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Bearer {API_KEY}", **kwargs.pop("headers", {})}

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, headers=headers, **kwargs)

    return asyncio.run(run())


def _cottage() -> dict:
    return json.loads((ROOT / "examples" / "cottage_2bd.json").read_text(encoding="utf-8"))


def _broken() -> dict:
    return json.loads((ROOT / "examples" / "broken_layout.json").read_text(encoding="utf-8"))


def _finish(settings) -> None:
    from fpgen.worker import run_once

    while True:
        finished = run_once(settings)
        if finished is None:
            return
        assert finished.status == "succeeded", finished.error_message


def _layout(api, settings, payload: dict) -> dict:
    created = _send(api, "POST", "/v1/jobs/generate", json=payload)
    assert created.status_code == 202, created.text
    _finish(settings)
    job_id = created.json()["id"]
    document = _send(api, "GET", f"/v1/jobs/{job_id}/files/json")
    assert document.status_code == 200, document.text
    return json.loads(document.content)


def test_application_code_does_not_start_docker() -> None:
    for name in ("api.py", "worker.py", "db.py", "jobs.py", "settings.py"):
        text = (ROOT / "fpgen" / name).read_text(encoding="utf-8").lower()
        assert "docker" not in text
    compose = (ROOT / "tests" / "docker-compose.databases.yml").read_text(encoding="utf-8")
    assert "mysql:8.4" in compose
    assert "mariadb:11.4" in compose
    assert "does not start Docker" in compose


def test_generate_poll_and_download_round_trip(service) -> None:
    api, settings = service
    created = _send(
        api,
        "POST",
        "/v1/jobs/generate",
        json={"beds": 3, "baths": 2, "sqft": 1600, "seed": 1, "title": "Café plan"},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    queued = _send(api, "GET", f"/v1/jobs/{job_id}")
    assert queued.json()["status"] == "queued"

    _finish(settings)
    done = _send(api, "GET", f"/v1/jobs/{job_id}")
    body = done.json()
    assert body["status"] == "succeeded"
    assert body["validation"]["ok"] is True
    assert done.headers["cache-control"] == "private, no-store"
    assert body["files"]["png"].startswith("/v1/jobs/")

    png = _send(api, "GET", body["files"]["png"])
    svg = _send(api, "GET", body["files"]["svg"])
    document = _send(api, "GET", body["files"]["json"])
    assert png.status_code == 200
    assert png.content.startswith(b"\x89PNG")
    assert png.headers["content-type"].startswith("image/png")
    assert png.headers["cache-control"] == "private, no-store"
    _assert_drawing(png.content)
    assert b"<svg" in svg.content
    assert "Café" in svg.text
    layout = json.loads(document.content)
    assert layout["meta"]["seed"] == 1
    assert layout["rooms"]

    again = _layout(api, settings, {"beds": 3, "baths": 2, "sqft": 1600, "seed": 1})
    other = _layout(api, settings, {"beds": 3, "baths": 2, "sqft": 1600, "seed": 2})
    assert again == layout
    assert other != layout


def test_rectangular_and_l_shaped_plans(service) -> None:
    api, settings = service
    right = _layout(
        api,
        settings,
        {"beds": 3, "baths": 2, "sqft": 1800, "seed": 1, "shape": "L", "garage": 2, "garage_side": "right"},
    )
    left = _layout(
        api,
        settings,
        {"beds": 3, "baths": 2, "sqft": 1800, "seed": 1, "shape": "L", "garage": 2, "garage_side": "left"},
    )
    one_car = _layout(api, settings, {"beds": 3, "baths": 2, "sqft": 1600, "seed": 1, "garage": 1})
    family = _layout(api, settings, {"beds": 3, "baths": 2, "sqft": 1700, "seed": 2, "shape": "L"})

    assert right["meta"]["garage_side"] == "right"
    assert left["meta"]["garage_side"] == "left"
    assert right != left
    assert any(room["type"] == "garage" for room in right["rooms"])
    assert any(room["type"] == "garage" for room in one_car["rooms"])
    assert "rects" not in one_car["footprint"]
    assert "rects" in family["footprint"]
    assert any(room["type"] == "family" for room in family["rooms"])
    assert not any(room["type"] == "garage" for room in family["rooms"])

    rendered = _send(api, "POST", "/v1/jobs/render", json={"layout": _cottage(), "dpi": 72, "title": "Cottage"})
    assert rendered.status_code == 202, rendered.text
    _finish(settings)
    png = _send(api, "GET", f"/v1/jobs/{rendered.json()['id']}/files/png")
    assert png.status_code == 200
    assert png.content.startswith(b"\x89PNG")


def test_invalid_submissions_are_not_queued(service) -> None:
    from sqlalchemy import func, select

    from fpgen.db import session_scope
    from fpgen.models import Job

    api, settings = service
    broken = _send(api, "POST", "/v1/jobs/render", json={"layout": _broken()})
    impossible = _send(api, "POST", "/v1/jobs/generate", json={"beds": 4, "baths": 3, "sqft": 200})
    assert broken.status_code == 422
    assert broken.json()["detail"]["code"] == "validation_failed"
    assert impossible.status_code == 422
    assert "too small" in impossible.json()["detail"]["message"]
    with session_scope(settings) as session:
        assert int(session.scalar(select(func.count()).select_from(Job)) or 0) == 0


def test_cli_smoke(tmp_path: Path) -> None:
    rect = tmp_path / "rect"
    wing = tmp_path / "wing"
    cottage = tmp_path / "cottage"
    env = os.environ.copy()
    for name in ("DATABASE_URL", "FLOORPLAN_DATA_DIR", "FLOORPLAN_API_KEY"):
        env.pop(name, None)

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "floorplan.py"), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    generated = run(["--beds", "3", "--baths", "2", "--sqft", "1600", "--seed", "1", "--out", str(rect)])
    assert generated.returncode == 0, generated.stderr
    assert rect.with_suffix(".png").read_bytes() == (ROOT / "samples" / "3bd2ba_1600sqft_seed1.png").read_bytes()
    assert b"<svg" in rect.with_suffix(".svg").read_bytes()
    _assert_drawing(rect.with_suffix(".png").read_bytes())

    shaped = run(
        ["--beds", "3", "--baths", "2", "--sqft", "1800", "--seed", "1", "--shape", "L", "--garage", "2",
         "--garage-side", "left", "--out", str(wing)]
    )
    assert shaped.returncode == 0, shaped.stderr
    assert wing.with_suffix(".png").is_file()
    assert wing.with_suffix(".svg").is_file()
    _assert_drawing(wing.with_suffix(".png").read_bytes())

    rendered = run(["--layout", str(ROOT / "examples" / "cottage_2bd.json"), "--out", str(cottage)])
    assert rendered.returncode == 0, rendered.stderr
    assert cottage.with_suffix(".png").read_bytes().startswith(b"\x89PNG")

    broken = run(["--layout", str(ROOT / "examples" / "broken_layout.json"), "--validate-only"])
    assert broken.returncode == 1
    assert "INVALID" in broken.stdout


def _assert_drawing(png: bytes) -> None:
    from io import BytesIO

    image = Image.open(BytesIO(png))
    image.load()
    assert image.size[0] > 100 and image.size[1] > 100
    darkest, lightest = image.convert("L").getextrema()
    assert darkest != lightest
