#!/usr/bin/python3
"""Redeem one reset only after an explicit widget action; print one JSON result.

The caller owns the UUID and must retain it after uncertain failures. This
command never retries or generates a new idempotency key on its own.
"""

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid


def load_usage():
    spec = importlib.util.spec_from_file_location(
        "nachfq_usage_codex", Path(__file__).with_name("usage-codex.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_params(idempotency_key, credit_id=None):
    try:
        parsed = uuid.UUID(idempotency_key)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("idempotency key must be a UUID") from None
    if str(parsed) != idempotency_key.lower():
        raise ValueError("idempotency key must use the UUID hyphenated form")
    # IDs are opaque: preserve the exact value, but reject empty/control data.
    if credit_id is not None and (not isinstance(credit_id, str)
                                 or not credit_id.strip()
                                 or any(ord(char) < 32 or ord(char) == 127 for char in credit_id)):
        raise ValueError("credit ID must be a non-empty opaque ID without control characters")
    params = {"idempotencyKey": idempotency_key}
    if credit_id is not None:
        params["creditId"] = credit_id
    return params


MESSAGES = {
    "reset": "Reset completed.",
    "alreadyRedeemed": "This reset was already completed.",
    "noCredit": "No reset credits are available.",
    "nothingToReset": "There is no eligible usage window to reset.",
}


def persist_pending(path, params):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(params, handle)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def consume_reset(idempotency_key, credit_id=None, usage=None, pending_path=None):
    result = {"status": "error", "outcome": None,
              "idempotencyKey": idempotency_key, "creditId": credit_id, "retryable": False,
              "message": "", "limitsRefreshed": False,
              "limits": [], "resetCredits": None}
    try:
        params = validate_params(idempotency_key, credit_id)
    except ValueError as exc:
        result["message"] = str(exc)
        return result
    if pending_path is not None:
        # Only the CLI uses durable state. Tests inject a temporary path; pure
        # calls can omit it. A pending attempt always wins over a new UI UUID.
        path = Path(pending_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.with_suffix(".lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    result["message"] = "A reset is already in progress."
                    return result
                if path.exists():
                    pending = json.loads(path.read_text())
                    params = validate_params(pending.get("idempotencyKey"), pending.get("creditId"))
                persist_pending(path, params)
                result = consume_reset(params["idempotencyKey"], params.get("creditId"), usage)
                if result["status"] in ("success", "unavailable"):
                    # Failed cleanup should retain the same harmless UUID,
                    # never obscure a definitive redemption result.
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                else:
                    # An earlier request may already have reached the service.
                    # A retry failing before send cannot resolve that attempt.
                    result["retryable"] = True
                return result
        except Exception as exc:
            result["message"] = f"Could not preserve the reset attempt: {exc}"
            return result
    usage = usage or load_usage()
    try:
        collector = usage.load_collector()
        with usage.app_server(collector) as proc:
            # Once submitted, an unknown outcome must retain the original UUID.
            result["retryable"] = True
            response = usage.rpc_request(
                proc, 2, "account/rateLimitResetCredit/consume", params, timeout=15)
            outcome = (response.get("result") or {}).get("outcome")
            if outcome not in MESSAGES:
                raise RuntimeError("The server returned an unknown reset outcome.")
            result.update(status="success" if outcome in ("reset", "alreadyRedeemed")
                          else "unavailable", outcome=outcome,
                          retryable=False, message=MESSAGES[outcome])
            try:
                limits_response = usage.rpc_request(
                    proc, 3, "account/rateLimits/read", timeout=8)
                fields = usage.rate_limit_fields(collector, limits_response)
                result.update(limits=fields["limits"], resetCredits=fields["resetCredits"],
                              limitsRefreshed=True)
            except Exception:
                # The redemption already has a known outcome. A later refresh
                # error must never invite another logical redemption.
                result["message"] += " Usage could not be refreshed; it will be checked again."
    except Exception as exc:
        result["message"] = str(exc) or type(exc).__name__
    return result


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"status": "error", "outcome": None,
                          "retryable": False, "message": message}))
        raise SystemExit(2)


def main(argv=None):
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--credit-id")
    args = parser.parse_args(argv)
    state_root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    result = consume_reset(args.idempotency_key, args.credit_id,
                           pending_path=state_root / "omarchy" / "codex-reset-pending.json")
    print(json.dumps(result, separators=(",", ":")))
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
