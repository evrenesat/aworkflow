import { useId, useState, type TextareaHTMLAttributes } from 'react'

type TextEditorProps = TextareaHTMLAttributes<HTMLTextAreaElement> & {
  /** Controlled presentation state for consumers that place the toolbar elsewhere. */
  wrapLines?: boolean
  onWrapLinesChange?: (wrapLines: boolean) => void
  /** Skills places the shared toolbar beside its existing editor label. */
  showWrapToggle?: boolean
}

interface TextEditorToolbarProps {
  editorId: string
  wrapLines: boolean
  onWrapLinesChange: (wrapLines: boolean) => void
}

export function TextEditorToolbar({ editorId, wrapLines, onWrapLinesChange }: TextEditorToolbarProps) {
  const wrapToggleId = `${editorId}-wrap-lines`
  return (
    <div className="text-editor-toolbar">
      <label className="text-editor-wrap-toggle" htmlFor={wrapToggleId}>
        <input
          id={wrapToggleId}
          type="checkbox"
          checked={wrapLines}
          aria-controls={editorId}
          onChange={event => onWrapLinesChange(event.target.checked)}
        />
        <span>Wrap lines</span>
      </label>
    </div>
  )
}

/**
 * A native textarea with presentation-only wrapping controls. The textarea
 * value remains the caller's exact string; changing Wrap lines only changes
 * how the browser lays that string out.
 */
export function TextEditor({
  className = '',
  id,
  wrapLines: controlledWrapLines,
  onWrapLinesChange,
  showWrapToggle = true,
  ...props
}: TextEditorProps) {
  const generatedId = useId()
  const editorId = id ?? `${generatedId}-editor`
  const [uncontrolledWrapLines, setUncontrolledWrapLines] = useState(true)
  const wrapLines = controlledWrapLines ?? uncontrolledWrapLines
  const setWrapLines = (next: boolean) => {
    if (controlledWrapLines === undefined) setUncontrolledWrapLines(next)
    onWrapLinesChange?.(next)
  }
  const editorClassName = [
    'input',
    'text-editor-input',
    className,
    !wrapLines && 'text-editor-input-nowrap',
  ].filter(Boolean).join(' ')

  return (
    <div className="text-editor">
      {showWrapToggle && <TextEditorToolbar editorId={editorId} wrapLines={wrapLines} onWrapLinesChange={setWrapLines} />}
      <textarea
        {...props}
        id={editorId}
        className={editorClassName}
        wrap={wrapLines ? 'soft' : 'off'}
      />
    </div>
  )
}
