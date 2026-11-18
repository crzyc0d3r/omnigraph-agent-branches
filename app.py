"""Replay record-level branch conflicts or drive an installed local graph CLI."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent


class Conflict(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Graph:
    """Small independent replay model, not an implementation of the graph engine."""
    def __init__(self, records):
        self.main = deepcopy(records)
        self.snapshots = {digest(records): deepcopy(records)}
        self.branches = {}

    def branch(self, name):
        if not isinstance(name, str) or not name.startswith("agent/") or name in self.branches:
            raise ValueError("use a fresh agent/<name> branch")
        self.branches[name] = {"base": deepcopy(self.main), "data": deepcopy(self.main)}

    def write(self, branch, actor, key, value):
        if branch != f"agent/{actor}":
            raise PermissionError("an agent writes only its own replay branch")
        if branch not in self.branches or not isinstance(key, str) or not key or not isinstance(value, dict):
            raise ValueError("invalid branch write")
        self.branches[branch]["data"][key] = deepcopy(value)

    def diff(self, branch):
        b = self.branches[branch]
        return {k: {"before": deepcopy(b["base"].get(k)), "after": deepcopy(b["data"].get(k))}
                for k in sorted(b["base"].keys() | b["data"].keys())
                if b["base"].get(k) != b["data"].get(k)}

    def merge(self, branch, actor="reviewer"):
        if actor != "reviewer":
            raise PermissionError("only reviewer may merge in this replay policy")
        b = self.branches[branch]
        changes = self.diff(branch)
        conflicts = [k for k in changes if self.main.get(k) != b["base"].get(k)
                     and self.main.get(k) != b["data"].get(k)]
        if conflicts:
            raise Conflict("divergent_update: " + ", ".join(conflicts))
        kind = "fast_forward" if self.main == b["base"] else "merged"
        updated = deepcopy(self.main)
        for key in changes:
            updated[key] = deepcopy(b["data"][key])
        self.main = updated
        commit = digest(updated)
        self.snapshots[commit] = deepcopy(updated)
        return {"kind": kind, "commit": commit}

    def snapshot(self, commit):
        return deepcopy(self.snapshots[commit])


def demo():
    records = {"sessions": {"type": "Component", "store": "Redis", "owner": ""},
               "billing": {"type": "Component", "store": "Postgres", "owner": "payments"},
               "old-store": {"type": "Decision", "status": "active"},
               "migration": {"type": "Issue", "status": "open"}}
    graph = Graph(records)
    initial = digest(records)
    graph.branch("agent/claude")
    graph.branch("agent/codex")
    writes = {"sessions": {"type": "Component", "store": "Postgres", "owner": ""},
              "postgres-store": {"type": "Decision", "status": "accepted"},
              "affects-sessions": {"type": "Affects", "from": "postgres-store", "to": "sessions"},
              "affects-billing": {"type": "Affects", "from": "postgres-store", "to": "billing"},
              "supersedes": {"type": "Supersedes", "from": "postgres-store", "to": "old-store"},
              "migration": {"type": "Issue", "status": "closed"}}
    for key, value in writes.items():
        graph.write("agent/claude", "claude", key, value)
    graph.write("agent/codex", "codex", "sessions", {"type": "Component", "store": "Redis", "owner": "platform-team"})
    change = graph.diff("agent/claude")
    first = graph.merge("agent/claude")
    before_conflict = deepcopy(graph.main)
    try:
        graph.merge("agent/codex")
        raise AssertionError("stale record should conflict")
    except Conflict as exc:
        rejection = str(exc)
    assert graph.main == before_conflict
    graph.branch("agent/codex-2")
    fresh = deepcopy(graph.main["sessions"])
    fresh["owner"] = "platform-team"
    graph.write("agent/codex-2", "codex-2", "sessions", fresh)
    graph.merge("agent/codex-2")
    return {"mode": "offline replay; no native engine or agents", "native_verified": False,
            "six_write_diff": change, "first_merge": first, "stale_merge": rejection,
            "main": graph.main, "initial_snapshot": graph.snapshot(initial),
            "policy": "local replay check only; server authentication is separate"}


def native_plan(target):
    target = str(Path(target).resolve())
    def cmd(*args):
        return ["omnigraph", *args, "--target", target]
    return [
        {"step": "init", "argv": ["omnigraph", "init", "--schema", str(HERE / "schema.pg"), target]},
        {"step": "seed", "argv": cmd("load", "--data", str(HERE / "fixtures/seed.jsonl"), "--mode", "overwrite", "--json")},
        *[{"step": f"branch-{n}", "argv": cmd("branch", "create", f"agent/{n}", "--from", "main")} for n in ("claude", "codex")],
        *[{"step": f"write-{n}", "argv": cmd("load", "--data", str(HERE / f"fixtures/{n}.jsonl"), "--mode", "merge", "--branch", f"agent/{n}")} for n in ("claude", "codex")],
        {"step": "review", "argv": cmd("branch", "diff", "agent/claude", "--json")},
        {"step": "merge-first", "argv": cmd("branch", "merge", "agent/claude", "--into", "main", "--json")},
        {"step": "reject-stale", "expect_conflict": True, "argv": cmd("branch", "merge", "agent/codex", "--into", "main", "--json")},
        {"step": "refresh", "argv": cmd("branch", "create", "agent/codex-2", "--from", "main")},
        {"step": "write-fresh", "argv": cmd("load", "--data", str(HERE / "fixtures/codex-2.jsonl"), "--mode", "merge", "--branch", "agent/codex-2")},
        {"step": "merge-fresh", "argv": cmd("branch", "merge", "agent/codex-2", "--into", "main", "--json")},
        {"step": "current", "argv": cmd("query", "components", "--query", str(HERE / "queries.gq"), "--format", "json")},
        {"step": "initial", "argv": cmd("query", "components", "--query", str(HERE / "queries.gq"), "--snapshot", "$SEED_COMMIT", "--format", "json")},
    ]


def run_native(target):
    target = Path(target).resolve()
    if target.exists() or target.suffix != ".omni":
        raise ValueError("native target must be a fresh local .omni path")
    if not shutil.which("omnigraph"):
        raise ValueError("omnigraph CLI is not installed; use demo or native-plan")
    target.parent.mkdir(parents=True, exist_ok=True)
    seed = None
    results = []
    for operation in native_plan(target):
        argv = [seed if a == "$SEED_COMMIT" else a for a in operation["argv"]]
        if any(a is None for a in argv):
            raise ValueError("seed receipt lacked graph_commit_id")
        response = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        if operation.get("expect_conflict"):
            if response.returncode == 0 or "divergent_update" not in response.stdout + response.stderr:
                raise ValueError("native stale merge did not report expected divergent_update")
        elif response.returncode:
            raise ValueError(f"native step {operation['step']} failed ({response.returncode}): {response.stderr[:1000]}")
        if operation["step"] == "seed":
            receipt = json.loads(response.stdout)
            commit = receipt.get("commit") if isinstance(receipt, dict) else None
            seed = commit.get("graph_commit_id") if isinstance(commit, dict) else None
            if not isinstance(seed, str) or not seed.strip():
                raise ValueError("seed receipt must contain a nonempty string graph_commit_id")
        results.append({"step": operation["step"], "exit": response.returncode, "stdout": response.stdout})
    return {"mode": "native local CLI", "steps": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("demo", "native-plan", "native"), nargs="?", default="demo")
    parser.add_argument("--target", default="artifacts/graph.omni")
    args = parser.parse_args(argv)
    try:
        result = demo() if args.mode == "demo" else native_plan(args.target) if args.mode == "native-plan" else run_native(args.target)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
