# audiomat

Convert eBooks into audiobooks with cloned voices, locally and offline.

> "Vlož knihu, vypadne audiokniha." — feed in a book, get an audiobook out.

**Status:** pre-alpha, active scaffolding. Not usable yet.

## What audiomat is

A focused, GPU-accelerated audiobook generator built around
[OmniVoice](https://github.com/k2-fsa/OmniVoice) (Apache-2.0). One-stack,
opinionated, optimized for Czech (works for 600+ languages because OmniVoice
does, but Czech narrative quality is the design target).

You give it:

- An EPUB or TXT file
- A 5–10 s WAV of a voice you want to clone, plus its transcript

audiomat produces:

- An M4B audiobook with chapter markers
- Per-chapter WAVs (loudness-normalized to -16 LUFS audiobook standard)
- Resumable per-chunk cache (interrupted run = restart picks up where it left off)

## Why not [pick another tool]

There are several solid open-source audiobook generators (epub2tts,
audiobook_maker, abogen, chatterbox-Audiobook, …). audiomat differs:

- **One TTS engine, not a selector.** OmniVoice fixed. Less surface, fewer bugs.
- **Czech-first.** Section-header pause injection, time-marker handling, 35 s
  reference voice clip workflow validated on a real 13:46 audiobook render.
- **Project-shaped UX.** Named projects (not session UUIDs), shared voice
  library re-usable across books, parameter-sweep preview matrix.
- **Premium defaults.** `num_step=48`, `guidance_scale=2.0` baked in —
  validated via direct A/B against an original human recording.

## Planned architecture (v0.1)

| layer | choice |
|---|---|
| Backend | **FastAPI** + uvicorn (Python) |
| Frontend | **Vite + React + TypeScript** SPA |
| Styling | **Tailwind + shadcn/ui** |
| Real-time progress | Server-Sent Events (SSE) |
| TTS engine | OmniVoice (fixed) |
| Inference device | GPU (CUDA) only |
| Input formats | EPUB + TXT |
| Project model | 1 book = 1 project, immutable name post-create |
| Voice library | shared `voices/`, re-usable across projects |
| Storage layout | `~/audiomat/{voices,projects,cache}/` (Docker: `/data/`) |
| Preview | 4-cell parameter matrix (Fast / Balanced / Crisp / Stable) |
| Render speed | OmniVoice native `speed` param (slider 0.7–1.3, default 1.0) |
| UI design tooling | [ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) for component generation |

Single Docker image: multi-stage build compiles the React frontend, then
copies `dist/` into the FastAPI container which serves both API and static
assets on one port (default 7860).

Detailed design doc to follow in `docs/` once code lands.

## Install

The supported install path is Docker (multi-stage image bundles backend +
frontend). Local Python is fine for development.

### Docker (recommended)

```bash
git clone https://github.com/rkotulan/audiomat.git
cd audiomat
docker compose up --build
# open http://localhost:7860
```

The compose file mounts a named volume at `/data` for voices, projects,
and the OmniVoice model cache (~3 GB, downloaded on first render).

NVIDIA GPU + recent driver required (CUDA 12.8 wheels in the image).

### Manual Docker

```bash
docker run --gpus all \
    -p 7860:7860 \
    -v audiomat-data:/data \
    -e AUDIOMAT_LIBRARY_ROOT=/data \
    ghcr.io/rkotulan/audiomat:latest    # not yet published
```

## Development setup

Backend (Python ≥ 3.11, NVIDIA GPU + CUDA 12.x):

```bash
cd audiomat/
python -m venv .venv
. .venv/bin/activate         # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Frontend (Node ≥ 20):

```bash
cd frontend/
npm install
npm run dev      # Vite on http://localhost:5173, proxies /api → :8000
```

Optional Claude Code design skill (recommended if you use Claude Code):

```bash
npm install -g uipro-cli
uipro init --ai claude     # installs ui-ux-pro-max into .claude/skills/
```

The skill is gitignored — install once locally; restart Claude Code to pick
it up.

## License

MIT — see [LICENSE](LICENSE).

OmniVoice model checkpoint is pulled at runtime from
[k2-fsa/OmniVoice](https://huggingface.co/k2-fsa/OmniVoice) (Apache-2.0).
audiomat does not redistribute model weights.

## Acknowledgments

- [k2-fsa/OmniVoice](https://github.com/k2-fsa/OmniVoice) — TTS engine
- [DrewThomasson/ebook2audiobook](https://github.com/DrewThomasson/ebook2audiobook)
  — UX inspiration for the Gradio flow
- Cyprien Oucortex (Chatterbox-TTS-Czech, YodaLingua-Czech) — Czech TTS
  evaluation guidance
