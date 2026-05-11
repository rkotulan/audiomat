"""Hugging Face Hub helpers — model snapshot download + repo discovery.

Audiomat treats HF as a transport for sharing fine-tunes between users,
**not** as a runtime dependency. Downloads land in the local registry
under ``<library_root>/models/<slug>/``; subsequent loads are offline.

The HF token, when needed, comes from :mod:`audiomat.secrets` (which
honors the ``HF_TOKEN`` env var). Per-call ``token`` overrides storage
so the UI can "try this token once" before persisting.
"""
from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from audiomat.model_registry import (
    DEFAULT_MODEL_SLUG,
    TTSModel,
    _utcnow_iso,
)
from audiomat.slug import slugify


# --------------------------------------------------------------------
# Repo discovery
# --------------------------------------------------------------------


@dataclass
class HFRepoInfo:
    """Subset of huggingface_hub's RepoInfo we surface to the UI."""
    repo_id: str
    private: bool
    last_modified: str           # ISO 8601
    size_bytes: int              # sum of sibling sizes (best-effort, may be 0)
    tags: list[str]


def list_user_repos(token: str, repo_type: str = "model") -> list[HFRepoInfo]:
    """Return repos owned or accessible by the token holder. Used by the
    "Browse my HF models" picker so users don't have to remember the
    exact repo id. Public + private both returned — UI shows a lock
    badge for private ones.

    Raises ``RuntimeError`` if the token is invalid or auth fails."""
    from huggingface_hub import HfApi
    from huggingface_hub.utils import HfHubHTTPError

    api = HfApi(token=token)
    try:
        whoami = api.whoami()
    except HfHubHTTPError as e:
        raise RuntimeError(f"HF token rejected: {e}") from e
    user = whoami.get("name") or whoami.get("email") or ""
    if not user:
        raise RuntimeError("HF whoami returned no user")

    try:
        repos = list(api.list_models(author=user))
    except HfHubHTTPError as e:
        raise RuntimeError(f"HF list_models failed: {e}") from e

    out: list[HFRepoInfo] = []
    for r in repos:
        size = 0
        for sib in getattr(r, "siblings", None) or []:
            sz = getattr(sib, "size", None)
            if sz:
                size += sz
        out.append(HFRepoInfo(
            repo_id=r.id,
            private=bool(getattr(r, "private", False)),
            last_modified=str(getattr(r, "last_modified", "") or ""),
            size_bytes=size,
            tags=list(getattr(r, "tags", []) or []),
        ))
    return out


def probe_repo_size(repo_id: str, token: str | None, revision: str | None) -> int:
    """Pre-flight size estimate for a download. Sums sibling sizes from
    repo_info. Returns 0 if the API doesn't expose sizes (older HF Hub
    servers); the caller should treat 0 as 'unknown total'."""
    from huggingface_hub import HfApi
    from huggingface_hub.utils import HfHubHTTPError
    api = HfApi(token=token)
    try:
        info = api.repo_info(repo_id, revision=revision, files_metadata=True)
    except HfHubHTTPError:
        return 0
    total = 0
    for sib in info.siblings or []:
        sz = getattr(sib, "size", None) or 0
        total += sz
    return total


# --------------------------------------------------------------------
# Snapshot download → registry
# --------------------------------------------------------------------


def _dir_size_bytes(path: Path) -> int:
    """Recursive size of regular files, skipping symlinks (HF cache
    layout puts blobs behind symlinks)."""
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_symlink():
                continue
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def download_to_registry(
    models_root: Path,
    name: str,
    repo_id: str,
    revision: str | None = None,
    token: str | None = None,
    notes: str = "",
    overwrite: bool = False,
    progress_cb: Callable[[int, int], None] | None = None,
) -> TTSModel:
    """Download an HF model snapshot into the local registry as the
    given ``name``.

    Files land at ``<models_root>/<slug(name)>/``. Existing dir + same
    slug → FileExistsError unless ``overwrite=True``.

    ``progress_cb(downloaded_bytes, total_bytes)`` fires every ~500 ms
    from a watcher thread while the snapshot pulls. ``total_bytes`` is
    a pre-flight estimate from ``repo_info``; may be 0 if the HF API
    didn't populate sizes (caller should display indeterminate progress
    in that case).

    Returns the registered :class:`TTSModel`. On failure, the partial
    target dir is wiped and the exception propagates.
    """
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import HfHubHTTPError

    slug = slugify(name)
    if slug == DEFAULT_MODEL_SLUG:
        raise ValueError(
            f"slug {slug!r} is reserved for the stock OmniVoice model"
        )
    target = models_root / slug
    if target.exists() and not overwrite:
        raise FileExistsError(f"model already registered: {target}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    total_bytes = probe_repo_size(repo_id, token, revision)

    # Watcher thread polls disk size and forwards to progress_cb. Stops
    # via the stop_event once snapshot_download returns.
    stop_event = threading.Event()

    def _watch() -> None:
        while not stop_event.is_set():
            if progress_cb is not None:
                try:
                    progress_cb(_dir_size_bytes(target), total_bytes)
                except Exception:
                    pass
            stop_event.wait(0.5)

    watcher = threading.Thread(target=_watch, daemon=True, name=f"hf-progress-{slug}")
    if progress_cb is not None:
        watcher.start()

    try:
        snapshot_download(
            repo_id=repo_id,
            revision=revision,
            token=token,
            local_dir=str(target),
            local_dir_use_symlinks=False,    # real files — registry is portable
        )
    except HfHubHTTPError as e:
        stop_event.set()
        if progress_cb is not None:
            watcher.join(timeout=2)
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"HF download failed for {repo_id}: {e}") from e
    except Exception:
        stop_event.set()
        if progress_cb is not None:
            watcher.join(timeout=2)
        shutil.rmtree(target, ignore_errors=True)
        raise

    stop_event.set()
    if progress_cb is not None:
        watcher.join(timeout=2)
        # Final 100 % tick so the UI doesn't sit at 99.9 % after the
        # blob writes finish but before the watcher's last poll.
        try:
            final = _dir_size_bytes(target)
            progress_cb(final, max(total_bytes, final))
        except Exception:
            pass

    # Resolve final commit (revision may have been a branch name).
    pinned_revision = revision
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=token)
        info = api.repo_info(repo_id, revision=revision)
        pinned_revision = info.sha or revision
    except Exception:
        pass

    size = _dir_size_bytes(target)
    model = TTSModel(
        name=name,
        name_slug=slug,
        dir=target,
        source_type="hf",
        source_ref=repo_id,
        hf_revision=pinned_revision,
        size_bytes=size,
        notes=notes,
        created=_utcnow_iso(),
    )
    if not model.is_valid:
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError(
            f"HF repo {repo_id} downloaded successfully but doesn't look "
            f"like an OmniVoice checkpoint (missing config.json or weights)"
        )
    model.save()
    return model


