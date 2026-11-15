"""Offline tests for the freshness check: python3 -m unittest discover -s .github/freshness"""

import tempfile
import unittest
from pathlib import Path

import freshness as fr

V = "312b18dd0ef06d1b3e04824171177e696a518063"
SKILL = f"""---
name: s
description: d
depends_on:
  - repo: platform-factory/svc-hello
    path: .github/workflows/validate.yml
    verified_at: {V}
---

# s
"""


def fake(blobs: dict, commits=()):
    """blobs: {ref: blob sha or None}; ref "main" or the verified commit."""
    calls = []

    def get(path, params):
        calls.append((path, params))
        if path.endswith("/commits"):
            return list(commits)
        b = blobs.get(params["ref"], "missing-ref")
        if b == "missing-ref":
            raise fr.CheckError("unreadable")
        return None if b is None else {"type": "file", "sha": b}
    get.calls = calls
    return get


def tree(text=SKILL):
    d = tempfile.TemporaryDirectory()
    p = Path(d.name, "skills/s/SKILL.md")
    p.parent.mkdir(parents=True)
    p.write_text(text)
    return d


class FreshnessTest(unittest.TestCase):
    def test_unchanged_is_green(self):
        with tree() as d:
            code, rows, errors = fr.run(Path(d), fake({V: "b1", "main": "b1"}))
        self.assertEqual((code, errors), (0, []))
        self.assertTrue(rows[0]["fresh"])

    def test_changed_fails_and_lists_commits(self):
        commits = [{"sha": "c" * 40, "commit": {"message": "Add tests and the go-test job\n\nbody",
                                                "committer": {"date": "2026-11-12T00:00:00Z"}}},
                   {"sha": V, "commit": {"message": "older", "committer": {"date": "x"}}},
                   {"sha": "d" * 40, "commit": {"message": "oldest", "committer": {"date": "y"}}}]
        with tree() as d:
            code, rows, errors = fr.run(Path(d), fake({V: "b1", "main": "b2"}, commits))
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["reason"], "changed on main since verified_at")
        self.assertEqual([c["sha"] for c in rows[0]["commits_since"]], ["c" * 40])
        self.assertIn("Add tests and the go-test job", fr.summary(rows, errors))

    def test_removed_fails(self):
        with tree() as d:
            code, rows, _ = fr.run(Path(d), fake({V: "b1", "main": None}))
        self.assertEqual((code, rows[0]["reason"]), (1, "removed on main"))

    def test_cannot_read_or_bad_declaration_is_not_green(self):
        with tree() as d:
            self.assertEqual(fr.run(Path(d), fake({"main": "b1"}))[0], 2)          # verified ref unreadable
        with tree() as d:
            self.assertEqual(fr.run(Path(d), fake({V: None, "main": "b1"}))[0], 2)  # absent at verified_at
        for bad in (SKILL.replace(V, "312b18d"), SKILL.replace("    verified_at", "    verified"),
                    "# no front matter\n", SKILL.replace("depends_on:", "other:")):
            with tree(bad) as d:
                self.assertEqual(fr.run(Path(d), fake({V: "b1", "main": "b1"}))[0], 2, bad[:40])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(fr.run(Path(d), fake({}))[0], 2)                      # no skills at all

    def test_the_real_skill_declares_validate_yml(self):
        root = Path(__file__).resolve().parents[2]
        deps = fr.declared("skills/go-module-bump/SKILL.md", (root / "skills/go-module-bump/SKILL.md").read_text())
        self.assertEqual([(d["repo"], d["path"]) for d in deps],
                         [("platform-factory/svc-hello", ".github/workflows/validate.yml")])


if __name__ == "__main__":
    unittest.main()
