import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { formatMachineLabel } from '../label'
import { Combobox } from './Combobox'

describe('Combobox readable machine labels', () => {
  it('shows a readable selected value while retaining the raw value on focus', () => {
    render(<Combobox label="Workflow" value="implementation_plans" options={['implementation_plans']} optionLabel={formatMachineLabel} onChange={() => {}} />)
    const input = screen.getByRole('combobox') as HTMLInputElement
    expect(input.value).toBe('Implementation plans')
    fireEvent.focus(input)
    expect(input.value).toBe('implementation_plans')
  })

  it('searches readable labels and selects the exact raw identifier when labels collide', () => {
    const onChange = vi.fn()
    render(<Combobox label="Workflow" value="" options={['implementation_plans', 'implementation__plans']} optionLabel={formatMachineLabel} onChange={onChange} />)
    const input = screen.getByRole('combobox') as HTMLInputElement
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'Implementation plans' } })
    expect(screen.getByRole('option', { name: 'Implementation plans implementation_plans' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'Implementation plans implementation__plans' })).toBeTruthy()
    fireEvent.click(screen.getByRole('option', { name: 'Implementation plans implementation__plans' }))
    expect(onChange).toHaveBeenCalledWith('implementation__plans')
  })

  it('opens the bounded suggestions above a lower-edge control and keeps keyboard selection exact', () => {
    const onChange = vi.fn()
    render(<Combobox label="Profile" value="" options={['codex.worker', 'codex.reviewer']} onChange={onChange} />)
    const input = screen.getByRole('combobox', { name: 'Profile' })
    vi.spyOn(input, 'getBoundingClientRect').mockReturnValue({ top: 700, bottom: 740, left: 20, right: 300, width: 280, height: 40 } as DOMRect)

    fireEvent.focus(input)
    const listbox = screen.getByRole('listbox', { name: 'Profile suggestions' })
    expect(listbox.className).toContain('combobox-listbox-above')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('codex.reviewer')
  })

  it('shows compact option hints without changing the raw selected identity', () => {
    const onChange = vi.fn()
    render(<Combobox
      label="Team family"
      value=""
      options={['product']}
      optionLabel={() => 'Product'}
      optionHint={() => 'Worker: codex.fast · Reviewer: codex.review · Upgrade route: Base → Fast'}
      onChange={onChange}
    />)
    const input = screen.getByRole('combobox', { name: 'Team family' })
    fireEvent.focus(input)
    expect(screen.getByRole('option', { name: /Product.*Worker: codex\.fast.*Base → Fast/ })).toBeTruthy()
    fireEvent.click(screen.getByRole('option', { name: /Product.*Worker: codex\.fast/ }))
    expect(onChange).toHaveBeenCalledWith('product')
  })
})
