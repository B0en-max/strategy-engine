/**
 * 前端最小测试 2/2：核心组件渲染（Stage 17）。
 *
 * 用 react-dom/server 渲染成 HTML 字符串再断言关键文字，
 * 不引入 jsdom / @testing-library/react（保持最小化）。
 */

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import App from '../src/App.jsx'
import ExecutionPanel from '../src/components/ExecutionPanel.jsx'
import ErrorBanner from '../src/components/ErrorBanner.jsx'
import OrdersPanel from '../src/components/OrdersPanel.jsx'
import PositionsPanel from '../src/components/PositionsPanel.jsx'
import SignalsPanel from '../src/components/SignalsPanel.jsx'
import StrategyRunnerPanel from '../src/components/StrategyRunnerPanel.jsx'
import TradesPanel from '../src/components/TradesPanel.jsx'
import { formatNumber, formatTimestamp, indexById, symbolLabel } from '../src/format.js'

const render = (element) => renderToStaticMarkup(element)
const noop = () => {}
const symbolsById = indexById([{ id: 1, symbol: '600519', name: '贵州茅台', market: 'CN' }])

const strategies = [{ id: 1, name: 'close_direction_v1', version: 1, status: 'active' }]
const symbols = [{ id: 1, symbol: '600519', name: '贵州茅台', market: 'CN' }]
const form = { strategyId: '1', symbolId: '1', timeframe: '15m', startTimestamp: '', endTimestamp: '' }

describe('格式化工具', () => {
  it('formatNumber 处理空值与小数', () => {
    expect(formatNumber(null)).toBe('-')
    expect(formatNumber(1258.19)).toBe('1258.19')
    expect(formatNumber(1258, 4)).toBe('1258')
    expect(formatNumber(1.5, 4)).toBe('1.5')
  })

  it('formatTimestamp 标注 UTC', () => {
    expect(formatTimestamp('2026-09-30 14:45:00')).toBe('2026-09-30 14:45:00 UTC')
    expect(formatTimestamp(null)).toBe('-')
  })

  it('symbolLabel 能用标的 id 找到名称，找不到时退回 #id', () => {
    expect(symbolLabel(symbolsById, 1)).toBe('600519 贵州茅台')
    expect(symbolLabel(symbolsById, 99)).toBe('#99')
  })
})

describe('StrategyRunnerPanel', () => {
  it('渲染策略 / 标的 / 周期选择与运行按钮', () => {
    const html = render(
      <StrategyRunnerPanel
        strategies={strategies}
        symbols={symbols}
        timeframes={['15m', '1d']}
        form={form}
        onChange={noop}
        onRun={noop}
        running={false}
        result={null}
        error={null}
        healthOk
      />,
    )
    expect(html).toContain('close_direction_v1 v1 [active]')
    expect(html).toContain('600519 贵州茅台 (CN)')
    expect(html).toContain('运行策略')
    expect(html).not.toContain('disabled')
  })

  it('没有策略时给出提示，运行按钮禁用', () => {
    const html = render(
      <StrategyRunnerPanel
        strategies={[]}
        symbols={[]}
        timeframes={['15m']}
        form={{ ...form, strategyId: '', symbolId: '' }}
        onChange={noop}
        onRun={noop}
        running={false}
        result={null}
        error={null}
        healthOk={false}
      />,
    )
    expect(html).toContain('当前数据库没有策略记录')
    expect(html).toContain('disabled')
  })

  it('显示运行结果与运行失败信息', () => {
    const html = render(
      <StrategyRunnerPanel
        strategies={strategies}
        symbols={symbols}
        timeframes={['15m']}
        form={form}
        onChange={noop}
        onRun={noop}
        running={false}
        result={{
          bars: 319,
          signals: 300,
          long_signals: 150,
          short_signals: 150,
          skipped: 0,
          strategy_type: 'close_direction',
          first_bar_timestamp: '2026-09-02 09:30:00',
          last_bar_timestamp: '2026-09-30 14:45:00',
        }}
        error="运行失败：数据库里没有该周期的 K 线"
        healthOk
      />,
    )
    expect(html).toContain('319')
    expect(html).toContain('long 150 / short 150')
    expect(html).toContain('2026-09-30 14:45:00 UTC')
    expect(html).toContain('没有该周期的 K 线')
  })
})

describe('SignalsPanel', () => {
  const baseProps = {
    symbolsById,
    loading: false,
    error: null,
    autoRefresh: true,
    lastUpdated: null,
    selectedSignalId: null,
    busySignalId: null,
    onToggleAutoRefresh: noop,
    onRefresh: noop,
    onSelect: noop,
    onExecute: noop,
  }

  it('展示 timestamp / direction / price / reason / status，pending 才有模拟执行按钮', () => {
    const html = render(
      <SignalsPanel
        {...baseProps}
        signals={[
          {
            id: 1,
            symbol_id: 1,
            timeframe: '15m',
            timestamp: '2026-09-02 09:45:00',
            direction: 'long',
            price: 1500.5,
            reason: 'close 1499.0 -> 1500.5',
            status: 'pending',
          },
          {
            id: 2,
            symbol_id: 1,
            timeframe: '15m',
            timestamp: '2026-09-02 10:00:00',
            direction: 'short',
            price: 1498.25,
            reason: 'close 1500.5 -> 1498.25',
            status: 'executed',
          },
        ]}
      />,
    )
    expect(html).toContain('2026-09-02 09:45:00')
    expect(html).toContain('long')
    expect(html).toContain('short')
    expect(html).toContain('1500.5')
    expect(html).toContain('close 1499.0 -&gt; 1500.5')
    expect(html).toContain('pending')
    expect(html).toContain('executed')
    expect(html.match(/模拟执行/g)).toHaveLength(1)
  })

  it('无信号时给出空态提示', () => {
    const html = render(<SignalsPanel {...baseProps} signals={[]} />)
    expect(html).toContain('暂无信号')
  })

  it('展示加载失败信息', () => {
    const html = render(<SignalsPanel {...baseProps} signals={[]} error="无法连接 API" />)
    expect(html).toContain('无法连接 API')
  })
})

