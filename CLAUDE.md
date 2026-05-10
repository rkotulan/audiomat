# audiomat — Czech-first audiobook TTS app

Public app: turn EPUB/TXT + 5–10 s voice clip → M4B audiobook with cloned
voice. GPU-only, OmniVoice fixed, FastAPI + React stack. Predecessor /
research dir is `C:\Dev\skleneny-muz-tts\` (legacy CZ audiobook project,
not a git repo) — pipeline logic was developed there first and ported
here.

## Quick status (2026-05-10)

End-to-end built and live-tested. ~30+ local commits on `main`, **no
pushes yet** (user reviews before going public). Working tree clean.
Active test target: **Rezavý les v1** (Anders de la Motte EPUB, ~128
chapters, voice = Jitka Ježková).

## Hardware target

- **GPU**: NVIDIA RTX 5070 Blackwell sm_120, 12 GB VRAM (CUDA 12.8)
- **OS**: Windows 11, PowerShell 5.1 (cp1250 codepage). Non-ASCII home
  dir `C:\Users\Táta` — npm chokes; see "npm wrapper" gotcha below.
- **PyTorch**: 2.8.0+cu128 (CUDA 12.8 wheels, Blackwell-compatible)

## Stack

| layer | choice |
|---|---|
| Backend | **FastAPI** + uvicorn (Python ≥3.11), single-file `audiomat/api.py` |
| Frontend | **Vite + React 19 + TypeScript 6** SPA in `frontend/` |
| Styling | **Tailwind v4 + shadcn/ui** (new-york style, neutral base) |
| Real-time progress | Server-Sent Events (`sse-starlette`) |
| TTS engine | **OmniVoice fixed** (`k2-fsa/OmniVoice` 0.1.5, Apache-2.0) |
| Inference device | GPU only (CUDA), no CPU mode |
| Input formats | EPUB + TXT |
| UI design tooling | [`ui-ux-pro-max-skill`](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) (per-dev, gitignored) |
| License | MIT (code), Apache-2.0 (model, fetched at runtime) |
| Container | Multi-stage Docker: Node 22 → CUDA 12.8 + Python 3.12, port 7860 |

**Production OmniVoice params** (premium default, baked in):
`num_step=48`, `guidance_scale=2.0`, `language="cs"`, `dtype=float16`,
`speed=1.0`. Validated via direct A/B against original Ježková recording
in skleneny-muz-tts.

## Repo layout

```
C:\Dev\audiomat\
├── audiomat/                 ← Python package (backend)
│   ├── api.py                ← FastAPI app, ~25 routes, SSE progress
│   ├── render.py             ← ProjectRenderer (render_all + render_indices)
│   ├── tts.py                ← OmniVoiceTTS wrapper, lazy load
│   ├── audio.py              ← ffmpeg conversion + concat-loudnorm + M4B
│   ├── epub.py               ← ebooklib + bs4 + Czech sentence splitter
│   ├── headers.py            ← inject_header_pause + strip_markers
│   ├── chunker.py            ← 90–200 char chunking
│   ├── num2text.py           ← num2words wrapper, CZ glue normalization
│   ├── slug.py               ← Czech-aware Unicode → ASCII slugifier
│   ├── voice.py              ← Voice dataclass + library CRUD
│   ├── project.py            ← Project + RenderParams + ProjectStatus
│   ├── paths.py              ← AudiomatPaths, env override
│   └── transcribe.py         ← faster-whisper draft transcription
├── frontend/
│   └── src/
│       ├── App.tsx           ← React Router, 6 pages
│       ├── pages/            ← Landing, Voices, VoiceNew, Projects,
│       │                       ProjectNew, ProjectDetail
│       ├── components/
│       │   ├── ConfirmDialog.tsx  ← useConfirm() hook (shadcn AlertDialog)
│       │   ├── Layout.tsx
│       │   └── ui/                ← shadcn primitives (alert-dialog,
│       │                            button, card, checkbox, dialog,
│       │                            input, label, progress, separator,
│       │                            slider, tabs, textarea, badge)
│       └── lib/                   ← api.ts, types.ts, utils.ts
├── docker/entrypoint.sh
├── Dockerfile                 ← multi-stage frontend → CUDA runtime
├── docker-compose.yml
├── pyproject.toml             ← package metadata (no entry-point yet)
└── requirements.txt           ← torch+cu128, omnivoice, fastapi, ebooklib,
                                 faster-whisper, num2words, soundfile, …
