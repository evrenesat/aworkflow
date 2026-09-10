/**
 * Turn a machine identifier into a sentence-style label without changing
 * names that are already intended for display.
 *
 * The raw identifier remains the value used by state, requests and DOM
 * identity; this helper is only for visible presentation text.
 */
export function formatMachineLabel(value: string): string {
  const trimmed = value.trim()
  if (!trimmed) return trimmed

  const tokens = trimmed.split(/_+/).filter(Boolean)
  if (!tokens.length) return ''
  if (tokens.some(token => !/^(?:[a-z0-9]+|[A-Z0-9]+)$/.test(token))) return value

  const label = tokens.join(' ')
  return label.charAt(0).toUpperCase() + label.slice(1)
}

function isMachineIdentifier(value: string): boolean {
  const trimmed = value.trim()
  const tokens = trimmed.split(/_+/).filter(Boolean)
  return tokens.length > 0 && tokens.every(token => /^(?:[a-z0-9]+|[A-Z0-9]+)$/.test(token))
}

/**
 * Format one machine identifier within a sibling choice collection.
 *
 * Distinct identifiers that collapse to the same readable label retain their
 * raw identifier as a visible suffix so users can select the intended entry.
 */
export function formatMachineChoice(value: string, siblings: readonly string[]): string {
  const label = formatMachineLabel(value)
  const collides = siblings.some((sibling) => sibling !== value && formatMachineLabel(sibling) === label)
  return collides && isMachineIdentifier(value) ? `${label} (${value})` : label
}
