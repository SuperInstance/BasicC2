"""
fleet.agent — the box-side agent.

Kept from BasicC2: connect OUT to the controller, hold the connection,
reconnect stubbornly when it drops (now with exponential backoff +
jitter instead of a fixed 5 s). Replaced: everything the connection is
used for (see DESIGN.md).

Per task frame, in order:
  1. Verify the controller's signature. Bad -> error frame, nothing
     booked, nothing executed. (Refusals write nothing.)
  2. For `run`: check the argv against this box's own allowlist.
     Not allowlisted -> error frame, nothing booked, nothing run.
  3. Book the mirror RECEIVE of the <job>:task exchange (signed).
  4. Execute the named op — never a shell.
  5. Book the SEND of the <job>:result exchange (signed) and return a
     result frame carrying the result payload and this ledger's full
     bytes, so the desk can derive completion from records later.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import random
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import common
from .common import Key, stitch

RUN_TIMEOUT_S = 120


class Agent:
    def __init__(self, config: dict, state_dir, allowlist=None):
        self.agent_id = config["agent_id"]
        self.key = Key.from_secret(bytes.fromhex(config["secret_hex"]))
        self.controller_pub = bytes.fromhex(config["controller_pubkey_hex"])
        ctl = config["controller"]
        self.host = ctl["host"]
        self.port = int(ctl["port"])
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.state_dir / f"{self.agent_id}.jsonl"
        # Allowlist: list of argv prefixes, each a list of strings.
        # An argv is runnable iff some entry is an element-wise prefix.
        self.allowlist = [list(e) for e in (allowlist or [])]
        self._stop = threading.Event()
        self._sock = None

    # -- connection lifecycle ------------------------------------------------

    def run_forever(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._serve_once()
                attempt = 0  # a clean drop restarts backoff from the floor
            except (OSError, ValueError):
                pass  # connect refused / handshake refused / bad frame
            if self._stop.is_set():
                break
            backoff = min(30.0, 2.0 ** min(attempt, 5))
            attempt += 1
            self._stop.wait(backoff * (0.75 + 0.5 * random.random()))

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _serve_once(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=15)
        sock.settimeout(None)
        self._sock = sock
        try:
            if not self._handshake(sock):
                return
            while not self._stop.is_set():
                frame = common.recv_frame(sock)
                if frame is None:
                    return  # controller closed
                if frame.get("type") == "task":
                    self._handle_task(sock, frame)
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None

    def _handshake(self, sock) -> bool:
        common.send_frame(sock, {"type": "hello", "agent": self.agent_id})
        challenge = common.recv_frame(sock)
        if not challenge or challenge.get("type") != "challenge":
            return False  # unknown identity gets silence, not a session
        try:
            nonce = bytes.fromhex(challenge["nonce"])
        except (KeyError, ValueError):
            return False
        sig = self.key.sign(nonce).hex()
        common.send_frame(sock, {"type": "auth", "sig": sig})
        welcome = common.recv_frame(sock)
        return bool(welcome and welcome.get("type") == "welcome")

    # -- task handling -----------------------------------------------------------

    def _allowlisted(self, argv) -> bool:
        if not isinstance(argv, list) or not argv:
            return False
        if not all(isinstance(a, str) for a in argv):
            return False
        return any(argv[:len(prefix)] == prefix
                   for prefix in self.allowlist if prefix)

    def _handle_task(self, sock, frame: dict) -> None:
        job = frame.get("job")
        payload = frame.get("payload")
        # 1. Authenticate the order before anything else. A forged or
        # tampered task is refused in full: no booking, no execution.
        if not common.verify_frame(self.controller_pub, frame):
            self._send_error(sock, job, "bad_signature")
            return
        if not isinstance(payload, dict) or payload.get("job") != job:
            self._send_error(sock, job, "malformed_task")
            return
        kind = payload.get("kind")
        args = payload.get("args") or {}
        # 2. The allowlist gate also precedes any booking — a refused
        # job leaves no trace in this ledger either.
        if kind == "run" and not self._allowlisted(args.get("argv")):
            self._send_error(sock, job, "not_allowlisted")
            return
        if kind not in ("ping", "run"):
            self._send_error(sock, job, "unknown_kind")
            return
        # 3. Book what we received — the same payload the controller
        # booked as sent; if they differ, agree() refuses downstream.
        common.append_booking(
            self.ledger_path, self.agent_id, stitch.book_receive,
            xchg=f"{job}:task", peer=common.CONTROLLER_ID,
            payload=payload, secret_key=self.key.secret_bytes)
        # 4. Execute the named op.
        if kind == "ping":
            result = self._exec_ping()
        else:
            result = self._exec_run(args["argv"])
        # 5. Book the result, then report it with the ledger itself.
        common.append_booking(
            self.ledger_path, self.agent_id, stitch.book_send,
            xchg=f"{job}:result", peer=common.CONTROLLER_ID,
            payload=result, secret_key=self.key.secret_bytes)
        ledger_b64 = base64.b64encode(
            self.ledger_path.read_bytes()).decode("ascii")
        common.send_frame(sock, {
            "type": "result", "job": job, "payload": result,
            "ledger_b64": ledger_b64})

    def _send_error(self, sock, job, error: str) -> None:
        try:
            common.send_frame(sock, {
                "type": "error", "job": job, "error": error})
        except OSError:
            pass  # connection dying anyway; run_forever will reconnect

    # -- named ops (never a shell) -------------------------------------------------

    @staticmethod
    def _exec_ping() -> dict:
        return {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "utc": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _exec_run(argv) -> dict:
        try:
            proc = subprocess.run(argv, shell=False, capture_output=True,
                                  timeout=RUN_TIMEOUT_S)
            exit_code, out = proc.returncode, proc.stdout or b""
        except subprocess.TimeoutExpired as e:
            exit_code, out = 124, e.output or b""
        except OSError:
            exit_code, out = 127, b""  # allowlisted but not executable
        return {
            "argv": list(argv),
            "exit": exit_code,
            "stdout_sha256": hashlib.sha256(out).hexdigest(),
            "stdout_preview": out.decode("utf-8", errors="replace")[:512],
        }


def load_allowlist(path) -> list:
    if path is None:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"allowlist {path} must be a JSON list of "
                         f"argv prefixes")
    return data


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="fleet.agent",
                                 description="Fleet agent daemon")
    ap.add_argument("--config", required=True,
                    help="enroll bundle JSON written by `desk enroll`")
    ap.add_argument("--state", default="./agent-state",
                    help="agent state dir (holds this agent's ledger)")
    ap.add_argument("--allowlist", default=None,
                    help="JSON file: list of allowed argv prefixes "
                         "(default: <state>/allowlist.json if present)")
    args = ap.parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    allowlist_path = args.allowlist
    if allowlist_path is None:
        candidate = Path(args.state) / "allowlist.json"
        allowlist_path = str(candidate) if candidate.exists() else None
    agent = Agent(config, args.state,
                  allowlist=load_allowlist(allowlist_path))
    print(f"agent {agent.agent_id} -> controller "
          f"{agent.host}:{agent.port}", flush=True)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        agent.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
