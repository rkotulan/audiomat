# audiomat

Self-hosted, GPU-accelerated audiobook generator built around
[OmniVoice](https://github.com/k2-fsa/OmniVoice) (Apache-2.0). Feed it
an EPUB and a 5–10 s reference voice clip; out comes an M4B audiobook
with chapter markers, narrated in the cloned voice.

## Tested on Czech

OmniVoice itself supports 600+ languages, but **audiomat's quality bar
was set on Czech**. The pipeline was validated by rendering a
13-hour-46-minute Czech audiobook end-to-end and A/B-ing it directly
against the original human narrator's recording. Czech-specific edge
cases handled out of the box:

- Section-header pause injection (`Podzim 1973`, `Podzim dva tisíce
  devatenáct` style POV/time markers)
- BCP 47 → ISO 639-1 normalization for EPUB DC language metadata
  (`cs-CZ` → `cs`) so digits don't get read character-by-character
- Number-to-text expansion via `num2words` with Czech glue
  normalization (`devětset` → `devět set`)
- Czech sentence splitter with abbreviation block-list
- Czech-aware Unicode → ASCII slugifier for filesystem paths

Other languages will likely work — they just haven't been validated
to the same depth.

## Quick start

```bash
docker run --gpus all -p 7860:7860 \
    -v audiomat-data:/data \
    kotulan/audiomat:latest
```

Open <http://localhost:7860>.

Or via compose:

```yaml
services:
  audiomat:
    image: kotulan/audiomat:latest
    ports: ["7860:7860"]
    volumes: [audiomat-data:/data]
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
volumes:
  audiomat-data:
```

## Requirements

- NVIDIA GPU with CUDA 12.8 support (tested on RTX 5070 Blackwell, 12 GB VRAM)
- NVIDIA Container Toolkit (`--gpus all`)
- ~7 GB image + ~3.3 GB OmniVoice weights, downloaded on first render
  and cached in the volume across container rebuilds. Set `HF_TOKEN`
  to skip the unauthenticated rate limit.

## What you give it

- An EPUB or TXT file
- A 5–10 s WAV of the voice you want to clone, plus its transcript

The reference audio and transcript must match in content — the model
uses the pair to learn the speaker's chars-per-second ratio.

## What you get

- **M4B** audiobook with chapter markers
- **Per-chapter WAVs** loudness-normalized to -16 LUFS (audiobook standard)
- **Resumable per-chunk cache** — interrupted runs restart where they
  left off; a manifest tracks which text produced which audio so
  re-renders only re-synth changed chunks

## Volume layout

```
/data/
├── voices/<slug>/   24 kHz mono WAV + transcript + meta.json
├── projects/<slug>/ config.json + book.epub + chunks/<NNN_stem>/ + final.m4b
└── cache/           HuggingFace model cache (~3.3 GB OmniVoice)
```

Override the library root with `AUDIOMAT_LIBRARY_ROOT=/path` (defaults
to `/data` in this image).

## Tags

| tag | description |
|---|---|
| `latest` | most recent release |
| `0.2.0` | voice picker matrix, long-source clip extractor, clone validator |
| `0.1.0` | pinned first public release |

## Links

- **Source / issues**: <https://github.com/rkotulan/audiomat>
- **TTS engine**: [k2-fsa/OmniVoice](https://huggingface.co/k2-fsa/OmniVoice) (Apache-2.0)

## License

MIT (code) — see [LICENSE](https://github.com/rkotulan/audiomat/blob/main/LICENSE).
OmniVoice model weights are pulled at runtime under their original
Apache-2.0 license; this image does not redistribute them.