```

## Filesystem layout (runtime data, gitignored)

```
~/audiomat/                       (Docker volume: /data/)
├── voices/<slug>/                ← shared library
│   ├── voice.wav                 ← 24 kHz mono 16-bit, 5–10 s
│   ├── voice.txt                 ← matching transcript
│   └── meta.json
├── projects/<slug>/              ← per-book, self-contained
│   ├── config.json               ← params, status, voice ref pointer
│   ├── book.epub                 ← copied on import
│   ├── chunks/<NNN_stem>/        ← OmniVoice render output
│   │   ├── chunks/chunk_NNNN.wav
│   │   ├── manifest.json         ← {chunk_NNNN.wav: <text it was synthesized from>}
│   │   └── <NNN_stem>.wav        ← loudnorm-ed concat
│   ├── final.m4b
│   └── render_log.txt
└── cache/                        ← HF model cache (~3 GB OmniVoice)
```

Override library root via env: `AUDIOMAT_LIBRARY_ROOT=/data` (Docker).

## API surface (`audiomat/api.py`)

- **voices**: `GET /voices`, `GET /voices/{slug}`, `POST /voices/draft-upload`,
  `POST /voices/auto-transcribe`, `POST /voices`, `DELETE /voices/{slug}`,
  `GET /voices/{slug}/audio`, `GET /voices/draft-audio`
- **projects**: `GET /projects`, `POST /projects` (auto-skip front-matter
  via `_auto_skip_indices`), `GET /projects/{slug}`,
  `DELETE /projects/{slug}`, `PATCH /projects/{slug}/params`,
  `PATCH /projects/{slug}/blocks-skipped` (auto-prunes orphan chunks)
- **chapters**: `GET /projects/{slug}/chapters`,
  `GET /projects/{slug}/chapter-audio/{stem}`,
  `DELETE /projects/{slug}/chapters/{stem}` (force re-render)
- **preview**: `POST /projects/{slug}/preview-matrix` (4 fixed presets:
  Fast 32/2.0, **Balanced 48/2.0** ⭐, Crisp 48/2.5, Stable 64/2.0),
  `POST /projects/{slug}/preview-custom` (Fine tune dialog target),
  `GET /projects/{slug}/preview-audio/{filename}`
- **render**: `POST /projects/{slug}/render` (accepts `{indices: [...]}`
  for selective), `POST /projects/{slug}/cancel-render`,
  `GET /projects/{slug}/progress` (SSE stream — `chunk_synthed` events
  carry `text_chars` + `gen_seconds` for ETA),
  `POST /projects/{slug}/build-m4b`, `GET /projects/{slug}/m4b`

## UI flow (ProjectDetail tabs)

**Overview** (book info + Next button) → **Preview** (4-cell matrix +
per-variant Fine tune dialog) → **Render** (3-button action row: Render
all / Render pending / Render selected, plus Stop button when busy;
live ETA + chapters list with Re-render + Skip toggles + inline audio
per rendered row) → **Output** (M4B build / download / rebuild) →
**Advanced** (only OutputParamsCard + Danger zone delete — voice-synth
params live on Preview tab via Fine tune).

## Known gotchas (carry these across sessions)

### FastAPI route registration order matters
Literal paths must be registered **before** `{slug}` catchalls.
Example bug: `GET /voices/draft-audio` registered AFTER
`GET /voices/{slug}` is shadowed → silent 404 with wrong message.
**Apply**: when adding new GET routes under a prefix that has a `{slug}`
pattern, place them ABOVE the wildcard in source order. (See commit
`beb4431`.)

### Manifest cache hashes only chunk TEXT, not voice/params
Changing `voice_ref`, `num_step`, `guidance_scale`, or `speed` on
already-rendered chapters produces NO cache invalidation — next render
returns stale audio. **Workaround**: per-row Re-render button wipes
`chunks/<stem>/`. **Future fix**: include voice slug + params signature
in the cache key.

### EPUB DRM watermarks (Palmknihy, copyright/imprint)
First user upload of Rezavý les rendered the Palmknihy notice as
chapter 001. **Apply**: `_auto_skip_indices(blocks)` in `api.py` scans
first 10 blocks against `_METADATA_PATTERNS` (Palmknihy, Copyright, ©,
ISBN, "Published by", "Translation ©", autorského práva, …). New
projects auto-populate `book.blocks_skipped`; existing projects toggle
Skip per row.

### `bs4.BeautifulSoup(html_bytes, "lxml")` floods uvicorn logs with `XMLParsedAsHTMLWarning`
Suppress locally with `warnings.catch_warnings()` +
`simplefilter("ignore", XMLParsedAsHTMLWarning)`. **Don't** switch to
xml parser — too strict for real EPUBs. (See commit `a06eec2`.)

### ETA based on wall elapsed lies when first chapter is tiny
User saw "ETA 27h" when first selected chapter was 21 chars (Neděle).
**Why**: rate = doneChars / wall_elapsed conflated TTS model load + HF
cache check + per-chunk fixed cost. **Apply**: track `gen_seconds` +
`text_chars` per `chunk_synthed` event, use those for the rate
denominator. Threshold at synthSeconds > 1.5 AND synthChars > 150
before quoting any rate. Also scope ETA to active job's `renderScope`,
not whole book. (Commits `308fc31`, `87e9c34`, `4e8e9d9`.)

### Czech number expansion via num2words 'cs' produces glued forms
("devětset", "dvěstě"). Skleněný muž JSON style (which produces better
TTS audio) prefers spaces ("devět set", "dvě stě"). **Apply**:
`audiomat/num2text.py` regex post-processes num2words output. Add new
patterns only if a real audio sample noticeably degrades.

### EPUB DC language metadata is BCP 47 ("cs-CZ"), num2words wants ISO 639-1 ("cs")
`cs-CZ` would silently fail num2words → NotImplementedError caught →
digits stay raw → TTS reads them character-by-character. **Apply**:
`num2text.normalize_lang(lang)` strips BCP 47 region/script suffix.
Always pass `project.book.language` through this before downstream use.

### Section header detection covers `<h1>-<h6>` HTML tags + Czech time-marker regex
("Podzim 1973" / "Podzim dva tisíce devatenáct"). For EPUBs that use
plain `<p>` for headers, user must manually populate
`params.section_headers` in `config.json`. (Commits `b689014`,
`2aedff0`.)

### Mid-chapter cache loss
`render.py` persists manifest **per chunk** now (crash-safe, post-fix).
If you see manifest only persisted at end of `render_block()`, the
crash-safety fix has regressed.

## Dev workflow

### Backend
```pwsh
cd C:\Dev\audiomat
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn audiomat.api:app --reload --port 8000
```

### Frontend
```pwsh
cd C:\Dev\audiomat\frontend
npm install
npm run dev   # Vite on http://localhost:5173, proxies /api → :8000
```

### npm wrapper for non-ASCII home dir
`C:\Users\Táta` makes npm/npx fail with EPERM mkdir errors. **Apply**:
prefix all npm/npx invocations with:

```bash
NPM_CONFIG_CACHE=C:\npm-cache \
NPM_CONFIG_USERCONFIG=C:\npm-config\.npmrc \
HOME=C:\npm-config \
USERPROFILE=C:\npm-config \
npm <cmd>
```

Pre-create `C:\npm-cache` and `C:\npm-config` once.

### Docker
```bash
docker compose up --build   # http://localhost:7860
```

Multi-stage: Node 22 builder → CUDA 12.8 + Python 3.12 runtime. NVIDIA
GPU + recent driver required.

## Testing pattern

No automated tests yet (only per-module `__main__` smoke tests +
FastAPI TestClient one-off checks). User tests by **manually clicking
through the actual flow in the browser + screenshots when something is
off**. When user reports a bug, ask for a screenshot if not provided;
otherwise trust description and trace the user flow before guessing.

## What NOT to do

- **Don't push to GitHub** without explicit instruction. All commits
  stay local until user reviews. After a commit, mention the local hash
  and stop — don't follow up with `git push`.
- **Don't bundle small fixes into one commit.** One commit per logical
  change, even tiny UX nits — makes review and bisect cleaner.
- **Don't redistribute Ježková-specific voices, references, or training
  data publicly** (CZ §81–90 OZ personality rights). For audiomat
  public release, ship only royalty-free / consented neutral demo voice.
- **Don't regenerate** `~/audiomat/voices/<slug>/voice.txt` files
  blindly with whisper-medium — CZ accuracy is poor (Egipský / zrsky /
  přez / podníky); user revises manually before save.
- **Don't change `voice_ref` / `num_step` / `gs` / `speed`** and expect
  manifest cache to invalidate — it doesn't. Use Re-render button per
  row, or wipe `chunks/<stem>/` manually.
- **Don't bump OmniVoice ref length above ~10 s** — past 20 s the model
  warns every call AND is 2.6× slower per chunk.
- **Don't use OmniVoice with mismatched `ref_audio` and `ref_text`
  content** — model interprets it as a fast-speaking speaker → drmoleni
  output.
- **Don't write `Out-File` on Czech text** without `-Encoding utf8`
  (default UTF-16 LE BOM breaks downstream readers). Set
  `$env:PYTHONIOENCODING = "utf-8"` for stdout/stderr.

## Sister project (predecessor)

`C:\Dev\skleneny-muz-tts\` — original Czech audiobook project. Has its
own CLAUDE.md with full Stage 1–3 history (S2.cpp Vulkan build,
Chatterbox-CZ evaluation, OmniVoice param A/B). Contains production
M4B `Skleneny_muz_omnivoice.m4b` (354 MB, 12:47:31), per-chapter cache
in `chapter_outputs_omnivoice_step48_gs2/`, all original render scripts
(`render_omnivoice.py`, etc), reference clips in `reference/`. Not a
git repo. Reference for pipeline logic + license-free experimental
artifacts.

When user says "pokračujeme s audiomat" / "stage 4" → default working
dir is here (`C:\Dev\audiomat\`). When user says "skleněný muž" or
references a script like `render_omnivoice.py` directly → that's the
predecessor.
