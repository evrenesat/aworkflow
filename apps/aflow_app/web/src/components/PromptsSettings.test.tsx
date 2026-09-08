import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PromptsSettings } from './PromptsSettings'
import type { GuidedFormProjection } from '../types'

const baseDraft = (): GuidedFormProjection => ({
  default_workflow: null,
  max_turns: null,
  harnesses: {},
  roles: {},
  teams: {},
  workflow_default_teams: {},
  workflows: {},
  prompts: { implementation_plans: 'Read the plan.', review_plans: 'Review it.' },
  prompt_usages: { implementation_plans: ['workflow.demo.steps.implement.prompts'] },
  role_prompts: {},
})

describe('PromptsSettings', () => {
  it('offers no deletion for referenced prompts and discloses their usages', () => {
    render(<PromptsSettings draft={baseDraft()} change={() => {}} rename={() => {}} names={{}} deleted={[]} onDelete={() => {}} onUndo={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Delete implementation_plans' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'More actions for prompt implementation_plans' })).toBeNull()
    fireEvent.click(screen.getByText('Used by 1 configured reference'))
    expect(screen.getByText('workflow.demo.steps.implement.prompts')).toBeTruthy()
    expect(screen.getByText('No configured references')).toBeTruthy()
  })

  it('labels the secondary action as a prompt, never as plan deletion', () => {
    render(<PromptsSettings draft={baseDraft()} change={() => {}} rename={() => {}} names={{}} deleted={[]} onDelete={() => {}} onUndo={() => {}} />)
    const trigger = screen.getByRole('button', { name: 'More actions for prompt review_plans' })
    fireEvent.click(trigger)
    expect(screen.getByRole('menuitem', { name: 'Delete prompt…' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: /Delete plans/i })).toBeNull()
  })

  it('opens and closes the More menu with the keyboard', () => {
    render(<PromptsSettings draft={baseDraft()} change={() => {}} rename={() => {}} names={{}} deleted={[]} onDelete={() => {}} onUndo={() => {}} />)
    const trigger = screen.getByRole('button', { name: 'More actions for prompt review_plans' })
    trigger.focus()
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(trigger)
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    fireEvent.keyDown(trigger, { key: 'Escape' })
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('menuitem', { name: 'Delete prompt…' })).toBeNull()
  })

  it('requires confirmation and delegates deletion and undo to its draft owner', () => {
    const onDelete = vi.fn(), onUndo = vi.fn()
    const view = render(<PromptsSettings draft={baseDraft()} change={() => {}} rename={() => {}} names={{}} deleted={[]} onDelete={onDelete} onUndo={onUndo} />)
    fireEvent.click(screen.getByRole('button', { name: 'More actions for prompt review_plans' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete prompt…' }))
    expect(onDelete).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Delete prompt' }))
    expect(onDelete).toHaveBeenCalledWith('review_plans', 'Review it.')
    view.rerender(<PromptsSettings draft={baseDraft()} change={() => {}} rename={() => {}} names={{}} deleted={[{ name: 'review_plans', text: 'Review it.' }]} onDelete={onDelete} onUndo={onUndo} />)
    fireEvent.click(screen.getByRole('button', { name: 'Undo deletion of review_plans' }))
    expect(onUndo).toHaveBeenCalledWith('review_plans', 'review_plans')
  })
})
