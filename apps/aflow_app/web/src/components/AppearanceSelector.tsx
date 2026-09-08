import { useSyncExternalStore } from 'react'
import { getAppearance, setAppearance, subscribeAppearance, type Appearance } from '../theme'

export function AppearanceSelector() {
  const appearance = useSyncExternalStore(subscribeAppearance, getAppearance)
  return (
    <section className="card">
      <h2>Appearance</h2>
      <label className="dashboard-field">
        <span>Color theme</span>
        <select className="input" value={appearance} onChange={event => setAppearance(event.target.value as Appearance)}>
          <option value="system">System</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </label>
    </section>
  )
}
