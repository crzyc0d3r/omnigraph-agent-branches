from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import app


class BranchTests(unittest.TestCase):
    def test_demo_preserves_both_and_original_snapshot(self):
        result = app.demo()
        self.assertEqual(result["main"]["sessions"]["store"], "Postgres")
        self.assertEqual(result["main"]["sessions"]["owner"], "platform-team")
        self.assertEqual(result["initial_snapshot"]["sessions"]["store"], "Redis")
        self.assertEqual(result["initial_snapshot"]["sessions"]["owner"], "")
        self.assertEqual(len(result["six_write_diff"]), 6)
        self.assertIn("divergent_update", result["stale_merge"])
        self.assertEqual(result["first_merge"]["kind"], "fast_forward")

    def test_conflict_is_record_level_and_atomic(self):
        g = app.Graph({"x": {"a": 1, "b": 1}, "y": {"a": 0}})
        for name in ("a", "b"): g.branch("agent/" + name)
        g.write("agent/a", "a", "x", {"a": 2, "b": 1})
        g.write("agent/b", "b", "x", {"a": 1, "b": 2})
        g.write("agent/b", "b", "y", {"a": 9})
        g.merge("agent/a")
        before = deepcopy(g.main)
        with self.assertRaises(app.Conflict): g.merge("agent/b")
        self.assertEqual(g.main, before)

    def test_disjoint_entities_merge(self):
        g = app.Graph({"x": {"a": 1}, "y": {"a": 1}})
        for name in ("a", "b"): g.branch("agent/" + name)
        g.write("agent/a", "a", "x", {"a": 2})
        g.write("agent/b", "b", "y", {"a": 2})
        g.merge("agent/a")
        self.assertEqual(g.merge("agent/b")["kind"], "merged")
        self.assertEqual(g.main, {"x": {"a": 2}, "y": {"a": 2}})

    def test_snapshots_and_writes_are_copied(self):
        source = {"x": {"a": 1}}
        g = app.Graph(source)
        source["x"]["a"] = 9
        snap = g.snapshot(app.digest({"x": {"a": 1}})); snap["x"]["a"] = 8
        self.assertEqual(g.main["x"]["a"], 1)

    def test_replay_policy(self):
        g = app.Graph({}); g.branch("agent/a")
        with self.assertRaises(PermissionError): g.write("agent/a", "b", "x", {})
        with self.assertRaises(PermissionError): g.merge("agent/a", actor="a")
        with self.assertRaises(ValueError): g.branch("agent/a")

    def test_native_plan_has_exact_public_cli_shapes(self):
        plan = app.native_plan("local.omni")
        self.assertEqual(plan[0]["argv"][:3], ["omnigraph", "init", "--schema"])
        reject = next(x for x in plan if x["step"] == "reject-stale")
        self.assertTrue(reject["expect_conflict"])
        self.assertEqual(reject["argv"][1:4], ["branch", "merge", "agent/codex"])
        self.assertIn("$SEED_COMMIT", plan[-1]["argv"])
        self.assertTrue(all(isinstance(x["argv"], list) for x in plan))

    def test_native_never_reuses_target_or_runs_without_cli(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/"exists.omni"; path.write_text("keep")
            with self.assertRaises(ValueError): app.run_native(path)
        with patch("app.shutil.which", return_value=None), patch("app.subprocess.run") as run:
            with self.assertRaises(ValueError): app.run_native("absent.omni")
            run.assert_not_called()

    def test_native_adapter_receipt_and_expected_conflict(self):
        def response(argv, **kwargs):
            if "--mode" in argv and "overwrite" in argv:
                return SimpleNamespace(returncode=0, stdout='{"commit":{"graph_commit_id":"seed-receipt"}}', stderr="")
            if argv[1:4] == ["branch", "merge", "agent/codex"]:
                return SimpleNamespace(returncode=4, stdout='{"kind":"divergent_update"}', stderr="")
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        with tempfile.TemporaryDirectory() as d, patch("app.shutil.which", return_value="omnigraph"), patch("app.subprocess.run", side_effect=response) as runner:
            result = app.run_native(Path(d)/"graph.omni")
            self.assertEqual(len(result["steps"]), 14)
            self.assertIn("seed-receipt", runner.call_args.args[0])

    def test_malformed_seed_receipt_stops_before_more_commands(self):
        for receipt in ([], {"commit": None}, {"commit": {"graph_commit_id": {}}}, {"commit": {"graph_commit_id": ""}}):
            responses = [SimpleNamespace(returncode=0, stdout="", stderr=""),
                         SimpleNamespace(returncode=0, stdout=json.dumps(receipt), stderr="")]
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as d, patch("app.shutil.which", return_value="omnigraph"), patch("app.subprocess.run", side_effect=responses) as runner:
                with self.assertRaisesRegex(ValueError, "seed receipt"):
                    app.run_native(Path(d)/"graph.omni")
                self.assertEqual(runner.call_count, 2)

    def test_cli_and_fixture_shapes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): self.assertEqual(app.main([]), 0)
        self.assertFalse(json.loads(out.getvalue())["native_verified"])
        for path in (app.HERE/"fixtures").glob("*.jsonl"):
            for line in path.read_text().splitlines():
                item = json.loads(line)
                self.assertTrue("type" in item or "edge" in item)


if __name__ == "__main__": unittest.main()
