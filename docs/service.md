# Floor Plan HTTP service

Run the generator as a local API and one worker. This is for one person, on one Ubuntu computer, with plans stored on that computer. Python 3.12 is the target. Docker is not required to run the service. SQLite is the database unless you switch it later.

The command-line tool (`python floorplan.py ...`) is unchanged and does not need any of this.

## Install

```bash
sudo apt update && sudo apt install -y python3-venv python3.12-venv
cd ~/floorplan-gen
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-api.txt
```

Install the optional database driver only when you are switching off SQLite:

```bash
pip install -r requirements-mysql.txt
```

## Configure

The process reads environment variables. It does not load `.env` by itself.

```bash
cp .env.example .env
# Edit .env. Replace FLOORPLAN_API_KEY with a long random string.
# Leave DATABASE_URL unset to use SQLite, or set an absolute sqlite URL.
set -a && source .env && set +a
```

| Variable | Role |
|---|---|
| `FLOORPLAN_API_KEY` | Bearer token for every route except `GET /health`. Required to start the API. The worker and migrations do not need it. |
| `FLOORPLAN_DATA_DIR` | Runtime directory. Default: `<repository>/.runtime`. Artifacts are stored under `artifacts/` inside it. |
| `DATABASE_URL` | SQLAlchemy URL. When unset, the database is `<data dir>/floorplan.sqlite`. |

Generate a key with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Create the tables

From the repository root, with the virtualenv active and `DATABASE_URL` pointing at an empty database (or left unset for the default SQLite file):

```bash
alembic upgrade head
```

Running that again is safe. It does not delete jobs. Starting the API or the worker does not create tables.

## Start the API and the worker

Use two terminals. In both, `source .venv/bin/activate`, `cd` to the repository, and export the same environment (`set -a && source .env && set +a`).

```bash
uvicorn fpgen.api:app --host 127.0.0.1 --port 8000 --workers 1
```

```bash
python -m fpgen.worker
```

Leave both running. This version is one API process and one worker. Multiple worker processes are outside this version. The worker claims one job at a time. Ctrl-C stops it from claiming another job and lets the current job finish. If the worker is killed in the middle of a job, the next start puts that job back on the queue under the same id, or marks it failed after it has been recovered more than 3 times.

Succeeded and failed jobs, and their files, are kept for 24 hours. Queued and running jobs are not expired. The worker removes expired jobs at startup and about once a minute.

## Call the API

Base URL on this computer: `http://127.0.0.1:8000`.

`GET /health` has no authentication and returns `{"status":"ok"}`.

Every other route requires:

```http
Authorization: Bearer <FLOORPLAN_API_KEY>
```

Send `Content-Type: application/json` on POST requests. A body larger than 1 MiB is rejected. Numbers must be finite. DPI must be an integer from 72 to 300. A drawing that would exceed 25 million pixels is rejected.

| Request | Success | Notes |
|---|---|---|
| `POST /v1/jobs/generate` | 202 | Queues a plan. Does not draw it inline. |
| `POST /v1/jobs/render` | 202 | Queues a drawing of a layout you supply. |
| `GET /v1/jobs/{id}` | 200 | Status of that job. |
| `POST /v1/layouts/validate` | 200 | Validation report, including when the layout is invalid (`ok: false`). |
| `GET /v1/jobs/{id}/files/{format}` | 200 | `format` is `png`, `svg`, or `json`. |
| `GET /health` | 200 | No API key. |

Generation accepts `beds`, `baths`, `sqft`, `seed`, `laundry`, `garage` (0–3), `shape` (`rect` or `L`), `garage_side` (`left` or `right`), and optional `title` and `dpi`. Omitted fields use the same defaults as the command line (`beds` 3, `baths` 2, `sqft` 1600, `seed` 1, `garage` 0, `shape` rect, `dpi` 170). `sqft` is living area. A program the generator already knows is impossible (for example too few square feet for the room count) returns 422 and is not queued.

Rendering accepts `layout`, and optional `title` and `dpi`. The layout is validated before it is queued. Validation errors return 422 with those errors, and nothing is queued. A layout that is only structurally valid but fails the rules still returns 200 from `POST /v1/layouts/validate` with `ok: false`.

A generate request that passes the size checks can still fail later. If the worker's layout fails validation, the job fails and the error message contains the validation errors.

Submission returns 202, a `Location` header, and:

```json
{
  "id": "generated-uuid",
  "status": "queued",
  "status_url": "/v1/jobs/generated-uuid",
  "poll_after_seconds": 2
}
```

