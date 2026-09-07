import type { ProjectInfo } from '../types'

interface ProjectOverviewProps {
  project: ProjectInfo
  onOpenView: (view: 'settings' | 'plans' | 'runs' | 'projects') => void
}

const nextStepGuidance: Record<ProjectInfo['readiness'], { text: string; action: 'settings' | 'plans' | 'projects' }> = {
  configuration_required: {
    text: 'Settings are incomplete: finish and save them before plans can run.',
    action: 'settings',
  },
  blocked: {
    text: 'This project is not a usable Git root yet. Fix the directory (a valid Git commit HEAD is required), then come back here and re-check the project from the project list.',
    action: 'projects',
  },
  ready: {
    text: 'Settings are ready. Choose or create a plan, then run it.',
    action: 'plans',
  },
}

/**
 * Landing view for one project: explains how plans, workflows, teams, and
 * runs fit together and offers the next concrete actions.
 */
export function ProjectOverview({ project, onOpenView }: ProjectOverviewProps) {
  const guidance = nextStepGuidance[project.readiness]
  return (
    <div className="project-overview">
      <section className="card overview-concepts" aria-label="What things mean">
        <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>How {project.display_name} fits together</h2>
        <ul className="overview-concept-list">
          <li><strong>Plan</strong> — what to do: the task a run works through.</li>
          <li><strong>Workflow</strong> — the ordered process: the steps a run follows.</li>
          <li><strong>Team</strong> — assigns agents to the workflow's roles.</li>
          <li><strong>Run</strong> — one execution of a plan through its workflow.</li>
        </ul>
      </section>

      <section className="card overview-path" aria-label="Path to a finished run">
        <h3 style={{ fontSize: '1rem', fontWeight: 600 }}>The path to a finished run</h3>
        <ol className="overview-steps">
          <li className="done">Choose a project — you are in {project.display_name}</li>
          <li>Check settings — workflows, agents, and defaults</li>
          <li>Choose or create a plan</li>
          <li>Start and follow a run</li>
        </ol>
        <p className="text-sm overview-next-step">{guidance.text}</p>
        <div className="dashboard-actions">
          {project.readiness === 'blocked' && (
            <button
              className={`btn btn-sm ${guidance.action === 'projects' ? 'btn-primary' : 'btn-secondary'}`}
              onClick={() => onOpenView('projects')}
            >
              Back to Projects (re-check after repair)
            </button>
          )}
          <button
            className={`btn btn-sm ${guidance.action === 'settings' ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => onOpenView('settings')}
          >
            Open settings
          </button>
          <button
            className={`btn btn-sm ${guidance.action === 'plans' ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => onOpenView('plans')}
          >
            Open plans
          </button>
          <button className="btn btn-secondary btn-sm" onClick={() => onOpenView('runs')}>
            Open runs
          </button>
        </div>
      </section>
    </div>
  )
}
