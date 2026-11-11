import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/keepalive.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
})
const { windowSummary, isOvernight, minutesOf } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const window = (start_time, end_time, start_day = 0, end_day = 0) =>
  ({ start_time, end_time, start_day, end_day })

test('overnight display makes the following day explicit', () => {
  assert.equal(isOvernight('13:00', '04:00'), true)
  assert.equal(windowSummary(window('13:00', '04:00')), '每天 13:00–次日 04:00')
  assert.equal(windowSummary(window('13:00', '04:00', 1, 15)), '每天 13:00–次日 04:00 · 每月 1–15 日（按开始日期）')
})
test('same-day, midnight and unlimited schedules keep their meaning', () => {
  assert.equal(windowSummary(window('08:00', '23:00')), '每天 08:00–23:00')
  assert.equal(windowSummary(window('13:00', '00:00')), '每天 13:00–24:00')
  assert.equal(isOvernight('13:00', '00:00'), false)
  assert.equal(windowSummary(window('00:00', '04:00')), '每天 00:00–04:00')
  assert.equal(isOvernight('00:00', '04:00'), false)
  assert.equal(windowSummary(window('', '')), '')
})
test('time parsing rejects malformed and out-of-range times', () => {
  assert.equal(minutesOf('13:00'), 780)
  for (const value of ['', 'oops', '1:00', '24:00', '13:60']) {
    assert.equal(minutesOf(value), -1)
    assert.equal(isOvernight('13:00', value), false)
  }
})
