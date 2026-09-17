# VPS setup — one step at a time

You need a Linux VPS with Docker Engine and the Compose plugin, a domain pointing to it, and an OpenAI API key with access to the configured models. The repository does not create a VPS or spend on model calls by itself.

## 1. Put the repository on the VPS

```bash
git clone https://github.com/shay-p-code/research-nexus.git
cd research-nexus
python3 scripts/init_env.py
```

The last command creates a private `.env` with distinct random secrets and refuses to overwrite an existing file. Keep this file and your backups private. It must not be committed.

## 2. Set the domain and API key

Edit `.env` locally on the VPS:

```dotenv
DOMAIN=research.your-domain.example
PUBLIC_URL=https://research.your-domain.example
OPENAI_API_KEY=your-own-key
```

Keep the generated PostgreSQL URL/password and other secrets. Set model IDs to models your OpenAI project can use. Defaults are `gpt-5-mini` for research/librarian and `text-embedding-3-small` for retrieval (1536 dimensions). Changing the embedding model after saving notes requires a re-embedding migration; do not simply mix different models in one library.

Your existing Caddy handles public HTTPS and certificates. The database has no public port. Nexus binds to `127.0.0.1:18473` and n8n to `127.0.0.1:18474`. These high ports reduce the chance of colliding with other apps; they are not a security boundary. Loopback binding keeps them private. Override them with `NEXUS_API_PORT` and `NEXUS_N8N_PORT` in `.env` if needed. Existing `.env` files use these defaults without editing or regenerating secrets.

## 3. Start the services

```bash
docker compose up -d --build postgres api n8n
docker compose ps
curl --fail http://127.0.0.1:18473/healthz
```

Expect `{"status":"ok"}`. This command starts only PostgreSQL, Nexus and n8n. It leaves the optional bundled Caddy service stopped.

For Caddy running directly on the VPS (or with host networking), add the site block from `deploy/existing-Caddyfile.example` to your existing configuration. Replace the example hostname with your `DOMAIN` and use the configured API host port. It proxies to `127.0.0.1:18473` and denies `/internal` and `/internal/*`. Validate your full Caddy configuration before reloading the existing service. Preserve streaming HTTP and allow at least 600 seconds if your existing proxy has request timeouts. Then check `https://your-research-domain/healthz`.

If your existing Caddy is a container on a bridge network, its `127.0.0.1` refers to that container. Connect Caddy and the Nexus API to a shared Docker network and use the API container's internal port `8000` through a unique network alias instead; the host-loopback example does not apply to that layout. Do not expose n8n through the research domain.

For a fresh VPS with no existing reverse proxy, the bundled Caddy remains available with `docker compose --profile public up -d --build`. Use that only when ports 80/443 are available and point the domain at the VPS. The existing-Caddy deployment above does not use that profile.

The n8n image is fixed at `2.39.6`, the stable release observed when this version was built. Other base images follow their specified major release tracks. For a reproducible production rollout, resolve and pin image digests after testing updates on your VPS. Python dependencies are hash-locked in `requirements.lock`.

## 4. Set up n8n privately

From your own computer, open an SSH tunnel:

```bash
ssh -L 18474:127.0.0.1:18474 your-user@your-vps
```

Open `http://localhost:18474` in your browser and create the n8n owner account. Its password is separate from the Research Nexus owner password. If you changed `NEXUS_N8N_PORT`, use that port on both sides of the SSH tunnel and in the browser URL.

Import `n8n/research-workflow.json` using n8n's workflow import option.

Create a **Header Auth** credential:

- Name: `Nexus worker`
- Header name: `Authorization`
- Header value: `Bearer ` followed by the `WORKER_TOKEN` from `.env`

Select that credential on all three HTTP Request nodes: **Claim next stage**, **Research agent**, and **Librarian**. Save and publish/activate the workflow. Imported workflows start inactive deliberately so credentials can be configured first.

The schedule runs every minute. It processes one research or librarian stage per execution; the next scheduled execution continues saved work. The two agents' prompts/code live in `nexus/providers.py`, keeping model behavior versioned alongside the database code. HTTP retries are off. Failures are retained in the research database for explicit review/retry.

