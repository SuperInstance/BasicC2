# BasicC2, adapted: a fleet tasking system with receipts

A fork of [0vm/BasicC2](https://github.com/0vm/BasicC2) (MIT),
rebuilt from a broadcast-shell toy into a small **tasking system
for your own machines**: send jobs to enrolled agents, and get
back *proof* the work happened — not a process claiming it did.

The original's one good idea is kept: agents connect **out** to
the controller and hold the line, so boxes behind NAT, WSL, and
home routers all work. Everything else was replaced:

| upstream | here |
|---|---|
| XOR + one shared secret | per-agent **Ed25519** keys, issued at enrollment; unknown keys get a closed socket |
| broadcast raw shell to everyone | named jobs to named agents; `run` is shell-free argv gated by a **per-agent allowlist** |
| `recv(1024)`, no framing | length-prefixed JSON frames |
| results printed and gone | results **booked into two ledgers**; completion is derived, not reported |

## The receipt idea

Every job is two exchanges in [stitch](https://github.com/SuperInstance/stitch)
ledgers (vendored, `fleet/vendor/`):

- **`:task`** — the controller books SEND, the agent books RECEIVE
  of the identical payload. Agreement *mints* "delivered intact."
- **`:result`** — the agent books SEND of its result, the
  controller books RECEIVE of what actually arrived. Agreement
  mints "the result on record is the result the agent produced."

`desk derive <job_id>` re-checks both exchanges plus both ledger
audits, from the stored records alone, any time later. A job the
desk cannot derive did not — as far as the record is concerned —
happen. Failures mint too: a job that exits 127 is recorded at
exit 127, signed by both sides. The record keeps what occurred,
not what anyone hoped.

## Quick start

Full operator guide: [README-OPS.md](./README-OPS.md).
Design record, playtests, and honest limits: [DESIGN.md](./DESIGN.md).

```
python3 -m fleet.desk --state ./ops-state init --host 0.0.0.0 --port 21212
python3 -m fleet.controller --state ./ops-state            # long-running
python3 -m fleet.desk --state ./ops-state enroll thor --host <controller-ip> --port 21212
# carry ops-state/enroll/thor.json to the box, write its allowlist.json, then there:
python3 -m fleet.agent --config thor.json --state ./agent-state --allowlist allowlist.json

python3 -m fleet.desk --state ./ops-state send --target all --kind ping
python3 -m fleet.desk --state ./ops-state derive <job_id>  # the receipt
```

Python 3 stdlib only. No installs.

## Status: v1, live on a real fleet

Deployed 2026-10-27 on a three-box home fleet (controller on the
hub box; agents on all three). First useful work — a census run
*through the system*, every row derived from minted jobs:

| | Thor | Orin | ProArt (WSL) |
|---|---|---|---|
| kernel | tegra 6.8 | tegra 6.8 | WSL2 |
| load avg | 5.5–8.9 (working) | 0.19 | 0.25 |
| memory used | 81 / 126 GB | 4.1 / 7.5 GB | 9 / 16 GB |
| root disk used | 69% | 5% | 9% |
| GPU job | exit 0 | exit 0 | exit 0 (via WSL driver path) |

**Known limits (found in playtests, recorded in DESIGN.md):**
no heartbeat reaping yet — a half-dead agent registration can
look ONLINE until a job goes missing; agents read their allowlist
once at startup, so allowlist edits need an agent restart;
controller is LAN-scoped (an internet relay is future work);
agents run as plain processes, not system services.

## Provenance

Forked from 0vm/BasicC2 under MIT (see `LICENSE`); the upstream
files (`control_server.py`, `target_server.py`, `UDP/`) remain
untouched in-tree, and the original README is preserved as
[UPSTREAM.md](./UPSTREAM.md). The adaptation is the `fleet/`
package. Stitch is vendored from SuperInstance/stitch v0.4.1
under its own terms (`fleet/vendor/VENDOR.md`).
