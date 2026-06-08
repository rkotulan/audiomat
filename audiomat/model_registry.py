"""Registry of user-installed TTS model checkpoints.

A "model" here = a directory containing what
``OmniVoice.from_pretrained(<local_path>)`` consumes (config.json,
model.safetensors, tokenizer files, etc.). The stock
``k2-fsa/OmniVoice`` pulled from Hugging Face on first use is **not**
in the registry — it's the implicit default; only user-added fine-tunes
and HF-sourced snapshots are tracked here.

A registry entry lives under ``models/<slug>/`` with:

* the checkpoint files themselves (whatever the source dir contained)
* ``meta.json`` — display name, source provenance, size, notes

Mirrors :mod:`audiomat.voice` in spirit (one directory per asset, plain
JSON metadata, lightweight CRUD).
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from audiomat.slug import slugify


# Slug used by the UI / config to mean "the stock public OmniVoice model
# (k2-fsa/OmniVoice). Reserved — registry CRUD refuses to register a
# user model with this slug to keep the special-case lookup unambiguous."""
DEFAULT_MODEL_SLUG = "default"
DEFAULT_MODEL_HF_ID = "k2-fsa/OmniVoice"


SourceType = Literal["local", "hf"]
Backend = Literal["omnivoice", "higgs"]
LicenseFlag = Literal["permissive", "non_commercial"]


