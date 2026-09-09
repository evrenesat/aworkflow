import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SkillsSettings } from './SkillsSettings'
import type { SkillInstallResult, SkillSummary } from '../types'

const summaries: SkillSummary[] = [
  { name: 'aflow-manager', default: true, revision: 'c'.repeat(64), source: 'bundled', edited: false, installed: true, links: [], detected_harnesses: [] },
  { name: 'aflow-assistant', default: false, revision: 'd'.repeat(64), source: 'saved', edited: true, installed: false, links: [], detected_harnesses: [] },
]

const baseProps = {
  skills: summaries,
  loadError: null,
  selected: 'aflow-manager',
  onSelect: vi.fn(),
  content: 'manager content',
  contentLoading: false,
  contentError: null,
  draft: null,
  unsavedNames: [] as readonly string[],
  onEdit: vi.fn(),
  hasUnsavedEdits: false,
  onInstall: vi.fn(),
  installing: false,
  installResult: null,
  installError: null,
}

describe('SkillsSettings', () => {
  it('renders the registry with optional labels and per-skill status', () => {
    render(<SkillsSettings {...baseProps} />)
    expect(screen.getByRole('button', { name: 'aflow-manager', exact: true })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'aflow-assistant (optional)', exact: true })).toBeTruthy()
    expect(screen.getByText(/bundled · unmodified · installed/)).toBeTruthy()
  })

  it('marks unsaved entries and forwards edits for the selected skill', () => {
    const onEdit = vi.fn()
    const onSelect = vi.fn()
    render(<SkillsSettings {...baseProps} selected="aflow-assistant" content="assistant content" draft="assistant draft" unsavedNames={['aflow-assistant']} hasUnsavedEdits onEdit={onEdit} onSelect={onSelect} />)
    expect(screen.getByRole('button', { name: 'aflow-assistant (optional) · unsaved' })).toBeTruthy()
    expect(screen.getByText(/saved · edited · not installed · unsaved edits/)).toBeTruthy()
    expect(screen.getByText(/saved but not installed/)).toBeTruthy()
    expect(screen.getByText(/default install excludes it/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'aflow-manager', exact: true }))
    expect(onSelect).toHaveBeenCalledWith('aflow-manager')
    fireEvent.change(screen.getByLabelText('SKILL.md for aflow-assistant'), { target: { value: 'new text' } })
    expect(onEdit).toHaveBeenCalledWith('aflow-assistant', 'new text')
  })

  it('disables Install while edits are unsaved and reports install outcomes', () => {
    const clean = render(<SkillsSettings {...baseProps} />)
    expect((clean.getByRole('button', { name: 'Install/reinstall all' }) as HTMLButtonElement).disabled).toBe(false)
    expect(clean.queryByText(/Save your skill edits first/)).toBeNull()
    clean.unmount()
    const result: SkillInstallResult = { mode: 'auto', succeeded: false, cancelled: false, refresh: [], operations: [{ harness: 'claude', skill: 'aflow-manager', destination: '/tmp/skills', status: 'failed', error_code: 'file_collision', error: 'blocked', displaced_path: null }] }
    render(<SkillsSettings {...baseProps} hasUnsavedEdits unsavedNames={['aflow-manager']} installResult={result} installError={null} />)
    expect((screen.getByRole('button', { name: 'Install/reinstall all' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/Save your skill edits first/)).toBeTruthy()
    expect(screen.getByText(/Install finished with failures\..*file_collision/)).toBeTruthy()
  })

  it('shows load, empty, and error states', () => {
    const loading = render(<SkillsSettings {...baseProps} skills={null} />)
    expect(loading.getByText('Loading skills…')).toBeTruthy()
    loading.unmount()
    const empty = render(<SkillsSettings {...baseProps} skills={[]} />)
    expect(empty.getByText('No bundled skills are registered.')).toBeTruthy()
    expect((empty.getByRole('button', { name: 'Install/reinstall all' }) as HTMLButtonElement).disabled).toBe(true)
    empty.unmount()
    render(<SkillsSettings {...baseProps} skills={null} loadError="boom" content={null} />)
    expect(screen.getByText(/Skills could not be loaded: boom/)).toBeTruthy()
  })
})
