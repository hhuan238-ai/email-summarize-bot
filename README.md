# Email Summarize Bot

Daily Gmail summary bot that collects the previous day's received emails, asks OpenAI to summarize them in Traditional Chinese, and sends one digest email back to you.

## What It Does

- Wakes up hourly in GitHub Actions.
- Sends once between 6:00 AM and noon in the configured local timezone.
- Searches Gmail for messages received during the previous local calendar day.
- Excludes sent mail, drafts, spam, and trash.
- Reads sender, recipients, subject, timestamp, snippet, body text, links, and attachment names.
- Produces a Traditional Chinese digest with overview, action items, grouped outline, per-email summaries, links, and attachments.
- Checks sent mail first so delayed schedule runs do not create duplicate digests.

## Required Secrets

Add these repository secrets in GitHub. Without them, the workflow cannot read Gmail, call OpenAI, or send email.

| Secret | Description |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API key used to generate the summary. |
| `GMAIL_CLIENT_ID` | OAuth client ID from Google Cloud. |
| `GMAIL_CLIENT_SECRET` | OAuth client secret from Google Cloud. |
| `GMAIL_REFRESH_TOKEN` | OAuth refresh token with Gmail read/send scopes. |
| `GMAIL_USER_EMAIL` | Gmail account to read from and send as, for example `hhuan238@ucr.edu`. |
| `SUMMARY_RECIPIENT_EMAIL` | Recipient for the digest, for example `hhuan238@ucr.edu`. |

Optional repository variables:

| Variable | Default |
| --- | --- |
| `TIMEZONE` | `America/Los_Angeles` |
| `SUMMARY_MODEL` | `gpt-4.1-mini` |
| `MAX_EMAILS` | `500` |
| `RUN_AFTER_HOUR_LOCAL` | `6` |
| `RUN_BEFORE_HOUR_LOCAL` | `12` |

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

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python email_summarize_bot.py
```

## Schedule

GitHub Actions uses UTC cron, so the workflow wakes up hourly:

```text
0 * * * *
```

The script checks `TIMEZONE` and only sends inside the local retry window, defaulting to 6:00 AM through noon. It also checks sent mail for the same digest subject before sending, so delayed GitHub schedule runs do not create duplicates.
