"""
fleet.common — shared plumbing: framing, canonical JSON, key helpers,
state layout, and the vendored-stitch import shim.

Everything here is Python stdlib plus the vendored stitch snapshot
(see vendor/VENDOR.md). No other dependencies exist in this system.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

# --- vendored stitch ---------------------------------------------------
# The snapshot lives at fleet/vendor/stitch (a package directory).
# Putting fleet/vendor on sys.path makes `import stitch` resolve to
# the snapshot regardless of the caller's working directory.
_VENDOR = Path(__file__).resolve().parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

import stitch  # noqa: E402  (vendored v0.4.1 — see vendor/VENDOR.md)
from stitch.crypto.ed25519 import Key, verify  # noqa: E402

__all__ = [
    "stitch", "Key", "CONTROLLER_ID", "DEFAULT_PORT", "MAX_FRAME",
    "canonical_json", "send_frame", "recv_frame", "sign_frame",
    "verify_frame", "Paths", "read_json", "write_json", "append_booking",
]

#: Tenant identity the controller books under in its stitch ledger.
CONTROLLER_ID = "controller"

#: Default controller port (matches upstream BasicC2's 5000).
DEFAULT_PORT = 5000

#: Frame size cap. Frames carry agent ledgers, which are small; the
#: cap exists so a garbage length prefix can't make a reader wait for
#: gigabytes that will never arrive.
MAX_FRAME = 64 * 1024 * 1024

_FRAME = struct.Struct(">I")  # 4-byte big-endian length prefix


# --- canonical JSON ------------------------------------------------------

def canonical_json(obj) -> bytes:
    """
    The one JSON encoding used on the wire and in signatures: sorted
    keys, tight separators, raw UTF-8 — the same canonical form stitch
    uses for its payload hashes (the v0.3 byte-exactness pin), so a
    frame's signature and a booking's payload hash never disagree
    about what the bytes were.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# --- framing ---------------------------------------------------------------

def send_frame(sock, obj) -> None:
    """Send one length-prefixed JSON frame."""
    data = canonical_json(obj)
    sock.sendall(_FRAME.pack(len(data)) + data)


def _recv_exact(sock, n: int):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None  # peer closed
        buf += chunk
    return buf


def recv_frame(sock):
    """
    Receive one length-prefixed JSON frame. Returns the decoded object,
    or None when the peer closed the connection (cleanly or mid-frame).
    Raises ValueError on an over-cap frame.
    """
    header = _recv_exact(sock, _FRAME.size)
    if header is None:
        return None
    (n,) = _FRAME.unpack(header)
    if n > MAX_FRAME:
        raise ValueError(f"frame length {n} exceeds cap {MAX_FRAME}")
    body = _recv_exact(sock, n)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


# --- frame signatures --------------------------------------------------------

def sign_frame(key: Key, frame: dict) -> dict:
    """Return a copy of `frame` with a "sig" field added (hex)."""
    out = dict(frame)
    out["sig"] = key.sign(canonical_json(frame)).hex()
    return out


def verify_frame(pubkey: bytes, frame) -> bool:
    """
    Verify a frame's "sig" against `pubkey` (32 raw bytes). The signed
    material is the canonical JSON of the frame without "sig".
    Missing/malformed signature -> False, never an exception.
    """
    if not isinstance(frame, dict):
        return False
    sig_hex = frame.get("sig")
    if not isinstance(sig_hex, str):
        return False
    try:
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    body = {k: v for k, v in frame.items() if k != "sig"}
    return verify(pubkey, canonical_json(body), sig)


# --- state layout ------------------------------------------------------------

class Paths:
    """
    The desk state directory layout. Everything the controller, desk
    CLI, and tests need to find is derived from one root, so a state
    dir is a self-contained operation: copy it and you copied the
    desk (keys, registry, queue, and both sides' records).
    """

    def __init__(self, root):
        self.root = Path(root)

    @property
    def controller_file(self) -> Path:
        return self.root / "controller.json"

    @property
    def registry_file(self) -> Path:
        return self.root / "agents.json"

    @property
    def online_file(self) -> Path:
        return self.root / "online.json"

    @property
    def inbox_dir(self) -> Path:
        return self.root / "inbox"

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def ledgers_dir(self) -> Path:
        return self.root / "ledgers"

    @property
    def agent_ledgers_dir(self) -> Path:
        return self.root / "agent-ledgers"

    @property
    def enroll_dir(self) -> Path:
        return self.root / "enroll"

    @property
    def controller_ledger(self) -> Path:
        return self.ledgers_dir / "controller.jsonl"

    def agent_ledger_copy(self, agent_id: str) -> Path:
        return self.agent_ledgers_dir / f"{agent_id}.jsonl"

    def inbox_file(self, job_id: str) -> Path:
        return self.inbox_dir / f"{job_id}.json"

    def job_file(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def bundle_file(self, agent_id: str) -> Path:
        return self.enroll_dir / f"{agent_id}.json"

    def ensure(self) -> None:
        for d in (self.root, self.inbox_dir, self.jobs_dir,
                  self.ledgers_dir, self.agent_ledgers_dir,
                  self.enroll_dir):
            d.mkdir(parents=True, exist_ok=True)


# --- small file helpers ------------------------------------------------------

def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    """Write JSON via a temp file + rename, so a concurrent reader
    never sees a half-written state file."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


# --- stitch booking helper -----------------------------------------------------

def append_booking(ledger_path: Path, identity: str, book_fn, *,
                   xchg: str, peer: str, payload, secret_key: bytes):
    """
    Open a ledger, append one signed booking, close it. Callers hold
    whatever lock their threading model needs (the controller has one;
    the agent is single-threaded per connection). Opening per booking
    keeps the file handle lifecycle trivially safe across threads and
    processes — the ledger format is append-only, so this loses
    nothing.
    """
    ledger = stitch.Ledger(path=str(ledger_path))
    try:
        return book_fn(stitch.Tenant(identity=identity), ledger,
                       xchg=xchg, peer=peer, payload=payload,
                       secret_key=secret_key)
    finally:
        ledger.close()
