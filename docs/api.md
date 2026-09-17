# API reference

Send `Authorization: Bearer <API_TOKEN>` for `/api/*`. Worker routes require `WORKER_TOKEN` instead. `/mcp` uses OAuth access tokens, not either static API token. All research and question endpoints share the same library.

| Method and path | Purpose |
| --- | --- |
| `GET /healthz` | Database connectivity check; no credentials required |
| `POST /api/inspect` | Search existing notes/jobs and get an inspection ID |
| `POST /api/jobs` | Queue research from an inspection |
| `GET /api/jobs?limit=20&offset=0` | Recent jobs, newest first |
| `GET /api/jobs/{id}` | Status, saved reports, citations and librarian summaries |
| `POST /api/jobs/{id}/cancel` | Cancel queued/running work |
| `POST /api/jobs/{id}/retry` | Operator retry of a failed stage |
| `POST /api/answer` | Database-only answer, note citations and missing evidence |
| `POST /internal/claim` | n8n only: atomically reserve one queued stage |
| `POST /internal/jobs/{id}/research` | n8n only: execute the leased research stage |
| `POST /internal/jobs/{id}/librarian` | n8n only: execute the leased librarian stage |

`POST /api/inspect` and `POST /api/answer` take:

```json
{"question": "What do the existing trials establish about the topic?"}
```

`POST /api/jobs` takes:

```json
{
  "inspection_id": "the-id-returned-by-inspect",
  "focus": "Find independent primary studies covering the unresolved population.",
  "max_rounds": 3,
  "refresh": false
}
```

It returns `{"reused": false, "job": {...}}` or an existing job with `reused=true`. Inspections expire after 30 minutes for new submissions. Replays of an already-used inspection continue to return its job.

Worker claims return `{"available": false}` when idle, or `available`, `job_id`, `stage`, and `lease_token`. Pass the lease token as JSON to the corresponding stage endpoint. Tokens are single-stage credentials and must never be included in public URLs or logs.

`/api/answer` returns `parts` (each with `text` and `note_ids`), `gaps`, cited note objects under `sources`, and `database_only=true`. Render the note's source URLs as clickable links alongside the answer. No retrieved evidence means empty `parts` and an explicit gap.

`stop_reason` describes why execution ended. In particular, `round_limit` is not a claim of complete research. `failed` stages retain their previous saved research; cancellation discards any late in-flight write.

Validation errors use HTTP 422; unknown objects 404; invalid authentication 401; stale leases/invalid state transitions 409; worker/model output failures 502. Failed stages do not automatically retry. Basic OpenAPI metadata is available at `/openapi.json`; it contains no credentials or research content.