describe('ExecutionPanel', () => {
  it('未选中信号时执行按钮禁用', () => {
    const html = render(
      <ExecutionPanel
        selectedSignal={null}
        symbolsById={symbolsById}
        quantity="1"
        price=""
        onQuantityChange={noop}
        onPriceChange={noop}
        onExecute={noop}
        executing={false}
        result={null}
        error={null}
      />,
    )
    expect(html).toContain('未选中信号')
    expect(html).toContain('disabled')
  })

  it('选中信号后展示信号信息、执行结果与 409 错误提示', () => {
    const html = render(
      <ExecutionPanel
        selectedSignal={{ id: 5, symbol_id: 1, direction: 'long', price: 1500.5, status: 'pending' }}
        symbolsById={symbolsById}
        quantity="2"
        price=""
        onQuantityChange={noop}
        onPriceChange={noop}
        onExecute={noop}
        executing={false}
        result={{
          signal_id: 5,
          order_id: 4,
          trade_id: 3,
          side: 'buy',
          quantity: 2,
          price: 1500.5,
          position_quantity: 2,
          position_avg_price: 1500.5,
        }}
        error="操作被拒绝：信号 5 当前状态为 executed"
      />,
    )
    expect(html).toContain('600519 贵州茅台')
    expect(html).toContain('#5 / #4 / #3')
    expect(html).toContain('buy')
    expect(html).toContain('操作被拒绝')
  })
})

describe('持仓 / 订单 / 成交面板', () => {
  it('持仓面板展示 symbol / quantity / avg_price', () => {
    const html = render(
      <PositionsPanel
        positions={[{ id: 1, symbol_id: 1, quantity: 2, avg_price: 1500.5, updated_at: '2026-10-07 07:00:00' }]}
        symbolsById={symbolsById}
        loading={false}
        onRefresh={noop}
      />,
    )
    expect(html).toContain('600519 贵州茅台')
    expect(html).toContain('1500.5')
    expect(html).toContain('2026-10-07 07:00:00 UTC')
  })

  it('持仓为空时显示空态', () => {
    const html = render(
      <PositionsPanel positions={[]} symbolsById={symbolsById} loading={false} onRefresh={noop} />,
    )
    expect(html).toContain('暂无持仓')
  })

  it('订单面板展示最近订单', () => {
    const html = render(
      <OrdersPanel
        orders={[
          {
            id: 4,
            signal_id: 5,
            symbol_id: 1,
            side: 'buy',
            order_type: 'market',
            quantity: 2,
            price: 1500.5,
            status: 'filled',
            created_at: '2026-10-07 07:00:00',
          },
        ]}
        symbolsById={symbolsById}
        loading={false}
        onRefresh={noop}
      />,
    )
    expect(html).toContain('market')
    expect(html).toContain('filled')
  })

  it('成交面板展示最近成交', () => {
    const html = render(
      <TradesPanel
        trades={[
          {
            id: 3,
            signal_id: 5,
            symbol_id: 1,
            side: 'buy',
            quantity: 2,
            price: 1500.5,
            fee: 0,
            timestamp: '2026-09-02 09:45:00',
          },
        ]}
        symbolsById={symbolsById}
        loading={false}
        onRefresh={noop}
      />,
    )
    expect(html).toContain('2026-09-02 09:45:00')
    expect(html).toContain('buy')
  })
})

describe('ErrorBanner', () => {
  it('有消息时渲染，无消息时不渲染', () => {
    expect(render(<ErrorBanner message="API 返回错误（HTTP 500）" onDismiss={noop} />)).toContain(
      'API 返回错误（HTTP 500）',
    )
    expect(render(<ErrorBanner message={null} onDismiss={noop} />)).toBe('')
  })
})

describe('App', () => {
  it('整页能渲染出策略执行 / 信号 / 持仓 / 订单 / 成交区块', () => {
    const html = render(<App />)
    expect(html).toContain('Strategy Engine 执行器前端')
    expect(html).toContain('策略执行')
    expect(html).toContain('信号（Signals）')
    expect(html).toContain('模拟执行')
    expect(html).toContain('持仓（Positions）')
    expect(html).toContain('订单（Orders）')
    expect(html).toContain('成交（Trades）')
    expect(html).toContain('检测中')
  })
})

describe('刷新按钮状态', () => {
  it('加载中时刷新按钮显示刷新中并禁用', () => {
    const html = render(
      <PositionsPanel positions={[]} symbolsById={symbolsById} loading onRefresh={vi.fn()} />,
    )
    expect(html).toContain('刷新中')
    expect(html).toContain('disabled')
  })
})
