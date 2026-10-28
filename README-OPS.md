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
it dispatches jobs and takes results.

## 2. Enroll each box (once per agent)

```
python3 -m fleet.desk --state ./ops-state enroll thor --host <controller-LAN-address> --port 5000
```

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

## 3. Task the fleet

```
python3 -m fleet.desk --state ./ops-state agents
python3 -m fleet.desk --state ./ops-state send --target thor --kind ping
python3 -m fleet.desk --state ./ops-state send --target thor --kind run -- echo hi
python3 -m fleet.desk --state ./ops-state send --target all  --kind ping
python3 -m fleet.desk --state ./ops-state jobs
```

`send` prints the job id. `jobs` shows queued / dispatched / done /
partial / refused, from the state files — no need to ask the
controller process anything.

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

## State layout (`--state`, default `./ops-state`)

| path | what |
|---|---|
| `controller.json` | controller keypair (0600) + bind address |
| `agents.json` | enrollment registry: agent_id → public key |
| `online.json` | who's connected right now (controller-maintained) |
| `enroll/<agent>.json` | enroll bundles (0600, contain secret keys) |
| `inbox/<job_id>.json` | queued jobs (`desk send` writes these) |
| `jobs/<job_id>.json` | per-job status (controller-maintained) |
| `ledgers/controller.jsonl` | the controller's stitch ledger |
| `agent-ledgers/<agent_id>.jsonl` | latest ledger copy per agent |

## Tests

```
python3 -m pytest fleet/tests/ -q
```

(in-process loop tests: minting, allowlist refusal, tampered
signature refusal, unenrolled handshake refusal.)
