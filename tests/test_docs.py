"""The operator docs and Postman collection match the running service."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def test_postman_collection_only_needs_base_url_and_api_key() -> None:
    collection = json.loads(
        (ROOT / "docs" / "floor-plan-api.postman_collection.json").read_text(encoding="utf-8")
    )
    variables = {item["key"]: item.get("value", "") for item in collection["variable"]}
    assert set(variables) >= {"base_url", "api_key", "job_id"}
    assert variables["base_url"] == "http://127.0.0.1:8000"
    assert variables["api_key"] == ""
    assert variables["job_id"] == ""
    assert collection["info"]["schema"].endswith("/v2.1.0/collection.json")

    names = {item["name"] for item in collection["item"]}
    assert {"Health", "Generate", "Render", "Validate layout", "Poll job", "Download PNG", "Download SVG", "Download JSON"} <= names

    blob = json.dumps(collection)
    assert "{{base_url}}" in blob
    assert "{{api_key}}" in blob
    assert "{{job_id}}" in blob
    assert "every two seconds" in blob.lower() or "every 2 seconds" in blob.lower()
    health = next(item for item in collection["item"] if item["name"] == "Health")
    assert health["request"]["auth"]["type"] == "noauth"
    assert collection["auth"]["type"] == "bearer"
    generate = next(item for item in collection["item"] if item["name"] == "Generate")
    script = "\n".join(generate["event"][0]["script"]["exec"])
    assert "job_id" in script


def test_service_doc_covers_startup_tunnel_and_database_switch() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "service.md").read_text(encoding="utf-8")
    combined = readme + "\n" + guide
    for phrase in (
        "uvicorn fpgen.api:app --host 127.0.0.1 --port 8000 --workers 1",
        "python -m fpgen.worker",
        "alembic upgrade head",
        "http://127.0.0.1:8000",
        "every two seconds",
        "Cloudflare Access",
        "FLOORPLAN_API_KEY",
        "requirements-mysql.txt",
    ):
        assert phrase in combined
    assert "does not move" in guide or "do not move" in guide or "do not transfer" in guide.lower() or "Old job ids and history do not move" in guide
    assert "John is looking into it" in guide
    assert "bearer API key is the only authentication" in guide or "bearer API key is the auth" in guide.lower() or "bearer API key is the only" in guide
    assert "retention" in guide.lower()
    assert "Docker is not required" in guide
    for step in ("Stop submitting", "Stop the API and the worker", "empty", "Start the API and the worker"):
        assert step in guide


def test_documented_api_and_worker_processes(tmp_path: Path) -> None:
    data = tmp_path / "runtime"
    database = data / "floorplan.sqlite"
    env = os.environ.copy()
    for name in ("DATABASE_URL", "FLOORPLAN_DATA_DIR", "FLOORPLAN_API_KEY"):
        env.pop(name, None)
    env.update(
        {
            "DATABASE_URL": "sqlite:///" + database.as_posix(),
            "FLOORPLAN_DATA_DIR": str(data),
            "FLOORPLAN_API_KEY": "doc-test-key",
        }
    )
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert migrated.returncode == 0, migrated.stderr

    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "fpgen.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--workers",
            "1",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker = subprocess.Popen(
        [sys.executable, "-m", "fpgen.worker"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with httpx.Client(base_url="http://127.0.0.1:8765", timeout=5) as client:
            deadline = time.monotonic() + 20
            health = None
            while time.monotonic() < deadline:
                if api.poll() is not None or worker.poll() is not None:
                    break
                try:
                    health = client.get("/health")
                except httpx.HTTPError:
                    time.sleep(0.2)
                    continue
                if health.status_code == 200:
                    break
                time.sleep(0.2)
            assert health is not None and health.status_code == 200, _output(api, worker)
            assert health.json() == {"status": "ok"}
            assert client.post("/v1/jobs/generate", json={"beds": 1}).status_code == 401

            created = client.post(
                "/v1/jobs/generate",
                headers={"Authorization": "Bearer doc-test-key"},
                json={"beds": 1, "baths": 1, "sqft": 620, "seed": 1},
            )
            assert created.status_code == 202, created.text
            job_id = created.json()["id"]
            status = "queued"
            body: dict = {}
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and status not in {"succeeded", "failed"}:
                polled = client.get(f"/v1/jobs/{job_id}", headers={"Authorization": "Bearer doc-test-key"})
                assert polled.status_code == 200, polled.text
                body = polled.json()
                status = body["status"]
                time.sleep(0.2)
            assert status == "succeeded", body
            png = client.get(body["files"]["png"], headers={"Authorization": "Bearer doc-test-key"})
            assert png.status_code == 200
            assert png.content.startswith(b"\x89PNG")
            assert png.headers["content-type"].startswith("image/png")
    finally:
        _stop(worker)
        _stop(api)


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _output(api: subprocess.Popen[str], worker: subprocess.Popen[str]) -> str:
    return f"api exited {api.poll()}, worker exited {worker.poll()}"
