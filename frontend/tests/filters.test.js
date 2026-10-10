/**
 * 前端最小测试：信号 / 已执行交易列表筛选（纯逻辑）。
 *
 * 覆盖要求的 4 个开关、互斥说明、空结果与可扩展描述。
 */

import { describe, expect, it } from 'vitest'

import {
  DEFAULT_SIGNAL_FILTERS,
  RECORD_KIND,
  RECORD_KIND_LABEL,
  SIGNAL_FILTER_OPTIONS,
  applySignalFilters,
  describeFilters,
  recordKind,
  summarizeSignals,
  wantedDirection,
} from '../src/filters/signals.js'

const PENDING_LONG = { id: 1, direction: 'long', status: 'pending' }
const PENDING_SHORT = { id: 2, direction: 'short', status: 'pending' }
const EXECUTED_LONG = { id: 3, direction: 'long', status: 'executed' }
const EXECUTED_SHORT = { id: 4, direction: 'short', status: 'executed' }
const ROWS = [PENDING_LONG, PENDING_SHORT, EXECUTED_LONG, EXECUTED_SHORT]

const ids = (rows) => rows.map((row) => row.id)

describe('默认值 / 开关契约', () => {
  it('4 个开关的默认状态与要求一致', () => {
    expect(DEFAULT_SIGNAL_FILTERS).toEqual({
      showSignals: true,
      executedOnly: false,
      longOnly: false,
      shortOnly: false,
    })
  })

  it('界面渲染用的开关列表包含要求的 4 项且顺序固定', () => {
    expect(SIGNAL_FILTER_OPTIONS.map((option) => option.key)).toEqual([
      'showSignals',
      'executedOnly',
      'longOnly',
      'shortOnly',
    ])
    expect(SIGNAL_FILTER_OPTIONS.map((option) => option.label)).toEqual([
      '显示 Signals',
      '只显示已执行',
      '只显示 Long',
      '只显示 Short',
    ])
  })
})

describe('recordKind：区分 Signal 与 Executed Trade（复用 status，不新增字段）', () => {
  it('executed → 已执行交易；其它状态 → 策略信号', () => {
    expect(recordKind(EXECUTED_LONG)).toBe(RECORD_KIND.executed)
    expect(recordKind(PENDING_LONG)).toBe(RECORD_KIND.signal)
    // 未来新增的状态（cancelled 等）默认归入「策略信号」，无需改判定逻辑
    expect(recordKind({ status: 'cancelled' })).toBe(RECORD_KIND.signal)
    expect(RECORD_KIND_LABEL[RECORD_KIND.executed]).toBe('已执行交易')
    expect(RECORD_KIND_LABEL[RECORD_KIND.signal]).toBe('策略信号（未执行）')
  })
})

