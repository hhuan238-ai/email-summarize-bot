from __future__ import annotations

import base64
import os
import tempfile
import unittest
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
from google.genai import errors, types

import digest_delivery as delivery
import email_summarize_bot as bot
import watchdog_check as watchdog


TIMEZONE = ZoneInfo("America/Los_Angeles")
SUBJECT = "昨日郵件摘要 - 2026/09/07"
RECIPIENT = "owner@example.com"
RECORD = bot.EmailRecord(
    "sender@example.com", RECIPIENT, "", "Action required", "2026-09-07 10:00 PDT",
    "Please reply", "Please reply by Friday.", [], [],
)


def gemini_error(code: int, message: str) -> errors.APIError:
    error_type = errors.ServerError if code >= 500 else errors.ClientError
    return error_type(code, {"error": {"code": code, "message": message}})


def stored_message(subject: str = SUBJECT, recipient: str = RECIPIENT, kind: str = "full") -> dict:
    headers = [{"name": "Subject", "value": subject}, {"name": "To", "value": recipient}]
    if kind:
        headers.append({"name": "X-Digest-Kind", "value": kind})
    return {"labelIds": ["SENT"], "payload": {"headers": headers}}


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        system_environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("GMAIL_", "GEMINI_", "SUMMARY_", "GITHUB_", "RUN_", "WATCHDOG_"))
            and key not in {"TIMEZONE", "DRY_RUN", "TARGET_DATE", "FORCE_RESEND", "FALLBACK_AFTER_HOUR_LOCAL"}
        }
        environment = patch.dict(os.environ, system_environment, clear=True)
        environment.start()
        self.addCleanup(environment.stop)


class SummaryRetryTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        os.environ["GEMINI_API_KEY"] = "test-key"
        client_patch = patch.object(bot.genai, "Client")
        self.client_factory = client_patch.start()
        self.addCleanup(client_patch.stop)
        self.generate = self.client_factory.return_value.__enter__.return_value.models.generate_content
        sleep_patch = patch.object(bot, "sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_overload_recovers_without_fallback(self):
        self.generate.side_effect = [
            gemini_error(503, "High demand"), gemini_error(503, "High demand"),
            SimpleNamespace(text="完整摘要"),
        ]
        result = bot.build_summary([RECORD], "2026/09/07", "test-model")
        self.assertEqual(result.kind, "full")
        self.assertEqual(result.body, "完整摘要")
        self.assertEqual(self.generate.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        options = self.client_factory.call_args.kwargs["http_options"]
        self.assertEqual(options.retry_options.attempts, 1)

    def test_persistent_overload_has_bounded_retries(self):
        self.generate.side_effect = gemini_error(503, "High demand")
        with self.assertRaises(bot.SummaryUnavailable) as raised:
            bot.build_summary([RECORD], "2026/09/07", "test-model")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(self.generate.call_count, 4)
        self.assertEqual(self.sleep.call_count, 3)

    def test_daily_quota_is_not_retried(self):
        self.generate.side_effect = gemini_error(429, "GenerateRequestsPerDay quota exceeded")
        with self.assertRaises(bot.SummaryUnavailable) as raised:
            bot.generate_summary_text("test", "test-model")
        self.assertFalse(raised.exception.retryable)
        self.generate.assert_called_once()
        self.sleep.assert_not_called()

    def test_invalid_model_is_not_retried(self):
        self.generate.side_effect = gemini_error(404, "Model not found")
        with self.assertRaises(bot.SummaryUnavailable) as raised:
            bot.generate_summary_text("test", "test-model")
        self.assertFalse(raised.exception.retryable)
        self.generate.assert_called_once()

    def test_network_timeout_is_retried(self):
        self.generate.side_effect = [httpx.ReadTimeout("timeout"), SimpleNamespace(text="摘要")]
        self.assertEqual(bot.generate_summary_text("test", "test-model"), "摘要")
        self.assertEqual(self.generate.call_count, 2)

    def test_server_retry_delay_is_honored(self):
        response = httpx.Response(429, headers={"retry-after": "45"})
        throttled = errors.ClientError(429, {"error": {"message": "Too many requests"}}, response)
        self.generate.side_effect = [throttled, SimpleNamespace(text="摘要")]
        bot.generate_summary_text("test", "test-model")
        self.sleep.assert_called_once_with(45)

    def test_retry_info_beyond_budget_defers_to_later_run(self):
        throttled = errors.ClientError(429, {"error": {
            "message": "Too many requests",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "600s"}],
        }})
        self.generate.side_effect = throttled
        with self.assertRaises(bot.SummaryUnavailable):
            bot.generate_summary_text("test", "test-model")
        self.generate.assert_called_once()
        self.sleep.assert_not_called()

    def test_no_request_is_started_after_retry_budget(self):
        with patch.object(bot, "monotonic", side_effect=[0, 361]):
            with self.assertRaises(bot.SummaryUnavailable):
                bot.generate_summary_text("test", "test-model")
        self.generate.assert_not_called()

    def test_unexpected_bug_is_not_hidden_as_fallback(self):
        self.generate.side_effect = ValueError("programming bug")
        with self.assertRaisesRegex(ValueError, "programming bug"):
            bot.generate_summary_text("test", "test-model")
        self.generate.assert_called_once()

    def test_empty_inbox_does_not_call_gemini(self):
        result = bot.build_summary([], "2026/09/07", "test-model")
        self.assertEqual(result.kind, "empty")
        self.client_factory.assert_not_called()

    def test_blank_response_is_not_success(self):
        self.generate.return_value = SimpleNamespace(text=" ")
        with self.assertRaises(bot.SummaryUnavailable):
            bot.generate_summary_text("test", "test-model")
        self.assertEqual(self.generate.call_count, 4)


class DeliveryTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.service = MagicMock()
        self.messages = self.service.users.return_value.messages.return_value

    def test_search_checks_exact_subject_and_pages(self):
        self.messages.list.return_value.execute.side_effect = [
            {"messages": [{"id": "wrong"}], "nextPageToken": "next"},
            {"messages": [{"id": "correct"}]},
        ]
        self.messages.get.return_value.execute.side_effect = [
            stored_message(subject=f"Re: {SUBJECT}"), stored_message(),
        ]
        result = delivery.find_sent_digest(self.service, RECIPIENT, SUBJECT)
        self.assertEqual(result.message_id, "correct")
        self.assertEqual(self.messages.list.call_args.kwargs["pageToken"], "next")

    def test_wrong_recipient_does_not_suppress_delivery(self):
        self.messages.list.return_value.execute.return_value = {"messages": [{"id": "wrong"}]}
        self.messages.get.return_value.execute.return_value = stored_message(recipient="other@example.com")
        self.assertIsNone(delivery.find_sent_digest(self.service, RECIPIENT, SUBJECT))

    def test_encoded_subject_is_compared_after_decoding(self):
        message = stored_message()
        encoded_subject = base64.b64encode(SUBJECT.encode()).decode()
        message["payload"]["headers"][0]["value"] = f"=?utf-8?b?{encoded_subject}?="
        self.messages.list.return_value.execute.return_value = {"messages": [{"id": "encoded"}]}
        self.messages.get.return_value.execute.return_value = message
        self.assertEqual(delivery.find_sent_digest(self.service, RECIPIENT, SUBJECT).message_id, "encoded")

    def test_new_complete_digest_wins_over_old_fallback(self):
        self.messages.list.return_value.execute.return_value = {"messages": [{"id": "old"}, {"id": "new"}]}
        self.messages.get.return_value.execute.side_effect = [stored_message(kind="fallback"), stored_message()]
        self.assertEqual(delivery.find_sent_digest(self.service, RECIPIENT, SUBJECT).kind, "full")

    def test_legacy_fallback_is_recognized(self):
        self.messages.list.return_value.execute.return_value = {"messages": [{"id": "old"}]}
        body = base64.urlsafe_b64encode("Gemini 摘要服務目前無法使用".encode()).decode()
        self.messages.get.return_value.execute.side_effect = [
            stored_message(kind=""),
            {"payload": {"mimeType": "text/plain", "body": {"data": body}}},
        ]
        self.assertEqual(delivery.find_sent_digest(self.service, RECIPIENT, SUBJECT).kind, "fallback")

    def test_send_rechecks_duplicates(self):
        existing = delivery.SentDigest("already", "full")
        with patch.object(delivery, "find_sent_digest", return_value=existing):
            result = delivery.send_email(self.service, RECIPIENT, RECIPIENT, SUBJECT, "body")
        self.assertEqual(result, existing)
        self.messages.send.assert_not_called()

    def test_send_marks_kind_and_date(self):
        self.messages.send.return_value.execute.return_value = {"id": "sent"}
        with patch.object(delivery, "find_sent_digest", return_value=None):
            delivery.send_email(
                self.service, RECIPIENT, RECIPIENT, SUBJECT, "完整摘要",
                kind="full", target_date="2026/09/07",
            )
        raw = self.messages.send.call_args.kwargs["body"]["raw"]
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(message["X-Digest-Kind"], "full")
        self.assertEqual(message["X-Digest-Date"], "2026/09/07")
        self.assertEqual(str(message["Subject"]), SUBJECT)
        self.messages.send.return_value.execute.assert_called_once_with(num_retries=0)

    def test_send_timeout_reconciles_without_second_send(self):
        self.messages.send.return_value.execute.side_effect = TimeoutError("response lost")
        self.messages.list.return_value.execute.return_value = {"messages": [{"id": "accepted"}]}
        with patch.object(delivery, "find_sent_digest", return_value=None), patch.object(delivery, "sleep"):
            result = delivery.send_email(self.service, RECIPIENT, RECIPIENT, SUBJECT, "body", kind="full")
        self.assertEqual(result.message_id, "accepted")
        self.messages.send.assert_called_once()
        self.assertIn("rfc822msgid:", self.messages.list.call_args.kwargs["q"])

    def test_unconfirmed_send_raises_without_retrying_post(self):
        self.messages.send.return_value.execute.side_effect = TimeoutError("response lost")
        self.messages.list.return_value.execute.return_value = {}
        with patch.object(delivery, "find_sent_digest", return_value=None), patch.object(delivery, "sleep"):
            with self.assertRaises(delivery.SendResultUnknown):
                delivery.send_email(self.service, RECIPIENT, RECIPIENT, SUBJECT, "body")
        self.messages.send.assert_called_once()
        self.assertEqual(self.messages.list.call_count, 3)

    def test_status_written_without_message_body(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["GITHUB_OUTPUT"] = str(Path(directory) / "output")
            os.environ["GITHUB_STEP_SUMMARY"] = str(Path(directory) / "summary")
            delivery.report_outcome("retry_pending", "Temporary overload.")
            self.assertIn("digest_status=retry_pending", Path(os.environ["GITHUB_OUTPUT"]).read_text())


class RunPolicyTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        os.environ.update({
            "GMAIL_USER_EMAIL": RECIPIENT, "SUMMARY_RECIPIENT_EMAIL": RECIPIENT,
            "RUN_AFTER_HOUR_LOCAL": "6", "RUN_BEFORE_HOUR_LOCAL": "24",
        })
        self.now = datetime(2026, 9, 8, 8, tzinfo=TIMEZONE)
        clock_patch = patch.object(bot, "datetime", wraps=datetime)
        self.clock = clock_patch.start()
        self.clock.now.side_effect = lambda timezone: self.now.astimezone(timezone)
        self.addCleanup(clock_patch.stop)
        for name, value in (
            ("load_dotenv", None), ("gmail_service", MagicMock()), ("find_sent_digest", None),
            ("list_message_ids", ["mail"]), ("read_message", RECORD),
            ("send_email", delivery.SentDigest("sent", "full")), ("report_outcome", None),
        ):
            patcher = patch.object(bot, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_temporary_failure_before_cutoff_sends_nothing(self):
        with patch.object(bot, "build_summary", side_effect=bot.SummaryUnavailable("overloaded", True)):
            bot.main()
        self.send_email.assert_not_called()
        self.report_outcome.assert_called_with("retry_pending", "2026/09/07: overloaded")

    def test_after_cutoff_sends_fallback(self):
        self.now = self.now.replace(hour=12, minute=5)
        self.send_email.return_value = delivery.SentDigest("sent", "fallback")
        with patch.object(bot, "build_summary", side_effect=bot.SummaryUnavailable("overloaded", True)):
            bot.main()
        self.assertEqual(self.send_email.call_args.kwargs["kind"], "fallback")
        self.assertEqual(self.report_outcome.call_args.args[0], "fallback_sent")

    def test_success_sends_full(self):
        with patch.object(bot, "build_summary", return_value=bot.SummaryResult("摘要", "full")):
            bot.main()
        self.send_email.assert_called_once()
        self.assertEqual(self.send_email.call_args.kwargs["kind"], "full")

    def test_empty_inbox_sends_one_notice_without_api_key(self):
        self.list_message_ids.return_value = []
        bot.main()
        self.send_email.assert_called_once()
        self.assertEqual(self.send_email.call_args.kwargs["kind"], "empty")

    def test_existing_fallback_is_not_automatically_resent(self):
        self.find_sent_digest.return_value = delivery.SentDigest("existing", "fallback")
        bot.main()
        self.list_message_ids.assert_not_called()
        self.send_email.assert_not_called()

    def test_preview_can_inspect_existing_date_without_sending(self):
        os.environ.update({"DRY_RUN": "true", "TARGET_DATE": "2026-09-05", "GITHUB_EVENT_NAME": "workflow_dispatch"})
        self.find_sent_digest.return_value = delivery.SentDigest("existing", "fallback")
        with patch.object(bot, "build_summary", return_value=bot.SummaryResult("摘要", "full")) as summarize:
            bot.main()
        self.assertEqual(summarize.call_args.args[1], "2026/09/05")
        self.send_email.assert_not_called()
        self.assertEqual(self.report_outcome.call_args.args[0], "dry_run")

    def test_scheduled_run_cannot_force_resend(self):
        os.environ.update({"FORCE_RESEND": "true", "GITHUB_EVENT_NAME": "schedule"})
        self.find_sent_digest.return_value = delivery.SentDigest("existing", "full")
        bot.main()
        self.send_email.assert_not_called()

    def test_permanent_failure_is_visible(self):
        with patch.object(bot, "build_summary", side_effect=bot.SummaryUnavailable("invalid model", False)):
            with self.assertRaises(bot.SummaryUnavailable):
                bot.main()
        self.send_email.assert_not_called()
        self.assertEqual(self.report_outcome.call_args.args[0], "configuration_error")

    def test_permanent_failure_after_cutoff_delivers_but_stays_visible(self):
        self.now = self.now.replace(hour=13)
        self.send_email.return_value = delivery.SentDigest("sent", "fallback")
        with patch.object(bot, "build_summary", side_effect=bot.SummaryUnavailable("invalid model", False)):
            with self.assertRaises(bot.SummaryUnavailable):
                bot.main()
        self.send_email.assert_called_once()
        self.assertEqual(self.report_outcome.call_args.args[0], "fallback_sent")

    def test_late_schedule_can_still_deliver(self):
        self.now = self.now.replace(hour=22)
        with patch.object(bot, "build_summary", return_value=bot.SummaryResult("摘要", "full")):
            bot.main()
        self.send_email.assert_called_once()

    def test_window_must_allow_fallback(self):
        os.environ["RUN_BEFORE_HOUR_LOCAL"] = "12"
        with self.assertRaisesRegex(ValueError, "Delivery window"):
            bot.main()


class SdkIntegrationTests(IsolatedTest):
    def test_real_sdk_with_mock_http_recovers_from_503(self):
        os.environ["GEMINI_API_KEY"] = "test-key"
        responses = [
            httpx.Response(503, json={"error": {"code": 503, "message": "High demand", "status": "UNAVAILABLE"}}),
            httpx.Response(200, json={"candidates": [{
                "content": {"role": "model", "parts": [{"text": "完整摘要"}]}, "finishReason": "STOP",
            }]}),
        ]
        requests = []

        def handle(request):
            requests.append(request)
            return responses.pop(0)

        with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
            actual_client = bot.genai.Client(
                api_key="test-key",
                http_options=types.HttpOptions(
                    httpx_client=transport, retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
            with patch.object(bot.genai, "Client", return_value=actual_client), patch.object(bot, "sleep"):
                result = bot.generate_summary_text("Synthetic test content", "test-model")
        self.assertEqual(result, "完整摘要")
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request.method == "POST" for request in requests))


class DateAndWatchdogTests(IsolatedTest):
    def test_previous_date_uses_los_angeles_not_machine_timezone(self):
        now = datetime(2026, 9, 8, 1, tzinfo=ZoneInfo("Asia/Taipei"))
        _, _, target = bot.previous_day_bounds("America/Los_Angeles", now=now)
        self.assertEqual(target, "2026/09/06")

    def test_future_or_incomplete_date_rejected(self):
        now = datetime(2026, 9, 8, 8, tzinfo=TIMEZONE)
        with self.assertRaises(ValueError):
            bot.previous_day_bounds("America/Los_Angeles", "2026-09-08", now)

    def test_dst_calendar_day_can_be_23_hours(self):
        now = datetime(2026, 3, 9, 8, tzinfo=TIMEZONE)
        start, end, _ = bot.previous_day_bounds("America/Los_Angeles", now=now)
        self.assertEqual(end.timestamp() - start.timestamp(), 23 * 3600)

    def test_watchdog_distinguishes_fallback_without_new_alert(self):
        os.environ.update({"GMAIL_USER_EMAIL": RECIPIENT, "SUMMARY_RECIPIENT_EMAIL": RECIPIENT})
        with (
            patch.object(watchdog, "load_dotenv"),
            patch.object(watchdog, "should_check_now", return_value=True),
            patch.object(watchdog, "gmail_service"),
            patch.object(watchdog, "find_sent_digest", return_value=delivery.SentDigest("sent", "fallback")),
            patch.object(watchdog, "send_email") as send,
            patch.object(watchdog, "report_outcome") as report,
        ):
            watchdog.main()
        send.assert_not_called()
        self.assertEqual(report.call_args.args[0], "fallback_sent")
        self.assertIn("degraded", report.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
