import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { TextEditor } from './TextEditor'

describe('TextEditor', () => {
  it('keeps exact text while changing only visual wrapping', () => {
    const onChange = vi.fn()
    const text = 'a very long line with TOML = { preserved = true }'
    const view = render(<TextEditor aria-label="Document" value={text} onChange={onChange} />)

    const editor = screen.getByRole('textbox', { name: 'Document' }) as HTMLTextAreaElement
    const toggle = screen.getByRole('checkbox', { name: 'Wrap lines' }) as HTMLInputElement
    expect(toggle.checked).toBe(true)
    expect(editor.getAttribute('wrap')).toBe('soft')

    const nextText = text + '\nunchanged bytes'
    fireEvent.change(editor, { target: { value: nextText } })
    expect(onChange).toHaveBeenCalled()
    view.rerender(<TextEditor aria-label="Document" value={nextText} onChange={onChange} />)

    fireEvent.click(toggle)
    expect(toggle.checked).toBe(false)
    expect(editor.getAttribute('wrap')).toBe('off')
    expect(editor.className).toContain('text-editor-input-nowrap')
    expect(editor.value).toBe(nextText)
  })
})
