# Email Summarize Bot

Daily Gmail summary bot that collects the previous day's received emails, asks Gemini to summarize them in Traditional Chinese, and sends one digest email back to you.

## What It Does

- Runs in GitHub Actions, so your computer does not need to be on.
- Starts at 06:00 local time, retries transient Gemini failures, and sends one digest per target date.
- Searches Gmail for messages received during the previous local calendar day.
- Excludes sent mail, drafts, spam, and trash.
- Reads sender, recipients, subject, timestamp, snippet, body text, links, and attachment names.
- Produces a Traditional Chinese digest with overview, action items, grouped outline, per-email summaries, links, and attachments.
- Defers a non-AI fallback until noon if Gemini is still unavailable; later scheduled runs can still deliver.
- Serializes sender workflows and checks exact subject/recipient in sent mail before sending.
- Reconciles uncertain Gmail send results instead of retrying the send request.
- Reports full, fallback, empty, pending, and unknown delivery outcomes separately.
- Includes one hourly watchdog that alerts you if the daily digest is missing after the retry window.

## Required Secrets

Add these repository secrets in GitHub. Without them, the cloud workflow cannot read Gmail, call Gemini, or send email.

| Secret | Description |
| --- | --- |
| `GEMINI_API_KEY` | Gemini API key used to generate the summary. |
| `GMAIL_CLIENT_ID` | OAuth client ID from Google Cloud. |
| `GMAIL_CLIENT_SECRET` | OAuth client secret from Google Cloud. |
| `GMAIL_REFRESH_TOKEN` | OAuth refresh token with Gmail read/send scopes. |
| `GMAIL_USER_EMAIL` | Gmail account to read from and send as, for example `hhuan238@ucr.edu`. |
| `SUMMARY_RECIPIENT_EMAIL` | Recipient for the digest, for example `hhuan238@ucr.edu`. |

Never commit these values to the repository.

## Cloud Setup

Use this setup for a bot that keeps running when your computer is off.

1. Create a Gemini API key in Google AI Studio and save it as the repository secret `GEMINI_API_KEY`.
2. In Google Cloud, enable the Gmail API.
3. Create an OAuth client for a Desktop app.
4. Add your Gmail account as a test user if the OAuth app is still in testing mode.
5. Copy the OAuth client values into your local shell.

PowerShell:

```powershell
$env:GMAIL_CLIENT_ID="your-client-id"
$env:GMAIL_CLIENT_SECRET="your-client-secret"
```

Bash:

```bash
export GMAIL_CLIENT_ID="your-client-id"
export GMAIL_CLIENT_SECRET="your-client-secret"
```

6. Install dependencies and generate the Gmail refresh token:

```bash
pip install -r requirements.txt
python scripts/get_gmail_refresh_token.py
```

7. Open the printed Google authorization URL, sign in with the Gmail account you want summarized, and approve Gmail read/send access.
8. Add the printed values to GitHub repository secrets: `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, and `GMAIL_REFRESH_TOKEN`.
9. Add `GMAIL_USER_EMAIL` and `SUMMARY_RECIPIENT_EMAIL` as repository secrets.
10. Go to the Actions tab, choose "Daily Email Summary", and run it once with "Run workflow". Leave `dry_run=true` to verify generation without sending. Set `dry_run=false` when ready to send.

Manual inputs:

| Input | Behavior |
| --- | --- |
| `target_date` | Optional completed local date in `YYYY-MM-DD`; blank means yesterday. Today/future dates are rejected. |
| `dry_run` | Defaults to true for manual runs. Reads mail and generates a summary, but never sends. Logs only status/counts, not email bodies. |
| `force_resend` | Defaults to false. Explicit manual override for a deliberate replacement; can create a second email. Ignored for scheduled runs. |

To recover an older date, first run its explicit `target_date` with `dry_run=true`. Only after checking the outcome, use `dry_run=false` and, if a digest already exists, `force_resend=true`. A preview that reports `retry_pending` or `kind=fallback` is not a successful full summary.

## Optional Variables

| Variable | Default |
| --- | --- |
| `TIMEZONE` | `America/Los_Angeles` |
| `SUMMARY_MODEL` | `gemini-2.5-flash-lite` |
| `SUMMARY_ATTEMPTS` | `4` total attempts, maximum `6` |
| `MAX_EMAILS` | `500` |
| `RUN_AFTER_HOUR_LOCAL` | `6` |
| `RUN_BEFORE_HOUR_LOCAL` | `24` |
| `FALLBACK_AFTER_HOUR_LOCAL` | `12` |
| `WATCHDOG_AFTER_HOUR_LOCAL` | `13` |
| `WATCHDOG_BEFORE_HOUR_LOCAL` | `24` |

Remove or update any existing `RUN_BEFORE_HOUR_LOCAL=12` override: the delivery window must extend past the fallback cutoff. Both workflows use `TIMEZONE`, independently of your computer's timezone. The original date-formatted Gmail query is preserved.

## Gmail OAuth Scopes

The refresh token needs these scopes:

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
```

