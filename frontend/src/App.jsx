/**
 * 第一版执行器前端（Stage 17）。
 *
 * 分层：
 *   App（本文件）    —— 状态编排：加载数据、调用 API、把结果分发给各展示组件
 *   components/*     —— 纯展示 + 回调（不写任何策略/订单/成交/持仓逻辑）
 *   api/client.js    —— 唯一的 HTTP 出口
 *
 * 前端边界（刻意不做）：
 *   - 不计算持仓、不判断信号方向、不生成订单/成交（全部由后端 Stage 13/16 负责）
 *   - 不做登录 / 权限 / 实时行情 / 图表 / Dashboard
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react'

import * as api from './api/client.js'
import { CHART_SIGNAL_LIMIT, DEFAULT_KLINE_LIMIT, buildCandles, candleTimesOf, splitByChart } from './chart/klines.js'
import { API_BASE_URL, SIGNALS_REFRESH_INTERVAL_MS } from './config.js'
import { DEFAULT_SIGNAL_FILTERS, applySignalFilters, summarizeSignals } from './filters/signals.js'
import { indexById } from './format.js'
import ErrorBanner from './components/ErrorBanner.jsx'
import ExecutionPanel from './components/ExecutionPanel.jsx'
import HealthBar from './components/HealthBar.jsx'
import KlineChartPanel from './components/KlineChartPanel.jsx'
import OrdersPanel from './components/OrdersPanel.jsx'
import PositionsPanel from './components/PositionsPanel.jsx'
import SignalsPanel from './components/SignalsPanel.jsx'
import StrategyRunnerPanel from './components/StrategyRunnerPanel.jsx'
import TradesPanel from './components/TradesPanel.jsx'
import MarketPage from './components/market/MarketPage.jsx'

/** 可选周期：与数据库现有 K 线一致（15m / 1d），后续接入更多周期时在这里补充 */
export const TIMEFRAMES = ['15m', '1d']

/** 列表默认展示条数 */
export const RECENT_LIMIT = 20

const INITIAL_FORM = {
  strategyId: '',
  symbolId: '',
  timeframe: TIMEFRAMES[0],
  startTimestamp: '',
  endTimestamp: '',
}

