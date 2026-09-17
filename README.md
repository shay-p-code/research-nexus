# Research Nexus

Autonomous research that builds on a private, cited knowledge base.

**First version:** API, private ChatGPT MCP connection, n8n workflow and VPS deployment files. Deployment and a live research run still need your VPS, domain and OpenAI API key. No GitHub Actions are used.

## How it works

1. **ChatGPT interprets the request.** It searches existing notes and past jobs, then proposes reusing the evidence, waiting for active research, or investigating specific gaps.
2. **n8n runs the work.** A scheduled workflow claims one durable stage at a time and calls the research agent or librarian.
3. **The researcher gathers evidence.** It uses web search, prioritizes primary sources, and receives existing notes so each round can build on previous work.
4. **The librarian saves readable notes.** It records sources, uncertainty and contradictions, and adds evidence to existing notes when appropriate.
5. **ChatGPT answers later questions from the database.** The answer route has no web-search tool. It returns citations and says when evidence is missing.

The agent code runs in the API service; n8n controls when it runs. PostgreSQL owns the job state and research, so closing ChatGPT does not stop a submitted job.

## Start here

Read [the VPS setup guide](docs/setup.md). It is arranged as small steps.

For local development with Python 3.12:

```bash
python scripts/init_env.py
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install -e '.[dev]'
```

Edit `.env`: use `DATABASE_URL=sqlite:///./nexus.db` for local development and supply `OPENAI_API_KEY`. Then:

```bash
.venv/bin/uvicorn nexus.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

SQLite is a development convenience; the VPS stack uses PostgreSQL with pgvector. Run tests with `.venv/bin/pytest -q`; they use temporary databases and a fake model provider and do not incur API costs.

## What prevents repeated work?

- A mandatory preflight search finds related notes and prior jobs using meaning and keywords.
- Normalized identical questions reuse active or completed jobs. An intentional refresh must be explicit.
- Database constraints prevent racing submissions from creating two active identical jobs.
- Canonical source URLs, normalized note text and librarian review reduce duplicate storage.
- A round without new note/source evidence stops the loop. Each job also has a hard round limit.

Semantic deduplication is approximate. Different phrasing, stale sources or a poor model judgment can still repeat research. Exact repeats and worker retries have stronger database guarantees. A `completed` job means it stopped cleanly; check `stop_reason` and the remaining gaps before claiming the question is answered.

## Boundaries of this version

This is a single-owner system, with one shared research library per deployment. It is not a multi-tenant service. Citations must come from the research provider's returned annotations, but that does not prove each claim is true or that a cited page fully supports it. Source quality and semantic merging still depend on the models.

The saved research reports and source links are retained; full source pages/PDF snapshots are not archived. No email alerts, automatic recurring topics, team roles or GUI dashboard are included. ChatGPT is the user interface.

See [architecture](docs/architecture.md), [API reference](docs/api.md), and [validation status](docs/validation.md).
