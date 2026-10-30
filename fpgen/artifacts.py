"""Filesystem layout for job artifacts.

Published files live in ``<data_dir>/artifacts/<job_id>/``. A job is rendered
into ``<data_dir>/artifacts/.staging/<job_id>/`` and that directory is renamed
into place only after every file exists. Names are fixed; callers never join
a client-supplied path onto the artifact root.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from fpgen.settings import Settings

LAYOUT_FILENAME = "layout.json"
PNG_FILENAME = "plan.png"
SVG_FILENAME = "plan.svg"
ARTIFACT_FILES = {
    "json": LAYOUT_FILENAME,
    "png": PNG_FILENAME,
    "svg": SVG_FILENAME,
}


def artifacts_root(settings: Settings) -> Path:
    return settings.data_dir / "artifacts"


def published_dir(settings: Settings, job_id: uuid.UUID) -> Path:
    return _job_dir(artifacts_root(settings), job_id)


def staging_dir(settings: Settings, job_id: uuid.UUID) -> Path:
    return _job_dir(artifacts_root(settings) / ".staging", job_id)


def reset_staging(settings: Settings, job_id: uuid.UUID) -> Path:
    """Create an empty staging directory for one attempt."""
    path = staging_dir(settings, job_id)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def remove_job_artifacts(settings: Settings, job_id: uuid.UUID) -> None:
    """Delete the published directory and any staging directory for one job."""
    for path in (published_dir(settings, job_id), staging_dir(settings, job_id)):
        if path.exists():
            shutil.rmtree(path)


def publish_staging(settings: Settings, job_id: uuid.UUID) -> Path:
    """Rename the staging directory to the published directory.

    ``os.rename`` is atomic when both directories are on the same filesystem,
    which they are: both live under the artifact root. The destination is
    removed first when a previous attempt left one behind.
    """
    staging = staging_dir(settings, job_id)
    destination = published_dir(settings, job_id)
    if not staging.is_dir():
        raise FileNotFoundError(f"Staging directory is missing for job {job_id}.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    os.rename(staging, destination)
    return destination


def remove_unreferenced_artifacts(settings: Settings, live_ids: set[uuid.UUID]) -> None:
    """Delete artifact directories whose job id is no longer stored."""
    live = {str(job_id) for job_id in live_ids}
    root = artifacts_root(settings)
    if not root.exists():
        return
    for directory in (root / ".staging", root):
        if not directory.exists():
            continue
        for child in directory.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_dir() and child.name not in live:
                shutil.rmtree(child, ignore_errors=True)


def _job_dir(root: Path, job_id: uuid.UUID) -> Path:
    name = str(job_id)
    if Path(name).name != name:
        raise ValueError(f"Job id {job_id} is not a single path segment.")
    return root / name
