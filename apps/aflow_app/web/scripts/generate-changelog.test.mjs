import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { generateChangelog, parseDevlogEntries } from './generate-changelog.mjs'

function fixtureRoots() {
  const sourceRoot = mkdtempSync(join(tmpdir(), 'aflow-changelog-source-'))
  const outputRoot = mkdtempSync(join(tmpdir(), 'aflow-changelog-output-'))
  return { sourceRoot, outputRoot }
}

test('parses supported date separators, strips only trailing plan metadata, and stably sorts', () => {
  const entries = parseDevlogEntries(`
## 2026-01-02: Later title (plan: later-plan)
## not a dated heading
## 2026-01-04 — Newest\n
## 2026-01-02 — First same-day title
## 2026-01-02: Second same-day title
## 2026-01-01: Keep (plan: metadata) in the middle
## 2026-01-01: Keep (plan: metadata)\n
## 2026-01-01: Compact(plan: metadata)
## 2026-02-31: Invalid calendar date
## 2026-01-03 -- Unsupported separator
`)

  assert.deepEqual(entries, [
    { date: '2026-01-04', title: 'Newest' },
    { date: '2026-01-02', title: 'Later title' },
    { date: '2026-01-02', title: 'First same-day title' },
    { date: '2026-01-02', title: 'Second same-day title' },
    { date: '2026-01-01', title: 'Keep (plan: metadata) in the middle' },
    { date: '2026-01-01', title: 'Keep' },
    { date: '2026-01-01', title: 'Compact' },
  ])
})

test('ignores H2 headings inside backtick and tilde fences', () => {
  const entries = parseDevlogEntries(`
## 2026-03-01: Outside one
\`\`\`markdown
## 2099-01-01: Inside backticks
\`\`\`
~~~text
## 2099-01-02: Inside tildes
~~~
## 2026-02-01: Outside two
`)

  assert.deepEqual(entries.map(({ title }) => title), ['Outside one', 'Outside two'])
})

test('writes deterministic JSON and escaped Markdown with complete Unicode titles', () => {
  const { sourceRoot, outputRoot } = fixtureRoots()
  writeFileSync(
    join(sourceRoot, 'DEVLOG.md'),
    '## 2026-04-01 — Über café [docs] & `literal` *all* — keep\n',
    'utf8',
  )

  const first = generateChangelog({ sourceRoot, outputRoot })
  const firstJson = readFileSync(first.jsonPath, 'utf8')
  const firstMarkdown = readFileSync(first.markdownPath, 'utf8')
  const second = generateChangelog({ sourceRoot, outputRoot })

  assert.equal(firstJson, readFileSync(second.jsonPath, 'utf8'))
  assert.equal(firstMarkdown, readFileSync(second.markdownPath, 'utf8'))
  assert.deepEqual(JSON.parse(firstJson), {
    schema_version: 1,
    entries: [{ date: '2026-04-01', title: 'Über café [docs] & `literal` *all* — keep' }],
  })
  assert.ok(firstMarkdown.includes('- Über café \\[docs\\] & \\`literal\\` \\*all\\* — keep'))
})

test('changes the generated release content when a new dated heading is added', () => {
  const { sourceRoot, outputRoot } = fixtureRoots()
  const sourcePath = join(sourceRoot, 'DEVLOG.md')
  writeFileSync(sourcePath, '## 2026-04-01: Existing release\n', 'utf8')
  const first = generateChangelog({ sourceRoot, outputRoot })
  const firstJson = readFileSync(first.jsonPath, 'utf8')

  writeFileSync(
    sourcePath,
    '## 2026-04-01: Existing release\n## 2026-04-02: New release\n',
    'utf8',
  )
  generateChangelog({ sourceRoot, outputRoot })

  assert.notEqual(firstJson, readFileSync(first.jsonPath, 'utf8'))
  assert.equal(JSON.parse(readFileSync(first.jsonPath, 'utf8')).entries[0].title, 'New release')
})

test('rejects a missing DEVLOG and a source with no valid dated entries', () => {
  const missing = fixtureRoots()
  assert.throws(
    () => generateChangelog(missing),
    /unable to read DEVLOG source/u,
  )

  const empty = fixtureRoots()
  writeFileSync(join(empty.sourceRoot, 'DEVLOG.md'), '# No dated entries\n', 'utf8')
  assert.throws(
    () => generateChangelog(empty),
    /contains no valid dated H2 entries/u,
  )
})
