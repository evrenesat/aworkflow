import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const CHANGELOG_SCHEMA_VERSION = 1

const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url))
const REPOSITORY_ROOT = resolve(SCRIPT_DIRECTORY, '../../../..')
const DATE_HEADING = /^(\d{4}-\d{2}-\d{2})\s*(?:—|:)\s*(.+)$/u
const HEADING = /^\s*##[ \t]+(.+?)\s*$/u
const FENCE = /^\s*(`{3,}|~{3,})/u
const PLAN_METADATA = /\s*\(plan:\s*[^()\r\n]*\)\s*$/u

const MARKDOWN_SPECIAL_CHARACTERS = new Set([
  '\\', '`', '*', '_', '{', '}', '[', ']', '(', ')', '#', '+', '-', '.', '!', '|', '<', '>', '~',
])

function normalizeWhitespace(value) {
  return value.trim().replace(/\s+/gu, ' ')
}

function validIsoDate(value) {
  const [yearText, monthText, dayText] = value.split('-')
  const year = Number(yearText)
  const month = Number(monthText)
  const day = Number(dayText)
  if (!Number.isInteger(year) || !Number.isInteger(month) || !Number.isInteger(day)) {
    return false
  }
  if (month < 1 || month > 12 || day < 1) {
    return false
  }
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return day <= daysInMonth[month - 1]
}

function parseHeading(line) {
  const headingMatch = line.match(HEADING)
  if (!headingMatch) {
    return null
  }
  const normalizedHeading = normalizeWhitespace(headingMatch[1])
  const dateMatch = normalizedHeading.match(DATE_HEADING)
  if (!dateMatch || !validIsoDate(dateMatch[1])) {
    return null
  }
  const title = normalizeWhitespace(dateMatch[2].replace(PLAN_METADATA, ''))
  if (!title) {
    return null
  }
  return { date: dateMatch[1], title }
}

export function parseDevlogEntries(markdown) {
  const entries = []
  let fence = null

  for (const line of markdown.split(/\r?\n/u)) {
    const fenceMatch = line.match(FENCE)
    if (fenceMatch) {
      const marker = fenceMatch[1]
      const kind = marker[0]
      if (!fence) {
        fence = { kind, length: marker.length }
      } else if (fence.kind === kind && marker.length >= fence.length) {
        fence = null
      }
      continue
    }
    if (fence) {
      continue
    }
    const entry = parseHeading(line)
    if (entry) {
      entries.push(entry)
    }
  }

  return entries
    .map((entry, sourceIndex) => ({ ...entry, sourceIndex }))
    .sort((left, right) => {
      if (left.date === right.date) {
        return left.sourceIndex - right.sourceIndex
      }
      return left.date < right.date ? 1 : -1
    })
    .map(({ date, title }) => ({ date, title }))
}

function escapeMarkdown(value) {
  return [...value]
    .map((character) => MARKDOWN_SPECIAL_CHARACTERS.has(character) ? `\\${character}` : character)
    .join('')
}

function renderMarkdown(entries) {
  const lines = [
    '# Changelog',
    '',
    '<!-- Generated from DEVLOG.md; do not edit. -->',
    '',
  ]
  let previousDate = null
  for (const entry of entries) {
    if (entry.date !== previousDate) {
      if (previousDate !== null) {
        lines.push('')
      }
      lines.push(`## ${entry.date}`, '')
      previousDate = entry.date
    }
    lines.push(`- ${escapeMarkdown(entry.title)}`)
  }
  return `${lines.join('\n')}\n`
}

function asPath(value, label) {
  if (typeof value === 'string') {
    return value
  }
  if (value instanceof URL) {
    return fileURLToPath(value)
  }
  throw new TypeError(`${label} must be a filesystem path or file URL`)
}

function resolveRoots(sourceRootOrOptions, outputRootArgument) {
  let sourceRoot = sourceRootOrOptions
  let outputRoot = outputRootArgument
  if (
    sourceRootOrOptions &&
    typeof sourceRootOrOptions === 'object' &&
    !(sourceRootOrOptions instanceof URL)
  ) {
    ({ sourceRoot, outputRoot } = sourceRootOrOptions)
  }
  if (sourceRoot === undefined || sourceRoot === null) {
    throw new TypeError('sourceRoot is required')
  }
  const sourcePath = resolve(asPath(sourceRoot, 'sourceRoot'))
  const destinationPath = resolve(asPath(outputRoot ?? sourceRoot, 'outputRoot'))
  return { sourcePath, destinationPath }
}

export function generateChangelog(sourceRootOrOptions, outputRootArgument) {
  const { sourcePath, destinationPath } = resolveRoots(sourceRootOrOptions, outputRootArgument)
  const devlogPath = resolve(sourcePath, 'DEVLOG.md')
  let devlog
  try {
    devlog = readFileSync(devlogPath, 'utf8')
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`unable to read DEVLOG source at ${devlogPath}: ${detail}`)
  }

  const entries = parseDevlogEntries(devlog)
  if (entries.length === 0) {
    throw new Error(`DEVLOG source at ${devlogPath} contains no valid dated H2 entries`)
  }

  const generatedDirectory = resolve(destinationPath, 'apps/aflow_app/web/src/generated')
  const jsonPath = resolve(generatedDirectory, 'changelog.json')
  const markdownPath = resolve(destinationPath, 'CHANGELOG.md')
  mkdirSync(generatedDirectory, { recursive: true })

  const json = `${JSON.stringify({ schema_version: CHANGELOG_SCHEMA_VERSION, entries }, null, 2)}\n`
  writeFileSync(jsonPath, json, 'utf8')
  writeFileSync(markdownPath, renderMarkdown(entries), 'utf8')
  return { entries, jsonPath, markdownPath }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const result = generateChangelog({ sourceRoot: REPOSITORY_ROOT, outputRoot: REPOSITORY_ROOT })
    console.log(`generated ${result.entries.length} changelog entries`)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    console.error(`generate-changelog: ${detail}`)
    process.exitCode = 1
  }
}
