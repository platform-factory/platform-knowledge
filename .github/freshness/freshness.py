"""The freshness check: does any skill describe a file that has changed since it was verified?

Why (ADR-0024 §13, C-22): a skill that tells an agent how a repository
works goes stale silently when that repository changes. Each skill declares,
in its front matter, the files it describes and the commit at which it was
last checked against them:

    depends_on:
      - repo: platform-factory/svc-hello
        path: .github/workflows/validate.yml
        verified_at: <full commit SHA>

This check reads each declared file at the dependent repository's `main`
through the GitHub API and compares it with the same file at `verified_at`.
A difference (or a file that no longer exists) fails the check, and the
summary lists the commits on `main` that touched the file since. The fix is
a person re-reading the skill against the file, updating it if needed,
moving `verified_at`, and cutting a new release.

It fails, too, when it cannot read something: a check that cannot look must
not report that nothing changed.

Standard library only; YAML front matter is read with PyYAML when present,
otherwise with `yq` (both are on GitHub-hosted runners).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

API = "https://api.github.com"
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class CheckError(Exception):
    pass


def parse_yaml(text: str):
    try:
        import yaml  # type: ignore
    except ImportError:
        yq = shutil.which("yq")
        if not yq:
            raise CheckError("neither PyYAML nor yq is available")
        return json.loads(subprocess.run([yq, "-o=json", "."], input=text.encode(),
                                         capture_output=True, check=True).stdout)
    return yaml.safe_load(text)


def front_matter(text: str) -> dict:
    if not text.startswith("---\n"):
        raise CheckError("no front matter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise CheckError("front matter is not closed")
    data = parse_yaml(text[4:end + 1])
    if not isinstance(data, dict):
        raise CheckError("front matter is not a mapping")
    return data


def declared(skill_path: str, text: str) -> list[dict]:
    fm = front_matter(text)
    deps = fm.get("depends_on")
    if not isinstance(deps, list) or not deps:
        raise CheckError(f"{skill_path}: depends_on is missing or empty")
    out = []
    for i, d in enumerate(deps):
        if not isinstance(d, dict) or set(d) != {"repo", "path", "verified_at"}:
            raise CheckError(f"{skill_path}: depends_on[{i}] must have exactly repo, path, verified_at")
        if not SHA40.match(str(d["verified_at"])):
            raise CheckError(f"{skill_path}: depends_on[{i}].verified_at must be a full commit SHA")
        out.append({"skill": skill_path, **{k: str(v) for k, v in d.items()}})
    return out


def make_get(token: str | None, api: str = API) -> Callable[[str, dict], object]:
    def get(path: str, params: dict):
        url = f"{api}/{path}?{urllib.parse.urlencode(params)}" if params else f"{api}/{path}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "platform-knowledge-freshness"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise CheckError(f"GET {path}: HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            raise CheckError(f"GET {path}: {e}")
    return get


def blob_sha(get, repo: str, path: str, ref: str) -> str | None:
    meta = get(f"repos/{repo}/contents/{urllib.parse.quote(path)}", {"ref": ref})
    if meta is None:
        return None
    if not isinstance(meta, dict) or meta.get("type") != "file":
        raise CheckError(f"{repo}:{path} at {ref} is not a file")
    return meta["sha"]


def commits_since(get, repo: str, path: str, verified_at: str) -> list[dict]:
    commits = get(f"repos/{repo}/commits", {"sha": "main", "path": path, "per_page": 30}) or []
    out = []
    for c in commits:
        if c.get("sha") == verified_at:
            break
        out.append({"sha": c.get("sha"), "date": ((c.get("commit") or {}).get("committer") or {}).get("date"),
                    "message": ((c.get("commit") or {}).get("message") or "").split("\n", 1)[0]})
    return out


def check(dep: dict, get) -> dict:
    repo, path, ver = dep["repo"], dep["path"], dep["verified_at"]
    at_verified = blob_sha(get, repo, path, ver)
    if at_verified is None:
        raise CheckError(f"{repo}:{path} does not exist at verified_at {ver}")
    at_main = blob_sha(get, repo, path, "main")
    fresh = at_main == at_verified
    row = {**dep, "fresh": fresh, "blob_at_verified": at_verified, "blob_at_main": at_main}
    if not fresh:
        row["reason"] = "removed on main" if at_main is None else "changed on main since verified_at"
        row["commits_since"] = commits_since(get, repo, path, ver)
    return row


def run(root: Path, get) -> tuple[int, list[dict], list[str]]:
    rows, errors = [], []
    skills = sorted(root.glob("skills/*/SKILL.md"))
    if not skills:
        errors.append("no skills/*/SKILL.md found")
    for s in skills:
        rel = str(s.relative_to(root))
        try:
            for dep in declared(rel, s.read_text()):
                rows.append(check(dep, get))
        except CheckError as e:
            errors.append(str(e))
    stale = [r for r in rows if not r["fresh"]]
    return (2 if errors else 1 if stale else 0), rows, errors


def summary(rows: list[dict], errors: list[str]) -> str:
    lines = ["## Skill freshness", "", "| skill | depends on | verified at | on main |", "|---|---|---|---|"]
    for r in rows:
        state = "unchanged" if r["fresh"] else f"**{r['reason']}**"
        lines.append(f"| `{r['skill']}` | `{r['repo']}` `{r['path']}` | `{r['verified_at'][:12]}` | {state} |")
    for r in rows:
        if not r["fresh"] and r.get("commits_since"):
            lines += ["", f"Commits on `{r['repo']}` `main` touching `{r['path']}` since `{r['verified_at'][:12]}`:"]
            lines += [f"- `{c['sha'][:12]}` {c['date']}: {c['message']}" for c in r["commits_since"]]
    if errors:
        lines += ["", "The check could not finish, so it does not report the skills fresh:"]
        lines += [f"- {e}" for e in errors]
    return "\n".join(lines) + "\n"


def main() -> int:
    root = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    code, rows, errors = run(root, make_get(os.environ.get("GH_TOKEN")))
    text = summary(rows, errors)
    print(text)
    print(json.dumps({"exit": code, "rows": rows, "errors": errors}, indent=2, sort_keys=True))
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a") as f:
            f.write(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
