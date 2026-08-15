# Operations

## Local setup

1. Install `uv`, Docker, and Python 3.12.
2. Create the bot in BotFather and copy `.env.example` to `.env` with real secrets.
3. Leave webhook variables unset for long polling.
4. Run `docker compose up --build`.

Compose runs a one-shot `migrate` service before starting the bot. The application image itself never migrates on startup. The webhook deployment exposes database-backed readiness at `GET /health`, liveness at `GET /live`, and listens on `OCEAN_PORT`.

## Production deployment

Use a Singapore region where the chosen hosting and managed PostgreSQL providers offer one. Set an HTTPS `OCEAN_WEBHOOK_BASE_URL` and an independent random `OCEAN_WEBHOOK_SECRET`; Telegram requests are rejected unless the `X-Telegram-Bot-Api-Secret-Token` matches. Restrict database ingress to the application, require TLS, use a dedicated least-privilege database role, and store all secrets in the platform secret manager.

Run exactly one migration job per release before replacing application instances. For a serverless service, set `OCEAN_RETENTION_MODE=external` and `OCEAN_WEBHOOK_SETUP_MODE=external`. Schedule exactly one daily `python -m ocean_bot retention` job with overlapping executions disabled. After the service is reachable, run `python -m ocean_bot set-webhook` as an explicit release step and verify Telegram's webhook status. The retention command emits only aggregate row counts.

For an always-on VPS deployment, the default `background` retention and `startup` webhook setup modes remain available. Do not run a scheduled retention command at the same time as the background loop.

Do not log environment variables or Telegram updates. Configure the platform to retain sanitized application error logs for no more than 30 days. Use `/live` only for process liveness and `/health` for readiness and external monitoring. Alert on failed readiness checks, repeated restarts, database exhaustion, failed scheduled jobs, and webhook delivery errors.

## Backup and restore

Take encrypted daily PostgreSQL backups with a 30-day rolling retention policy. At least monthly, restore the latest backup into an isolated database, run `alembic upgrade head`, verify user/assessment row counts, and execute a read-only export smoke test. Record the restore date, duration, and operator. Never use production bot credentials against a restored database.

## Shutdown and incidents

On shutdown, stop accepting new requests, allow in-flight handlers to finish, cancel the retention task, close Telegram sessions, and dispose database connections. Rotate the bot token, webhook secret, analytics pepper, and database credentials after suspected disclosure. Rotating the analytics pepper intentionally breaks linkage to older raw events; purge those events if linkage is no longer operationally useful.

Before public launch, complete an owner pilot and a 5-10 person pilot. Verify authoritative item wording and scoring, consent/export/deletion, restart/resume, stale callback handling, backups and restore, retention jobs, and the privacy notice's provider names and deployment region.
