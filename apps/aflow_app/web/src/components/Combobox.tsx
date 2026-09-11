import { useEffect, useId, useRef, useState } from 'react'

interface ComboboxProps {
  /** Visible control label. */
  label: string
  /** Optional shorter label shown next to the control while keeping `label` accessible. */
  visibleLabel?: string
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
  /** Presentation label for a raw option; selection and callbacks keep the raw option. */
  optionLabel?: (option: string) => string
  /**
   * Resolved value shown while the control is unfocused and nothing is typed,
   * even when the stored value is empty ("follow default"). Focusing always
   * switches to a blank search query; blur and Escape restore this label.
   */
  resolvedDisplay?: string | null
  /** Small indicator beside the resolved value (for example "Default"). */
  resolvedBadge?: string
  /** A real selectable row that returns to the omitted/default value. */
  defaultOption?: { value: string; label: string; hint?: string }
}

/**
 * Keyboard-operable combobox over an explicit suggestion list: Arrow keys move
 * an option, Enter applies it, Escape restores the committed value, and the
 * listbox is exposed through standard combobox aria wiring. The displayed
 * value is presentation only; parents keep request state authoritative.
 */
export function Combobox({
  label,
  visibleLabel,
  value,
  onChange,
  options,
  allowCustom = false,
  placeholder,
  disabled = false,
  emptyOption,
  optionBadges,
  optionLabel,
  resolvedDisplay = null,
  resolvedBadge,
  defaultOption,
}: ComboboxProps) {
  const id = useId()
  const listboxId = `${id}-listbox`
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [open, setOpen] = useState(false)
  const [text, setText] = useState<string | null>(null)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [listPlacement, setListPlacement] = useState<'below' | 'above'>('below')
  const previousValueRef = useRef(value)

  useEffect(() => {
    if (previousValueRef.current && !value && text) setText(null)
    previousValueRef.current = value
  }, [text, value])

  const labelForOption = optionLabel ?? ((option: string) => option)
  const visibleLabels = options.map(labelForOption)
  const labelCounts = visibleLabels.reduce((counts, label) => counts.set(label, (counts.get(label) ?? 0) + 1), new Map<string, number>())
  const showResolved = text === null && Boolean(resolvedDisplay) && !value
  const query = text ?? (showResolved ? '' : value)
  const normalizedQuery = query.toLowerCase()
  const filtered = query
    ? options.filter((option) => option.toLowerCase().includes(normalizedQuery) || labelForOption(option).toLowerCase().includes(normalizedQuery))
    : options
  const showDefaultRow = Boolean(defaultOption) && (!query || (defaultOption!.label.toLowerCase().includes(query.toLowerCase())))

  const highlightedIndex = Math.max(0, Math.min(activeIndex, Math.max(0, filtered.length - 1)))
  const expanded = open && !disabled
  const optionId = (option: string) => `${listboxId}-${encodeURIComponent(option)}`

  function commit(next: string) {
    if (disabled) return
    onChange(next)
    setText(null)
    setOpen(false)
  }

  function openFromUser() {
    const rect = inputRef.current?.getBoundingClientRect()
    const viewportHeight = typeof window === 'undefined' ? Number.POSITIVE_INFINITY : window.innerHeight
    const roomBelow = rect ? viewportHeight - rect.bottom : viewportHeight
    const roomAbove = rect?.top ?? viewportHeight
    setListPlacement(roomBelow < Math.min(220, viewportHeight - 24) && roomAbove > roomBelow ? 'above' : 'below')
    setOpen(true)
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (!open) {
        openFromUser()
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
      if (allowCustom && text !== null && activeIndex < 0) {
        commit(query.trim())
        return
      }
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
      <label className="text-xs text-dim" htmlFor={id}>{visibleLabel ?? label}</label>
      <div className="combobox-control">
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
          value={showResolved ? resolvedDisplay! : !open && text === null && optionLabel ? labelForOption(value) : query}
          onFocus={() => {
            openFromUser()
            setActiveIndex(-1)
          }}
          onChange={(event) => {
            setText(event.target.value)
            if (allowCustom) onChange(event.target.value)
            openFromUser()
            setActiveIndex(-1)
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
        {showResolved && resolvedBadge && <span className="text-xs text-dim combobox-badge">{resolvedBadge}</span>}
      </div>
      {expanded && (
        <ul id={listboxId} role="listbox" aria-label={`${label} suggestions`} className={`combobox-listbox ${listPlacement === 'above' ? 'combobox-listbox-above' : ''}`}>
          {defaultOption && showDefaultRow && (
            <li
              key={defaultOption.value}
              id={optionId(defaultOption.label)}
              role="option"
              aria-selected={value === defaultOption.value}
              className="suggestion-option combobox-option combobox-default-option"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => commit(defaultOption.value)}
            >
              <span className="mono text-sm">{defaultOption.label}</span>
              {defaultOption.hint && <span className="text-xs text-dim">{defaultOption.hint}</span>}
            </li>
          )}
          {emptyOption && !query && !defaultOption && (
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
                <span className="text-sm">{labelForOption(option)}</span>
                {optionLabel && labelCounts.get(labelForOption(option))! > 1 && labelForOption(option) !== option && (
                  <span className="mono text-xs text-dim">{option}</span>
                )}
                {optionBadges?.[option] && (
                  <span className="text-xs text-dim">{optionBadges[option]}</span>
                )}
            </li>
          ))}
          {filtered.length === 0 && !showDefaultRow && (
            <li className="combobox-hint text-xs" role="presentation">
              {allowCustom ? `Use the typed value “${query}”` : 'No matching configured choice'}
            </li>
          )}
        </ul>
      )}
    </div>
  )
}
