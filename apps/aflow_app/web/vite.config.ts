import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import type { Plugin } from 'vite'

const changelogSourcePath = fileURLToPath(new URL('./src/generated/changelog.json', import.meta.url))

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function readGeneratedChangelog(): string {
  let source: string
  try {
    source = readFileSync(changelogSourcePath, 'utf8')
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`unable to read generated changelog at ${changelogSourcePath}: ${detail}`)
  }

  let payload: unknown
  try {
    payload = JSON.parse(source)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`generated changelog is not valid JSON at ${changelogSourcePath}: ${detail}`)
  }
  if (!isRecord(payload) || payload.schema_version !== 1 || !Array.isArray(payload.entries) || payload.entries.length === 0) {
    throw new Error(
      `generated changelog must have schema_version=1 and nonempty entries at ${changelogSourcePath}`,
    )
  }
  for (const [index, entry] of payload.entries.entries()) {
    if (
      !isRecord(entry) ||
      Object.keys(entry).sort().join(',') !== 'date,title' ||
      typeof entry.date !== 'string' ||
      !/^\d{4}-\d{2}-\d{2}$/u.test(entry.date) ||
      typeof entry.title !== 'string' ||
      entry.title.trim() === ''
    ) {
      throw new Error(`generated changelog entry ${index} is invalid at ${changelogSourcePath}`)
    }
  }
  return source
}

function generatedChangelogPlugin(): Plugin {
  return {
    name: 'aflow-generated-changelog',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'changelog.json',
        source: readGeneratedChangelog(),
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), generatedChangelogPlugin()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    environment: 'happy-dom',
    setupFiles: './src/test-setup.ts',
    include: ['src/**/*.{test,spec}.{js,mjs,cjs,ts,mts,cts,jsx,tsx}'],
  },
})
