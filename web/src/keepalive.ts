import type { Keepalive } from './types'

type Window = Pick<Keepalive, 'start_time' | 'end_time' | 'start_day' | 'end_day'>

/** 结束 00:00 就是「到当天午夜」，按面板习惯写成 24:00，避免被读成零长度 */
function endLabel(start: string, end: string): string {
  return start !== '00:00' && end === '00:00' ? '24:00' : end
}

/** 结束 00:00 仍表示当天午夜；其他结束早于开始的时段延续到次日。 */
export function isOvernight(start: string, end: string): boolean {
  const from = minutesOf(start)
  const to = minutesOf(end)
  return from >= 0 && to > 0 && to < from
}

/** 运行窗口的一句话摘要，空串 = 全天全月（等于没设窗口） */
export function windowSummary(item: Window): string {
  const parts: string[] = []
  const overnight = isOvernight(item.start_time, item.end_time)
  if (item.start_time && item.end_time) {
    const end = endLabel(item.start_time, item.end_time)
    parts.push(`每天 ${item.start_time}–${overnight ? '次日 ' : ''}${end}`)
  }
  if (item.start_day && item.end_day) {
    parts.push(`每月 ${item.start_day}–${item.end_day} 日${overnight ? '（按开始日期）' : ''}`)
  }
  return parts.join(' · ')
}

/** HH:MM → 分钟数，格式不对返回 -1 */
export function minutesOf(value: string): number {
  const match = /^(\d{2}):(\d{2})$/.exec(value)
  if (!match) return -1
  const hour = Number(match[1])
  const minute = Number(match[2])
  return hour < 24 && minute < 60 ? hour * 60 + minute : -1
}
