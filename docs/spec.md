# Telegram OCEAN Bot Specification

## Purpose

Provide English-language Mini-IPIP (20 item), IPIP Big-Five Factor Markers
(50 item), and IPIP-NEO-120 (120 item) self-reflection assessments in Telegram
for users aged 13 or older.
The bot is informational and non-clinical.

## Test seams

The agreed test interfaces are:

1. Questionnaire loading and scoring through the scoring interface.
2. Answer order, transitions, completion, scoring, and expiry through the
   Assessment interface.
3. Consent, start, resume, answer, back, cancel, history, and results through
   the Assessment interaction interface.
4. Complete export and atomic deletion through the User data rights interface.
5. Retention policy and retry health through the Data retention interface.
6. Runtime modes, readiness, and owned resource cleanup through the runtime
   interface.
7. Callback formats and Telegram delivery through the Telegram adapters.
8. Local durability, revisioned synchronization, idempotent retry, and queue
   draining through the replication service interface.

Tests use published scoring keys and fixed worked examples. They do not assert
private helper calls or implementation details behind these interfaces.

## Product behavior

- Quick mode uses the public-domain 20-item Mini-IPIP.
- Detailed mode uses the public-domain 50-item IPIP Big-Five Factor Markers.
- Facet mode uses the public-domain 120-item IPIP-NEO-120 and reports six
  facet means within each OCEAN domain.
- Published wording, order, and item direction are preserved.
- Emotional Stability is inverted and presented as Neuroticism.
- Results show raw 1-5 averages. A qualified international online-sample
  percentile may be shown only when a validated benchmark is configured.
- Users can go back, cancel, and resume one active test. Incomplete tests
  expire after 30 days. Completed results are immutable and retained until
  deletion.
- Required commands are `/start`, `/test`, `/results`, `/export`,
  `/delete_me`, `/privacy`, `/help`, and allowlisted `/admin_stats`.
- Consent is required before collection. The bot is restricted to ages 13+.
- Operational analytics are disclosed and required. Raw analytics expire
  after 90 days, error logs after 30 days, and anonymous counters may remain.
- Logs never contain raw questionnaire answers, profiles, bot tokens, or raw
  Telegram updates.
- In authoritative mode, a Telegram callback is acknowledged immediately and
  each answer advances only after SQLite commits it on a persistent Railway
  Volume. Supabase receives idempotent revisioned snapshots asynchronously.
- Active snapshots synchronize after five idle seconds and at least every 30
  seconds; completion synchronizes immediately. Analytics use a durable local
  outbox and idempotent remote event IDs.
- Authoritative mode supports one Railway replica. `/results`, `/export`, and
  `/delete_me` cover both local and synchronized records.

## Explicit exclusions

Payments, advertisements, AI-generated interpretations, translations,
reminders, leaderboards, social profiles, custom share images, diagnoses,
career or relationship recommendations, and parental-consent workflows are
outside the MVP.
