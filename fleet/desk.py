"""
fleet.desk — the operator CLI.

    desk [--state DIR] init [--host H] [--port P]
    desk [--state DIR] enroll <agent_id> [--host H] [--port P]
    desk [--state DIR] agents
    desk [--state DIR] send --target <agent_id|all> --kind ping
    desk [--state DIR] send --target <agent_id> --kind run -- echo hi
    desk [--state DIR] jobs
    desk [--state DIR] derive <job_id>

The desk is the only place keys are born. `enroll` generates an
agent's keypair, registers its public key, and writes the bundle the
operator carries to the box. `derive` is the point of the whole
system: it re-derives a job's completion from the two stitch ledgers
— controller's and the agent's stored copy — and reports per-exchange
MINTED / NOT-MINTED plus the audit verdicts. Nothing is believed;
everything is derived.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import common
from .common import Key, Paths, stitch


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_secret_file(path: Path, obj) -> None:
    common.write_json(path, obj)
    os.chmod(path, 0o600)  # secret keys: owner-read only


# --- init / enroll ---------------------------------------------------------

def cmd_init(paths: Paths, host: str, port: int) -> None:
    paths.ensure()
    if paths.controller_file.exists():
        raise SystemExit(f"already initialized: {paths.controller_file} "
                         f"exists (refusing to overwrite the "
                         f"controller key)")
    key = Key.generate()
    _write_secret_file(paths.controller_file, {
        "identity": common.CONTROLLER_ID,
        "host": host,
        "port": port,
        "pubkey_hex": key.public_bytes.hex(),
        "secret_hex": key.secret_bytes.hex(),
        "created_utc": _utc_now(),
    })
    if not paths.registry_file.exists():
        common.write_json(paths.registry_file, {})
    print(f"initialized desk state at {paths.root}")
    print(f"controller pubkey: {key.public_bytes.hex()}")
    print(f"controller will bind {host}:{port}")


def cmd_enroll(paths: Paths, agent_id: str, host=None, port=None) -> Path:
    paths.ensure()
    cfg = common.read_json(paths.controller_file)
    if cfg is None:
        raise SystemExit("desk not initialized — run `desk init` first")
    registry = common.read_json(paths.registry_file, {}) or {}
    if agent_id in registry:
        raise SystemExit(f"agent {agent_id!r} is already enrolled "
                         f"(refusing to re-key it implicitly)")
    key = Key.generate()
    registry[agent_id] = {
        "pubkey_hex": key.public_bytes.hex(),
        "enrolled_utc": _utc_now(),
    }
    common.write_json(paths.registry_file, registry)
    bundle_host = host or (cfg["host"] if cfg["host"] != "0.0.0.0"
                           else "127.0.0.1")
    bundle = {
        "agent_id": agent_id,
        "pubkey_hex": key.public_bytes.hex(),
        "secret_hex": key.secret_bytes.hex(),
        "controller_pubkey_hex": cfg["pubkey_hex"],
        "controller": {"host": bundle_host,
                       "port": port or cfg["port"]},
    }
    bundle_path = paths.bundle_file(agent_id)
    _write_secret_file(bundle_path, bundle)
    print(f"enrolled {agent_id}: pubkey {key.public_bytes.hex()}")
    print(f"bundle written to {bundle_path} — carry it to the box "
          f"(it contains the agent's secret key)")
    return bundle_path


# --- status ------------------------------------------------------------------

def _controller_reachable(paths: Paths) -> bool | None:
    """True/False when the controller answers a TCP connect at its
    configured address; None when we cannot tell (no config). The
    online list is a file the controller maintains — when the
    controller is dead the file freezes, so `agents` must not
    present it as live truth (playtest finding F10)."""
    import socket
    cfg = common.read_json(paths.controller_file)
    if not cfg:
        return None
    host = cfg.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    try:
        with socket.create_connection((host, int(cfg["port"])), timeout=2):
            return True
    except OSError:
        return False


def cmd_agents(paths: Paths) -> None:
    registry = common.read_json(paths.registry_file, {}) or {}
    online = common.read_json(paths.online_file, {}) or {}
    if not registry:
        print("no agents enrolled")
        return
    if _controller_reachable(paths) is False:
        cfg = common.read_json(paths.controller_file, {}) or {}
        print(f"WARNING: controller not reachable at "
              f"{cfg.get('host')}:{cfg.get('port')} — the online list "
              f"below is the last state it wrote and may be stale.")
    for agent_id in sorted(registry):
        info = online.get(agent_id)
        state = (f"ONLINE  {info['addr']} since {info['since']}"
                 if info else "offline")
        print(f"{agent_id:<16} {state:<44} "
              f"pubkey {registry[agent_id]['pubkey_hex'][:16]}…")


def cmd_jobs(paths: Paths) -> None:
    specs = []
    for f in sorted(paths.inbox_dir.glob("*.json")):
        spec = common.read_json(f)
        if spec:
            specs.append(spec)
    if not specs:
        print("no jobs")
        return
    for spec in specs:
        job = common.read_json(paths.job_file(spec["job_id"]), {}) or {}
        status = job.get("status", "queued")
        extra = ""
        if job.get("results"):
            extra = f" results from: {', '.join(sorted(job['results']))}"
        if job.get("refused"):
            extra += (f" refused by: "
                      f"{', '.join(sorted(job['refused']))}")
        argv = (spec.get("args") or {}).get("argv")
        argv_s = ""
        if argv:
            joined = " ".join(argv)
            argv_s = f" argv={joined[:44]}{'…' if len(joined) > 44 else ''}"
        print(f"{spec['job_id']:<28} target={spec.get('target'):<10} "
              f"kind={spec.get('kind'):<5} status={status}{extra}{argv_s}")


def cmd_show(paths: Paths, job_id: str) -> None:
    """Print what a job actually brought back (playtest finding F5:
    status and verdicts are not the answers — the results are)."""
    spec = common.read_json(paths.inbox_file(job_id))
    if spec is None:
        raise SystemExit(f"no such job: {job_id}")
    job = common.read_json(paths.job_file(job_id), {}) or {}
    argv = (spec.get("args") or {}).get("argv")
    print(f"{job_id}  target={spec.get('target')} kind={spec.get('kind')}"
          + (f" argv={' '.join(argv)}" if argv else "")
          + f"  status={job.get('status', 'queued')}")
    for agent_id in sorted(job.get("refused") or {}):
        print(f"  {agent_id}: REFUSED ({job['refused'][agent_id]})")
    for agent_id in sorted(job.get("results") or {}):
        r = job["results"][agent_id]
        print(f"  {agent_id}:")
        if spec.get("kind") == "ping":
            for k in sorted(r):
                print(f"    {k}: {r[k]}")
        else:
            print(f"    exit: {r.get('exit')}")
            if r.get("stdout_sha256"):
                print(f"    stdout sha256: {r['stdout_sha256'][:16]}…")
            prev = (r.get("stdout_preview") or "").rstrip()
            if prev:
                print("    stdout (preview, first 512 chars booked):")
                for line in prev.splitlines():
                    print(f"      {line}")


# --- send ----------------------------------------------------------------------

def cmd_send(paths: Paths, target: str, kind: str, args: dict) -> str:
    paths.ensure()
    if paths.controller_file.exists() is False:
        raise SystemExit("desk not initialized — run `desk init` first")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    job_id = f"job-{stamp}-{uuid.uuid4().hex[:6]}"
    common.write_json(paths.inbox_file(job_id), {
        "job_id": job_id, "target": target, "kind": kind, "args": args,
        "sent_utc": _utc_now(),
    })
    print(job_id)
    return job_id


# --- derive ----------------------------------------------------------------------

def derive(paths: Paths, job_id: str):
    """
    Derive a job's completion from the records alone. Returns
    (rows, all_minted): one row per target agent with per-exchange
    MINTED / NOT-MINTED, the agree() reason, and both audit verdicts.
    """
    spec = common.read_json(paths.inbox_file(job_id))
    registry = common.read_json(paths.registry_file, {}) or {}
    cfg = common.read_json(paths.controller_file)
    if cfg is None:
        raise SystemExit("desk not initialized — run `desk init` first")
    booking_keys = {common.CONTROLLER_ID: bytes.fromhex(cfg["pubkey_hex"])}
    for agent_id, entry in registry.items():
        booking_keys[agent_id] = bytes.fromhex(entry["pubkey_hex"])
    if spec and spec.get("target") not in (None, "all"):
        targets = [spec["target"]]
    else:
        targets = sorted(registry)
    rows = []
    for agent_id in targets:
        row = {"agent": agent_id}
        copy_path = paths.agent_ledger_copy(agent_id)
        if not paths.controller_ledger.exists() or not copy_path.exists():
            row.update(task="NOT-MINTED", result="NOT-MINTED",
                       detail="missing ledger (controller or agent copy)",
                       audit_ok=False)
            rows.append(row)
            continue
        va = stitch.audit_ledger(str(paths.controller_ledger))
        vb = stitch.audit_ledger(str(copy_path))
        row["audit_ok"] = va.ok and vb.ok
        row["audit"] = (f"controller {'PASS' if va.ok else 'FAIL'} "
                        f"({va.entries} entries), agent "
                        f"{'PASS' if vb.ok else 'FAIL'} "
                        f"({vb.entries} entries)")
        for label in ("task", "result"):
            verdict = stitch.agree(
                str(paths.controller_ledger), str(copy_path),
                f"{job_id}:{label}", booking_keys=booking_keys)
            row[label] = "MINTED" if verdict.agreed else "NOT-MINTED"
            row[f"{label}_reason"] = verdict.reason
            row[f"{label}_detail"] = verdict.detail
        rows.append(row)
    all_minted = bool(rows) and all(
        r["task"] == "MINTED" and r["result"] == "MINTED" and r["audit_ok"]
        for r in rows)
    return rows, all_minted


def cmd_derive(paths: Paths, job_id: str) -> int:
    rows, all_minted = derive(paths, job_id)
    print(f"derive {job_id} (from the ledgers alone):")
    if not rows:
        print("  no target agents")
        return 1
    for row in rows:
        print(f"  agent {row['agent']}:")
        if "audit" in row:
            print(f"    audit:  {row['audit']}")
        else:
            print(f"    audit:  n/a — {row['detail']}")
        for label in ("task", "result"):
            reason = row.get(f"{label}_reason", row.get("detail", ""))
            print(f"    {job_id}:{label:<6} {row[label]:<10} ({reason})")
    print(f"  verdict: {'COMPLETE — both exchanges minted' if all_minted else 'NOT COMPLETE'}")
    return 0 if all_minted else 1


# --- CLI ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fleet.desk",
                                 description="Fleet operator desk")
    ap.add_argument("--state", default="./ops-state",
                    help="desk state dir (default ./ops-state)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create controller key + state dirs")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=common.DEFAULT_PORT)

    p = sub.add_parser("enroll", help="enroll an agent (generates its keypair)")
    p.add_argument("agent_id")
    p.add_argument("--host", default=None,
                   help="controller address agents should dial "
                        "(default: init host, or 127.0.0.1)")
    p.add_argument("--port", type=int, default=None)

    sub.add_parser("agents", help="registry + online status")

    p = sub.add_parser("send", help="queue a job")
    p.add_argument("--target", required=True)
    p.add_argument("--kind", required=True, choices=["ping", "run"])
    p.add_argument("--args-json", default=None,
                   help="explicit args object as JSON (overrides -- argv)")
    p.add_argument("rest", nargs=argparse.REMAINDER,
                   help="for --kind run: `-- argv...`")

    sub.add_parser("jobs", help="job status from state files")

    p = sub.add_parser("show", help="print a job's results (what came back)")
    p.add_argument("job_id")

    p = sub.add_parser("derive", help="derive a job's completion from the ledgers")
    p.add_argument("job_id")

    args = ap.parse_args(argv)
    paths = Paths(args.state)

    if args.cmd == "init":
        cmd_init(paths, args.host, args.port)
    elif args.cmd == "enroll":
        cmd_enroll(paths, args.agent_id, host=args.host, port=args.port)
    elif args.cmd == "agents":
        cmd_agents(paths)
    elif args.cmd == "send":
        if args.args_json is not None:
            job_args = json.loads(args.args_json)
        elif args.kind == "run":
            rest = list(args.rest)
            if rest and rest[0] == "--":
                rest = rest[1:]
            if not rest:
                raise SystemExit("send --kind run needs an argv: "
                                 "`-- echo hi`")
            job_args = {"argv": rest}
        else:
            job_args = {}
        cmd_send(paths, args.target, args.kind, job_args)
    elif args.cmd == "jobs":
        cmd_jobs(paths)
    elif args.cmd == "show":
        cmd_show(paths, args.job_id)
    elif args.cmd == "derive":
        return cmd_derive(paths, args.job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
