export type Appearance = 'system' | 'light' | 'dark'
export const APPEARANCE_KEY = 'aflow.appearance'
const normalize = (value: unknown): Appearance => value === 'light' || value === 'dark' ? value : 'system'
let preference: Appearance = 'system'
const subscribers = new Set<() => void>()

export function getAppearance(): Appearance { return preference }

function apply() {
  const dark = preference === 'dark' || (preference === 'system' && window.matchMedia?.('(prefers-color-scheme: dark)').matches)
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light'
  subscribers.forEach(listener => listener())
}

export function setAppearance(value: Appearance) {
  preference = normalize(value)
  try { localStorage.setItem(APPEARANCE_KEY, preference) } catch { /* Browser storage may be disabled. */ }
  apply()
}

export function subscribeAppearance(listener: () => void) {
  subscribers.add(listener)
  return () => { subscribers.delete(listener) }
}

export function initializeTheme() {
  try { preference = normalize(localStorage.getItem(APPEARANCE_KEY)) } catch { preference = 'system' }
  const media = window.matchMedia?.('(prefers-color-scheme: dark)')
  const onStorage = (event: StorageEvent) => {
    if (event.key === APPEARANCE_KEY || event.key === null) {
      preference = normalize(event.newValue)
      apply()
    }
  }
  media?.addEventListener('change', apply)
  window.addEventListener('storage', onStorage)
  apply()
  return () => {
    media?.removeEventListener('change', apply)
    window.removeEventListener('storage', onStorage)
  }
}