describe('applySignalFilters', () => {
  it('默认：显示策略信号，不显示已执行交易', () => {
    expect(ids(applySignalFilters(ROWS, DEFAULT_SIGNAL_FILTERS))).toEqual([1, 2])
  })

  it('关闭「显示 Signals」→ 未执行 Signal 消失（此处也不显示已执行）', () => {
    const rows = applySignalFilters(ROWS, { ...DEFAULT_SIGNAL_FILTERS, showSignals: false })
    expect(rows).toEqual([])
  })

  it('开启「只显示已执行」→ 只剩已执行交易（且优先于总开关）', () => {
    expect(ids(applySignalFilters(ROWS, { ...DEFAULT_SIGNAL_FILTERS, executedOnly: true }))).toEqual([
      3, 4,
    ])
    expect(
      ids(
        applySignalFilters(ROWS, {
          ...DEFAULT_SIGNAL_FILTERS,
          showSignals: false,
          executedOnly: true,
        }),
      ),
    ).toEqual([3, 4])
  })

  it('开启「只显示 Long」→ 只剩 Long（对信号与已执行交易都生效）', () => {
    expect(
      ids(applySignalFilters(ROWS, { ...DEFAULT_SIGNAL_FILTERS, longOnly: true })),
    ).toEqual([1])
    expect(
      ids(
        applySignalFilters(ROWS, {
          ...DEFAULT_SIGNAL_FILTERS,
          executedOnly: true,
          longOnly: true,
        }),
      ),
    ).toEqual([3])
  })

  it('开启「只显示 Short」→ 只剩 Short', () => {
    expect(
      ids(applySignalFilters(ROWS, { ...DEFAULT_SIGNAL_FILTERS, shortOnly: true })),
    ).toEqual([2])
    expect(
      ids(
        applySignalFilters(ROWS, {
          ...DEFAULT_SIGNAL_FILTERS,
          executedOnly: true,
          shortOnly: true,
        }),
      ),
    ).toEqual([4])
  })

  it('Long 与 Short 同时开启 → 视为不限制方向（不会互相抵消成空列表）', () => {
    const rows = applySignalFilters(ROWS, {
      ...DEFAULT_SIGNAL_FILTERS,
      longOnly: true,
      shortOnly: true,
    })
    expect(ids(rows)).toEqual([1, 2])
    expect(wantedDirection({ longOnly: true, shortOnly: true })).toBeNull()
  })

  it('筛选无结果时返回空数组（界面据此显示空状态）', () => {
    // 1）开关组合本身就不显示任何记录
    expect(
      applySignalFilters(ROWS, { ...DEFAULT_SIGNAL_FILTERS, showSignals: false }),
    ).toEqual([])
    // 2）条件互相限制后没有命中：只有 executed long 的数据里找 executed short
    expect(
      applySignalFilters([EXECUTED_LONG], {
        ...DEFAULT_SIGNAL_FILTERS,
        executedOnly: true,
        shortOnly: true,
      }),
    ).toEqual([])
  })

  it('空输入 / 缺省参数不抛异常', () => {
    expect(applySignalFilters(null, DEFAULT_SIGNAL_FILTERS)).toEqual([])
    expect(applySignalFilters(ROWS)).toHaveLength(2)
    expect(applySignalFilters(ROWS, undefined)).toHaveLength(2)
  })

  it('保持原有顺序，便于列表「取最近 N 条」', () => {
    expect(ids(applySignalFilters(ROWS, DEFAULT_SIGNAL_FILTERS))).toEqual([1, 2])
  })
})

describe('summarizeSignals（K 线图与日志共用的统计口径）', () => {
  it('按状态与方向统计，且 total = pending + executed', () => {
    expect(summarizeSignals(ROWS)).toEqual({
      total: 4,
      pending: 2,
      executed: 2,
      long: 2,
      short: 2,
    })
  })

  it('只统计传入的记录（筛选结果），空输入返回全 0', () => {
    expect(summarizeSignals([EXECUTED_LONG])).toEqual({
      total: 1,
      pending: 0,
      executed: 1,
      long: 1,
      short: 0,
    })
    expect(summarizeSignals([])).toEqual({ total: 0, pending: 0, executed: 0, long: 0, short: 0 })
    expect(summarizeSignals(null)).toEqual({ total: 0, pending: 0, executed: 0, long: 0, short: 0 })
  })

  it('与筛选结果一致（同一份数据算出来的数字用于两边显示）', () => {
    const filtered = applySignalFilters(ROWS, { ...DEFAULT_SIGNAL_FILTERS, executedOnly: true })
    expect(summarizeSignals(filtered)).toMatchObject({ total: 2, executed: 2, pending: 0 })
  })
})

describe('describeFilters（避免用户把筛选结果误读成没有数据）', () => {
  it('描述默认、已执行、方向与双开的情况', () => {
    expect(describeFilters(DEFAULT_SIGNAL_FILTERS)).toBe('当前筛选：仅策略信号（未执行） · 全部方向')
    expect(describeFilters({ ...DEFAULT_SIGNAL_FILTERS, executedOnly: true })).toBe(
      '当前筛选：仅已执行交易 · 全部方向',
    )
    expect(describeFilters({ ...DEFAULT_SIGNAL_FILTERS, showSignals: false })).toBe(
      '当前筛选：不显示任何信号 · 全部方向',
    )
    expect(describeFilters({ ...DEFAULT_SIGNAL_FILTERS, longOnly: true })).toBe(
      '当前筛选：仅策略信号（未执行） · 仅 Long',
    )
    expect(describeFilters({ ...DEFAULT_SIGNAL_FILTERS, longOnly: true, shortOnly: true })).toBe(
      '当前筛选：仅策略信号（未执行） · Long + Short（不限制方向）',
    )
  })
})