@dataclass
class TTSModel:
    """One registered TTS model checkpoint."""

    name: str
    name_slug: str
    dir: Path
    source_type: SourceType         # "local" (path import) | "hf" (HF snapshot)
    source_ref: str                 # for "local": original src path; for "hf": "<org>/<repo>"
    hf_revision: str | None = None  # for "hf": pinned commit SHA / branch
    size_bytes: int = 0
    notes: str = ""
    created: str = ""
    # v0.4: backend drives the TTS class dispatch in
    # ``audiomat.state.get_tts``. "omnivoice" loads the OmniVoiceTTS
    # wrapper around the omnivoice pip package; "higgs" loads the
    # HiggsTTS wrapper around transformers + the multimodalart Higgs
    # Audio v3 port. Both adapters expose the same generate() signature
    # so the renderer doesn't need backend-specific branches.
    backend: Backend = "omnivoice"
    # User-facing license obligation surfaced in the UI (badge on the
    # Models page, confirm dialog when assigning to a voice). Audiomat
    # itself stays MIT regardless — this flag only documents the
    # weights' license so the operator knows what they're agreeing to.
    license: LicenseFlag = "permissive"

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    @property
    def is_valid(self) -> bool:
        """The minimum a from_pretrained call expects: a config.json plus
        at least one weights shard."""
        if not (self.dir / "config.json").exists():
            return False
        return any(
            self.dir.glob(pattern)
            for pattern in ("*.safetensors", "pytorch_model.bin",
                            "model.safetensors.index.json")
        )

    @property
    def from_pretrained_target(self) -> str:
        """What to pass to ``OmniVoice.from_pretrained(...)``. Always the
        local dir — even HF-sourced models live on disk after registration,
        so runtime is offline."""
        return str(self.dir.resolve())

    # -- IO --

    def save(self) -> None:
        """Write meta.json. Caller is responsible for placing the model
        files into ``self.dir`` first (via :meth:`register_local` or
        :meth:`register_hf`)."""
        meta = {
            "name": self.name,
            "name_slug": self.name_slug,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "hf_revision": self.hf_revision,
            "size_bytes": int(self.size_bytes),
            "notes": self.notes,
            "created": self.created or _utcnow_iso(),
            "backend": self.backend,
            "license": self.license,
        }
        self.meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, dir: Path) -> "TTSModel":
        meta_path = dir / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(meta_path)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return cls(
            name=meta["name"],
            name_slug=meta.get("name_slug") or slugify(meta["name"]),
            dir=dir,
            source_type=meta.get("source_type", "local"),
            source_ref=meta.get("source_ref", ""),
            hf_revision=meta.get("hf_revision"),
            size_bytes=int(meta.get("size_bytes", 0)),
            notes=meta.get("notes", ""),
            created=meta.get("created", ""),
            # Defaults for v0.3-and-earlier meta.json files that lack
            # these fields. Existing user registry entries keep working:
            # they all targeted OmniVoice which is Apache-2.0.
            backend=meta.get("backend", "omnivoice"),
            license=meta.get("license", "permissive"),
        )

    @classmethod
    def list_all(cls, models_root: Path) -> list["TTSModel"]:
        if not models_root.exists():
            return []
        out: list[TTSModel] = []
        for d in sorted(models_root.iterdir()):
            if not d.is_dir() or not (d / "meta.json").exists():
                continue
            try:
                out.append(cls.load(d))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return out

    @classmethod
    def find_by_slug(cls, models_root: Path, slug: str) -> "TTSModel | None":
        target = models_root / slug
        if not (target / "meta.json").exists():
            return None
        try:
            return cls.load(target)
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    @classmethod
    def register_local(
        cls,
        models_root: Path,
        name: str,
        src_dir: Path,
        notes: str = "",
        overwrite: bool = False,
        backend: Backend = "omnivoice",
        license: LicenseFlag = "permissive",
    ) -> "TTSModel":
        """Copy a local checkpoint directory into the registry.

        ``src_dir`` is expected to contain the files needed by the
        chosen backend's ``from_pretrained`` (config.json + weight
        shards for both omnivoice and Higgs ports). The files are
        **copied** (not symlinked) so deleting the source dir
        afterwards doesn't break the registry entry.

        ``backend`` selects the TTS adapter the renderer will dispatch
        to. ``license`` documents the weights' obligations for the UI;
        audiomat code stays MIT regardless."""
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

        if not src_dir.exists() or not src_dir.is_dir():
            raise FileNotFoundError(f"source dir missing: {src_dir}")

        target.mkdir(parents=True)
        size = 0
        for f in src_dir.iterdir():
            if f.is_file():
                dst = target / f.name
                shutil.copyfile(f, dst)
                size += dst.stat().st_size

        model = cls(
            name=name,
            name_slug=slug,
            dir=target,
            source_type="local",
            source_ref=str(src_dir.resolve()).replace("\\", "/"),
            size_bytes=size,
            notes=notes,
            created=_utcnow_iso(),
            backend=backend,
            license=license,
        )
        if not model.is_valid:
            # Roll back — the source dir wasn't a usable checkpoint.
            shutil.rmtree(target, ignore_errors=True)
            raise ValueError(
                f"source {src_dir} doesn't look like an OmniVoice "
                f"checkpoint (missing config.json or weight files)"
            )
        model.save()
        return model

    def delete(self) -> None:
        if self.dir.exists():
            shutil.rmtree(self.dir)


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_model_target(
    models_root: Path,
    tts_model: str | None,
) -> tuple[str, str | None]:
    """Resolve a voice's ``tts_model`` field to a ``from_pretrained``
    target plus optional revision.

    * ``None`` / empty / ``"default"`` → stock ``k2-fsa/OmniVoice``
      (loaded from HF, pinned by the revision baked into ``tts.py``).
    * Other slug → look up registry; raise KeyError if missing.

    Returns ``(target, revision)`` where ``target`` is what
    ``OmniVoice.from_pretrained`` gets and ``revision`` is None for local
    models (revision is meaningless for a local snapshot)."""
    if not tts_model or tts_model == DEFAULT_MODEL_SLUG:
        return DEFAULT_MODEL_HF_ID, None
    model = TTSModel.find_by_slug(models_root, tts_model)
    if model is None:
        raise KeyError(f"tts_model not found in registry: {tts_model!r}")
    return model.from_pretrained_target, None


if __name__ == "__main__":
    # Smoke test: round-trip a synthetic checkpoint through the registry.
    # `python -m audiomat.model_registry`
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Pretend checkpoint dir
        src = tmp_path / "fake_ckpt"
        src.mkdir()
        (src / "config.json").write_text('{"fake": true}\n')
        (src / "model.safetensors").write_bytes(b"\x00" * 4096)

        root = tmp_path / "models"
        root.mkdir()
        m = TTSModel.register_local(root, "Jezkova Test v1", src)
        print(f"registered : {m.name} ({m.name_slug}) size={m.size_bytes}B")
        print(f"target     : {m.from_pretrained_target}")
        print(f"is_valid   : {m.is_valid}")

        listed = TTSModel.list_all(root)
        print(f"list_all   : {[x.name for x in listed]}")

        target, rev = resolve_model_target(root, "Jezkova_Test_v1")
        print(f"resolve(slug)   : target={target} rev={rev}")
        target, rev = resolve_model_target(root, None)
        print(f"resolve(None)   : target={target} rev={rev}")
        target, rev = resolve_model_target(root, "default")
        print(f"resolve(default): target={target} rev={rev}")
