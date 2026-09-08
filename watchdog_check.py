from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from email_summarize_bot import gmail_service, previous_day_bounds, required_env, send_email, sent_digest_exists
from digest_delivery import find_sent_digest, report_outcome


def should_check_now(tz_name: str) -> bool:
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True

    start_hour = int(os.getenv("WATCHDOG_AFTER_HOUR_LOCAL", "13"))
    end_hour = int(os.getenv("WATCHDOG_BEFORE_HOUR_LOCAL", "24"))
    now = datetime.now(ZoneInfo(tz_name))
    print(f"Current local time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} ({tz_name}).")
    if not (start_hour <= now.hour < end_hour):
        print(f"Skipping watchdog at local hour {now.hour}; configured window is {start_hour}:00-{end_hour}:00.")
        return False
    return True


def build_alert_body(target_date: str, digest_subject: str, workflow_name: str, run_id: str, tz_name: str) -> str:
    now = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
    run_url = f"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/{run_id}" if run_id else ""
    return f"""Email Summarize Bot watchdog alert

Target digest date: {target_date}
Expected digest subject: {digest_subject}
Checked at: {now}
Watchdog workflow: {workflow_name or "unknown"}
GitHub run: {run_url or "unknown"}

The watchdog could not find the expected daily digest in sent mail after the retry window.
Please open GitHub Actions and inspect the Daily Email Summary workflow logs.
"""


def main() -> None:
    load_dotenv()

    tz_name = os.getenv("TIMEZONE", "America/Los_Angeles")
    if not should_check_now(tz_name):
        return

    sender = required_env("GMAIL_USER_EMAIL")
    recipient = required_env("SUMMARY_RECIPIENT_EMAIL")
    _, _, target_date = previous_day_bounds(tz_name)
    digest_subject = f"昨日郵件摘要 - {target_date}"
    alert_subject = f"Email Summarize Bot 警告 - 未找到摘要 - {target_date}"
    print(f"Watchdog target digest date: {target_date}. Expected subject: {digest_subject}")

    service = gmail_service()
    existing = find_sent_digest(service, recipient, digest_subject)
    if existing:
        detail = f"{target_date}: Gmail message ID {existing.message_id}."
        if existing.kind == "fallback":
            detail += " Fallback delivered; AI summary is degraded, not complete."
        elif existing.kind == "legacy":
            detail += " Legacy digest delivered; AI summary quality is unknown."
        report_outcome(f"{existing.kind}_sent", detail)
        return

    if sent_digest_exists(service, recipient, alert_subject):
        print(f"Alert already sent for {target_date}; skipping duplicate alert.")
        return

    body = build_alert_body(
        target_date=target_date,
        digest_subject=digest_subject,
        workflow_name=os.getenv("GITHUB_WORKFLOW", ""),
        run_id=os.getenv("GITHUB_RUN_ID", ""),
        tz_name=tz_name,
    )
    send_email(service, sender, recipient, alert_subject, body)
    report_outcome("digest_missing", f"{target_date}: missing-digest alert delivered.")


if __name__ == "__main__":
    main()
