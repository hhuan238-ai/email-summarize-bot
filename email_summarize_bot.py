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
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from openai import OpenAI


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
            f"昨日郵件摘要 - {target_date}\n\n"
            "昨天沒有收到符合條件的郵件。\n"
        )

    prompt_items = "\n\n---\n\n".join(
        format_email_for_prompt(index, record) for index, record in enumerate(records, start=1)
    )
    client = OpenAI()
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "You are an executive email summarizer. Write in Traditional Chinese. "
                    "Be concise, accurate, and action-oriented. Do not invent facts."
                ),
            },
            {
                "role": "user",
                "content": f"""
請將以下 {len(records)} 封郵件整理成一封每日摘要 email。

日期：{target_date}

請使用這些段落：
1. 總覽：郵件總數與最重要的 3-5 件事
2. 需要我回覆或處理的事項
3. 依主題/寄件者分組的大綱
4. 每封郵件的精簡摘要：寄件者、時間、主旨、重點、可能的下一步
5. 重要連結與附件清單

郵件內容：

{prompt_items}
""".strip(),
            },
        ],
    )
    return response.output_text.strip()


def send_email(service: Any, sender: str, recipient: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = sender
    message["Subject"] = subject
    message.set_content(body)

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    service.users().messages().send(userId="me", body={"raw": encoded}).execute()


def main() -> None:
    load_dotenv()

    tz_name = os.getenv("TIMEZONE", "America/Los_Angeles")
    model = os.getenv("SUMMARY_MODEL", "gpt-4.1-mini")
    max_emails = int(os.getenv("MAX_EMAILS", "500"))
    sender = required_env("GMAIL_USER_EMAIL")
    recipient = required_env("SUMMARY_RECIPIENT_EMAIL")

    start, end, target_date = previous_day_bounds(tz_name)
    query = gmail_query(start, end)
    service = gmail_service()

    message_ids = list_message_ids(service, query, max_emails)
    records = [read_message(service, message_id, tz_name) for message_id in message_ids]
    summary = build_summary(records, target_date, model)

    subject = f"昨日郵件摘要 - {target_date}"
    send_email(service, sender, recipient, subject, summary)
    print(f"Sent summary for {target_date} with {len(records)} emails to {recipient}.")


if __name__ == "__main__":
    main()
