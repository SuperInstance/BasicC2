"""
End-to-end loop tests for the fleet tasking system.

Everything runs in-process on ephemeral ports with temp state dirs:
a real Controller thread, a real Agent thread, real Ed25519 keys,
real stitch ledgers. The assertions that matter are the derive
assertions — a job counts as done only when `desk.derive` mints both
exchanges from the two ledger files, exactly as the operator will
run it.
"""

import hashlib
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from fleet import common, desk
from fleet.agent import Agent
from fleet.common import Key, Paths
from fleet.controller import Controller


def wait_for(predicate, timeout=15.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class Rig:
    """One desk state dir + one controller + enrolled agent 'thor'."""

    def __init__(self, tmp_path: Path):
        self.paths = Paths(tmp_path / "ops-state")
        desk.cmd_init(self.paths, "127.0.0.1", 0)
        self.controller = Controller(self.paths.root,
                                     host="127.0.0.1", port=0)
        self.controller.start()
        self.bundle_path = desk.cmd_enroll(
            self.paths, "thor", host="127.0.0.1",
            port=self.controller.port)
        self.agent_state = tmp_path / "agent-state"
        self.agents = []

    def start_agent(self, allowlist) -> Agent:
        config = json.loads(self.bundle_path.read_text())
        agent = Agent(config, self.agent_state, allowlist=allowlist)
        t = threading.Thread(target=agent.run_forever, daemon=True)
        t.start()
        self.agents.append(agent)
        wait_for(lambda: "thor" in (common.read_json(
            self.paths.online_file, {}) or {}),
            what="agent thor to come online")
        return agent

    def job(self, job_id):
        return common.read_json(self.paths.job_file(job_id), {}) or {}

    def wait_job_status(self, job_id, status):
        wait_for(lambda: self.job(job_id).get("status") == status,
                 what=f"job {job_id} to reach status {status} "
                      f"(last seen: {self.job(job_id)})")

    def agent_ledger_text(self) -> str:
        p = self.agent_state / "thor.jsonl"
        return p.read_text() if p.exists() else ""

    def close(self):
        for a in self.agents:
            a.stop()
        self.controller.stop()


@pytest.fixture()
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


# --- the happy paths: jobs that must mint ------------------------------------

def test_ping_job_mints_both_exchanges(rig):
    rig.start_agent(allowlist=[])
    job_id = desk.cmd_send(rig.paths, "thor", "ping", {})
    rig.wait_job_status(job_id, "done")

    result = rig.job(job_id)["results"]["thor"]
    assert result["hostname"]
    assert result["python"]

    rows, all_minted = desk.derive(rig.paths, job_id)
    assert all_minted, rows
    assert rows[0]["task"] == "MINTED"
    assert rows[0]["result"] == "MINTED"
    assert rows[0]["audit_ok"]


def test_allowlisted_run_job(rig):
    rig.start_agent(allowlist=[["echo"]])
    job_id = desk.cmd_send(rig.paths, "thor", "run",
                           {"argv": ["echo", "hi"]})
    rig.wait_job_status(job_id, "done")

    result = rig.job(job_id)["results"]["thor"]
    assert result["exit"] == 0
    assert result["stdout_sha256"] == hashlib.sha256(b"hi\n").hexdigest()
    assert result["stdout_preview"] == "hi\n"

    rows, all_minted = desk.derive(rig.paths, job_id)
    assert all_minted, rows


# --- refusals: nothing executed, nothing booked --------------------------------

def test_non_allowlisted_run_is_refused_and_books_nothing(rig):
    rig.start_agent(allowlist=[["echo"]])
    job_id = desk.cmd_send(rig.paths, "thor", "run",
                           {"argv": ["rm", "-rf", "/tmp/nope"]})
    rig.wait_job_status(job_id, "refused")
    assert rig.job(job_id)["refused"]["thor"] == "not_allowlisted"

    # Refusals write nothing: the agent's ledger has no booking for
    # this job, and derive cannot mint either exchange.
    assert job_id not in rig.agent_ledger_text()
    rows, all_minted = desk.derive(rig.paths, job_id)
    assert not all_minted
    assert rows[0]["task"] == "NOT-MINTED"
    assert rows[0]["result"] == "NOT-MINTED"


def test_unenrolled_agent_handshake_is_closed(rig):
    sock = socket.create_connection(("127.0.0.1", rig.controller.port),
                                    timeout=10)
    try:
        common.send_frame(sock, {"type": "hello", "agent": "ghost"})
        # No challenge, no welcome: the controller just closes.
        assert common.recv_frame(sock) is None
    finally:
        sock.close()


def test_tampered_task_signature_is_refused(tmp_path):
    """A fake controller that cannot sign as the real one gets nothing:
    an error frame, no execution, no booking — and the same agent then
    serves a correctly signed task, proving the refusal was about the
    signature, not a wedged agent."""
    # Real desk state, but the agent will be pointed at a fake listener.
    paths = Paths(tmp_path / "ops-state")
    desk.cmd_init(paths, "127.0.0.1", 0)
    bundle_path = desk.cmd_enroll(paths, "thor", host="127.0.0.1", port=1)
    config = json.loads(bundle_path.read_text())
    controller_cfg = common.read_json(paths.controller_file)
    real_controller_key = Key.from_secret(
        bytes.fromhex(controller_cfg["secret_hex"]))

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    fake_port = listener.getsockname()[1]
    config["controller"] = {"host": "127.0.0.1", "port": fake_port}

    agent_state = tmp_path / "agent-state"
    agent = Agent(config, agent_state, allowlist=[["echo"]])
    t = threading.Thread(target=agent.run_forever, daemon=True)
    t.start()
    try:
        conn, _ = listener.accept()
        conn.settimeout(10)
        # Handshake: accept the agent's proof without checking (we are
        # a fake; the agent authenticates US only via frame signatures).
        hello = common.recv_frame(conn)
        assert hello["agent"] == "thor"
        import os
        nonce = os.urandom(32)
        common.send_frame(conn, {"type": "challenge",
                                 "nonce": nonce.hex()})
        auth = common.recv_frame(conn)
        assert auth["type"] == "auth"
        common.send_frame(conn, {"type": "welcome", "agent": "thor"})

        # Tampered task: signed by the real controller key, then the
        # payload is altered — the signature no longer covers it.
        payload = {"job": "job-evil-1", "kind": "run",
                   "args": {"argv": ["echo", "pwned"]}, "target": "thor"}
        frame = common.sign_frame(real_controller_key, {
            "type": "task", "job": "job-evil-1", "payload": payload})
        frame["payload"] = {**payload, "args": {"argv": ["echo",
                                                         "tampered"]}}
        common.send_frame(conn, frame)
        reply = common.recv_frame(conn)
        assert reply["type"] == "error"
        assert reply["error"] == "bad_signature"
        assert reply["job"] == "job-evil-1"

        # Correctly signed task on the same connection: served.
        good_payload = {"job": "job-good-1", "kind": "ping",
                        "args": {}, "target": "thor"}
        common.send_frame(conn, common.sign_frame(real_controller_key, {
            "type": "task", "job": "job-good-1", "payload": good_payload}))
        result = common.recv_frame(conn)
        assert result["type"] == "result"
        assert result["job"] == "job-good-1"
        assert result["payload"]["hostname"]

        # The ledger proves the split: the good job is booked, the
        # tampered one left no trace at all.
        ledger_text = (agent_state / "thor.jsonl").read_text()
        assert "job-good-1" in ledger_text
        assert "job-evil-1" not in ledger_text
        conn.close()
    finally:
        agent.stop()
        listener.close()
