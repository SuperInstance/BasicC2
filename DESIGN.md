# DESIGN — BasicC2, adapted into a fleet tasking system

Process record, written as the work happens. Upstream files
(`control_server.py`, `target_server.py`, `UDP/`) are kept untouched
for provenance: this is a fork in the literal sense — the original is
preserved, and everything new lives under `fleet/`.

## What this is

A v1 tasking system for Casey's own agent fleet: a controller (the
"desk") holds persistent connections from agents running on the fleet
boxes, dispatches named jobs to them, and — the part that makes it
more than a remote shell — proves completion afterwards from two
signed, append-only ledgers using the vendored stitch library.
Nothing about completion is taken on a live process's say-so: the
desk *derives* it from records (`desk derive`), the same discipline
as the Stitch War card game.

## Kept from BasicC2 (and why)

- **Agents connect OUT to the controller and hold the connection.**
  This topology is the original's one real insight, and it matches
  the house: boxes sit behind NAT/WSL/routers, so the reachable
  direction is outbound from the box to a hub — the same reason the
  fleet's reverse SSH tunnels work.
- **A threaded controller**, one thread per connection — adequate at
  fleet scale (a handful of boxes) and kept deliberately simple.
- **Stubborn reconnect** on the agent. Kept, but the fixed 5 s retry
  becomes exponential backoff (1 s → 30 s cap) with jitter, so a
  controller restart doesn't get stampeded in lockstep.

## Replaced (and why)

- **XOR + one shared secret → per-agent Ed25519 enrollment.** The
  upstream "encryption" is a repeating-key XOR with a passphrase that
  ships in the source. Replaced with real identity: the desk
  generates a keypair per agent (`desk enroll`), keeps a registry of
  public keys, and the agent proves itself by signing a random nonce
  at handshake. Unknown identity or bad signature = connection
  closed; there is no other way in. Task frames are signed by the
  controller's key and verified by the agent before it does anything.
- **Broadcast raw shell → named, allowlisted ops with a job
  lifecycle.** Upstream: type a string, every connected box runs it
  through `shell=True`. Here: a job is a file
  (`state/inbox/<job_id>.json`) naming a target agent (or `all`), a
  `kind` (`ping`, `run`), and `args`. `run` takes an `argv` list,
  executes it **without a shell**, and only if it matches an
  allowlist prefix from the agent's own allowlist file. Anything else
  is refused — and refusals write nothing: no execution, no ledger
  entry.
- **Printed results → stitch-minted receipts.** Upstream printed
  command output at the server and kept nothing. Here every dispatch
  and every result is booked into two sovereign stitch ledgers (see
  the two-mint model below); the agent ships its ledger back with
  each result so the desk holds the records and can re-derive any
  job's completion at any time.
- **No framing → length-prefixed frames.** Upstream did
  `recv(1024)` and hoped one read equals one message; TCP makes no
  such promise (commands truncate or coalesce). Every frame here is
  a 4-byte big-endian length prefix + UTF-8 JSON, with a size cap.

## The two-mint job model

A job completes in two exchanges, each minted independently by
`stitch.agree()` over the controller's ledger and the agent's stored
ledger, with `booking_keys` enforcing both parties' signatures:

1. **`<job_id>:task` — delivery.** Dispatch: the controller books
   `book_send(xchg=<job>:task, peer=<agent>, payload=task_payload,
   secret_key=controller_key)`. On receipt (after verifying the
   frame signature and the allowlist) the agent books the mirror
   `book_receive(xchg=<job>:task, peer="controller",
   payload=task_payload, secret_key=agent_key)`. Minted delivery
   means: the desk can prove it sent exactly this job, and the
   agent can prove it received exactly this job.
2. **`<job_id>:result` — completion.** After executing, the agent
   books `book_send(xchg=<job>:result, peer="controller",
   payload=result_payload, secret_key=agent_key)` and returns a
   result frame carrying `result_payload` **and its full ledger
   bytes**. The controller books the mirror `book_receive` for what
   it actually received and files the ledger copy under
   `state/agent-ledgers/<agent_id>.jsonl`. Minted completion means
   the result the desk holds is bit-for-bit the result the agent
   committed to in its own ledger — a result frame claiming anything
   else fails `agree()` with `payload_mismatch`.

`task_payload = {job, kind, args, target}` and
`result_payload = {argv, exit, stdout_sha256, stdout_preview}` for
`run`, `{hostname, platform, python, utc}` for `ping`. Payloads are
strings/ints only — no floats (stitch v0.4.1 refuses non-finite
floats at booking time, and avoiding floats entirely keeps hashes
boring).

`desk derive <job_id>` runs `agree()` with `booking_keys` for both
exchanges plus `audit_ledger()` on both ledgers, and prints
MINTED / NOT-MINTED per exchange with the audit verdict. That
command is the product: a job isn't done because a process said so;
it's done because two records agree.

## Topology

The controller runs on the LAN hub. Oracle public inbound is blocked
(measured 2026-10-27), so v1 does not pretend the controller is
internet-reachable; agents reach it over the LAN (or, later, over
the existing Oracle reverse-tunnel slots). An off-LAN relay is a
later option, deliberately not built here.

The file-based inbox (`state/inbox/`) is the control channel between
the desk CLI and the controller process: `desk send` writes a file,
the controller's dispatcher thread picks it up. No extra IPC, and
the queue is inspectable with `ls` — plain files as the interface,
like the rest of the fleet's tooling.

