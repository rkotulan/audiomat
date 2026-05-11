"""FastAPI routers — split out of the original god-file api.py.

Each router owns a coherent slice of the API surface:

* :mod:`.system` — model status banner.
* :mod:`.voices` — voice library CRUD + draft staging.
* :mod:`.projects` — project CRUD + metadata patching.
* :mod:`.preview` — sample-text preview matrix + custom variant.
* :mod:`.chapters` — per-chapter listing + audio + cache reset.
* :mod:`.render` — render lifecycle (start / cancel / SSE progress) + M4B build.

``audiomat.api`` wires them via ``app.include_router(...)``.
"""
