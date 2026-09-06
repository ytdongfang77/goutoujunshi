#!/usr/bin/env python3
"""Reproducible integration tests; all memory data stays in temporary storage."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_imported_files_match_manifest(self):
        manifest = json.loads((ROOT / "documentation/organization-import.json").read_text())
        files = manifest["files"]
        self.assertEqual(len(files), 8)
        self.assertEqual(len({entry["source_name"] for entry in files}), 8)
        for entry in files:
            with self.subTest(file=entry["source_name"]):
                path = ROOT / entry["path"]
                self.assertEqual(path.name, entry["source_name"])
                data = path.read_bytes()
                self.assertEqual(len(data), entry["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])

    def test_all_imports_reachable_from_skill(self):
        pending = [ROOT / "SKILL.md"]
        visited = set()
        while pending:
            path = pending.pop().resolve()
            if path in visited:
                continue
            visited.add(path)
            for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                if re.match(r"\w+://", target) or target.startswith("#"):
                    continue
                next_path = (path.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(next_path.is_file(), str(next_path))
                if next_path.suffix == ".md":
                    pending.append(next_path)
        manifest = json.loads((ROOT / "documentation/organization-import.json").read_text())
        for entry in manifest["files"]:
            self.assertIn((ROOT / entry["path"]).resolve(), visited, entry["path"])

    def test_runtime_only_installation(self):
        with tempfile.TemporaryDirectory(prefix="goutoujunshi-install-") as directory:
            destination = Path(directory) / "goutoujunshi"
            destination.mkdir()
            for name in ("SKILL.md", "agents", "references", "scripts"):
                source = ROOT / name
                if source.is_dir():
                    shutil.copytree(source, destination / name)
                else:
                    shutil.copy2(source, destination / name)
            result = subprocess.run(
                [sys.executable, str(destination / "scripts/validate_skill.py"), "--runtime"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class MemoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="goutoujunshi-memory-test-")
        self.addCleanup(self.temporary.cleanup)
        self.store = Path(self.temporary.name) / "memory"
        self.env = dict(os.environ, GOUTOUJUNSHI_MEMORY_DIR=str(self.store),
                        PYTHONDONTWRITEBYTECODE="1")

    def run_memory(self, *args, error=None):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/memory_store.py"), *args],
            env=self.env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 1 if error else 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        if error:
            self.assertEqual(payload["error"]["code"], error)
        return payload

    def apply(self, subject="test-object-a", value="喜欢散步", source="user_report", error=None):
        delta = {"scope": "object", "subject_id": subject, "field": "preference",
                 "value": value, "source_type": source, "confidence": "high"}
        return self.run_memory("apply", "--json", json.dumps(delta, ensure_ascii=False), error=error)

    def enable(self):
        self.run_memory("enable", "--confirm")

    def test_status_and_consent_do_not_write_before_authorization(self):
        self.assertFalse(self.run_memory("status")["exists"])
        self.run_memory("enable", error="CONFIRMATION_REQUIRED")
        self.apply(error="NOT_INITIALIZED")
        self.assertFalse(self.store.exists())

    def test_write_recall_update_and_undo(self):
        self.enable()
        self.apply()
        context = self.run_memory("context", "--subject-id", "test-object-a")
        self.assertEqual(context["memories"][0]["value"], "喜欢散步")
        change = self.apply(value="喜欢看展")
        self.assertEqual(self.run_memory("show")["count"], 1)
        self.run_memory("undo", "--op-id", change["op_id"])
        self.assertEqual(self.run_memory("show")["memories"][0]["value"], "喜欢散步")

    def test_object_isolation_and_delete(self):
        self.enable()
        self.apply()
        self.apply(subject="test-object-b", value="喜欢咖啡")
        context = self.run_memory("context", "--subject-id", "test-object-a")
        self.assertEqual({m["subject_id"] for m in context["memories"]}, {"test-object-a"})
        self.run_memory("forget-object", "test-object-a", error="CONFIRMATION_REQUIRED")
        self.run_memory("forget-object", "test-object-a", "--confirm")
        self.assertEqual(self.run_memory("context", "--subject-id", "test-object-a")["count"], 0)
        self.assertEqual(self.run_memory("context", "--subject-id", "test-object-b")["count"], 1)

    def test_pause_resume_and_revoke(self):
        self.enable()
        self.apply()
        self.run_memory("pause")
        self.apply(error="MEMORY_PAUSED")
        self.run_memory("context", error="MEMORY_PAUSED")
        self.run_memory("resume")
        self.assertEqual(self.run_memory("context")["count"], 1)
        self.run_memory("revoke", "--confirm")
        self.apply(error="CONSENT_REQUIRED")
        self.run_memory("context", error="CONSENT_REQUIRED")

    def test_inference_cannot_be_saved_as_object_fact(self):
        self.enable()
        self.apply(source="assistant_inference", error="SOURCE_NOT_ELIGIBLE")
        self.assertEqual(self.run_memory("show")["count"], 0)

    def test_clear_removes_database(self):
        self.enable()
        self.apply()
        self.run_memory("clear", error="CONFIRMATION_REQUIRED")
        self.assertTrue((self.store / "memory.sqlite3").is_file())
        self.run_memory("clear", "--confirm")
        self.assertFalse(self.run_memory("status")["exists"])
        self.assertEqual(list(self.store.glob("memory.sqlite3*")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
