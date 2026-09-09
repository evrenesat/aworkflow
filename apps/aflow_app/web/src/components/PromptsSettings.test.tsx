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
  template_variables: [
    {
      token: '{ORIGINAL_PLAN_PATH}',
      description: 'The original input plan path for this run.',
      scope: 'Named workflow step prompts and named merge prompts.',
      absent_value: 'Always receives the controller path.',
      example: 'plans/request.md',
      applicable_prompt_types: ['workflow step prompt', 'merge prompt'],
    },
    {
      token: '{WORK_ON_NEXT_CHECKPOINT_CMD}',
      description: 'An instruction for the current checkpoint.',
      scope: 'Named workflow step prompts and named merge prompts.',
      absent_value: 'Empty when no checkpoint is available.',
      example: 'Work only on Checkpoint #2. Do not repeat earlier checkpoints, and do not skip ahead.',
      applicable_prompt_types: ['workflow step prompt', 'merge prompt'],
    },
    {
      token: '{MAIN_BRANCH}',
      description: 'The configured merge target branch.',
      scope: 'Named merge prompts only.',
      absent_value: 'Not substituted outside a merge prompt.',
      example: 'main',
      applicable_prompt_types: ['merge prompt'],
    },
  ],
})

describe('PromptsSettings', () => {
  it('offers no deletion for referenced prompts and discloses their usages', () => {
    render(<PromptsSettings draft={baseDraft()} change={() => {}} rename={() => {}} names={{}} deleted={[]} onDelete={() => {}} onUndo={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Delete implementation_plans' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'More actions for prompt implementation_plans' })).toBeNull()
    fireEvent.click(screen.getByText('Used by 1 configured reference'))
    expect(screen.getByText('workflow.demo.steps.implement.prompts')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'review_plans', exact: true }))
    expect(screen.getByText('No configured references')).toBeTruthy()
  })

  it('labels the secondary action as a prompt, never as plan deletion', () => {
    render(<PromptsSettings draft={baseDraft()} change={() => {}} rename={() => {}} names={{}} deleted={[]} onDelete={() => {}} onUndo={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'review_plans', exact: true }))
    const trigger = screen.getByRole('button', { name: 'More actions for prompt review_plans' })
    fireEvent.click(trigger)
    expect(screen.getByRole('menuitem', { name: 'Delete prompt…' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: /Delete plans/i })).toBeNull()
  })

  it('shows the renderer variable reference beneath named prompt text', () => {
    render(<PromptsSettings draft={baseDraft()} change={() => {}} rename={() => {}} names={{}} deleted={[]} onDelete={() => {}} onUndo={() => {}} />)
    expect(screen.getByRole('region', { name: 'Template variables' })).toBeTruthy()
    expect(screen.getByText('{ORIGINAL_PLAN_PATH}')).toBeTruthy()
    expect(screen.getByText('plans/request.md')).toBeTruthy()
    expect(screen.getByText('Work only on Checkpoint #2. Do not repeat earlier checkpoints, and do not skip ahead.')).toBeTruthy()
  })

  it('explains that role overrides are literal while retaining the shared reference', () => {
    const draft = { ...baseDraft(), roles: { worker: 'codex.worker' }, role_prompts: { worker: 'literal role text' } }
    render(<PromptsSettings draft={draft} change={() => {}} rename={() => {}} names={{}} deleted={[]} onDelete={() => {}} onUndo={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Global / worker', exact: true }))
    expect(screen.getByText(/Role and team prompt overrides are literal text/)).toBeTruthy()
    expect(screen.getByText('{MAIN_BRANCH}')).toBeTruthy()
  })

  it('keeps prompt editing available when the help catalog is unavailable', () => {
    const draft = { ...baseDraft(), template_variables: undefined }
    render(<PromptsSettings draft={draft} change={() => {}} rename={() => {}} names={{}} deleted={[]} onDelete={() => {}} onUndo={() => {}} />)
    expect((screen.getByRole('textbox', { name: 'Prompt text implementation_plans' }) as HTMLTextAreaElement).value).toBe('Read the plan.')
    expect(screen.getByText(/reference is temporarily unavailable/)).toBeTruthy()
  })

  it('opens and closes the More menu with the keyboard', () => {
    render(<PromptsSettings draft={baseDraft()} change={() => {}} rename={() => {}} names={{}} deleted={[]} onDelete={() => {}} onUndo={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'review_plans', exact: true }))
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
    fireEvent.click(screen.getByRole('button', { name: 'review_plans', exact: true }))
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
