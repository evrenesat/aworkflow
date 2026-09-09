import type { GuidedTemplateVariable } from '../types'

interface TemplateVariablesHelpProps {
  variables?: readonly GuidedTemplateVariable[]
  context: 'named' | 'role'
}

/**
 * Shared, visible reference for the substitutions owned by the workflow
 * renderer.  It deliberately has no editor state, so changing tabs or
 * switching prompts cannot reset the surrounding draft.
 */
export function TemplateVariablesHelp({ variables, context }: TemplateVariablesHelpProps) {
  const roleContext = context === 'role'
  return (
    <section className="template-variable-help" aria-label="Template variables">
      <h4>Template variables</h4>
      {roleContext ? (
        <p className="text-xs text-dim">
          Role and team prompt overrides are literal text and are not passed through the template renderer.
          The catalog below applies to named workflow prompts.
        </p>
      ) : (
        <p className="text-xs text-dim">
          These substitutions are applied when a named prompt is rendered. Merge-only entries are labeled by scope.
          Examples are illustrative, not resolved values from an unselected run.
        </p>
      )}
      {variables?.length ? (
        <ul className="template-variable-list">
          {variables.map(variable => (
            <li key={variable.token}>
              <code className="mono">{variable.token}</code>{' '}
              <span>{variable.description}</span>
              <div className="text-xs text-dim">
                <span><strong>Scope:</strong> {variable.scope}</span>{' '}
                <span><strong>Unavailable:</strong> {variable.absent_value}</span>{' '}
                <span><strong>Example:</strong> <code className="mono">{variable.example}</code></span>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-dim">
          The reference is temporarily unavailable; editing remains available and brace text keeps its existing behavior.
        </p>
      )}
    </section>
  )
}