## Own-fleet posture

- **Enrollment is the only way in.** No discovery, no scanning, no
  spread mechanics of any kind. An agent exists because the desk
  generated its keypair and a human carried the bundle to the box.
- **No shell, ever.** Named ops; `run` is argv-only and gated by a
  per-agent allowlist the operator writes.
- **Secrets stay put.** Agent secret keys live only in the enroll
  bundle and the agent's own state dir; the controller's secret key
  lives in the desk state dir (0600). Ledger identities are the
  human names (`thor`, `controller`, …); the registry binds names
  to keys out of band, which is stitch's documented model.
- This system is for machines the operator owns. The enrollment gate
  is not a setting to relax; it's the shape of the thing.

## Layout

```
fleet/
  __init__.py
  common.py       framing, canonical JSON, key helpers, stitch shim
  controller.py   listener + handshake + dispatcher + result intake
  agent.py        connect-out agent: verify, book, execute, book, report
  desk.py         operator CLI: init / enroll / agents / send / jobs / derive
  vendor/         stitch v0.4.1 snapshot (see vendor/VENDOR.md)
  tests/test_loop.py
ops-state/        (default --state) controller keys, registry, inbox,
                  job status, controller ledger, agent ledger copies
```

## Verification (2026-10-27)

**Unit/integration tests** — `fleet/tests/test_loop.py`, 5 tests, all
in-process on ephemeral ports with temp state (real controller
thread, real agent thread, real keys, real ledgers):

```
.....                                                                    [100%]
5 passed in 1.09s
```

Coverage: ping job mints both exchanges with audits PASS;
allowlisted `run` (`echo hi`) exits 0 with the stdout hash checked;
non-allowlisted `run` is refused, books nothing, and derives
NOT-MINTED; an unenrolled hello is closed without a challenge; a
task frame whose payload was altered after signing is refused
(`bad_signature`, nothing booked, nothing executed) and the same
agent then serves a correctly signed task — the refusal is about
the signature, not a wedged agent.

One API fact was learned by failure during the build and is recorded
in `vendor/VENDOR.md`: stitch's `Key.public_bytes` / `secret_bytes`
/ `identity` are properties, not methods.

**Two-process smoke** — controller and agent as separate OS
processes on localhost (system Python, no test harness), one ping
job, derived afterwards:

```
--- desk agents ---
smokebox         ONLINE  127.0.0.1:54118 since 2026-10-28T02:43:38.740177+00:00 pubkey 9692a77dac0d43e6…
--- sent ping job: job-20261028T024340-cd8c24 ---
--- desk jobs ---
job-20261028T024340-cd8c24   target=smokebox   kind=ping  status=done results from: smokebox
--- desk derive job-20261028T024340-cd8c24 ---
derive job-20261028T024340-cd8c24 (from the ledgers alone):
  agent smokebox:
    audit:  controller PASS (2 entries), agent PASS (2 entries)
    job-20261028T024340-cd8c24:task   MINTED     (agreed)
    job-20261028T024340-cd8c24:result MINTED     (agreed)
  verdict: COMPLETE — both exchanges minted
derive exit: 0
```

**Known v1 limits, stated rather than smoothed over:** result frames
themselves are not transport-signed (their authenticity rests on the
signed bookings inside the agent ledger the frame carries — a frame
whose payload disagrees with the booking fails `derive`); the
controller books a result RECEIVE for whatever arrives on an
authenticated connection, so a compromised agent key can mint false
results *for its own jobs* — key custody is the security boundary,
as designed; target `all` dispatches only to agents that connect,
and a job whose target never connects stays `queued` forever (no
expiry in v1).

## Live deployment (2026-10-27, Muse — first real run)

The v1 system is **deployed and working** on the home fleet:

- **Controller** on the Thor (`~/fleet-ops`, bind 0.0.0.0:21212),
  pubkey `505ddc47…`. **Agents** enrolled and connected from all
  three boxes: thor `69a92c47…`, orin `64e23088…`, proart
  `851fcb94…` — each holding its own key, connecting out to the
  controller, exactly the topology in this document.
- **First fleet-wide jobs** (2026-10-28 ~03:06 UTC): `ping` to all
  (job-…eba8a6) and `run nvidia-smi` to all (job-…ab6000). Both
  completed from all three agents, and `desk derive` reports, per
  agent, **both exchanges MINTED and both audits PASS** (controller
  ledger 24 entries at derive time). The receipts were derived from
  the stored ledgers alone, after the fact — not from live claims.

Two honest findings from the run, recorded as v1 limits:

1. **Zombie registration.** The thor agent's first registration
   showed ONLINE but silently never processed dispatches (its
   ledger stayed at genesis while the controller booked SENDs into
   the void). Restarting the agent fixed it. v1 has no heartbeat
   reaping: a half-dead registration looks exactly like a live one
   until a job goes missing. The jobs record is what exposed it —
   status showed results from two agents, not three.
2. **The record keeps failures faithfully.** On the ProArt (WSL),
   `nvidia-smi` is not on the agent's PATH, so the run job minted
   with **exit 127** — delivery and result both agreed, recording
   a failure exactly as it happened. (thor and orin exited 0.)
   A receipt system that only mints successes would be worthless;
   this one mints what occurred.
