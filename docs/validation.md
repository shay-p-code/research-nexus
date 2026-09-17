# Validation status

The repository is an initial implementation, not an already-running VPS service.

## Automated checks

Run `pytest -q` after installing the development dependencies. The suite runs offline with a fake model provider and a real temporary SQLite database. It exercises:

- End-to-end research → librarian → next round → cited database answer.
- Existing research passed into later rounds, duplicate source/note merging and no-progress termination.
- Exact duplicate reuse, idempotent submissions and racing job starts/claims.
- Round budgets, cancellation during a provider call and stale-worker fencing.
- Librarian-only retries from saved reports, sanitized failures and expired inspections.
- Citation rejection, database abstention and separation of worker/operator permissions.
- Real MCP HTTP transport, OAuth metadata, login, PKCE, token refresh, replay rejection and callback allowlisting.

## Live checks still required

This development environment does not provide Docker or a VPS session. The Compose stack, PostgreSQL/pgvector execution, n8n import/activation, real OpenAI responses and actual ChatGPT account linking must be verified during deployment. No real model calls were made during the offline tests.

The first live research task should be narrow and limited to one round. Check the original sources, resulting notes, follow-up query and deduplication behavior before raising budgets.
