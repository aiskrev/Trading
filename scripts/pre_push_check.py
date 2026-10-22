"""Gate run by .githooks/pre-push before anything leaves this machine.

1. Secret scan of every commit being pushed (added lines + added file names).
2. Released risk config still matches its pinned hashes.
3. Full test suite, run in a clean temporary worktree of the exact commit being pushed
   (so uncommitted local changes cannot make a broken commit look green).

Exit code != 0 blocks the push; the commit stays local.
Usage (git calls it): python scripts/pre_push_check.py <remote-name> <remote-url>  < refs on stdin
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0" * 40

# Built by concatenation so this file never matches its own patterns.
SECRET_PATTERNS = [
    re.compile("sk-" + r"ant-[A-Za-z0-9_\-]{20,}"),
    re.compile("AK" + r"IA[0-9A-Z]{16}"),
    re.compile("-----BEGIN " + r"[A-Z ]*PRIVATE KEY-----"),
    re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}"),
    re.compile("github_" + r"pat_[A-Za-z0-9_]{40,}"),
    re.compile("xox" + r"[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)(api[_-]?key|secret|passw(or)?d|pwd|token|refresh[_-]?token)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
    re.compile(r"(?i)(passw(or)?d|pass|pwd)\s*[:=]\s*[^\s'\"]{8,}"),
]
BANNED_NAME = re.compile(r"(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx)|id_(rsa|ed25519|ecdsa)|secrets?\.(ya?ml|json))$",
                         re.IGNORECASE)


def scan_text(lines: Iterable[str]) -> List[str]:
    hits = []
    for n, line in enumerate(lines, 1):
        for pat in SECRET_PATTERNS:
            if pat.search(line):
                hits.append(f"line {n}: matches {pat.pattern[:28]}...")
                break
    return hits


def scan_names(names: Iterable[str]) -> List[str]:
    return [n for n in names if BANNED_NAME.search(n.replace("\\", "/"))]


# Git exports these to hooks (e.g. GIT_INDEX_FILE when the push starts from post-commit). If they
# leak into our own git/pytest subprocesses, `git worktree add` writes into the main index and fails.
HOOK_GIT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_QUARANTINE_PATH")


def clean_env(base: dict = None) -> dict:
    env = dict(os.environ if base is None else base)
    for var in HOOK_GIT_VARS:
        env.pop(var, None)
    return env


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True, env=clean_env()).stdout


def commits_to_push(local_sha: str, remote_sha: str) -> List[str]:
    if remote_sha != ZERO:
        return git("rev-list", f"{remote_sha}..{local_sha}").split()
    return git("rev-list", local_sha, "--not", "--remotes").split()


def secret_gate(commits: List[str]) -> List[str]:
    problems = []
    for c in commits:
        patch = git("show", "--format=", "--no-color", "--unified=0", c)
        added = [l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++")]
        problems += [f"{c[:10]} {h}" for h in scan_text(added)]
        names = git("show", "--format=", "--name-only", "--diff-filter=A", c).split()
        problems += [f"{c[:10]} forbidden file {n}" for n in scan_names(names)]
    return problems


def config_gate() -> List[str]:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "lock_risk_config.py"), "--check"],
                          cwd=ROOT, capture_output=True, text=True, env=_env(ROOT))
    return [] if proc.returncode == 0 else [proc.stdout.strip() or proc.stderr.strip()]


def test_gate(sha: str) -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="ta-prepush-"))
    tree = tmp / "tree"
    try:
        git("worktree", "add", "--detach", "--quiet", str(tree), sha)
        print(f"[pre-push] running tests on {sha[:10]} in a clean worktree ...", flush=True)
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x"],
                              cwd=tree, env=_env(tree))
        return proc.returncode == 0
    except subprocess.CalledProcessError as exc:
        print(f"[pre-push] could not create a clean worktree: {(exc.stderr or '').strip()[:300]}")
        return False
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT, capture_output=True,
                       env=clean_env())
        subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True, env=clean_env())
        shutil.rmtree(tmp, ignore_errors=True)


def _env(tree: Path) -> dict:
    paths = [str(tree / "src")]
    if (ROOT / ".deps").is_dir():
        paths.append(str(ROOT / ".deps"))  # dependencies are installed next to the main checkout only
    env = clean_env()
    env["PYTHONPATH"] = os.pathsep.join(paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def main(stdin_lines: List[str]) -> int:
    try:
        return _main(stdin_lines)
    except Exception as exc:  # never fail silently: say why the push was blocked
        print(f"[pre-push] BLOCKED: gate error {type(exc).__name__}: {str(exc)[:300]}")
        return 1


def _main(stdin_lines: List[str]) -> int:
    refs = [l.split() for l in stdin_lines if l.strip()]
    pushes = [(ls, rs) for _, ls, _, rs in refs if ls != ZERO]  # ignore branch deletions
    if not pushes:
        return 0
    failed = False
    for local_sha, remote_sha in pushes:
        commits = commits_to_push(local_sha, remote_sha)
        secrets = secret_gate(commits)
        if secrets:
            failed = True
            print("[pre-push] BLOCKED: possible secrets in commits being pushed:")
            print("\n".join(f"  - {s}" for s in secrets))
            print("  Remove them from history (not just a new commit) and rotate the credential.")
    config = config_gate()
    if config:
        failed = True
        print(f"[pre-push] BLOCKED: released risk config changed: {config}")
    if not failed:
        for local_sha, _ in pushes:
            if not test_gate(local_sha):
                failed = True
                print(f"[pre-push] BLOCKED: tests failed for {local_sha[:10]}")
    if failed:
        print("[pre-push] push aborted; your commit is still local. Fix and commit again.")
        return 1
    print("[pre-push] secret scan, risk config and tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.stdin.read().splitlines()))