Poll `status_url` every two seconds until `status` is `succeeded` or `failed`. Polling does not create another job. There is no cancel, no priority, and no webhook.

- `queued` and `running` are not finished yet.
- `succeeded` includes `layout`, `validation` (`ok`, `errors`, `warnings`, `areas`), `expires_at`, and relative `files` links (`/v1/jobs/{id}/files/png`, `svg`, and `json`). Those links work on localhost and through a tunnel because they have no host in them.
- `failed` includes `error.code` and `error.message`.

Other responses:

| Status | When |
|---|---|
| 401 | Missing or wrong bearer token. |
| 422 | Malformed JSON, a value outside the rules above, an impossible generate spec, or a render layout with validation errors. |
| 429 | 100 jobs are already queued. `Retry-After: 30`. Running jobs do not count toward the 100. |
| 404 | Unknown or expired job id, or a file that is no longer there. |
| 409 | Download requested before the job has succeeded. |

```bash
curl -sS -D - \
  -H "Authorization: Bearer $FLOORPLAN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"beds":3,"baths":2,"sqft":1600,"seed":1}' \
  http://127.0.0.1:8000/v1/jobs/generate

curl -sS -H "Authorization: Bearer $FLOORPLAN_API_KEY" \
  http://127.0.0.1:8000/v1/jobs/$JOB_ID

curl -sS -L -H "Authorization: Bearer $FLOORPLAN_API_KEY" \
  -o plan.png \
  "http://127.0.0.1:8000/v1/jobs/$JOB_ID/files/png"
```

Use the same pattern with `/files/svg` and `/files/json`. Downloads send `Cache-Control: private, no-store`. Authenticated JSON responses do too.

## Postman

Import `docs/floor-plan-api.postman_collection.json`. Set two collection variables:

- `base_url` — `http://127.0.0.1:8000`, or the public origin if you are using a tunnel
- `api_key` — the same value as `FLOORPLAN_API_KEY`

`job_id` starts empty. The Generate request stores the returned id in that variable. Render, Poll, and the three downloads then use it. Poll every two seconds until the status is terminal. Health does not send the API key. The other requests send `Authorization: Bearer {{api_key}}`.

The collection is enough to generate, render, validate, poll, and download. You do not need to edit request bodies except to change the plan you want.

## Cloudflare Tunnel

The API listens on `127.0.0.1:8000`. Point the tunnel at that origin. A quick check on a machine that already has `cloudflared`:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

For a named tunnel, the ingress service URL is `http://127.0.0.1:8000`. The API process, the worker, this computer, and the tunnel all have to stay running for a remote request to finish. The relative file links in a successful poll response keep working through the tunnel.

No tunnel configuration is stored in this repository, so that path is not part of the automated tests. Confirm a real hostname when you have one: submit a job, poll every two seconds, and download the PNG, SVG, and JSON through the hostname.

### Open question: Cloudflare Access

Whether to put Cloudflare Access in front of the tunnel, as a second login in addition to the bearer API key, is undecided. John is looking into it. Until that choice is made, the bearer API key is the only authentication layer, and `GET /health` stays open. If Access is added later, update this tunnel section and the Postman collection's auth. No application code change is expected.

## Switch from SQLite to MySQL or MariaDB

Switching starts from an empty database. Old job ids and history do not move.

1. Stop submitting new work and let the queue drain (no `queued` or `running` jobs left).
2. Stop the API and the worker.
3. `pip install -r requirements-mysql.txt`
4. Change `DATABASE_URL` to the new database, for example `mysql+pymysql://floorplan:floorplan@127.0.0.1:3306/floorplan`.
5. Create that database empty (`utf8mb4`). Do not copy the SQLite file into it.
6. `alembic upgrade head` against that empty database.
7. Start the API and the worker again.

Keep the old database file and the old artifact directory until the 24-hour retention period of the last jobs has passed, then delete them yourself. Nothing deletes the previous database automatically. New submissions get new ids. Links and ids from before the switch return 404.

The same steps apply in either direction. The service code does not need to change, and it still does not need Docker. Docker is used only by the test suite (`tests/docker-compose.databases.yml`) to check MySQL 8.4 and MariaDB 11.4.

## What this version does not do

One user, one API process, one worker, files on local disk. Each submission creates a new job. There is no cancellation, no priorities, no webhooks, no migration of job history, and no automatic retry of an ordinary failed job. A job that fails validation stays failed.
