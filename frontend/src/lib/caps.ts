// Helpers for the v0.5 capabilities descriptor.
//
// `TTSCapabilities` lives inline on every `TTSModel` returned by
// `/api/models`. These helpers do the small lookups + value formatting
// every UI surface needs (Progress badges, Fine-tune dialog, license
// gate, matrix suppression) so components don't redo the same plumbing
// or accidentally branch on `backend === 'higgs'` literals.
import type {
  ParamSpec,
  PresetVariant,
  Project,
  TTSCapabilities,
  TTSModel,
} from './types'

// The reserved slug for the synthetic stock OmniVoice card returned
// by /api/models. Keep in sync with `audiomat.model_registry.DEFAULT_MODEL_SLUG`.
export const DEFAULT_MODEL_SLUG = 'default'

/** Resolve a project's `tts_model` field to the matching `TTSModel`
 *  entry from the registry listing. `null` / `""` / `"default"` →
 *  the synthetic stock card. Unknown slug → `null` so the caller can
 *  show a "fallback" notice (we don't silently swap; renderer falls
 *  back to stock on its own, but the UI should still flag the missing
 *  pointer). */
export function modelForProject(
  project: Pick<Project, 'tts_model'>,
  models: TTSModel[],
): TTSModel | null {
  const slug = project.tts_model
  if (!slug || slug === DEFAULT_MODEL_SLUG) {
    return models.find((m) => m.name_slug === DEFAULT_MODEL_SLUG) ?? null
  }
  return models.find((m) => m.name_slug === slug) ?? null
}

/** Resolve a project's capabilities. Falls back to the synthetic stock
 *  card's capabilities if the project's slug isn't in the list (e.g.
 *  the user deleted the registered fine-tune; the renderer falls back
 *  to stock OmniVoice so the UI should reflect the same caps). Throws
 *  if the synthetic stock entry is also missing — that means the
 *  models list is empty, which should never happen given the API
 *  always prepends it. */
export function capsForProject(
  project: Pick<Project, 'tts_model'>,
  models: TTSModel[],
): TTSCapabilities {
  const found = modelForProject(project, models)
  if (found) return found.capabilities
  const stock = models.find((m) => m.name_slug === DEFAULT_MODEL_SLUG)
  if (!stock) {
    throw new Error(
      'capsForProject: synthetic stock OmniVoice entry missing from /api/models',
    )
  }
  return stock.capabilities
}

/** Render a numeric param value the way the spec declares it should
 *  look. `decimals=0` + `suffix=""` renders "48", `decimals=2` +
 *  `suffix="×"` renders "1.00×". Cross-language identical to the
 *  Python `ParamSpec.format` (see `audiomat/tts_capabilities.py`). */
export function formatParam(spec: ParamSpec, value: number): string {
  const shown = spec.decimals === 0 && spec.kind === 'int'
    ? String(Math.round(value))
    : value.toFixed(spec.decimals)
  return shown + spec.suffix
}

/** True if the engine exposes ≥2 preset variants — i.e. the A/B preview
 *  matrix actually has something to compare. Higgs returns false here
 *  so the Preview tab can short-circuit to the "skip" explainer. */
export function hasPresetMatrix(caps: TTSCapabilities): boolean {
  return caps.preset_variants.length >= 2 && caps.params.length >= 1
}

/** Default param dict for an engine — `{spec.name: spec.default}` for
 *  every declared spec. Used when switching engines: the new project
 *  params reset to these defaults so stale OmniVoice knobs don't apply
 *  to a Higgs render. */
export function defaultParams(caps: TTSCapabilities): Record<string, number> {
  const out: Record<string, number> = {}
  for (const p of caps.params) {
    out[p.name] = p.default
  }
  return out
}

/** Find a single param spec by name. Linear scan — param lists are
 *  3–5 items at most, so the indexing overhead isn't worth it. */
export function findParam(
  caps: TTSCapabilities,
  name: string,
): ParamSpec | undefined {
  return caps.params.find((p) => p.name === name)
}

/** Find a preset by its stable key (`"fast" | "balanced" | …`). */
export function findPreset(
  caps: TTSCapabilities,
  key: string,
): PresetVariant | undefined {
  return caps.preset_variants.find((v) => v.key === key)
}
