import React from 'react'
import { formatTimestamp } from '../format.js'

/**
 * 策略执行区（左侧）：策略 / 标的 / 周期 / 可选时间范围 + 运行按钮。
 * 只收集参数并回调给 App，本身不调用任何逻辑。
 */
export default function StrategyRunnerPanel({
  strategies,
  symbols,
  timeframes,
  form,
  onChange,
  onRun,
  running,
  result,
  error,
  healthOk,
}) {
  const canRun = Boolean(form.strategyId && form.symbolId && form.timeframe) && !running

  return (
    <section className="panel">
      <h2>策略执行</h2>

      <label className="field">
        <span>策略</span>
        <select
          name="strategyId"
          value={form.strategyId}
          onChange={(event) => onChange('strategyId', event.target.value)}
        >
          <option value="">请选择策略</option>
          {strategies.map((strategy) => (
            <option key={strategy.id} value={strategy.id}>
              {`${strategy.name} v${strategy.version} [${strategy.status}]`}
            </option>
          ))}
        </select>
      </label>
      {strategies.length === 0 ? (
        <p className="hint">当前数据库没有策略记录，需先创建策略（strategies 表为空）。</p>
      ) : null}

      <label className="field">
        <span>标的</span>
        <select
          name="symbolId"
          value={form.symbolId}
          onChange={(event) => onChange('symbolId', event.target.value)}
        >
          <option value="">请选择标的</option>
          {symbols.map((symbol) => (
            <option key={symbol.id} value={symbol.id}>
              {`${symbol.symbol} ${symbol.name} (${symbol.market})`}
            </option>
          ))}
        </select>
      </label>

      <label className="field">
        <span>周期</span>
        <select
          name="timeframe"
          value={form.timeframe}
          onChange={(event) => onChange('timeframe', event.target.value)}
        >
          {timeframes.map((timeframe) => (
            <option key={timeframe} value={timeframe}>
              {timeframe}
            </option>
          ))}
        </select>
      </label>

      <label className="field">
        <span>开始时间（可选）</span>
        <input
          name="startTimestamp"
          type="text"
          placeholder="YYYY-MM-DD HH:MM:SS（UTC）"
          value={form.startTimestamp}
          onChange={(event) => onChange('startTimestamp', event.target.value)}
        />
      </label>

      <label className="field">
        <span>结束时间（可选）</span>
        <input
          name="endTimestamp"
          type="text"
          placeholder="YYYY-MM-DD HH:MM:SS（UTC）"
          value={form.endTimestamp}
          onChange={(event) => onChange('endTimestamp', event.target.value)}
        />
      </label>

      <button
        type="button"
        className="primary"
        onClick={onRun}
        disabled={!canRun || !healthOk}
        title={healthOk ? '' : 'API 未连接'}
      >
        {running ? '运行中…' : '运行策略'}
      </button>

      {error ? <p className="error-text">运行失败：{error}</p> : null}

      {result ? (
        <dl className="result">
          <dt>本次 K 线</dt>
          <dd>{result.bars}</dd>
          <dt>新增信号</dt>
          <dd>
            {result.signals}（long {result.long_signals} / short {result.short_signals}）
          </dd>
          <dt>跳过（已存在）</dt>
          <dd>{result.skipped}</dd>
          <dt>数据时间范围</dt>
          <dd>
            {formatTimestamp(result.first_bar_timestamp)} ~ {formatTimestamp(result.last_bar_timestamp)}
          </dd>
          <dt>策略类型</dt>
          <dd>{result.strategy_type}</dd>
        </dl>
      ) : null}
    </section>
  )
}