n8n uses its own persistent SQLite database in `n8n_data`; this is separate from the PostgreSQL research database. Execution payload saving is disabled so raw reports and short-lived worker tokens are not copied into execution history. `N8N_SECURE_COOKIE=false` is only for the loopback editor accessed through SSH; if you expose the editor through HTTPS, change its URL/cookie settings accordingly.

## 5. Connect ChatGPT

Use ChatGPT's developer/custom MCP connection flow. Availability and labels depend on the account/workspace. Configure a private app with:

| Setting | Value |
| --- | --- |
| MCP URL | `https://your-research-domain/mcp` |
| Transport | Streamable HTTP |
| Authentication | OAuth |
| Client ID | `OAUTH_CLIENT_ID` from `.env` |
| Client secret | `OAUTH_CLIENT_SECRET` from `.env` |

This server uses a preconfigured private OAuth client, not dynamic public registration. Copy the **exact redirect URI shown by ChatGPT** into `.env` as a JSON array:

```dotenv
OAUTH_REDIRECT_URIS=["https://chatgpt.com/connector/oauth/your-actual-callback-id"]
```

Use the URI ChatGPT actually gives you; the above is only a shape example. Some existing connections use the stable callback configured in `.env.example`. Recreate the API container after editing environment settings:

```bash
docker compose up -d api
```

Complete linking. On the Research Nexus login page, enter `OWNER_PASSWORD` from `.env`. Do not paste these secrets into a chat message. The connection grants access to this one deployment's shared library and research queue. OAuth uses PKCE, expiring tokens, refresh-token rotation and exact callback matching.

This connection is separate from GitHub access. A repository URL alone cannot connect ChatGPT to a running database or n8n instance. No GitHub Actions or GPT Actions are needed.

Official references: [ChatGPT developer mode](https://developers.openai.com/api/docs/guides/developer-mode), [MCP authentication](https://developers.openai.com/plugins/build/auth).

## 6. Run one small research task

Select the Research Nexus connection in ChatGPT. Ask:

> Use Research Nexus. Check what we already know about [one narrow question]. If more research is needed, explain the gap and start one round.

The tools first inspect the library and then queue a job. n8n runs it after the chat ends. Later ask:

> Use Research Nexus to show the status and summary of my latest research job.

For saved knowledge only:

> Answer from the Research Nexus library only: [question]. Cite the saved sources and tell me what is missing.

The first real run is the live integration check: confirm n8n executes, a research report is saved, the librarian stores notes, and the answer tool returns those notes with source links.

## Operating it

- Each job defaults to 3 rounds, with a maximum of 5. Each research response is limited by `MAX_TOOL_CALLS` and `MAX_OUTPUT_TOKENS`. These limit work, not an exact dollar amount; input tokens and tool costs vary. Also set an appropriate budget/limit in your model provider account.
- `completed` plus `answered` means the librarian judged coverage sufficient. Other stopping reasons preserve uncertainty: `round_limit`, `no_new_evidence`, or `no_further_gaps`.
- Cancellation prevents further commits from a running stage but cannot undo an already-started provider charge.
- Inspect failed jobs via the API. `POST /api/jobs/{id}/retry` resumes the saved stage. A failed librarian resumes from the saved report. Retrying an uncertain research request can incur another provider charge.
- An abandoned worker lease expires after 15 minutes and becomes `failed` when n8n next claims work. It is not silently retried.
- Back up PostgreSQL, the `n8n_data` volume, and `.env` (especially the n8n encryption key). Store backups separately from the VPS. Test restoration before relying on them. For PostgreSQL, `docker compose exec -T postgres pg_dump -U nexus -d nexus -Fc > research-backup.dump` exports research and private OAuth data; protect the dump as private.
- Do not use `docker compose down -v` unless you deliberately intend to delete stored data. Schema creation currently supports a fresh installation; future schema changes must include migrations and a backup/restore plan.

Official n8n references: [Docker hosting](https://docs.n8n.io/hosting/installation/docker/), [HTTP Request node](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/), [Schedule Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger/).
