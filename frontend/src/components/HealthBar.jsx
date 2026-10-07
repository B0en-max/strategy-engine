import React from 'react'

/**
 * API 连接状态（调用 GET /health）。
 * state: 'loading' | 'ok' | 'error'
 */
export default function HealthBar({ baseUrl, state, data, error, onRetry }) {
  const label =
    state === 'loading' ? '检测中…' : state === 'ok' ? 'API 连接正常' : 'API 无法连接'

  return (
    <div className={`health health-${state}`}>
      <span className="health-dot" aria-hidden="true" />
      <span className="health-label">{label}</span>
      <span className="health-meta">地址：{baseUrl}</span>
      {state === 'ok' && data ? (
        <span className="health-meta">
          数据库：{data.db_path} · 仅模拟执行：{data.simulation_only ? '是' : '否'} · 已连接券商：
          {data.broker_connected ? '是' : '否'}
        </span>
      ) : null}
      {state === 'error' && error ? <span className="health-meta health-error">{error}</span> : null}
      <button type="button" className="link-button" onClick={onRetry}>
        重新检测
      </button>
    </div>
  )
}
