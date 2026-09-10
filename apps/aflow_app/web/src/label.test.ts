import { describe, expect, it } from 'vitest'
import { formatMachineChoice, formatMachineLabel } from './label'

describe('formatMachineLabel', () => {
  it.each([
    ['', ''],
    ['   ', ''],
    ['implementation_plans', 'Implementation plans'],
    ['alpha__beta___gamma', 'Alpha beta gamma'],
    ['HTTP_API', 'HTTP API'],
    ['worker', 'Worker'],
  ])('formats %j as %j', (value, expected) => {
    expect(formatMachineLabel(value)).toBe(expected)
  })

  it.each(['Custom Display', 'customName', 'Prompt key', '{NEXT_CP}', 'Camel_Case'])('leaves %j unchanged', value => {
    expect(formatMachineLabel(value)).toBe(value)
  })

  it('keeps unique choices compact', () => {
    expect(formatMachineChoice('fast_team', ['fast_team', 'slow_team'])).toBe('Fast team')
    expect(formatMachineChoice('HTTP_API', ['HTTP_API', 'worker'])).toBe('HTTP API')
  })

  it('shows raw identifiers when readable choices collide', () => {
    const siblings = ['fast_team', 'fast__team', 'slow_team']
    expect(formatMachineChoice('fast_team', siblings)).toBe('Fast team (fast_team)')
    expect(formatMachineChoice('fast__team', siblings)).toBe('Fast team (fast__team)')
    expect(formatMachineChoice('slow_team', siblings)).toBe('Slow team')
  })

  it('keeps a custom display name intact beside a machine identifier', () => {
    const siblings = ['Fast team', 'fast_team']
    expect(formatMachineChoice('Fast team', siblings)).toBe('Fast team')
    expect(formatMachineChoice('fast_team', siblings)).toBe('Fast team (fast_team)')
  })
})
