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
})
