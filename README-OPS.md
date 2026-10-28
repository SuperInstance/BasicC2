# README-OPS — running the fleet tasking system

Operator's guide. Design rationale lives in `DESIGN.md`; this file
is just the commands. Everything is Python 3 stdlib — no installs —
run from the repo root so `python3 -m fleet.…` resolves.

## 1. Set up the desk (once)

```
python3 -m fleet.desk --state ./ops-state init --host 0.0.0.0 --port 5000
python3 -m fleet.controller --state ./ops-state        # long-running
```

`init` generates the controller keypair into
`ops-state/controller.json` (0600). Leave the controller running;
it dispatches jobs and takes results. To run it detached:
`setsid nohup python3 -m fleet.controller --state ./ops-state > controller.log 2>&1 < /dev/null &`
(same pattern for agents). Or run both as services — next section.

### Run them as services (systemd user units)

```
fleet/install-services.sh --repo "$HOME/basicc2" --state "$HOME/fleet-ops" \
    --agent thor "$HOME/fleet-ops/enroll/thor.json" \
                 "$HOME/fleet-ops/agent-state" \
                 "$HOME/fleet-ops/allowlist.json"
```

The installer renders per-service config files into
`~/.config/fleet/` (`controller.conf`, `agent-<name>.conf`),
installs `fleet-controller.service` and the `fleet-agent@.service`
template into `~/.config/systemd/user/`, and enables them with
`--now`. One `--agent` group per agent that lives on this box.
Logs go to the journal, one line per event (connect, dispatch,
result, refusal, reap):

```
journalctl --user -u fleet-controller -u 'fleet-agent@*' -f
```

User units start at login; for boot survival — services up before
anyone logs in, which is what a fleet box wants — enable linger:

```
loginctl enable-linger "$USER"
```

## 2. Enroll each box (once per agent)

```
python3 -m fleet.desk --state ./ops-state enroll thor --host <controller-LAN-address> --port 5000
```

`--host`/`--port` here are **the address the agent will dial** to
reach the controller — the controller's LAN IP for an agent on
another box, `127.0.0.1` only when the agent lives on the
controller's own host. (The address in `init` is the bind address;
`0.0.0.0` there is normal.)

This registers thor's new public key and writes
`ops-state/enroll/thor.json` — the **enroll bundle**, which contains
thor's secret key. Carry it to the box by a channel you trust (scp
over the existing tunnels is fine) and keep it 0600 there.

On the box, write its allowlist — the argv prefixes this box will
run, and nothing else:

```
echo '[["echo"], ["hostname"], ["systemctl", "status"]]' > allowlist.json
```

Then start the agent (it reconnects on its own, forever):

```
python3 -m fleet.agent --config thor.json --state ./agent-state --allowlist allowlist.json
```

Two agents on one host (the natural trial setup): give each its
own `--state` dir (its ledger lives there) and its own allowlist
file if they differ. One agent per host needs nothing special.
The allowlist is **hot-reloaded**: the agent stats the file on
every dispatch and picks up edits with no restart. A malformed
edit keeps the previous list in force (and is logged) — a bad
file never crashes the agent and never widens what it will run.

## 3. Task the fleet

```
python3 -m fleet.desk --state ./ops-state agents
python3 -m fleet.desk --state ./ops-state send --target thor --kind ping
python3 -m fleet.desk --state ./ops-state send --target thor --kind run -- echo hi
python3 -m fleet.desk --state ./ops-state send --target all  --kind ping
python3 -m fleet.desk --state ./ops-state jobs
python3 -m fleet.desk --state ./ops-state show <job_id>
```

`send` prints the job id. `jobs` shows queued / dispatched / done /
partial / refused, from the state files. **`show` prints what a
job actually brought back** — per agent: exit code, the booked
stdout preview, its sha256; for `ping`, the reported facts. That
is the read path for results; the same data lives in
`ops-state/jobs/<job_id>.json` if you want it raw. (On `agents`:
agents heartbeat every 15 s and the controller **reaps** any
connection silent for 45 s — `--reap-s` on the controller,
`--heartbeat-s` on the agent — and a reconnecting agent displaces
its own older registration, so ONLINE now means recently-heard-
from. If the controller itself is dead, its `online.json` still
freezes, and `desk agents` warns that the list may be stale
rather than presenting it as live.)

## 4. Prove a job happened

```
python3 -m fleet.desk --state ./ops-state derive <job_id>
```

Derive re-reads the controller's ledger and the agent's stored
ledger copy, audits both, and checks both exchanges (`:task` =
delivery, `:result` = completion) with signature enforcement. Exit
code 0 only when everything is MINTED and both audits PASS. This is
the receipt; a job the desk cannot derive did not, as far as the
record is concerned, happen.

What a `run` result books: the argv, the exit code, the **sha256
of the full stdout**, and a **512-character preview** of it. The
preview is for reading; the hash is for proof — if you later hold
the full output (from the box itself), its hash either matches
the booked one or it isn't what the agent produced. Full stdout
is deliberately not shipped or stored by the desk in v1.

## State layout (`--state`, default `./ops-state`)

| path | what |
|---|---|
| `controller.json` | controller keypair (0600) + bind address |
| `agents.json` | enrollment registry: agent_id → public key |
| `online.json` | who's connected right now (controller-maintained) |
| `enroll/<agent>.json` | enroll bundles (0600, contain secret keys) |
| `inbox/<job_id>.json` | queued jobs (`desk send` writes these) |
| `jobs/<job_id>.json` | per-job status **and results** (controller-maintained; read via `desk show`) |
| `ledgers/controller.jsonl` | the controller's stitch ledger |
| `agent-ledgers/<agent_id>.jsonl` | latest ledger copy per agent |

## Tests

```
python3 -m pytest fleet/tests/ -q
```

(in-process loop tests: minting, allowlist refusal, tampered
signature refusal, unenrolled handshake refusal — plus the v1.1
liveness suite: zombie replacement, silent-connection reaping,
heartbeat survival, allowlist hot-reload.)
