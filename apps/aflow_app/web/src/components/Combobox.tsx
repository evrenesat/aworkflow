import { useId, useRef, useState } from 'react'

interface ComboboxProps {
  /** Visible control label. */
  label: string
  value: string
  onChange: (value: string) => void
  /** Closed suggestion list; always labeled as suggestions, never as the full set. */
  options: string[]
  /** When true the free-typed text may be kept even if it is not suggested. */
  allowCustom?: boolean
  placeholder?: string
  disabled?: boolean
  /** Hint shown when allowCustom is false and nothing is typed. */
  emptyOption?: string
  /** Visible source label per option (for example configured vs suggested). */
  optionBadges?: Record<string, string>
}

/**
 * Keyboard-operable combobox over an explicit suggestion list: Arrow keys move
 * an option, Enter applies it, Escape restores the committed value, and the
 * listbox is exposed through standard combobox aria wiring.
 */
export function Combobox({
  label,
  value,
  onChange,
  options,
  allowCustom = false,
  placeholder,
  disabled = false,
  emptyOption,
  optionBadges,
}: ComboboxProps) {
  const id = useId()
  const listboxId = `${id}-listbox`
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [open, setOpen] = useState(false)
  const [text, setText] = useState<string | null>(null)
  const [activeIndex, setActiveIndex] = useState(0)

  const query = text ?? value
  const filtered = query
    ? options.filter((option) => option.toLowerCase().includes(query.toLowerCase()))
    : options

  const highlightedIndex = Math.min(activeIndex, Math.max(0, filtered.length - 1))
  const expanded = open && !disabled
  const optionId = (option: string) => `${listboxId}-${encodeURIComponent(option)}`

  function commit(next: string) {
    if (disabled) return
    onChange(next)
    setText(null)
    setOpen(false)
    inputRef.current?.focus()
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (!open) {
        setOpen(true)
        setActiveIndex(0)
        return
      }
      const delta = event.key === 'ArrowDown' ? 1 : -1
      const count = Math.max(filtered.length, allowCustom ? 1 : 0)
      if (count === 0) return
      setActiveIndex((highlightedIndex + delta + count) % count)
      return
    }
    if (event.key === 'Enter') {
      if (!open) return
      event.preventDefault()
      if (filtered.length > 0) {
        commit(filtered[highlightedIndex])
        return
      }
      if (allowCustom && query.trim()) commit(query.trim())
      return
    }
    if (event.key === 'Escape' && open) {
      event.preventDefault()
      setText(null)
      setOpen(false)
    }
  }

  return (
    <div className="combobox">
      <label className="text-xs text-dim" htmlFor={id}>{label}</label>
      <input
        id={id}
        ref={inputRef}
        className="input"
        role="combobox"
        aria-expanded={expanded}
        aria-activedescendant={expanded && filtered.length ? optionId(filtered[highlightedIndex]) : undefined}
        aria-controls={listboxId}
        aria-autocomplete="list"
        aria-label={label}
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        placeholder={placeholder}
        value={query}
        onFocus={() => {
          setOpen(true)
          setActiveIndex(0)
        }}
        onChange={(event) => {
          setText(event.target.value)
          setOpen(true)
          setActiveIndex(0)
        }}
        onBlur={() => {
          // Typed text survives blur only where the contract allows custom values.
          if (text !== null) {
            if (allowCustom && text.trim() && text !== value) onChange(text.trim())
            setText(null)
          }
          setOpen(false)
        }}
        onKeyDown={handleKeyDown}
      />
      {expanded && (
        <ul id={listboxId} role="listbox" aria-label={`${label} suggestions`} className="combobox-listbox">
          {emptyOption && !query && (
            <li role="presentation" className="combobox-hint text-xs">{emptyOption}</li>
          )}
          {filtered.map((option, index) => (
            <li
              key={option}
              id={optionId(option)}
              role="option"
              aria-selected={option === value}
              className={`suggestion-option combobox-option ${index === highlightedIndex ? 'active' : ''}`}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => commit(option)}
            >
                <span className="mono text-sm">{option}</span>
                {optionBadges?.[option] && (
                  <span className="text-xs text-dim">{optionBadges[option]}</span>
                )}
            </li>
          ))}
          {filtered.length === 0 && (
            <li className="combobox-hint text-xs" role="presentation">
              {allowCustom ? `Use the typed value “${query}”` : 'No matching configured choice'}
            </li>
          )}
        </ul>
      )}
    </div>
  )
}
