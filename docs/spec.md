# Telegram OCEAN Bot Specification

## Purpose

Provide English-language Mini-IPIP (20 item) and IPIP Big-Five Factor Markers
(50 item) self-reflection assessments in Telegram for users aged 13 or older.
The bot is informational and non-clinical.

## Test seams

The agreed public test boundaries are:

1. Questionnaire loading and scoring through the scoring API.
2. Start, answer, back, cancel, resume, complete, expire, export, and delete
   behavior through the assessment service API.
3. Durable user and assessment behavior through repository interfaces.
4. Commands, callbacks, stale/duplicate input, and authorization through the
   Telegram update handlers.

Tests use published scoring keys and fixed worked examples. They do not assert
private helper calls or database implementation details.

## Product behavior

- Quick mode uses the public-domain 20-item Mini-IPIP.
- Detailed mode uses the public-domain 50-item IPIP Big-Five Factor Markers.
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

## Explicit exclusions

Payments, advertisements, AI-generated interpretations, translations,
reminders, leaderboards, social profiles, custom share images, diagnoses,
career or relationship recommendations, and parental-consent workflows are
outside the MVP.
