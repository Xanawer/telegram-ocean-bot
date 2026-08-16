# Domain Context

## Assessment

One run of a versioned Questionnaire for one Telegram user. An Assessment is active,
completed, cancelled, or expired. It owns answer order, valid transitions, expiry,
scoring completion, and the view exposed to callers. Completed Assessments are immutable.

## Assessment interaction

One complete user intent involving an Assessment, such as consent, start or resume,
answer, back, cancel, history, or result deletion. The Assessment interaction owns the
clock value, Assessment transition, operational analytics meaning, and transport-neutral
reply outcome. Telegram parsing, callback encoding, delivery, and acknowledgement remain
in the Telegram adapter.

## Questionnaire

A versioned, ordered set of public-domain IPIP items and scoring keys. Quick mode uses the
20-item Mini-IPIP; detailed mode uses the 50-item IPIP Big-Five Factor Markers; facet mode
uses the 120-item IPIP-NEO-120 with six facets per domain.

## User data rights

The user's paired access and deletion guarantee. It owns the complete JSON export,
pseudonymous analytics linkage, atomic deletion of linkable records, and preservation of
anonymous aggregate counters.

## Operational analytics

Required pseudonymous events and anonymous counters used to operate and improve the bot.
An analytics recording failure after an Assessment transition is logged but does not
prevent the user interaction from completing.

## Local replication

The single Railway bot replica commits active assessment state and transition
analytics to SQLite on a persistent Volume. A durable outbox batches complete,
revisioned snapshots and idempotent analytics events to Supabase. SQLite owns
active and pending state; Supabase owns confirmed history. Shadow mode keeps
Supabase authoritative while exercising the local mirror before cutover.

## Data retention

The privacy policy and execution of expiry and purging. Incomplete Assessments expire
after 30 days and raw operational analytics events after 90 days. The same policy runs in
either a background loop or an external one-shot job; runtime mode changes scheduling,
not retention behavior.