export default function App() {
  // --- 基础信息 ---------------------------------------------------------
  const [health, setHealth] = useState({ state: 'loading', data: null, error: null })
  const [strategies, setStrategies] = useState([])
  const [symbols, setSymbols] = useState([])
  const [loadError, setLoadError] = useState(null)

  // --- 策略执行 ---------------------------------------------------------
  const [form, setForm] = useState(INITIAL_FORM)
  const [runState, setRunState] = useState({ running: false, result: null, error: null })

  // --- 信号 -------------------------------------------------------------
  const [signals, setSignals] = useState([])
  const [signalsState, setSignalsState] = useState({
    loading: false,
    error: null,
    autoRefresh: true,
    lastUpdated: null,
  })
  const [selectedSignalId, setSelectedSignalId] = useState(null)

  // --- 列表筛选（区分「策略信号」与「已执行交易」）------------------------
  const [signalsFilter, setSignalsFilter] = useState(DEFAULT_SIGNAL_FILTERS)

  // --- K 线（图表，Stage 18）-------------------------------------------
  const [klines, setKlines] = useState([])
  const [klinesState, setKlinesState] = useState({ loading: false, error: null })

  // --- 模拟执行 ---------------------------------------------------------
  const [quantity, setQuantity] = useState('1')
  const [price, setPrice] = useState('')
  const [execState, setExecState] = useState({ executing: false, result: null, error: null })
  const [busySignalId, setBusySignalId] = useState(null)

  // --- 持仓 / 订单 / 成交 -----------------------------------------------
  const [positions, setPositions] = useState([])
  const [orders, setOrders] = useState([])
  const [trades, setTrades] = useState([])
  const [recordsState, setRecordsState] = useState({ loading: false, error: null })
  // 顶层工作区：executor = 策略执行（默认，保持原有行为）；market = 市场选股（Stage 20）
  const [workspace, setWorkspace] = useState('executor')

  const symbolsById = useMemo(() => indexById(symbols), [symbols])
  const selectedSignal = useMemo(
    () => signals.find((signal) => signal.id === selectedSignalId) ?? null,
    [signals, selectedSignalId],
  )
  // K 线行 → 蜡烛数据（纯换算，见 chart/klines.js）
  const candles = useMemo(() => buildCandles(klines), [klines])
  // ---- 统一的 Signal / Trade 派生层（K 线图与交易日志共用同一份结果）----
  //   已加载 signals → applySignalFilters（唯一筛选逻辑）→ filteredSignals
  //        ├── K 线图：filteredSignals（按 timestamp 落到对应 K 线上）
  //        └── 交易日志：filteredSignals（同一个数组，不各自维护一套、也不各自筛选）
  const filteredSignals = useMemo(
    () => applySignalFilters(signals, signalsFilter),
    [signals, signalsFilter],
  )
  // 供一致性核对：筛选结果里哪些落在当前 K 线上（onChart + offChart === filteredSignals.length）
  const chartMatches = useMemo(
    () => splitByChart(filteredSignals, candleTimesOf(candles)),
    [filteredSignals, candles],
  )
  // K 线图与日志共用同一份统计口径（避免两处各算一遍得出不一致的数字）
  const signalSummary = useMemo(() => summarizeSignals(filteredSignals), [filteredSignals])

  // ------------------------------------------------------------------ 加载
  const checkHealth = useCallback(async () => {
    setHealth({ state: 'loading', data: null, error: null })
    try {
      const data = await api.getHealth()
      setHealth({ state: 'ok', data, error: null })
    } catch (error) {
      setHealth({ state: 'error', data: null, error: error.message })
    }
  }, [])

  const loadReferenceData = useCallback(async () => {
    try {
      const [strategyRows, symbolRows] = await Promise.all([api.listStrategies(), api.listSymbols()])
      setStrategies(strategyRows)
      setSymbols(symbolRows)
      setLoadError(null)
      setForm((previous) => ({
        ...previous,
        strategyId: previous.strategyId || (strategyRows[0] ? String(strategyRows[0].id) : ''),
        symbolId: previous.symbolId || (symbolRows[0] ? String(symbolRows[0].id) : ''),
      }))
    } catch (error) {
      setLoadError(error.message)
    }
  }, [])

  const loadSignals = useCallback(
    async ({ quiet = false } = {}) => {
      if (!form.strategyId) {
        setSignals([])
        return
      }
      if (!quiet) setSignalsState((previous) => ({ ...previous, loading: true, error: null }))
      try {
        const rows = await api.listSignals({
          strategyId: Number(form.strategyId),
          symbolId: form.symbolId ? Number(form.symbolId) : undefined,
          timeframe: form.timeframe || undefined,
          limit: CHART_SIGNAL_LIMIT,
        })
        setSignals(rows)
        setSignalsState((previous) => ({
          ...previous,
          loading: false,
          error: null,
          lastUpdated: new Date().toLocaleTimeString(),
        }))
      } catch (error) {
        setSignalsState((previous) => ({ ...previous, loading: false, error: error.message }))
      }
    },
    [form.strategyId, form.symbolId, form.timeframe],
  )

  const loadKlines = useCallback(async () => {
    if (!form.symbolId || !form.timeframe) {
      setKlines([])
      return
    }
    // 切换标的 / 周期 / 起止时间时先清空，避免残留旧 K 线与旧信号标记
    setKlines([])
    setKlinesState({ loading: true, error: null })
    try {
      const rows = await api.listKlines({
        symbolId: Number(form.symbolId),
        timeframe: form.timeframe,
        startTimestamp: form.startTimestamp.trim() || undefined,
        endTimestamp: form.endTimestamp.trim() || undefined,
        limit: DEFAULT_KLINE_LIMIT,
      })
      setKlines(rows)
      setKlinesState({ loading: false, error: null })
    } catch (error) {
      setKlines([])
      setKlinesState({ loading: false, error: error.message })
    }
  }, [form.symbolId, form.timeframe, form.startTimestamp, form.endTimestamp])

  const loadRecords = useCallback(async () => {
    setRecordsState({ loading: true, error: null })
    try {
      const [positionRows, orderRows, tradeRows] = await Promise.all([
        api.listPositions(),
        api.listOrders({ limit: RECENT_LIMIT }),
        api.listTrades({ limit: RECENT_LIMIT }),
      ])
      setPositions(positionRows)
      setOrders(orderRows)
      setTrades(tradeRows)
      setRecordsState({ loading: false, error: null })
    } catch (error) {
      setRecordsState({ loading: false, error: error.message })
    }
  }, [])

  // 首次加载：健康检查 + 策略 / 标的 + 持仓订单成交
  useEffect(() => {
    checkHealth()
    loadReferenceData()
    loadRecords()
  }, [checkHealth, loadReferenceData, loadRecords])

  // 策略 / 标的 / 周期变化 → 重新拉信号
  useEffect(() => {
    loadSignals()
  }, [loadSignals])

  // 标的 / 周期 / 起止时间变化 → 重新加载 K 线
  useEffect(() => {
    loadKlines()
  }, [loadKlines])

  // 信号自动刷新
  useEffect(() => {
    if (!signalsState.autoRefresh) return undefined
    const timer = setInterval(() => {
      loadSignals({ quiet: true })
    }, SIGNALS_REFRESH_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [signalsState.autoRefresh, loadSignals])

  // ------------------------------------------------------------------ 交互
  const handleFormChange = useCallback((name, value) => {
    setForm((previous) => ({ ...previous, [name]: value }))
  }, [])

  const handleRun = useCallback(async () => {
    if (!form.strategyId || !form.symbolId || runState.running) return
    setRunState({ running: true, result: null, error: null })
    setExecState({ executing: false, result: null, error: null })
    try {
      const result = await api.runStrategy(Number(form.strategyId), {
        symbolId: Number(form.symbolId),
        timeframe: form.timeframe,
        startTimestamp: form.startTimestamp.trim() || undefined,
        endTimestamp: form.endTimestamp.trim() || undefined,
      })
      setRunState({ running: false, result, error: null })
      setSelectedSignalId(null)
      await loadSignals({ quiet: true })
    } catch (error) {
      setRunState({ running: false, result: null, error: error.message })
    }
  }, [form, runState.running, loadSignals])

  const handleSelectSignal = useCallback((signal) => {
    setSelectedSignalId(signal.id)
  }, [])

  const handleFilterChange = useCallback((key, value) => {
    setSignalsFilter((previous) => ({ ...previous, [key]: value }))
  }, [])

  const handleExecute = useCallback(
    async (signal = selectedSignal) => {
      if (!signal) return

      const quantityValue = Number(quantity)
      if (quantity === '' || !Number.isFinite(quantityValue) || quantityValue <= 0) {
        setExecState({ executing: false, result: null, error: '数量必须是大于 0 的数字' })
        return
      }
      const trimmedPrice = price.trim()
      if (trimmedPrice !== '' && !(Number(trimmedPrice) > 0)) {
        setExecState({ executing: false, result: null, error: '价格必须是大于 0 的数字，或留空使用信号价' })
        return
      }

      setSelectedSignalId(signal.id)
      setBusySignalId(signal.id)
      setExecState({ executing: true, result: null, error: null })
      try {
        const result = await api.executeSignal(signal.id, {
          quantity: quantityValue,
          price: trimmedPrice === '' ? null : Number(trimmedPrice),
        })
        setExecState({ executing: false, result, error: null })
        await Promise.all([loadSignals({ quiet: true }), loadRecords()])
      } catch (error) {
        setExecState({ executing: false, result: null, error: error.message })
        // 409（非 pending）等情况下信号状态可能已变化，同步一次列表
        await loadSignals({ quiet: true })
      } finally {
        setBusySignalId(null)
      }
    },
    [quantity, price, selectedSignal, loadSignals, loadRecords],
  )

  // ------------------------------------------------------------------ 渲染
  const healthOk = health.state === 'ok'

  return (
    <div className="app">
      <header className="app-header">
        <h1>Strategy Engine 执行器前端</h1>
        <p className="hint">
          数据 → 策略 → 信号 → 模拟执行 → 持仓 / 订单 / 成交。当前只操作本地模拟执行器，
          <strong>不连接任何真实交易所</strong>。
        </p>
      </header>

      <nav className="workspace-nav">
        <button
          type="button"
          className={workspace === 'executor' ? 'workspace-tab active' : 'workspace-tab'}
          onClick={() => setWorkspace('executor')}
        >
          策略执行
        </button>
        <button
          type="button"
          className={workspace === 'market' ? 'workspace-tab active' : 'workspace-tab'}
          onClick={() => setWorkspace('market')}
        >
          市场选股
        </button>
      </nav>

      <HealthBar
        baseUrl={API_BASE_URL}
        state={health.state}
        data={health.data}
        error={health.error}
        onRetry={checkHealth}
      />

      {workspace === 'executor' ? (
        <>
          <ErrorBanner
            message={loadError}
            title="加载策略 / 标的失败"
            onDismiss={() => setLoadError(null)}
          />

      <KlineChartPanel
        candles={candles}
        signals={filteredSignals}
        summary={signalSummary}
        symbolsById={symbolsById}
        symbolId={form.symbolId ? Number(form.symbolId) : null}
        timeframe={form.timeframe}
        loading={klinesState.loading}
        error={klinesState.error}
        onRefresh={loadKlines}
        onSelectSignal={handleSelectSignal}
      />

      <main className="layout">
        <div className="column column-left">
          <StrategyRunnerPanel
            strategies={strategies}
            symbols={symbols}
            timeframes={TIMEFRAMES}
            form={form}
            onChange={handleFormChange}
            onRun={handleRun}
            running={runState.running}
            result={runState.result}
            error={runState.error}
            healthOk={healthOk}
          />
          <ExecutionPanel
            selectedSignal={selectedSignal}
            symbolsById={symbolsById}
            quantity={quantity}
            price={price}
            onQuantityChange={setQuantity}
            onPriceChange={setPrice}
            onExecute={() => handleExecute()}
            executing={execState.executing}
            result={execState.result}
            error={execState.error}
          />
        </div>

        <div className="column column-middle">
          <SignalsPanel
            signals={filteredSignals}
            symbolsById={symbolsById}
            loading={signalsState.loading}
            error={signalsState.error}
            autoRefresh={signalsState.autoRefresh}
            lastUpdated={signalsState.lastUpdated}
            selectedSignalId={selectedSignalId}
            busySignalId={busySignalId}
            filters={signalsFilter}
            onFilterChange={handleFilterChange}
            loadedCount={signals.length}
            filteredCount={filteredSignals.length}
            offChartCount={chartMatches.offChart.length}
            onToggleAutoRefresh={() =>
              setSignalsState((previous) => ({ ...previous, autoRefresh: !previous.autoRefresh }))
            }
            onRefresh={() => loadSignals()}
            onSelect={handleSelectSignal}
            onExecute={handleExecute}
          />
        </div>

        <div className="column column-right">
          <PositionsPanel
            positions={positions}
            symbolsById={symbolsById}
            loading={recordsState.loading}
            onRefresh={loadRecords}
          />
        </div>
      </main>

      <ErrorBanner
        message={recordsState.error}
        title="加载持仓 / 订单 / 成交失败"
        onDismiss={() => setRecordsState((previous) => ({ ...previous, error: null }))}
      />

      <div className="records">
        <OrdersPanel
          orders={orders}
          symbolsById={symbolsById}
          loading={recordsState.loading}
          onRefresh={loadRecords}
        />
        <TradesPanel
          trades={trades}
          symbolsById={symbolsById}
          loading={recordsState.loading}
          onRefresh={loadRecords}
        />
      </div>
        </>
      ) : (
        <MarketPage />
      )}
    </div>
  )
}
