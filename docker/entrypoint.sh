#!/usr/bin/env bash
# audiomat container entrypoint.
#
# Responsibilities:
#   * Ensure /data layout exists (voices/, projects/, cache/).
#   * Print a one-time banner with library paths.
#   * Exec the CMD (uvicorn by default).
#
# OmniVoice model weights download lazily on first /api/projects/<slug>/render
# call from k2-fsa/OmniVoice into HF_HOME (mounted to /data/cache/huggingface),
# so we don't pre-pull anything here. First render = ~3 GB download +
# ~5 minutes if cache cold. Subsequent runs reuse the volume.

set -e

mkdir -p "${AUDIOMAT_LIBRARY_ROOT:-/data}"/{voices,projects,cache}

echo "============================================================"
echo " audiomat — eBook → audiobook with cloned voices"
echo "------------------------------------------------------------"
echo " library root : ${AUDIOMAT_LIBRARY_ROOT:-/data}"
echo " HF cache     : ${HF_HOME:-/data/cache/huggingface}"
echo " serving on   : :7860 (mapped to host)"
echo "============================================================"

exec "$@"