def redownload(
    models_root: Path,
    existing: TTSModel,
    token: str | None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> TTSModel:
    """Re-pull an HF-sourced model in place. Useful if local files were
    corrupted or the user wants the latest commit (we re-pin to whatever
    revision the existing entry was recorded with, so this is a *fresh
    copy* of the same content by default).

    Errors if ``existing.source_type != 'hf'`` — local models have no
    remote to re-download from."""
    if existing.source_type != "hf":
        raise ValueError(
            f"can only re-download HF-sourced models; "
            f"{existing.name_slug!r} is source_type={existing.source_type!r}"
        )
    return download_to_registry(
        models_root,
        name=existing.name,
        repo_id=existing.source_ref,
        revision=existing.hf_revision,
        token=token,
        notes=existing.notes,
        overwrite=True,
        progress_cb=progress_cb,
    )


# --------------------------------------------------------------------
# Streaming download — generator-based for SSE consumers
# --------------------------------------------------------------------


@dataclass
class DownloadProgress:
    """One progress tick fired by :func:`stream_download_to_registry`."""
    kind: str                     # "started" | "progress" | "complete" | "error"
    downloaded_bytes: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    message: str | None = None
    model_slug: str | None = None


def stream_download_to_registry(
    models_root: Path,
    name: str,
    repo_id: str,
    revision: str | None = None,
    token: str | None = None,
    notes: str = "",
    overwrite: bool = False,
) -> Iterator[DownloadProgress]:
    """Same as :func:`download_to_registry` but yields progress events
    instead of taking a callback. The actual download runs in a worker
    thread; this generator pumps events into the SSE pipe.

    Events:
      * ``started``     — total_bytes (may be 0 if unknown)
      * ``progress``    — downloaded_bytes + percent (capped 0–100)
      * ``complete``    — model_slug of the newly registered entry
      * ``error``       — message
    """
    yield DownloadProgress(kind="started", total_bytes=probe_repo_size(repo_id, token, revision))

    result: dict = {"model": None, "error": None}
    last_pct = -1.0
    queue: list[tuple[int, int]] = []
    lock = threading.Lock()
    stop_event = threading.Event()

    def cb(downloaded: int, total: int) -> None:
        with lock:
            queue.append((downloaded, total))

    def worker() -> None:
        try:
            result["model"] = download_to_registry(
                models_root, name=name, repo_id=repo_id, revision=revision,
                token=token, notes=notes, overwrite=overwrite, progress_cb=cb,
            )
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        finally:
            stop_event.set()

    t = threading.Thread(target=worker, daemon=True, name=f"hf-download-{slugify(name)}")
    t.start()

    while not stop_event.is_set() or queue:
        if queue:
            with lock:
                downloaded, total = queue[-1]
                queue.clear()
            pct = (downloaded / total * 100.0) if total > 0 else 0.0
            pct = max(0.0, min(100.0, pct))
            if pct - last_pct >= 0.5 or pct == 100.0:
                yield DownloadProgress(
                    kind="progress",
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    percent=pct,
                )
                last_pct = pct
        else:
            time.sleep(0.25)

    t.join(timeout=5)

    if result["error"]:
        yield DownloadProgress(kind="error", message=result["error"])
        return
    model = result["model"]
    if model is None:
        yield DownloadProgress(kind="error", message="download finished with no model registered")
        return
    yield DownloadProgress(
        kind="complete",
        model_slug=model.name_slug,
        downloaded_bytes=model.size_bytes,
        total_bytes=model.size_bytes,
        percent=100.0,
    )


if __name__ == "__main__":
    # Smoke test: just verify imports + that the public surface
    # constructs without errors. Real HF traffic requires a token and
    # isn't exercised here.
    print("HFRepoInfo example:", HFRepoInfo(
        repo_id="example/foo", private=True,
        last_modified="2026-05-11T00:00:00Z",
        size_bytes=1234, tags=["tts"],
    ))
    print("DownloadProgress example:", DownloadProgress(kind="started"))
    print("Module imports OK.")
