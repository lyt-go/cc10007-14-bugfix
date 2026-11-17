import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'
import type { Row } from '@/views/activity/types'
import { COMPARISON_ACTIONS, availableActions, displayValue } from '@/views/activity/types'

const ENDPOINT = '/api/comparison'
const columns = [
  "对比编号",
  "对比名称",
  "实验组筛选",
  "对照组筛选",
  "对比指标",
  "时间对齐",
  "显著性检验",
  "效应量",
  "status",
  "评估状态",
  "回写活动编号",
  "最新执行编号",
]
const columnLabels: Record<string, string> = {
  status: '对比状态',
  评估状态: '评估状态',
  回写活动编号: '回写活动',
  最新执行编号: '最新执行编号',
}
const statuses = ["草稿", "对比审批中", "已执行", "已归档"]
const observationFields = [
  { key: '对照组样本量', label: '对照组样本量' },
  { key: '对照组转化数', label: '对照组转化数' },
  { key: '实验组样本量', label: '实验组样本量' },
  { key: '实验组转化数', label: '实验组转化数' },
]

type Execution = {
  执行编号: string
  执行时间: string
  快照版本: string
  对照组样本量: number
  对照组转化数: number
  实验组样本量: number
  实验组转化数: number
  显著性阈值: number
  关联活动方案编号: string | null
}

export default function ComparisonView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [observation, setObservation] = useState<Record<string, string>>({})
  const [executions, setExecutions] = useState<Execution[]>([])
  const [executionCode, setExecutionCode] = useState('')
  const filterFields = useMemo(() => ["对比编号", "对比名称", "实验组筛选"], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('对比组列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '对比分析列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function loadExecutions(code: string) {
    try {
      const response = await request(`${ENDPOINT}/executions?code=${encodeURIComponent(code)}`)
      if (!response.ok) {
        throw new Error('执行记录读取失败')
      }
      const payload = await response.json()
      setExecutionCode(code)
      setExecutions(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '执行记录读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    const values: Record<string, unknown> = { action }
    if (action === '执行对比') {
      for (const field of observationFields) {
        const raw = observation[field.key]
        if (raw !== undefined && raw !== '') {
          values[field.key] = Number(raw)
        }
      }
    }
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message ?? '对比分析动作未生效')
      } else {
        setNotice(payload.message ?? '')
        await loadExecutions(String(payload.entry?.对比编号 ?? row.对比编号))
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '对比分析操作失败')
    }
  }

  const stats = useMemo(() => ([
    { label: '对比组总数', value: total },
    { label: '已执行对比', value: rows.filter((row) => row.status === '已执行').length },
    { label: '待复核结论', value: rows.filter((row) => row.评估状态 === '待复核').length },
  ]), [rows, total])

  return (
    <section className="page" data-module="comparison">
      <header className="page-head">
        <div>
          <h2>对比分析管理</h2>
          <p className="page-desc">
            草稿对比不能直接执行；执行后会生成独立执行回执与数据快照，并同批回写引用它的活动方案状态。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出对比分析清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <div className="batch-bar">
        <span>执行对比观测数据（可留空，沿用既有快照）</span>
        {observationFields.map((field) => (
          <label key={field.key} className="filter-item">
            <span>{field.label}</span>
            <input
              value={observation[field.key] ?? ''}
              inputMode="numeric"
              onChange={(event) => setObservation((prev) => ({ ...prev, [field.key]: event.target.value }))}
            />
          </label>
        ))}
      </div>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{columnLabels[column] ?? column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const actions = availableActions(COMPARISON_ACTIONS, row)
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => <td key={column}>{displayValue(row[column])}</td>)}
                <td className="row-actions">
                  {actions.length === 0 && <span className="muted-text">—</span>}
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                  <button
                    className="link"
                    type="button"
                    onClick={() => void loadExecutions(String(row.对比编号))}
                  >
                    执行记录
                  </button>
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无对比分析数据，可先登记对比组</td>
            </tr>
          )}
        </tbody>
      </table>

      {executionCode && (
        <section className="detail-block">
          <h3>执行记录 · {executionCode}</h3>
          {executions.length === 0
            ? <p className="warn-text">该对比组尚无执行回执；未执行对比不能支撑活动结项结论。</p>
            : (
              <table className="data-table compact">
                <thead>
                  <tr>
                    <th>执行编号</th><th>快照版本</th><th>执行时间</th>
                    <th>对照组</th><th>实验组</th><th>显著性阈值</th><th>关联活动</th>
                  </tr>
                </thead>
                <tbody>
                  {executions.map((item) => (
                    <tr key={item.执行编号}>
                      <td>{item.执行编号}</td>
                      <td>{item.快照版本}</td>
                      <td>{item.执行时间}</td>
                      <td>{item.对照组转化数} / {item.对照组样本量}</td>
                      <td>{item.实验组转化数} / {item.实验组样本量}</td>
                      <td>{item.显著性阈值}</td>
                      <td>{item.关联活动方案编号 ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条对比分析记录</span>
        <span>
          {notice && <span className="notice-text">{notice}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
