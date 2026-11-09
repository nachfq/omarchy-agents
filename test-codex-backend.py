#!/usr/bin/python3
"""Offline tests: no Codex process is spawned and no credit is redeemed."""

from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import types
import tempfile
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


usage = load("usage", "usage-codex.py")
reset = load("reset", "reset-codex.py")
KEY = "8ae96ff3-3425-4f4c-8772-b6fd61502868"
CREDITS = {"availableCount": 3, "credits": [
    {"id": "opaque/credit 1", "status": "available", "expiresAt": None},
]}


def collector():
    # Loading Python source reads files only; its __main__ is never called.
    return usage.load_collector()


class BackendTests(unittest.TestCase):
    def fake_usage(self, outcome="reset", consume_error=None, refresh_error=None):
        calls = []

        @contextmanager
        def server(_collector):
            yield object()

        def request(proc, request_id, method, params=None, timeout=8):
            calls.append((method, params))
            if method == "account/rateLimitResetCredit/consume":
                if consume_error:
                    raise consume_error
                return {"result": {"outcome": outcome}}
            if refresh_error:
                raise refresh_error
            return {"result": {"rateLimitResetCredits": CREDITS,
                               "rateLimits": {"secondary": {
                                   "usedPercent": 0, "windowDurationMins": 10080,
                                   "resetsAt": 1900000000}}}}

        return types.SimpleNamespace(load_collector=collector, app_server=server,
                                     rpc_request=request, rate_limit_fields=usage.rate_limit_fields), calls

    def test_credit_states_preserved_without_inferred_count(self):
        for credits in (None, {"availableCount": 4, "credits": None},
                        {"availableCount": 0, "credits": []}, CREDITS):
            with self.subTest(credits=credits):
                fields = usage.rate_limit_fields(collector(), {
                    "result": {"rateLimitResetCredits": credits}})
                self.assertEqual(fields["resetCredits"], credits)
        self.assertIsNone(usage.rate_limit_fields(collector(), {"result": {}})["resetCredits"])

    def test_all_official_outcomes_and_refresh(self):
        for outcome in ("reset", "alreadyRedeemed", "noCredit", "nothingToReset"):
            with self.subTest(outcome=outcome):
                fake, calls = self.fake_usage(outcome)
                result = reset.consume_reset(KEY, "opaque/credit 1", fake)
                self.assertEqual(result["status"], "success" if outcome in (
                    "reset", "alreadyRedeemed") else "unavailable")
                self.assertEqual(result["outcome"], outcome)
                self.assertTrue(result["limitsRefreshed"])
                self.assertEqual(result["resetCredits"], CREDITS)
                self.assertEqual([call[0] for call in calls], [
                    "account/rateLimitResetCredit/consume", "account/rateLimits/read"])
                self.assertFalse(result["retryable"])

    def test_retries_keep_exact_key_and_credit(self):
        fake, calls = self.fake_usage(consume_error=TimeoutError("consume"))
        for _ in range(2):
            result = reset.consume_reset(KEY, "opaque/credit 1", fake)
            self.assertEqual(result["idempotencyKey"], KEY)
            self.assertTrue(result["retryable"])
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][1], {"idempotencyKey": KEY, "creditId": "opaque/credit 1"})

    def test_pending_attempt_survives_restart_with_different_ui_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            pending = Path(directory) / "pending.json"
            fake, calls = self.fake_usage(consume_error=TimeoutError("consume"))
            reset.consume_reset(KEY, "original", fake, pending)
            self.assertTrue(pending.exists())
            new_key = "123e4567-e89b-42d3-a456-426614174000"
            result = reset.consume_reset(new_key, "new", fake, pending)
            self.assertEqual(result["idempotencyKey"], KEY)
            self.assertEqual(result["creditId"], "original")
            self.assertEqual(calls[0][1], calls[1][1])
            good, _ = self.fake_usage(outcome="alreadyRedeemed")
            result = reset.consume_reset(new_key, "new", good, pending)
            self.assertEqual(result["outcome"], "alreadyRedeemed")
            self.assertFalse(pending.exists())

    def test_corrupt_pending_state_cannot_start_new_redemption(self):
        with tempfile.TemporaryDirectory() as directory:
            pending = Path(directory) / "pending.json"
            pending.write_text("garbage")
            fake, calls = self.fake_usage()
            result = reset.consume_reset(KEY, usage=fake, pending_path=pending)
            self.assertEqual(result["status"], "error")
            self.assertEqual(calls, [])

    def test_initialize_failure_cannot_discard_prior_uncertain_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            pending = Path(directory) / "pending.json"
            uncertain, calls = self.fake_usage(consume_error=TimeoutError("consume"))
            reset.consume_reset(KEY, "original", uncertain, pending)

            @contextmanager
            def unavailable_server(_collector):
                raise TimeoutError("initialize")
                yield

            unavailable, _ = self.fake_usage()
            unavailable.app_server = unavailable_server
            new_key = "123e4567-e89b-42d3-a456-426614174000"
            failed = reset.consume_reset(new_key, "new", unavailable, pending)
            self.assertEqual(failed["status"], "error")
            self.assertTrue(failed["retryable"])
            self.assertEqual(failed["idempotencyKey"], KEY)
            self.assertEqual(failed["creditId"], "original")
            self.assertEqual(json.loads(pending.read_text()), {
                "idempotencyKey": KEY, "creditId": "original"})

            successful, final_calls = self.fake_usage(outcome="alreadyRedeemed")
            finished = reset.consume_reset(new_key, "new", successful, pending)
            self.assertEqual(final_calls[0][1], calls[0][1])
            self.assertEqual(finished["outcome"], "alreadyRedeemed")
            self.assertFalse(pending.exists())

    def test_rpc_error_is_error(self):
        fake, _ = self.fake_usage(consume_error=usage.RpcError("consume", {
            "code": -32601, "message": "Unsupported method"}))
        result = reset.consume_reset(KEY, usage=fake)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["outcome"])
        self.assertIn("Unsupported method", result["message"])

    def test_refresh_error_does_not_change_known_redemption(self):
        fake, _ = self.fake_usage(refresh_error=TimeoutError("read"))
        result = reset.consume_reset(KEY, usage=fake)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["outcome"], "reset")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["limitsRefreshed"])

    def test_unknown_outcome_remains_retryable_error(self):
        fake, calls = self.fake_usage(outcome="surprise")
        result = reset.consume_reset(KEY, usage=fake)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["retryable"])
        self.assertEqual(len(calls), 1)

    def test_invalid_params_never_open_server(self):
        for key, credit in (("", None), ("oops", None),
                            (KEY.replace("-", ""), None), (KEY, ""),
                            (KEY, " \t"), (KEY, "opaque\ncredit")):
            with self.subTest(key=key, credit=credit):
                fake, calls = self.fake_usage()
                result = reset.consume_reset(key, credit, fake)
                self.assertEqual(result["status"], "error")
                self.assertFalse(result["retryable"])
                self.assertEqual(calls, [])
        self.assertEqual(reset.validate_params(KEY), {"idempotencyKey": KEY})

    def test_regular_collector_only_reads_and_preserves_credits(self):
        calls = []

        @contextmanager
        def server(_collector):
            yield object()

        def request(proc, request_id, method, params=None, timeout=8):
            calls.append(method)
            if method == "account/read":
                return {"result": {"account": {"planType": "pro"}}}
            if method != "account/rateLimits/read":
                self.fail("Regular collection attempted a mutation")
            return {"result": {"rateLimitResetCredits": CREDITS}}

        with patch.object(usage, "app_server", server), patch.object(usage, "rpc_request", request):
            result = usage.fetch_codex_rpc(collector())
        self.assertEqual(calls, ["account/read", "account/rateLimits/read"])
        self.assertEqual(result["resetCredits"], CREDITS)
        self.assertEqual(result["tierLabel"], "pro")

    def test_reader_drains_notifications_and_buffered_response(self):
        read_fd, write_fd = os.pipe()
        stdout = os.fdopen(read_fd, "rb", buffering=0)
        proc = types.SimpleNamespace(stdin=io.StringIO(), stdout=stdout)
        try:
            os.write(write_fd, b'{"method":"notice"}\n{"id":1,"result":{}}\n'
                     b'{"id":2,"result":{"outcome":"reset"}}\n')
            self.assertEqual(usage.rpc_request(proc, 1, "initialize")["result"], {})
            self.assertEqual(usage.rpc_request(proc, 2, "fake")["result"]["outcome"], "reset")
            os.write(write_fd, b'{"id":3,"error":{"code":-32601,"message":"bad"}}\n')
            with self.assertRaises(usage.RpcError):
                usage.rpc_request(proc, 3, "fake")
        finally:
            os.close(write_fd)
            stdout.close()


if __name__ == "__main__":
    unittest.main()
