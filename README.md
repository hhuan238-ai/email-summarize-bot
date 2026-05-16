# Email Summarize Bot

Daily Gmail summary bot that collects the previous day's received emails, asks OpenAI to summarize them in Traditional Chinese, and sends one digest email back to you.

## What It Does

- Runs in GitHub Actions, so your computer does not need to be on.
- Wakes up every 5 minutes and sends once between 6:00 AM and noon in the configured local timezone.
- Searches Gmail for messages received during the previous local calendar day.
- Excludes sent mail, drafts, spam, and trash.
- Reads sender, recipients, subject, timestamp, snippet, body text, links, and attachment names.
- Produces a Traditional Chinese digest with overview, action items, grouped outline, per-email summaries, links, and attachments.
- Checks sent mail first so delayed schedule runs do not create duplicate digests.
- Includes 5 independent watchdog workflows that alert you if the daily digest is missing after the retry window.

## Required Secrets

Add these repository secrets in GitHub. Without them, the cloud workflow cannot read Gmail, call OpenAI, or send email.

| Secret | Description |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API key used to generate the summary. |
| `GMAIL_CLIENT_ID` | OAuth client ID from Google Cloud. |
| `GMAIL_CLIENT_SECRET` | OAuth client secret from Google Cloud. |
| `GMAIL_REFRESH_TOKEN` | OAuth refresh token with Gmail read/send scopes. |
| `GMAIL_USER_EMAIL` | Gmail account to read from and send as, for example `hhuan238@ucr.edu`. |
| `SUMMARY_RECIPIENT_EMAIL` | Recipient for the digest, for example `hhuan238@ucr.edu`. |

Never commit these values to the repository.

## Cloud Setup

Use this setup for a bot that keeps running when your computer is off.

1. Create an OpenAI API key and save it as the repository secret `OPENAI_API_KEY`.
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
10. Go to the Actions tab, choose "Daily Email Summary", and run it once with "Run workflow" to verify.

## Optional Variables

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

GitHub Actions uses UTC cron, so the workflow wakes up every 5 minutes:

```text
*/5 * * * *
```

The script checks `TIMEZONE` and only sends inside the local retry window, defaulting to 6:00 AM through noon. It also checks sent mail for the same digest subject before sending, so delayed GitHub schedule runs do not create duplicates.

## Watchdogs

There are 5 independent watchdog workflows:

- `Email Summary Watchdog 1`
- `Email Summary Watchdog 2`
- `Email Summary Watchdog 3`
- `Email Summary Watchdog 4`
- `Email Summary Watchdog 5`

They wake up on staggered 5-minute schedules and only perform checks during the local watchdog window, defaulting to noon through 2:00 PM. Each watchdog checks sent mail for the expected digest subject:

```text
昨日郵件摘要 - YYYY/MM/DD
```

If no digest exists, the watchdog sends one alert email:

```text
Email Summarize Bot 警告 - 未找到摘要 - YYYY/MM/DD
```

The alert also has duplicate protection, so all 5 watchdogs can run without sending 5 warning emails.

## Troubleshooting

- If the manual workflow succeeds with `Digest already sent ... skipping duplicate`, the secrets and Gmail connection are working; the bot skipped because that date's digest already exists in sent mail.
- If GitHub logs show empty environment values, the repository secrets are missing or were added under the wrong repository.
- If Google OAuth shows a localhost server error while generating the refresh token, rerun `python scripts/get_gmail_refresh_token.py` and use the newly printed URL.
- If the bot does not send exactly at 6:00 AM, this is normal for GitHub scheduled workflows. It will send once when GitHub wakes it during the 6:00 AM to noon local retry window.
- If you receive `Email Summarize Bot 警告 - 未找到摘要`, open the Actions tab and inspect the `Daily Email Summary` workflow logs for that morning.