## Run Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python email_summarize_bot.py
```

The example environment defaults to `DRY_RUN=true`. Set it to `false` to send; local runs use the same date, delivery-window, and duplicate checks.

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python email_summarize_bot.py
```

## Schedule

GitHub Actions uses UTC cron. The daily workflow wakes up once per hour:

```text
7 * * * *
```

Scheduled runs proceed from 06:00 through 23:59 local time. Before noon, exhausted transient retries leave the date pending and send nothing. On a run at or after noon, the bot makes a final bounded generation attempt and sends a fallback if it still fails. No second full digest is automatically sent after a fallback. Empty inboxes bypass Gemini and send the no-mail notice.

GitHub schedule events can be delayed or dropped; noon is a fallback threshold, not an exact delivery guarantee. Runs after the original morning window can still deliver. Manual runs bypass the window but retain the target date's fallback cutoff unless the AI summary succeeds.

Gemini retries are managed at one application layer; SDK retries are explicitly disabled. Transient HTTP errors, transport failures and empty responses receive up to four attempts with exponential backoff (about 10, 20, 40 seconds plus jitter). Numeric provider retry delays are respected when they fit the six-minute retry budget. Individual request timeouts are at most 60 seconds; the workflow retains its 20-minute limit. Daily quota and invalid model/authentication errors are not retried inside the run. Configuration errors remain visible as failed jobs; unexpected programming errors are not converted into fallback messages.

The SDK version is pinned so retry behavior is reproducible. No alternate paid model is enabled automatically.

### Delivery outcomes

| Outcome | Meaning |
| --- | --- |
| `full_sent` | AI summary delivered. |
| `empty_sent` | No matching mail; notice delivered. |
| `fallback_sent` | Non-AI fallback delivered; summary quality is degraded. |
| `legacy_sent` | Older digest exists without a reliable quality marker. |
| `retry_pending` | Temporary generation failure; a later scheduled run may retry. |
| `configuration_error` | Model/authentication/quota settings need attention. |
| `send_unknown` | Gmail may have accepted the message; inspect sent mail before forcing another send. |
| `dry_run` | Generated/reported a result without sending mail. |

New messages carry `X-Digest-Kind` and `X-Digest-Date` headers. Both sender workflows share the `email-digest-delivery` concurrency group with `cancel-in-progress=false`. Gmail sends use a stable Message-ID for ordinary delivery, no automatic POST retries, and sent-mail reconciliation after ambiguous errors. This reduces duplicates but cannot provide transactional exactly-once guarantees across Gmail acceptance and a runner crash. Run local senders separately from cloud jobs; GitHub concurrency does not lock a local process.

## Watchdogs

The `Email Summary Watchdog` workflow wakes up once per hour. By default, it checks Gmail from 13:00 through 23:59 local time. It checks exact subject and recipient, and reports fallback delivery as degraded instead of complete AI success:

```text
昨日郵件摘要 - YYYY/MM/DD
```

If no digest exists, the watchdog sends one alert email:

```text
Email Summarize Bot 警告 - 未找到摘要 - YYYY/MM/DD
```

The alert has duplicate protection. Finding a fallback logs the degraded status without sending another warning or digest.

## Troubleshooting

- If the manual workflow succeeds with `Digest already sent ... skipping duplicate`, the secrets and Gmail connection are working; the bot skipped because that date's digest already exists in sent mail.
- If GitHub logs show empty environment values, the repository secrets are missing or were added under the wrong repository.
- `503` / high demand means Gemini is temporarily unavailable, not evidence of an invalid Gmail password or exhausted quota. The bot retries, then leaves the date pending before noon or sends a fallback after noon.
- `429` can mean short-term throttling or a daily quota limit. Daily quota errors need a quota reset or configuration change; invalid model/key/permission errors need their specific settings corrected.
- If Google OAuth shows a localhost server error while generating the refresh token, rerun `python scripts/get_gmail_refresh_token.py` and use the newly printed URL.
- If the bot does not send exactly at midnight, this is normal for GitHub scheduled workflows. It will send once when GitHub wakes it and no matching digest has been sent yet.
- If you receive `Email Summarize Bot 警告 - 未找到摘要`, open the Actions tab and inspect the `Daily Email Summary` workflow logs for that morning.

## Verification

Run `python -m unittest discover -s tests -v`. Tests mock Gemini/Gmail and never send mail. They cover overload recovery, exhausted retries, permanent errors, empty inboxes, delayed runs, explicit target dates, previews, exact-match/paginated deduplication, send-timeout reconciliation, and watchdog classification. The `Digest Tests` workflow runs without secrets on pushes and pull requests.
