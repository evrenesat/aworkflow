import type { ProjectReadiness } from './types'

export function readinessLabel(readiness: ProjectReadiness): string {
  if (readiness === 'ready') return 'Ready'
  if (readiness === 'blocked') return 'Blocked'
  return 'Configuration required'
}

export function readinessClass(readiness: ProjectReadiness): string {
  if (readiness === 'ready') return 'status-pill status-ready'
  if (readiness === 'blocked') return 'status-pill status-blocked'
  return 'status-pill status-config'
}
