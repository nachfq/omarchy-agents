#!/usr/bin/python3
"""Reuse Omarchy's collector with an unbuffered Codex RPC reader."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import select
import time
import subprocess
from contextlib import contextmanager


class RpcError(RuntimeError):
    """An explicit app-server error, distinct from an uncertain timeout."""

    def __init__(self, method, error):
        self.method = method
        self.error = error
        super().__init__(f"{method}: {error.get('message', 'RPC error')}")


def rpc_request(proc, request_id, method, params=None, timeout=8):
    payload = {"id": request_id, "method": method, "params": params or {}}
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    deadline = time.monotonic() + timeout
    pending = getattr(proc, "_omarchy_rpc_pending", b"")
    while True:
        # Drain complete buffered messages before waiting on the descriptor.
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == request_id:
                proc._omarchy_rpc_pending = pending
                if isinstance(message.get("error"), dict):
                    raise RpcError(method, message["error"])
                return message
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            break
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            break
        pending += chunk
    proc._omarchy_rpc_pending = pending
    raise TimeoutError(method)


@contextmanager
def app_server(collector):
    """Use the same installed Codex and environment as Omarchy's collector."""
    codex = collector.find_command("codex")
    if not codex:
        raise FileNotFoundError("codex not found in PATH")
    proc = subprocess.Popen(
        [codex, "-s", "read-only", "-a", "on-request", "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, env=collector.ENV,
    )
    try:
        rpc_request(proc, 1, "initialize", {
            "clientInfo": {"name": "omarchy-agent-usage", "version": "1"}
        }, timeout=8)
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        yield proc
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()


def rate_limit_fields(collector, response):
    """Preserve reset-credit unknown/count-only/detail states verbatim."""
    raw = response.get("result") or {}
    limits = raw.get("rateLimits") or {}
    return {
        "limits": [entry for window in (limits.get("primary"), limits.get("secondary"))
                   if (entry := collector.limit_window(window)) is not None],
        "resetCredits": raw.get("rateLimitResetCredits"),
        "tierLabel": str(limits.get("planType") or ""),
    }


def fetch_codex_rpc(collector):
    result = {"limits": [], "resetCredits": None, "tierLabel": "",
              "usageStatusText": "", "authHelpText": collector.AUTH_HELP}
    try:
        with app_server(collector) as proc:
            account_msg = rpc_request(proc, 2, "account/read", timeout=4)
            limits_msg = rpc_request(proc, 3, "account/rateLimits/read", timeout=4)
            result.update(rate_limit_fields(collector, limits_msg))
            account = (account_msg.get("result") or {}).get("account") or {}
            if not result["tierLabel"]:
                result["tierLabel"] = str(account.get("planType") or account.get("type") or "")
    except Exception as exc:
        result["usageStatusText"] = "Codex limits unavailable"
        result["authHelpText"] = str(exc)
    return result


def load_collector():
    source = Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")) / "bin/omarchy-agent-usage-codex"
    loader = importlib.machinery.SourceFileLoader("omarchy_usage_codex", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    collector = importlib.util.module_from_spec(spec)
    loader.exec_module(collector)
    collector.rpc_request = rpc_request
    collector.fetch_codex_rpc = lambda: fetch_codex_rpc(collector)
    return collector


if __name__ == "__main__":
    load_collector().main()
