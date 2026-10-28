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

v1.1 additions:
  - Heartbeat: a per-connection thread sends a small signed
    heartbeat frame whenever nothing else has been sent for
    heartbeat_s seconds, so a live-but-idle agent is never reaped —
    and a busy one isn't either: the heartbeat thread is separate
    from the task thread, so a long `run` (up to RUN_TIMEOUT_S)
    cannot starve it. All sends funnel through one lock so frames
    can never interleave on the wire.
  - Allowlist hot-reload: the allowlist file is stat'd on every
    task dispatch; an mtime change reloads it. A malformed new
    file keeps the previous list (and logs why) — a parse failure
    never crashes the agent and never widens access.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import random
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import common
from .common import Key, stitch

RUN_TIMEOUT_S = 120


class Agent:
    def __init__(self, config: dict, state_dir, allowlist=None,
                 allowlist_path=None, heartbeat_s=15.0):
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
        # Hot-reload (v1.1): when a path is known, the file is stat'd
        # per dispatch and reloaded on mtime change. The mtime
        # baseline is the file as loaded at startup.
        self.allowlist_path = (Path(allowlist_path)
                               if allowlist_path is not None else None)
        self._allowlist_mtime = self._stat_mtime(self.allowlist_path)
        #: Idle interval after which a signed heartbeat is sent.
        #: 0 disables heartbeats (the controller will reap an idle
        #: agent at its reap budget — used by tests, not operators).
        self.heartbeat_s = float(heartbeat_s)
        self._stop = threading.Event()
        self._sock = None
        self._send_lock = threading.Lock()
        self._last_sent = time.monotonic()

    # -- small helpers --------------------------------------------------------

    @staticmethod
    def _stat_mtime(path):
        if path is None:
            return None
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def _log(self, msg: str) -> None:
        common.log(f"agent/{self.agent_id}", msg)

    def _send_frame(self, sock, frame: dict) -> None:
        """Every send goes through here: the lock keeps a heartbeat
        frame from interleaving with a task frame on the wire, and
        _last_sent is what the heartbeat thread watches."""
        with self._send_lock:
            common.send_frame(sock, frame)
            self._last_sent = time.monotonic()

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
        hb_stop = threading.Event()
        try:
            if not self._handshake(sock):
                return
            self._log(f"connected to controller {self.host}:{self.port}")
            self._last_sent = time.monotonic()
            if self.heartbeat_s > 0:
                t = threading.Thread(target=self._heartbeat_loop,
                                     args=(sock, hb_stop), daemon=True,
                                     name="heartbeat")
                t.start()
            while not self._stop.is_set():
                frame = common.recv_frame(sock)
                if frame is None:
                    return  # controller closed
                if frame.get("type") == "task":
                    self._handle_task(sock, frame)
        finally:
            hb_stop.set()
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None

    def _heartbeat_loop(self, sock, hb_stop: threading.Event) -> None:
        """
        Send a signed heartbeat whenever the connection has been
        send-silent for heartbeat_s. Runs in its own thread so a long
        task execution in the main thread cannot starve it — the
        controller reaps on silence, and a busy agent is not a dead
        one. A failed send means the connection is dying; the main
        thread's recv will notice and run_forever reconnects, so
        here we just stop.
        """
        while not hb_stop.is_set() and not self._stop.is_set():
            hb_stop.wait(self.heartbeat_s)
            if hb_stop.is_set() or self._stop.is_set():
                return
            if time.monotonic() - self._last_sent < self.heartbeat_s:
                continue  # real traffic already proved liveness
            try:
                self._send_frame(sock, common.sign_frame(self.key, {
                    "type": "heartbeat", "agent": self.agent_id}))
            except OSError:
                return

    def _handshake(self, sock) -> bool:
        self._send_frame(sock, {"type": "hello", "agent": self.agent_id})
        challenge = common.recv_frame(sock)
        if not challenge or challenge.get("type") != "challenge":
            return False  # unknown identity gets silence, not a session
        try:
            nonce = bytes.fromhex(challenge["nonce"])
        except (KeyError, ValueError):
            return False
        sig = self.key.sign(nonce).hex()
        self._send_frame(sock, {"type": "auth", "sig": sig})
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

    def _maybe_reload_allowlist(self) -> None:
        """
        Hot-reload (v1.1): stat the allowlist file; on an mtime
        change, reload it. Failure policy is the security point: a
        malformed or unreadable new file KEEPS the previous list —
        the agent never crashes over an edit and access never widens
        on a parse failure — and the kept mtime advances so the same
        bad file isn't re-parsed (and re-logged) on every dispatch;
        the next edit is a new mtime and is tried again.
        """
        if self.allowlist_path is None:
            return
        mtime = self._stat_mtime(self.allowlist_path)
        if mtime is None or mtime == self._allowlist_mtime:
            return
        try:
            new = load_allowlist(self.allowlist_path)
        except Exception as e:  # JSON errors, bad shape, vanished file
            self._allowlist_mtime = mtime
            self._log(f"allowlist reload failed ({type(e).__name__}: "
                      f"{e}) — keeping the previous list "
                      f"({len(self.allowlist)} prefixes)")
            return
        self.allowlist = [list(e) for e in new]
        self._allowlist_mtime = mtime
        self._log(f"allowlist reloaded from {self.allowlist_path}: "
                  f"{len(self.allowlist)} prefixes")

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
        # job leaves no trace in this ledger either. The reload check
        # lives here, at the one moment the list is consulted, so an
        # edit takes effect on the next dispatch and only then.
        if kind == "run":
            self._maybe_reload_allowlist()
            if not self._allowlisted(args.get("argv")):
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
        self._send_frame(sock, {
            "type": "result", "job": job, "payload": result,
            "ledger_b64": ledger_b64})
        self._log(f"job {job} ({kind}) executed — result booked "
                  f"and sent")

    def _send_error(self, sock, job, error: str) -> None:
        self._log(f"refused job {job}: {error}")
        try:
            self._send_frame(sock, {
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
                         "(default: <state>/allowlist.json if present); "
                         "the file is hot-reloaded on edit while the "
                         "agent runs")
    ap.add_argument("--heartbeat-s", type=float, default=15.0,
                    help="send a signed heartbeat after this many "
                         "seconds of send-silence (default 15; "
                         "0 disables)")
    args = ap.parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    allowlist_path = args.allowlist
    if allowlist_path is None:
        candidate = Path(args.state) / "allowlist.json"
        allowlist_path = str(candidate) if candidate.exists() else None
    agent = Agent(config, args.state,
                  allowlist=load_allowlist(allowlist_path),
                  allowlist_path=allowlist_path,
                  heartbeat_s=args.heartbeat_s)
    print(f"agent {agent.agent_id} -> controller "
          f"{agent.host}:{agent.port}", flush=True)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        agent.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
