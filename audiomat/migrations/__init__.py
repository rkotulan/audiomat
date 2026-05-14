"""One-shot data migrations for audiomat.

Each module here owns one migration step. The lifespan handler in
``audiomat/api.py`` invokes them at startup; they're written to be
idempotent so a second run on an already-migrated library is a
fast no-op.

Current migrations:

* :mod:`audiomat.migrations.v0_3_sqlite` — moves voice meta + project
  state + chunk manifests off the v0.2 JSON files into the
  audiomat.db tables introduced in v0.3.
"""
