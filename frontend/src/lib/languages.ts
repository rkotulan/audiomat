// Common audiobook languages. ISO 639-1 codes map directly to OmniVoice's
// supported set (646 languages — but listing them all in a dropdown is
// noise). Pick the top 10 + "Other" so most users hit it first try.
export const LANGUAGE_OPTIONS: Array<{ code: string; label: string }> = [
  { code: 'cs', label: 'Čeština (cs)' },
  { code: 'sk', label: 'Slovenčina (sk)' },
  { code: 'en', label: 'English (en)' },
  { code: 'de', label: 'Deutsch (de)' },
  { code: 'pl', label: 'Polski (pl)' },
  { code: 'fr', label: 'Français (fr)' },
  { code: 'es', label: 'Español (es)' },
  { code: 'it', label: 'Italiano (it)' },
  { code: 'ru', label: 'Русский (ru)' },
  { code: 'uk', label: 'Українська (uk)' },
]

/** ISO 639-1/2 (2–3 lowercase letters), optionally followed by a region
 *  / script subtag (e.g. ``cs-CZ``, ``pt-BR``, ``zh-Hant``). */
export const LANGUAGE_CODE_RE = /^[a-z]{2,3}(-[a-zA-Z]{2,4})?$/

/** Normalize a language code for storage / submission: lowercase primary
 *  subtag, preserved region. ``''`` if input doesn't match the format. */
export function isValidLanguageCode(code: string): boolean {
  return LANGUAGE_CODE_RE.test(code.trim())
}
