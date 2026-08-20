from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "public_file_manifest.json"
ALLOWLIST_PATH = ROOT / "public_audit_allowlist.json"
LICENSE_PATH = ROOT / "LICENSE"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUDIT_SCHEMA = "mapi_public_audit.v3"
ALLOWED_AUTHOR_NAME = "Michał Chlewicki"
ALLOWED_PUBLIC_EMAIL = "info@morenatech.work"
ALLOWED_GIT_IDENTITIES = {
    (ALLOWED_AUTHOR_NAME, ALLOWED_PUBLIC_EMAIL),
    ("Cabo0m", "58841864+cabo0m@users.noreply.github.com"),
    ("GitHub", "noreply@github.com"),
}
EXPECTED_APACHE_LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
CANONICAL_HTTPS_ORIGIN = "https://github.com/cabo0m/mapi-unified.git"
CANONICAL_SSH_ORIGIN = "git@github.com:cabo0m/mapi-unified.git"
RELEASE_TAG = re.compile(r"^v(?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")

FORBIDDEN_SUFFIXES = {
    ".7z",
    ".bak",
    ".bin",
    ".db",
    ".gz",
    ".log",
    ".npy",
    ".npz",
    ".patch",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".zip",
}
FORBIDDEN_NAMES = {".env"}
FORBIDDEN_PARTS = {
    ".agents",
    ".codex",
    ".pytest_cache",
    "__pycache__",
    "artifacts",
    "backups",
    "data",
    "logs",
    "tmp",
}

# Hex avoids embedding the sensitive values that the scanner rejects.
FORBIDDEN_TEXT_HEX = (
    "4a61676f6461204c756e61",  # private assistant full name
    "433a5c55736572735c6d69636861",  # private user path
    "433a5c6a61676f64612d6d656d6f72792d617069",  # private repository path
    "2f7372762f4669726d615f6d6f72656e61746563682e776f726b5f4a61676f6461",  # private server path
)
FORBIDDEN_PRIVATE_EMAIL_HEX = "6d696368616c2e63686c657769636b6940676d61696c2e636f6d"

SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "bearer_token": re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    "generic_secret": re.compile(
        r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"
    ),
}

POLISH_DIACRITICS = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
POLISH_WORDS = re.compile(
    r"(?i)\b(?:jest|brak|pamięć|pamiec|wspomnienie|wspomnienia|narzędzie|narzedzie|"
    r"uruchom|błąd|blad|zapis|odczyt|bezpieczeństwo|bezpieczenstwo|szukaj|pobierz|"
    r"porównaj|porownaj|raport|integralności|integralnosci|zdrowia|stwórz|stworz|"
    r"zwaliduj|właściciel|wlasciciel|użytkownik|uzytkownik)\b"
)
MOJIBAKE_MARKERS = ("Ă", "Ä", "Ĺ", "â€")

