#!/usr/bin/env node
import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'

let input = ''
try {
  input = readFileSync('/dev/stdin', 'utf8')
} catch {
  // stdin not available or no data
}

const child = spawnSync('umem', ['hook', 'session-start'], {
  input,
  encoding: 'utf8',
  stdio: ['pipe', 'pipe', 'pipe'],
})

if (child.stdout) process.stdout.write(child.stdout)
if (child.stderr) process.stderr.write(child.stderr)
process.exit(child.status ?? 0)
