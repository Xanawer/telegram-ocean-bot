# Telegram OCEAN Bot

A privacy-conscious Telegram bot for the public-domain 20-item Mini-IPIP and 50-item IPIP Big-Five Factor Markers. It provides English-language personality self-reflection for users aged 13 or older. Results are not clinical advice or suitable for employment decisions.

## Features

- Exact, checksummed IPIP item text and scoring keys
- Raw 1-5 averages for Openness, Conscientiousness, Extraversion, Agreeableness, and Neuroticism
- Consent gate, one resumable active assessment, Back and Cancel controls, and 30-day expiry
- Persistent results, JSON export, individual result deletion, and complete account deletion
- Pseudonymous mandatory operational analytics with a 90-day raw retention limit
- Allowlisted aggregate `/admin_stats`
- Local long polling and authenticated production webhooks with `/health`

Population percentiles are intentionally unavailable until a compatible, documented benchmark dataset is installed. The bot never labels results as Singapore norms.

## Instruments

The Mini-IPIP wording and keys come from <https://ipip.ori.org/MiniIPIPKey.htm>. The 50-item form comes from <https://ipip.ori.org/new_ipip-50-item-scale.htm> and <https://ipip.ori.org/newBigFive5broadKey.htm>. IPIP items are public domain. The source scale's Emotional Stability factor is inverted item by item and reported as Neuroticism; fixture tests lock this behavior.

## Development

Requires Python 3.12 and `uv`.

```bash
uv sync
cp .env.example .env
OCEAN_DATABASE_URL=sqlite+aiosqlite:///local.db uv run alembic upgrade head
uv run pytest -q
uv run mypy src/ocean_bot
uv run ruff check src tests migrations
uv run python -m ocean_bot
```

Leave `OCEAN_WEBHOOK_BASE_URL` unset for long polling. For PostgreSQL development and the complete application stack, fill in `.env` and run:

```bash
docker compose up --build
```

## Operations and privacy

See [docs/operations.md](docs/operations.md) for deployment, backup/restore, shutdown, and launch gates. See [docs/privacy.md](docs/privacy.md) for data categories, purposes, retention, access, and deletion. The full product contract and exclusions are in [docs/spec.md](docs/spec.md).

This is a free personal project. It has no payments, ads, AI interpretation, diagnosis, reminders, leaderboards, or social profiles.