OBSOLETE_LICENSE_PHRASES = (
    "license selection is pending",
    "do not redistribute",
    "conditional go after a license is selected",
    "the owner must make the final decision",
    "not yet licensed",
    "license blocker",
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tracked_candidate_paths() -> list[Path]:
    ignored = {
        ".git",
        ".venv",
        ".ruff_cache",
        ".pytest_cache",
        "__pycache__",
        "build",
        "dist",
        "mapi_agent_memory.egg-info",
    }
    return sorted(
        (
            path
            for path in ROOT.rglob("*")
            if path.is_file()
            and not any(
                part in ignored or part.startswith(".pytest_tmp")
                for part in path.relative_to(ROOT).parts
            )
        ),
        key=lambda path: path.as_posix(),
    )


def _is_allowed_exception(
    allowlist: dict[str, list[str]],
    relative_path: str,
    rule: str,
) -> bool:
    return rule in set(allowlist.get(relative_path, []))


def _failure(failures: list[dict[str, str]], path: str, rule: str, detail: str = "") -> None:
    item = {"path": path, "rule": rule}
    if detail:
        item["detail"] = detail
    failures.append(item)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _config_values(key: str) -> list[str]:
    result = _git("config", "--get-all", key, check=False)
    if result.returncode not in {0, 1}:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _canonical_origin(url: str) -> str | None:
    accepted = {
        CANONICAL_HTTPS_ORIGIN: CANONICAL_HTTPS_ORIGIN,
        CANONICAL_HTTPS_ORIGIN.removesuffix(".git"): CANONICAL_HTTPS_ORIGIN,
        CANONICAL_SSH_ORIGIN: CANONICAL_SSH_ORIGIN,
        CANONICAL_SSH_ORIGIN.removesuffix(".git"): CANONICAL_SSH_ORIGIN,
    }
    return accepted.get(url)


def _scan_remote_policy(
    failures: list[dict[str, str]],
    result: dict[str, Any],
) -> None:
    remote_names = [line for line in _git("remote").stdout.splitlines() if line]
    configured_urls = _git(
        "config",
        "--get-regexp",
        r"^remote\..*\.(?:url|pushurl)$",
        check=False,
    )
    configured_url_lines = [
        line for line in configured_urls.stdout.splitlines() if line
    ]
    result["remotes"] = remote_names

    if not remote_names and not configured_url_lines:
        result["repository_state"] = "release_candidate"
        result["canonical_origin"] = None
        return

    result["repository_state"] = "invalid"
    result["canonical_origin"] = None
    if remote_names != ["origin"]:
        _failure(failures, ".git", "unexpected_remote_names")
        return

    fetch_urls = _config_values("remote.origin.url")
    push_urls = _config_values("remote.origin.pushurl")
    if len(fetch_urls) != 1:
        _failure(failures, ".git", "unexpected_origin_fetch_url_count")
        return
    if len(push_urls) > 1:
        _failure(failures, ".git", "unexpected_origin_push_url_count")
        return

    fetch_canonical = _canonical_origin(fetch_urls[0])
    push_canonical = _canonical_origin(push_urls[0]) if push_urls else fetch_canonical
    if fetch_canonical is None:
        _failure(failures, ".git", "origin_fetch_url_not_canonical")
        return
    if push_canonical is None:
        _failure(failures, ".git", "origin_push_url_not_canonical")
        return

    effective_fetch = [
        line
        for line in _git("remote", "get-url", "--all", "origin").stdout.splitlines()
        if line
    ]
    effective_push = [
        line
        for line in _git("remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
        if line
    ]
    if len(effective_fetch) != 1 or _canonical_origin(effective_fetch[0]) is None:
        _failure(failures, ".git", "origin_fetch_url_rewritten")
        return
    if len(effective_push) != 1 or _canonical_origin(effective_push[0]) is None:
        _failure(failures, ".git", "origin_push_url_rewritten")
        return

    result["repository_state"] = "published"
    result["canonical_origin"] = fetch_canonical


def _public_language_paths(paths: list[Path]) -> list[Path]:
    selected: list[Path] = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        if path.suffix.lower() == ".md":
            selected.append(path)
        elif relative in {".env.example", "pytest.ini"}:
            selected.append(path)
        elif relative.startswith(".github/") and path.suffix.lower() in {".yml", ".yaml"}:
            selected.append(path)
        elif relative.startswith("mapi/") and path.suffix.lower() == ".py":
            selected.append(path)
    return selected


def _scan_language_policy(
    failures: list[dict[str, str]],
    allowlist: dict[str, list[str]],
    paths: list[Path],
) -> int:
    checked = 0
    for path in _public_language_paths(paths):
        relative = path.relative_to(ROOT).as_posix()
        if _is_allowed_exception(allowlist, relative, "non_english_public_text"):
            continue
        text = path.read_text(encoding="utf-8")
        # The approved public author's proper name is not non-English prose.
        candidate = text.replace(ALLOWED_AUTHOR_NAME, "").replace(ALLOWED_PUBLIC_EMAIL, "")
        checked += 1
        if POLISH_DIACRITICS.search(candidate) or POLISH_WORDS.search(candidate):
            _failure(failures, relative, "non_english_public_text")
        if any(marker in candidate for marker in MOJIBAKE_MARKERS):
            _failure(failures, relative, "mojibake_public_text")

    from app.workshops.catalog import WORKSHOPS

    for workshop in WORKSHOPS.values():
        values = [workshop.purpose, *workshop.guardrails]
        values.extend(action.purpose for action in workshop.actions)
        candidate = "\n".join(values)
        if POLISH_DIACRITICS.search(candidate) or POLISH_WORDS.search(candidate):
            _failure(failures, f"workshop:{workshop.area}", "non_english_public_catalogue")
        if any(marker in candidate for marker in MOJIBAKE_MARKERS):
            _failure(failures, f"workshop:{workshop.area}", "mojibake_public_catalogue")
    return checked


def _scan_license_policy(failures: list[dict[str, str]], paths: list[Path]) -> dict[str, Any]:
    result = {"license": "Apache-2.0", "license_sha256": None}
    if not LICENSE_PATH.exists():
        _failure(failures, "LICENSE", "license_missing")
        return result

    normalized = LICENSE_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    result["license_sha256"] = digest
    if digest != EXPECTED_APACHE_LICENSE_SHA256:
        _failure(failures, "LICENSE", "license_not_standard_apache_2_0", digest)

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata.get("project", {})
    if project.get("license") != "Apache-2.0":
        _failure(failures, "pyproject.toml", "package_license_mismatch")
    authors = project.get("authors") or []
    if not any(
        item.get("name") == ALLOWED_AUTHOR_NAME and item.get("email") == ALLOWED_PUBLIC_EMAIL
        for item in authors
    ):
        _failure(failures, "pyproject.toml", "public_author_metadata_mismatch")
    for path in paths:
        if path.suffix.lower() != ".md":
            continue
        relative = path.relative_to(ROOT).as_posix()
        lowered = path.read_text(encoding="utf-8").casefold()
        for phrase in OBSOLETE_LICENSE_PHRASES:
            if phrase in lowered:
                _failure(failures, relative, "obsolete_license_language", phrase)
        if "gnu agpl" in lowered and relative != "docs/LICENSING.md":
            _failure(failures, relative, "unexpected_license_claim")

    if (ROOT / "docs" / "LICENSE_OPTIONS.md").exists():
        _failure(failures, "docs/LICENSE_OPTIONS.md", "obsolete_license_document")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "Apache License 2.0" not in readme or "(LICENSE)" not in readme:
        _failure(failures, "README.md", "readme_license_reference_missing")
    return result


def _scan_git_metadata(
    failures: list[dict[str, str]],
    *,
    synthetic_commit: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": (ROOT / ".git").exists(),
        "commit_count": None,
        "branches": [],
        "tags": [],
        "remotes": [],
        "notes": [],
        "reachable_blobs": 0,
        "repository_state": "invalid",
        "canonical_origin": None,
    }
    if not result["available"]:
        _failure(failures, ".git", "git_repository_missing")
        return result

    private_email = bytes.fromhex(FORBIDDEN_PRIVATE_EMAIL_HEX).decode("ascii")
    commits = [line for line in _git("rev-list", "--all").stdout.splitlines() if line]
    result["commit_count"] = len(commits)
    if not commits:
        _failure(failures, ".git", "git_history_missing")

    branches = [
        line
        for line in _git("for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.splitlines()
        if line
    ]
    result["branches"] = branches
    if synthetic_commit is None and branches != ["main"]:
        _failure(failures, ".git", "unexpected_branch", ",".join(branches))

    tags = [line for line in _git("tag", "--list").stdout.splitlines() if line]
    result["tags"] = tags
    invalid_tags = [tag for tag in tags if RELEASE_TAG.fullmatch(tag) is None]
    if invalid_tags:
        _failure(failures, ".git", "invalid_release_tag", ",".join(invalid_tags))

    notes = [
        line
        for line in _git("for-each-ref", "--format=%(refname)", "refs/notes").stdout.splitlines()
        if line
    ]
    result["notes"] = notes
    if notes:
        _failure(failures, ".git", "git_notes_not_allowed", ",".join(notes))

    _scan_remote_policy(failures, result)

    forbidden_text = tuple(bytes.fromhex(value).decode("utf-8") for value in FORBIDDEN_TEXT_HEX)
    blob_ids: set[str] = set()
    for commit in commits:
        record = _git(
            "show",
            "--no-patch",
            "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B",
            commit,
        ).stdout
        parts = record.split("\x00", 5)
        if len(parts) != 6:
            _failure(failures, commit, "git_commit_metadata_unreadable")
            continue
        _, author_name, author_email, committer_name, committer_email, message = parts
        author_identity = (author_name, author_email)
        committer_identity = (committer_name, committer_email)
        if commit != synthetic_commit and author_identity not in ALLOWED_GIT_IDENTITIES:
            _failure(failures, commit, "git_author_metadata_mismatch")
        if commit != synthetic_commit and committer_identity not in ALLOWED_GIT_IDENTITIES:
            _failure(failures, commit, "git_committer_metadata_mismatch")
        metadata_text = "\n".join(parts[1:]).casefold()
        if private_email.casefold() in metadata_text:
            _failure(failures, commit, "private_email_in_git_metadata")
        for value in forbidden_text:
            if value.casefold() in metadata_text:
                _failure(failures, commit, "private_identifier_in_git_metadata")
        for line in _git("ls-tree", "-r", "--format=%(objectname)", commit).stdout.splitlines():
            if line:
                blob_ids.add(line)

    result["reachable_blobs"] = len(blob_ids)
    scan_needles = (private_email, *forbidden_text)
    for blob_id in sorted(blob_ids):
        raw = _git("cat-file", "blob", blob_id).stdout
        lowered = raw.casefold()
        for needle in scan_needles:
            if needle.casefold() in lowered:
                _failure(failures, blob_id, "private_value_in_reachable_blob")

    logs_root = ROOT / ".git" / "logs"
    if logs_root.exists():
        for path in logs_root.rglob("*"):
            relative_log = path.relative_to(logs_root)
            if relative_log.parts[:2] == ("refs", "remotes"):
                continue
            if path.is_file() and private_email.casefold() in path.read_text(
                encoding="utf-8", errors="replace"
            ).casefold():
                _failure(
                    failures,
                    path.relative_to(ROOT).as_posix(),
                    "private_email_in_reflog",
                )
    return result


def audit_repository(*, synthetic_commit: str | None = None) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    if not MANIFEST_PATH.exists():
        return {
            "status": "failed",
            "schema": AUDIT_SCHEMA,
            "failures": [{"path": str(MANIFEST_PATH), "rule": "manifest_missing"}],
        }

    manifest = _load_json(MANIFEST_PATH)
    allowlist = _load_json(ALLOWLIST_PATH) if ALLOWLIST_PATH.exists() else {}
    expected = set(manifest["files"])
    actual_paths = _tracked_candidate_paths()
    actual = {path.relative_to(ROOT).as_posix() for path in actual_paths}

    for path in sorted(actual - expected):
        _failure(failures, path, "unexpected_file")
    for path in sorted(expected - actual):
        _failure(failures, path, "manifest_file_missing")

    forbidden_text = tuple(bytes.fromhex(value).decode("utf-8") for value in FORBIDDEN_TEXT_HEX)
    private_email = bytes.fromhex(FORBIDDEN_PRIVATE_EMAIL_HEX).decode("ascii")
    for path in actual_paths:
        relative = path.relative_to(ROOT)
        relative_text = relative.as_posix()
        lower_parts = {part.lower() for part in relative.parts}
        if path.name in FORBIDDEN_NAMES:
            _failure(failures, relative_text, "forbidden_name")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            _failure(failures, relative_text, "forbidden_file_type")
        if lower_parts & FORBIDDEN_PARTS:
            _failure(failures, relative_text, "forbidden_path_category")

        raw = path.read_bytes()
        if b"\x00" in raw and not _is_allowed_exception(allowlist, relative_text, "binary"):
            _failure(failures, relative_text, "unapproved_binary")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            if not _is_allowed_exception(allowlist, relative_text, "binary"):
                _failure(failures, relative_text, "unapproved_binary")
            continue
        lowered = text.casefold()
        if private_email.casefold() in lowered:
            _failure(failures, relative_text, "private_email")
        for value in forbidden_text:
            if value.casefold() in lowered and not _is_allowed_exception(
                allowlist, relative_text, "private_identifier"
            ):
                _failure(failures, relative_text, "private_identifier")
        if re.search(
            r"(?i)(?:[A-Z]:\\Users\\[^\\\s]+|[A-Z]:\\(?:path\\to\\mapi)?(?:\\|$)|/srv/[^\s'\"]+)",
            text,
        ):
            if not _is_allowed_exception(allowlist, relative_text, "absolute_path"):
                _failure(failures, relative_text, "absolute_path")
        for rule, pattern in SECRET_PATTERNS.items():
            if pattern.search(text) and not _is_allowed_exception(allowlist, relative_text, rule):
                _failure(failures, relative_text, rule)

    language_checked = _scan_language_policy(failures, allowlist, actual_paths)
    license_result = _scan_license_policy(failures, actual_paths)
    git_result = _scan_git_metadata(failures, synthetic_commit=synthetic_commit)

    return {
        "status": "ok" if not failures else "failed",
        "schema": AUDIT_SCHEMA,
        "file_count": len(actual),
        "manifest_sha256": hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
        "language_files_checked": language_checked,
        **license_result,
        "repository_state": git_result["repository_state"],
        "canonical_origin": git_result["canonical_origin"],
        "git": git_result,
        "failures": failures,
    }


def regenerate_manifest() -> dict[str, Any]:
    existing = _load_json(MANIFEST_PATH)
    files = [
        path.relative_to(ROOT).as_posix()
        for path in _tracked_candidate_paths()
    ]
    manifest = {
        "schema": existing["schema"],
        "source_commit": existing["source_commit"],
        "files": files,
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "written",
        "path": MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "file_count": len(files),
        "manifest_sha256": hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit the sanitized public repository.")
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="regenerate the canonical tracked-file inventory before auditing",
    )
    parser.add_argument(
        "--pull-request",
        action="store_true",
        help="allow the synthetic pull-request commit while still scanning all reachable history",
    )
    args = parser.parse_args()
    if args.write_manifest:
        print(json.dumps(regenerate_manifest(), indent=2))
        return

    synthetic_commit = None
    if args.pull_request:
        synthetic_commit = _git("rev-parse", "HEAD").stdout.strip()
    result = audit_repository(synthetic_commit=synthetic_commit)
    print(json.dumps(result, indent=2))
    if result["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
