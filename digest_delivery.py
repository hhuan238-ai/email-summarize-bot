from __future__ import annotations

import base64
import hashlib
import os
import socket
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid
from http.client import HTTPException
from pathlib import Path
from time import sleep
from typing import Any

import httplib2
from googleapiclient.errors import HttpError


@dataclass(frozen=True)
class SentDigest:
    message_id: str
    kind: str


class SendResultUnknown(RuntimeError):
    pass


def report_outcome(status: str, detail: str) -> None:
    print(f"Digest outcome: {status}. {detail}")
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"digest_status={status}\n")
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(f"### Email digest: {status}\n\n{detail}\n\n")


def headers_by_name(message: dict[str, Any]) -> dict[str, str]:
    return {
        header["name"].lower(): str(make_header(decode_header(header["value"])))
        for header in message.get("payload", {}).get("headers", [])
    }


def legacy_digest_kind(part: dict[str, Any]) -> str:
    body = part.get("body", {}).get("data", "")
    if body and part.get("mimeType", "").startswith("text/"):
        text = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8", errors="replace")
        if "Gemini 摘要服務目前無法使用" in text:
            return "fallback"
        if "昨天沒有收到符合條件的郵件" in text:
            return "empty"
    for child in part.get("parts", []) or []:
        kind = legacy_digest_kind(child)
        if kind != "legacy":
            return kind
    return "legacy"


def find_sent_digest(service: Any, recipient: str, subject: str) -> SentDigest | None:
    query = f'in:sent to:{recipient} subject:"{subject}"'
    page_token = None
    best_match = None
    priorities = {"legacy": 0, "fallback": 1, "empty": 2, "full": 3}
    while True:
        response = service.users().messages().list(
            userId="me", q=query, maxResults=100, pageToken=page_token,
        ).execute(num_retries=3)
        for candidate in response.get("messages", []):
            message = service.users().messages().get(
                userId="me", id=candidate["id"], format="metadata",
                metadataHeaders=["To", "Subject", "X-Digest-Kind"],
            ).execute(num_retries=3)
            headers = headers_by_name(message)
            recipients = {address.casefold() for _, address in getaddresses([headers.get("to", "")])}
            if headers.get("subject") != subject or recipient.casefold() not in recipients:
                continue
            if "SENT" not in message.get("labelIds", []):
                continue
            kind = headers.get("x-digest-kind", "legacy")
            if kind not in priorities:
                kind = "legacy"
            if kind == "legacy" and subject.startswith("昨日郵件摘要 - "):
                full_message = service.users().messages().get(
                    userId="me", id=candidate["id"], format="full",
                ).execute(num_retries=3)
                kind = legacy_digest_kind(full_message.get("payload", {}))
            match = SentDigest(candidate["id"], kind)
            if best_match is None or priorities[kind] > priorities[best_match.kind]:
                best_match = match
        page_token = response.get("nextPageToken")
        if not page_token:
            return best_match


def sent_digest_exists(service: Any, recipient: str, subject: str) -> bool:
    return find_sent_digest(service, recipient, subject) is not None


def reconcile_send(service: Any, message_id: str, kind: str) -> SentDigest | None:
    for delay in (2, 5, 10):
        sleep(delay)
        response = service.users().messages().list(
            userId="me", q=f"in:sent rfc822msgid:{message_id}", maxResults=1,
        ).execute(num_retries=3)
        if response.get("messages"):
            return SentDigest(response["messages"][0]["id"], kind)
    return None


def send_email(
    service: Any, sender: str, recipient: str, subject: str, body: str,
    *, kind: str = "legacy", target_date: str = "", force_resend: bool = False,
) -> SentDigest:
    if not force_resend:
        existing = find_sent_digest(service, recipient, subject)
        if existing:
            return existing
    identity = "\n".join((sender.casefold(), recipient.casefold(), subject))
    stable_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = sender
    message["Subject"] = subject
    message["Message-ID"] = make_msgid() if force_resend else f"<digest-{stable_id}@email-summarize-bot.local>"
    message["X-Digest-Kind"] = kind
    if target_date:
        message["X-Digest-Date"] = target_date
    message.set_content(body)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    try:
        result = service.users().messages().send(userId="me", body={"raw": encoded}).execute(num_retries=0)
        if result.get("id"):
            return SentDigest(result["id"], kind)
    except HttpError as error:
        if int(error.resp.status) < 500 and int(error.resp.status) != 408:
            raise
    except (socket.timeout, OSError, HTTPException, httplib2.HttpLib2Error):
        pass
    try:
        delivered = reconcile_send(service, message["Message-ID"].strip("<>"), kind)
        if delivered:
            return delivered
    except (HttpError, OSError, HTTPException, httplib2.HttpLib2Error):
        pass
    report_outcome("send_unknown", "Gmail delivery could not be confirmed; the send request was not retried.")
    raise SendResultUnknown("Check sent mail before resending; Gmail may already have accepted the message.")
