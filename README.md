# Email Summarize Bot

Daily Gmail summary bot that collects the previous day's received emails, asks OpenAI to summarize them in Traditional Chinese, and sends one digest email back to you.

## What It Does

- Runs every day at 6:00 AM America/Los_Angeles via GitHub Actions.
- Searches Gmail for messages received during the previous local calendar day.
- Excludes sent mail, drafts, spam, and trash.
- Reads sender, recipients, subject, timestamp, snippet, body text, links, and attachment names.
- Produces a Traditional Chinese digest with:
  - Overview
  - Top 3-5 important items
  - Action items
  - Topic/sender groups
  - Per-email concise summaries
  - Important links and attachment names
- Sends the result as one email.

## Required Secrets

Add these repository secrets in GitHub:

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
| `RUN_HOUR_LOCAL` | `6` |

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

The script checks `TIMEZONE` and exits unless the local hour equals `RUN_HOUR_LOCAL`. This keeps the digest at 6:00 AM Pacific across daylight saving time changes.
