"""
fleet.controller — the desk-side daemon.

Kept from BasicC2: a threaded TCP listener; agents connect out to it
and hold the connection. Replaced: everything else (see DESIGN.md).

Responsibilities:
  - Handshake: identify the agent against the enrollment registry and
    prove it holds the enrolled secret key (nonce signature).
  - Liveness (v1.1): track last-seen per connection from any frame
    (heartbeats included); a reaper thread closes connections silent
    longer than reap_s and removes them from online.json. A second
    handshake for an agent_id that already has a connection closes
    the older socket — newest wins, so a half-dead "zombie"
    registration cannot shadow the live agent.
  - Dispatcher: watch state/inbox/ for job files written by
    `desk send`; book each dispatch into the controller's stitch
    ledger (signed SEND of the :task exchange) and push a signed task
    frame to the target agent.
  - Result intake: on a result frame, file the agent's ledger copy,
    book the mirror RECEIVE of the :result exchange for exactly what
    arrived, and update the job's status file.

The controller never executes anything and never takes a job's word
for completion — `desk derive` re-derives completion from the two
ledgers at any time.
"""

from __future__ import annotations

import base64
import binascii
import os
import socket
import threading
import time
from datetime import datetime, timezone

from . import common
from .common import Key, Paths, stitch

DISPATCH_POLL_S = 0.25


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Controller:
    def __init__(self, state_dir, host=None, port=None, reap_s=45.0):
        self.paths = Paths(state_dir)
        self.paths.ensure()
        cfg = common.read_json(self.paths.controller_file)
        if cfg is None:
            raise SystemExit(
                f"no controller identity in {self.paths.root} — "
                f"run `desk init` first")
        self.key = Key.from_secret(bytes.fromhex(cfg["secret_hex"]))
        self.host = host if host is not None else cfg.get("host", "0.0.0.0")
        self.port = port if port is not None else cfg.get(
            "port", common.DEFAULT_PORT)
        #: Silence budget per connection, in seconds (v1.1). An agent
        #: heartbeats well inside this window; a connection silent
        #: longer is reaped. Deliberately generous next to the agent's
        #: default 15 s heartbeat: reaping is for the dead, not the
        #: slow.
        self.reap_s = float(reap_s)

        self._lock = threading.Lock()         # connections + online map
        self._ledger_lock = threading.Lock()  # controller ledger appends
        self._jobs_lock = threading.Lock()    # job status files
        self.connections: dict = {}           # agent_id -> socket
        self.online: dict = {}                # agent_id -> info dict
        self.last_seen: dict = {}             # agent_id -> monotonic ts
        self._stop = threading.Event()
        self._srv = None
        self._threads = []

    def _log(self, msg: str) -> None:
        common.log("controller", msg)

    # -- registry --------------------------------------------------------

    def _registry(self) -> dict:
        # Re-read from disk each time: `desk enroll` may add agents
        # while the controller is running.
        return common.read_json(self.paths.registry_file, {}) or {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(16)
        srv.settimeout(0.5)  # so the accept loop notices _stop
        self._srv = srv
        self.port = srv.getsockname()[1]  # resolves port=0 for tests
        for target in (self._accept_loop, self._dispatcher_loop,
                     self._reaper_loop):
            t = threading.Thread(target=target, daemon=True,
                                 name=target.__name__)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self.connections.values())
            self.connections.clear()
            self.online.clear()
            self.last_seen.clear()
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
        self._write_online()

    # -- connection handling -------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # listener closed by stop()
            t = threading.Thread(target=self._serve_conn, args=(conn, addr),
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def _serve_conn(self, conn: socket.socket, addr) -> None:
        agent_id = None
        try:
            agent_id, pubkey = self._handshake(conn)
            if agent_id is None:
                return  # refused; socket closed in finally
            with self._lock:
                old = self.connections.get(agent_id)
                self.connections[agent_id] = conn
                self.online[agent_id] = {
                    "addr": f"{addr[0]}:{addr[1]}", "since": _utc_now()}
                self.last_seen[agent_id] = time.monotonic()
            if old is not None and old is not conn:
                # Newest connection wins (v1.1): an agent that
                # handshakes again has, by definition, replaced its
                # older registration — the classic half-dead zombie
                # that in v1 silently swallowed dispatches while
                # looking ONLINE. Closing the old socket also ends
                # its serve thread, whose cleanup leaves the new
                # registration alone (identity check below).
                self._log(f"agent {agent_id} reconnected from "
                          f"{addr[0]}:{addr[1]} — closing its older "
                          f"connection (zombie replacement)")
                try:
                    old.close()
                except OSError:
                    pass
            else:
                self._log(f"agent {agent_id} connected from "
                          f"{addr[0]}:{addr[1]}")
            self._write_online()
            while not self._stop.is_set():
                frame = common.recv_frame(conn)
                if frame is None:
                    break
                with self._lock:
                    if self.connections.get(agent_id) is conn:
                        self.last_seen[agent_id] = time.monotonic()
                ftype = frame.get("type")
                if ftype == "result":
                    self._handle_result(agent_id, frame)
                elif ftype == "error":
                    self._handle_error(agent_id, frame)
                elif ftype == "heartbeat":
                    # Signed liveness only — an unsigned or badly
                    # signed heartbeat is not proof of the enrolled
                    # agent, so the connection is dropped (and the
                    # real agent reconnects). Heartbeats are never
                    # booked: they are transport, not record.
                    if not common.verify_frame(pubkey, frame):
                        self._log(f"agent {agent_id}: bad heartbeat "
                                  f"signature — closing connection")
                        break
                # unknown frame types are ignored
        except (OSError, ValueError):
            pass  # dropped/reset/oversized frame — connection is over
        finally:
            try:
                conn.close()
            except OSError:
                pass
            if agent_id is not None:
                with self._lock:
                    # Only clear if this socket is still the live one.
                    if self.connections.get(agent_id) is conn:
                        self.connections.pop(agent_id, None)
                        self.online.pop(agent_id, None)
                        self.last_seen.pop(agent_id, None)
                        was_live = True
                    else:
                        was_live = False
                if was_live:
                    self._log(f"agent {agent_id} disconnected")
                self._write_online()

    def _handshake(self, conn: socket.socket):
        """
        Enrollment is the only way in. Returns (agent_id, pubkey) on
        success, (None, None) — and the caller closes — on any
        failure: unknown identity, missing frames, or a nonce
        signature that doesn't verify against the registered public
        key. The pubkey rides along so the serve loop can verify
        heartbeat signatures without re-reading the registry.
        """
        hello = common.recv_frame(conn)
        if not hello or hello.get("type") != "hello":
            return None, None
        agent_id = hello.get("agent")
        entry = self._registry().get(agent_id) if isinstance(
            agent_id, str) else None
        if entry is None:
            return None, None  # unenrolled — no challenge, no oracle
        pubkey = bytes.fromhex(entry["pubkey_hex"])
        nonce = os.urandom(32)
        common.send_frame(conn, {"type": "challenge", "nonce": nonce.hex()})
        auth = common.recv_frame(conn)
        if not auth or auth.get("type") != "auth":
            return None, None
        try:
            sig = bytes.fromhex(auth.get("sig", ""))
        except ValueError:
            return None, None
        from stitch.crypto.ed25519 import verify
        if not verify(pubkey, nonce, sig):
            return None, None
        common.send_frame(conn, {"type": "welcome", "agent": agent_id})
        return agent_id, pubkey

    # -- liveness: reaping -------------------------------------------------

    def _reaper_loop(self) -> None:
        """
        Close connections that have sent nothing — task traffic,
        results, or signed heartbeats — for longer than reap_s, and
        remove them from online.json (v1.1; closes the root of
        playtest finding F10). This is what makes a half-dead
        registration stop looking ONLINE: silence is now a state the
        controller acts on, not a state it preserves.
        """
        tick = min(1.0, max(0.05, self.reap_s / 3.0))
        while not self._stop.is_set():
            self._stop.wait(tick)
            if self._stop.is_set():
                break
            now = time.monotonic()
            dead = []
            with self._lock:
                for agent_id, conn in list(self.connections.items()):
                    seen = self.last_seen.get(agent_id)
                    if seen is not None and now - seen > self.reap_s:
                        dead.append((agent_id, conn, now - seen))
                        self.connections.pop(agent_id, None)
                        self.online.pop(agent_id, None)
                        self.last_seen.pop(agent_id, None)
            for agent_id, conn, silent_for in dead:
                self._log(f"agent {agent_id} reaped: silent for "
                          f"{silent_for:.1f}s (budget {self.reap_s:g}s) "
                          f"— connection closed, removed from online")
                try:
                    conn.close()
                except OSError:
                    pass
            if dead:
                self._write_online()

    def _write_online(self) -> None:
        with self._lock:
            snapshot = dict(self.online)
        common.write_json(self.paths.online_file, snapshot)

    # -- dispatch --------------------------------------------------------------

    def _dispatcher_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._dispatch_pending()
            except Exception:
                # A malformed inbox file must not kill dispatch for
                # everyone else; the loop is the fleet's heartbeat.
                pass
            self._stop.wait(DISPATCH_POLL_S)

    def _dispatch_pending(self) -> None:
        for inbox_path in sorted(self.paths.inbox_dir.glob("*.json")):
            spec = common.read_json(inbox_path)
            if not spec or "job_id" not in spec:
                continue
            job = self._read_job(spec["job_id"])
            dispatched = set(job.get("dispatched", [])) if job else set()
            if spec.get("target") == "all":
                targets = sorted(self._registry().keys())
            else:
                targets = [spec.get("target")]
            for agent_id in targets:
                if agent_id in dispatched:
                    continue
                with self._lock:
                    conn = self.connections.get(agent_id)
                if conn is None:
                    continue  # agent offline; dispatch when it connects
                if self._dispatch_one(spec, agent_id, conn):
                    dispatched.add(agent_id)

    def _dispatch_one(self, spec: dict, agent_id: str,
                      conn: socket.socket) -> bool:
        job_id = spec["job_id"]
        # The payload is per-delivery: target names this agent even
        # when the job's target is "all", because this booking and
        # this frame are the exchange with this agent.
        task_payload = {
            "job": job_id,
            "kind": spec["kind"],
            "args": spec.get("args") or {},
            "target": agent_id,
        }
        # Book first, then send: the SEND booking is the desk's
        # commitment, and it must exist even if the send then fails
        # (a booked-but-undelivered task derives as NOT-MINTED, which
        # is the honest record of a failed dispatch).
        with self._ledger_lock:
            common.append_booking(
                self.paths.controller_ledger, common.CONTROLLER_ID,
                stitch.book_send, xchg=f"{job_id}:task", peer=agent_id,
                payload=task_payload,
                secret_key=self.key.secret_bytes)
        frame = common.sign_frame(self.key, {
            "type": "task", "job": job_id, "payload": task_payload})
        try:
            common.send_frame(conn, frame)
        except OSError:
            self._log(f"dispatch of {job_id} to {agent_id} failed: "
                      f"connection lost mid-send (SEND booking stands; "
                      f"the job will derive NOT-MINTED)")
            return False
        self._job_update(job_id, lambda j: self._job_mark(j, spec,
                                                          agent_id))
        self._log(f"dispatched {job_id} ({spec['kind']}) to {agent_id}")
        return True

    @staticmethod
    def _job_mark(job: dict, spec: dict, agent_id: str) -> dict:
        job.setdefault("job_id", spec["job_id"])
        job.setdefault("target", spec.get("target"))
        job.setdefault("kind", spec.get("kind"))
        job.setdefault("args", spec.get("args") or {})
        job.setdefault("results", {})
        job.setdefault("refused", {})
        dispatched = job.setdefault("dispatched", [])
        if agent_id not in dispatched:
            dispatched.append(agent_id)
        job["status"] = "dispatched"
        job["updated_utc"] = _utc_now()
        return job

    # -- result / error intake ---------------------------------------------------

    def _handle_result(self, agent_id: str, frame: dict) -> None:
        job_id = frame.get("job")
        payload = frame.get("payload")
        if not isinstance(job_id, str) or not isinstance(payload, dict):
            return
        # File the agent's ledger copy first: it is the record the
        # desk will derive from, and it must be the bytes the agent
        # actually sent, whatever happens next.
        ledger_b64 = frame.get("ledger_b64")
        if isinstance(ledger_b64, str):
            try:
                data = base64.b64decode(ledger_b64, validate=True)
                tmp = self.paths.agent_ledger_copy(agent_id).with_suffix(
                    ".jsonl.tmp")
                tmp.write_bytes(data)
                tmp.replace(self.paths.agent_ledger_copy(agent_id))
            except (binascii.Error, OSError):
                pass  # a corrupt copy simply won't mint at derive time
        # Book the mirror RECEIVE for exactly what arrived. If the
        # frame's payload differs from what the agent booked in its
        # own ledger, agree() will refuse with payload_mismatch —
        # which is the point of booking what we received, not what we
        # hoped for.
        with self._ledger_lock:
            common.append_booking(
                self.paths.controller_ledger, common.CONTROLLER_ID,
                stitch.book_receive, xchg=f"{job_id}:result",
                peer=agent_id, payload=payload,
                secret_key=self.key.secret_bytes)

        def mark(job: dict) -> dict:
            job.setdefault("results", {})[agent_id] = payload
            job.setdefault("refused", {})
            job.setdefault("dispatched", [])
            job["status"] = self._job_status(job)
            job["updated_utc"] = _utc_now()
            return job

        self._job_update(job_id, mark)
        self._log(f"result booked for {job_id} from {agent_id}")

    def _handle_error(self, agent_id: str, frame: dict) -> None:
        job_id = frame.get("job")
        if not isinstance(job_id, str):
            return
        self._log(f"refusal for {job_id} from {agent_id}: "
                  f"{frame.get('error', 'error')}")

        def mark(job: dict) -> dict:
            job.setdefault("results", {})
            job.setdefault("dispatched", [])
            job.setdefault("refused", {})[agent_id] = frame.get(
                "error", "error")
            job["status"] = self._job_status(job)
            job["updated_utc"] = _utc_now()
            return job

        self._job_update(job_id, mark)

    @staticmethod
    def _job_status(job: dict) -> str:
        dispatched = job.get("dispatched", [])
        results = job.get("results", {})
        refused = job.get("refused", {})
        settled = set(results) | set(refused)
        if dispatched and all(a in settled for a in dispatched):
            if results and refused:
                return "partial"
            return "done" if results else "refused"
        return "dispatched"

    # -- job status files ----------------------------------------------------------

    def _read_job(self, job_id: str):
        return common.read_json(self.paths.job_file(job_id))

    def _job_update(self, job_id: str, mutate) -> None:
        with self._jobs_lock:
            job = self._read_job(job_id) or {}
            job = mutate(job)
            common.write_json(self.paths.job_file(job_id), job)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="fleet.controller",
                                 description="Fleet controller daemon")
    ap.add_argument("--state", default="./ops-state")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--reap-s", type=float, default=45.0,
                    help="close connections silent this many seconds "
                         "(default 45; agents heartbeat at 15)")
    args = ap.parse_args(argv)
    ctl = Controller(args.state, host=args.host, port=args.port,
                     reap_s=args.reap_s)
    ctl.start()
    common.log("controller",
               f"listening on {ctl.host}:{ctl.port} "
               f"(state {ctl.paths.root}, reap after {ctl.reap_s:g}s "
               f"of silence)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        ctl.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
