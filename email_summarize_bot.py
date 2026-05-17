from __future__ import annotations

import base64
import html
import os
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


@dataclass(frozen=True)
class EmailRecord:
    sender: str
    to: str
    cc: str
    subject: str
    received_at: str
    snippet: str
    body: str
    links: list[str]
    attachments: list[str]


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def gmail_service() -> Any:
    credentials = Credentials(
        token=None,
        refresh_token=required_env("GMAIL_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required_env("GMAIL_CLIENT_ID"),
        client_secret=required_env("GMAIL_CLIENT_SECRET"),
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=credentials)


def previous_day_bounds(tz_name: str) -> tuple[datetime, datetime, str]:
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    target_day = today - timedelta(days=1)
    start = datetime.combine(target_day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end, target_day.strftime("%Y/%m/%d")


def gmail_query(start: datetime, end: datetime) -> str:
    after = start.strftime("%Y/%m/%d")
    before = end.strftime("%Y/%m/%d")
    return f"after:{after} before:{before} -in:sent -in:drafts -in:spam -in:trash"


def list_message_ids(service: Any, query: str, max_emails: int) -> list[str]:
    message_ids: list[str] = []
    page_token = None

    while len(message_ids) < max_emails:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                labelIds=["INBOX"],
                maxResults=min(100, max_emails - len(message_ids)),
                pageToken=page_token,
            )
            .execute()
        )
        message_ids.extend(item["id"] for item in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return message_ids


def header_value(headers: list[dict[str, str]], name: str) -> str:
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def decode_body(data: str | None) -> str:
    if not data:
        return ""
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8", errors="replace")


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<br\s*/?>", "\n", value)
    value = re.sub(r"(?s)</p\s*>", "\n", value)
    value = re.sub(r"(?s)<.*?>", " ", value)
    value = html.unescape(value)
    return normalize_space(value)


def normalize_space(value: str) -> str:
    value = value.replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def walk_parts(part: dict[str, Any]) -> tuple[list[str], list[str]]:
    bodies: list[str] = []
    attachments: list[str] = []

    filename = part.get("filename")
    if filename:
        attachments.append(filename)

    mime_type = part.get("mimeType", "")
    body_data = part.get("body", {}).get("data")
    if body_data and mime_type in {"text/plain", "text/html"}:
        decoded = decode_body(body_data)
        bodies.append(strip_html(decoded) if mime_type == "text/html" else normalize_space(decoded))

    for child in part.get("parts", []) or []:
        child_bodies, child_attachments = walk_parts(child)
        bodies.extend(child_bodies)
        attachments.extend(child_attachments)

    return bodies, attachments


def extract_links(text: str) -> list[str]:
    links = re.findall(r"https?://[^\s<>)\"']+", text)
    deduped: list[str] = []
    for link in links:
        clean = link.rstrip(".,;]")
        if clean not in deduped:
            deduped.append(clean)
    return deduped[:20]


def read_message(service: Any, message_id: str, tz_name: str) -> EmailRecord:
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = message.get("payload", {})
    headers = payload.get("headers", [])
    bodies, attachments = walk_parts(payload)
    body = normalize_space("\n\n".join(item for item in bodies if item))
    if not body:
        body = message.get("snippet", "")

    internal_ms = int(message.get("internalDate", "0"))
    received_at = datetime.fromtimestamp(internal_ms / 1000, ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")

    return EmailRecord(
        sender=header_value(headers, "From"),
        to=header_value(headers, "To"),
        cc=header_value(headers, "Cc"),
        subject=header_value(headers, "Subject") or "(no subject)",
        received_at=received_at,
        snippet=message.get("snippet", ""),
        body=body[:6000],
        links=extract_links(body),
        attachments=sorted(set(attachments)),
    )


def format_email_for_prompt(index: int, record: EmailRecord) -> str:
    links = "\n".join(f"  - {link}" for link in record.links) or "  - None"
    attachments = "\n".join(f"  - {name}" for name in record.attachments) or "  - None"
    return f"""
Email {index}
From: {record.sender}
To: {record.to}
Cc: {record.cc}
Received: {record.received_at}
Subject: {record.subject}
Snippet: {record.snippet}
Links:
{links}
Attachments:
{attachments}
Body:
{record.body}
""".strip()


def build_summary(records: list[EmailRecord], target_date: str, model: str) -> str:
    if not records:
        return (
            f"\u6628\u65e5\u90f5\u4ef6\u6458\u8981 - {target_date}\n\n"
            "\u6628\u5929\u6c92\u6709\u6536\u5230\u7b26\u5408\u689d\u4ef6\u7684\u90f5\u4ef6\u3002\n"
        )

    prompt_items = "\n\n---\n\n".join(
        format_email_for_prompt(index, record) for index, record in enumerate(records, start=1)
    )
    client = genai.Client(api_key=required_env("GEMINI_API_KEY"))
    prompt = (
        "You are an executive email summarizer. Write in Traditional Chinese. "
        "Be concise, accurate, and action-oriented. Do not invent facts.\n\n"
        f"Please summarize the following {len(records)} emails into one daily digest email. "
        "Write the digest in Traditional Chinese.\n\n"
        f"Date: {target_date}\n\n"
        "Use these sections:\n"
        "1. Overview: total email count and the 3-5 most important items\n"
        "2. Items that need my reply or action\n"
        "3. Outline grouped by topic or sender\n"
        "4. Concise summary for each email: sender, time, subject, key points, possible next step\n"
        "5. Important links and attachment list\n\n"
        f"Emails:\n\n{prompt_items}"
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        return (response.text or "").strip() or build_fallback_summary(records, target_date)
    except Exception as error:
        print(f"Gemini summary failed; sending fallback digest instead: {error}")
        return build_fallback_summary(records, target_date)


def build_fallback_summary(records: list[EmailRecord], target_date: str) -> str:
    lines = [
        f"昨日郵件摘要 - {target_date}",
        "",
        "Gemini 摘要服務目前無法使用，所以這封是系統自動產生的備援摘要。",
        f"共收到 {len(records)} 封符合條件的郵件。",
        "",
        "郵件清單",
    ]

    for index, record in enumerate(records, start=1):
        preview = record.body or record.snippet
        preview = normalize_space(preview)[:700]
        links = ", ".join(record.links[:5]) if record.links else "無"
        attachments = ", ".join(record.attachments) if record.attachments else "無"
        lines.extend(
            [
                "",
                f"{index}. {record.subject}",
                f"寄件者: {record.sender}",
                f"時間: {record.received_at}",
                f"收件者: {record.to}",
                f"附件: {attachments}",
                f"連結: {links}",
                f"內容預覽: {preview or record.snippet or '無內容'}",
            ]
        )

    lines.extend(
        [
            "",
            "系統提醒",
            "這封信表示 Gmail 抓信與寄信功能正常，但 Gemini API 摘要步驟失敗。請檢查 Gemini API key、免費額度或專案設定。"
        ]
    )
    return "\n".join(lines)


def send_email(service: Any, sender: str, recipient: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = sender
    message["Subject"] = subject
    message.set_content(body)

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    service.users().messages().send(userId="me", body={"raw": encoded}).execute()


def sent_digest_exists(service: Any, recipient: str, subject: str) -> bool:
    query = f'in:sent to:{recipient} subject:"{subject}"'
    response = service.users().messages().list(userId="me", q=query, maxResults=1).execute()
    return bool(response.get("messages"))


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def should_run_now(tz_name: str) -> bool:
    run_after = os.getenv("RUN_AFTER_HOUR_LOCAL")
    run_before = os.getenv("RUN_BEFORE_HOUR_LOCAL")
    exact_hour = os.getenv("RUN_HOUR_LOCAL")

    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True

    if not (run_after or run_before or exact_hour):
        return True

    now = datetime.now(ZoneInfo(tz_name))
    if exact_hour and not (run_after or run_before):
        if now.hour != int(exact_hour):
            print(f"Skipping run at local hour {now.hour}; configured hour is {exact_hour}.")
            return False
        return True

    start_hour = int(run_after or exact_hour or "6")
    end_hour = int(run_before or "12")
    if not (start_hour <= now.hour < end_hour):
        print(f"Skipping run at local hour {now.hour}; configured window is {start_hour}:00-{end_hour}:00.")
        return False
    return True


def main() -> None:
    load_dotenv()

    tz_name = os.getenv("TIMEZONE", "America/Los_Angeles")
    if not should_run_now(tz_name):
        return

    model = os.getenv("SUMMARY_MODEL", "gemini-2.0-flash")
    max_emails = int(os.getenv("MAX_EMAILS", "500"))
    sender = required_env("GMAIL_USER_EMAIL")
    recipient = required_env("SUMMARY_RECIPIENT_EMAIL")

    start, end, target_date = previous_day_bounds(tz_name)
    query = gmail_query(start, end)
    service = gmail_service()

    subject = f"\u6628\u65e5\u90f5\u4ef6\u6458\u8981 - {target_date}"
    if sent_digest_exists(service, recipient, subject) and not env_flag("FORCE_RESEND"):
        print(f"Digest already sent for {target_date}; skipping duplicate.")
        return

    message_ids = list_message_ids(service, query, max_emails)
    records = [read_message(service, message_id, tz_name) for message_id in message_ids]
    summary = build_summary(records, target_date, model)

    send_email(service, sender, recipient, subject, summary)
    print(f"Sent summary for {target_date} with {len(records)} emails to {recipient}.")


if __name__ == "__main__":
    main()
