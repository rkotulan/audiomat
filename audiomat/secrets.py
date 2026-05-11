"""Minimal credential store — single JSON file at
``<library_root>/secrets.json`` with mode 0o600.

Currently just the HF token; future fields go here too (e.g. Backblaze /
S3 keys for off-host backups). Not encrypted — fs perms are the
security boundary on a single-user host. Don't bind-mount this file
into anywhere it can leak.

The ``HF_TOKEN`` env var takes precedence over the stored token so a
Docker user can inject without writing to disk.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path


# Names of the slots in secrets.json. Single source of truth so we don't
# typo the key elsewhere.
HF_TOKEN_KEY = "hf_token"


def _load_all(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    # Best-effort chmod 0o600. On Windows this is a no-op (POSIX bits
    # ignored), but the file still lives under the user's library root
    # which inherits user-only ACLs by default.
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get_hf_token(secrets_path: Path) -> str | None:
    """Resolve the active HF token. Order of precedence:

    1. ``HF_TOKEN`` env var (Docker-friendly injection)
    2. ``hf_token`` field in ``secrets.json``
    3. None
    """
    env = os.environ.get("HF_TOKEN")
    if env:
        return env.strip() or None
    stored = _load_all(secrets_path).get(HF_TOKEN_KEY)
    return stored.strip() if stored else None


def set_hf_token(secrets_path: Path, token: str | None) -> None:
    """Store (or clear, when ``token`` is None / empty) the HF token in
    ``secrets.json``. Does not touch the ``HF_TOKEN`` env var."""
    data = _load_all(secrets_path)
    if not token or not token.strip():
        data.pop(HF_TOKEN_KEY, None)
    else:
        data[HF_TOKEN_KEY] = token.strip()
    _save_all(secrets_path, data)


def hf_token_source(secrets_path: Path) -> str | None:
    """Where the active token comes from — used by the settings endpoint
    so the UI can show ``Using HF_TOKEN env var`` vs ``Stored in
    secrets.json``. Returns None if no token is set."""
    if os.environ.get("HF_TOKEN", "").strip():
        return "env"
    stored = _load_all(secrets_path).get(HF_TOKEN_KEY)
    if stored and stored.strip():
        return "secrets_file"
    return None


if __name__ == "__main__":
    # Smoke test: round-trip a token through a tempfile.
    # `python -m audiomat.secrets`
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "secrets.json"
        print(f"empty: token={get_hf_token(path)!r} source={hf_token_source(path)!r}")
        set_hf_token(path, "hf_dummy_value")
        print(f"set  : token={get_hf_token(path)!r} source={hf_token_source(path)!r}")
        try:
            mode = oct(path.stat().st_mode & 0o777)
            print(f"mode : {mode}")
        except OSError as e:
            print(f"mode : (n/a — {e})")
        set_hf_token(path, None)
        print(f"clear: token={get_hf_token(path)!r} source={hf_token_source(path)!r}")
