# Privacy Notice

This free, English-language Telegram bot is for people aged 13 or older. It provides Big Five self-reflection for entertainment and education, not diagnosis or clinical, career, or relationship advice.

## Data collected

After consent, the service stores the Telegram numeric user ID and optional Telegram language code, consent time, assessment type and version, answers, computed scores, and assessment timestamps. Required operational analytics store a keyed pseudonymous user reference, event name, time, and limited non-answer properties such as questionnaire ID. Sanitized error logs must never contain raw Telegram updates, answers, scores, exports, bot tokens, or webhook secrets.

## Purposes and retention

Data is used to operate and debug the bot and to improve it in aggregate. Assessment history remains until the user deletes an individual result or uses `/delete_me`. Incomplete assessments expire after 30 days. Raw analytics events are deleted after 90 days. Sanitized error logs must be deleted after 30 days. Irreversible aggregate counters may be retained indefinitely.

## Access and deletion

`/export` downloads the user's stored assessment and analytics records. `/results` provides individual result deletion. `/delete_me` deletes the account, answers, results, and linkable analytics records. Aggregate counters that cannot identify or be relinked to a user are not personal data and are not affected.

## Processors and contact

Telegram processes messages under its own terms. Railway runs the application and
temporarily stores active or pending assessment records on its persistent volume.
Supabase stores synchronized assessment history and operational analytics in PostgreSQL.
Exports and deletion cover both stores. The live bot's `/privacy` response identifies the
configured privacy contact. The configured Supabase project region must be confirmed in
this notice before public launch.
