"""
v1.1 hardening tests: heartbeat + reaping, zombie replacement,
allowlist hot-reload.

Same style as test_loop.py — in-process, ephemeral ports, temp
state, real keys and ledgers — but with the liveness intervals
shrunk through the CLI-shaped knobs (Controller(reap_s=...),
Agent(heartbeat_s=...)) so the tests run in seconds.

The zombie simulations use a raw socket that performs a real
handshake with the enrolled key and then simply holds the
connection: that is exactly what a half-dead agent looks like from
the controller's side — a valid registration that never speaks.
"""

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from fleet import common, desk
from fleet.agent import Agent, load_allowlist
from fleet.common import Key, Paths
from fleet.controller import Controller


def wait_for(predicate, timeout=15.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class Rig11:
    def __init__(self, tmp_path: Path, reap_s=45.0):
        self.paths = Paths(tmp_path / "ops-state")
        desk.cmd_init(self.paths, "127.0.0.1", 0)
        self.controller = Controller(self.paths.root, host="127.0.0.1",
                                     port=0, reap_s=reap_s)
        self.controller.start()
        self.bundle_path = desk.cmd_enroll(
            self.paths, "thor", host="127.0.0.1",
            port=self.controller.port)
        self.agent_state = tmp_path / "agent-state"
        self.agents = []

    def config(self):
        return json.loads(self.bundle_path.read_text())

    def start_agent(self, **kwargs) -> Agent:
        agent = Agent(self.config(), self.agent_state, **kwargs)
        t = threading.Thread(target=agent.run_forever, daemon=True)
        t.start()
        self.agents.append(agent)
        return agent

    def wait_online(self, agent_id="thor"):
        wait_for(lambda: agent_id in (common.read_json(
            self.paths.online_file, {}) or {}),
            what=f"agent {agent_id} to come online")

    def job(self, job_id):
        return common.read_json(self.paths.job_file(job_id), {}) or {}

    def wait_job_status(self, job_id, status):
        wait_for(lambda: self.job(job_id).get("status") == status,
                 what=f"job {job_id} to reach status {status} "
                      f"(last seen: {self.job(job_id)})")

    def raw_handshake(self) -> socket.socket:
        """Connect and authenticate as thor, then hold the socket
        without speaking — a zombie registration, honestly built."""
        cfg = self.config()
        key = Key.from_secret(bytes.fromhex(cfg["secret_hex"]))
        sock = socket.create_connection(("127.0.0.1",
                                         self.controller.port),
                                        timeout=10)
        sock.settimeout(10)
        common.send_frame(sock, {"type": "hello", "agent": "thor"})
        challenge = common.recv_frame(sock)
        assert challenge and challenge["type"] == "challenge"
        nonce = bytes.fromhex(challenge["nonce"])
        common.send_frame(sock, {"type": "auth",
                                 "sig": key.sign(nonce).hex()})
        welcome = common.recv_frame(sock)
        assert welcome and welcome["type"] == "welcome"
        return sock

    def close(self):
        for a in self.agents:
            a.stop()
        self.controller.stop()


@pytest.fixture()
def rig11(tmp_path):
    r = Rig11(tmp_path)
    yield r
    r.close()


# --- (a) zombie replacement ----------------------------------------------------

def test_newest_connection_wins_and_job_uses_live_one(rig11):
    zombie = rig11.raw_handshake()
    rig11.wait_online()
    try:
        # The real agent connects with the same identity. Newest
        # wins: the controller must close the zombie's socket.
        rig11.start_agent(allowlist=[["echo"]])
        zombie.settimeout(10)
        assert common.recv_frame(zombie) is None, \
            "controller did not close the displaced zombie socket"

        # And the job goes to the live connection exactly once and
        # completes end-to-end, minted as always.
        job_id = desk.cmd_send(rig11.paths, "thor", "run",
                               {"argv": ["echo", "alive"]})
        rig11.wait_job_status(job_id, "done")
        assert rig11.job(job_id)["dispatched"] == ["thor"]
        assert rig11.job(job_id)["results"]["thor"]["exit"] == 0
        rows, all_minted = desk.derive(rig11.paths, job_id)
        assert all_minted, rows
    finally:
        zombie.close()


# --- (b) reaping -----------------------------------------------------------------

def test_silent_connection_is_reaped(tmp_path):
    rig = Rig11(tmp_path, reap_s=1.0)
    try:
        ghost = rig.raw_handshake()  # valid agent, then total silence
        rig.wait_online()
        # Inside the reap window the registration must vanish from
        # online.json and the controller must close the socket.
        wait_for(lambda: "thor" not in (common.read_json(
            rig.paths.online_file, {}) or {}),
            timeout=10.0, what="silent agent to be reaped")
        ghost.settimeout(10)
        assert common.recv_frame(ghost) is None, \
            "reaped connection was not closed by the controller"
        ghost.close()
    finally:
        rig.close()


def test_heartbeating_agent_is_not_reaped(tmp_path):
    """The complement of reaping: an agent that heartbeats inside
    the budget stays registered indefinitely while idle."""
    rig = Rig11(tmp_path, reap_s=2.0)
    try:
        rig.start_agent(allowlist=[], heartbeat_s=0.3)
        rig.wait_online()
        # Several reap windows pass with no jobs at all.
        time.sleep(5.0)
        assert "thor" in (common.read_json(rig.paths.online_file,
                                           {}) or {})
        job_id = desk.cmd_send(rig.paths, "thor", "ping", {})
        rig.wait_job_status(job_id, "done")
    finally:
        rig.close()


# --- (c) allowlist hot-reload ------------------------------------------------------

def _write_allowlist(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    # Force a distinct, increasing mtime even on filesystems with
    # coarse timestamps — the reload keys on mtime alone.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_allowlist_hot_reload_without_restart(tmp_path):
    rig = Rig11(tmp_path)
    try:
        allowlist_file = tmp_path / "allowlist.json"
        _write_allowlist(allowlist_file, '[["echo"]]')
        agent = rig.start_agent(
            allowlist=load_allowlist(allowlist_file),
            allowlist_path=allowlist_file)
        rig.wait_online()

        # hostname is not allowlisted yet: refused, nothing booked.
        job1 = desk.cmd_send(rig.paths, "thor", "run",
                             {"argv": ["hostname"]})
        rig.wait_job_status(job1, "refused")
        assert rig.job(job1)["refused"]["thor"] == "not_allowlisted"

        # Edit the file — NO agent restart — the same job now runs.
        _write_allowlist(allowlist_file, '[["echo"], ["hostname"]]')
        job2 = desk.cmd_send(rig.paths, "thor", "run",
                             {"argv": ["hostname"]})
        rig.wait_job_status(job2, "done")
        assert rig.job(job2)["results"]["thor"]["exit"] == 0
        rows, all_minted = desk.derive(rig.paths, job2)
        assert all_minted, rows

        # A malformed edit keeps the previous list in force: echo
        # (allowed before and after) still runs, hostname (added by
        # the last good edit) still runs, and nothing widened.
        _write_allowlist(allowlist_file, "{ this is not json")
        job3 = desk.cmd_send(rig.paths, "thor", "run",
                             {"argv": ["echo", "still-here"]})
        rig.wait_job_status(job3, "done")
        job4 = desk.cmd_send(rig.paths, "thor", "run",
                             {"argv": ["hostname"]})
        rig.wait_job_status(job4, "done")
        # The agent that served all four jobs is the one we started.
        assert rig.agents == [agent]
        assert agent.allowlist == [["echo"], ["hostname"]]
    finally:
        rig.close()
