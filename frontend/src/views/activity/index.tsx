import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'
import ActivityDetailPanel from './DetailPanel'
import type { Lookup, Row } from './types'
import { ACTIVITY_ACTIONS, availableActions, displayValue } from './types'

const ENDPOINT = '/api/activity'
const columns = [
  "方案编号",
  "活动名称",
  "活动类型",
  "目标人群",
  "核心指标",
  "对照方案",
  "评估周期天",
  "显著性阈值",
  "status",
  "结项状态",
  "引用执行编号",
]
const columnLabels: Record<string, string> = {
  status: '活动状态',
  结项状态: '结项状态',
  引用执行编号: '引用执行编号',
}
const statuses = ["草稿", "方案评审中", "评估中", "已结项", "已取消"]
const batchActions = ["提交方案评审", "启动评估", "结项评估"]

type BatchCheck = { id: number; 方案编号?: string | null; status?: string | null; ok: boolean; message: string }

export default function ActivityView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<number[]>([])
  const [batchAction, setBatchAction] = useState<string>(batchActions[2])
  const [checks, setChecks] = useState<BatchCheck[]>([])
  const [lookup, setLookup] = useState<Lookup | null>(null)
  const [lookupLoading, setLookupLoading] = useState(false)
  const [lookupError, setLookupError] = useState('')
  const filterFields = useMemo(() => ["方案编号", "活动名称", "活动类型"], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('活动方案列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '活动评估列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function openLookup(row: Row) {
    setLookupLoading(true)
    setLookupError('')
    setLookup(null)
    try {
      const response = await request(`${ENDPOINT}/results?code=${encodeURIComponent(String(row.方案编号))}`)
      if (!response.ok) {
        throw new Error('结果回查失败，执行回执可能尚未生成')
      }
      setLookup(await response.json())
    } catch (error) {
      setLookupError(error instanceof Error ? error.message : '结果回查失败')
    } finally {
      setLookupLoading(false)
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message ?? '活动评估动作未生效')
      } else {
        setNotice(payload.message ?? '')
        if (payload.assessment) await openLookup(row)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '活动评估操作失败')
    }
  }

  async function submitBatch() {
    setErrorMessage('')
    setNotice('')
    if (selected.length === 0) {
      setErrorMessage('请先勾选需要整组提交的活动方案')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/batch`, {
        method: 'POST',
        body: JSON.stringify({ values: { action: batchAction, ids: selected } }),
      })
      const payload = await response.json()
      setChecks(payload.checks ?? [])
      if (!payload.ok) {
        setErrorMessage(payload.message ?? '整组提交未通过核对')
      } else {
        setNotice(`${payload.message}，生成 ${payload.assessments?.length ?? 0} 份结项明细`)
        setSelected([])
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '整组提交失败')
    }
  }

  function toggleSelected(row: Row) {
    const id = Number(row.id)
    setSelected((prev) => prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id])
  }

  const stats = useMemo(() => ([
    { label: '活动方案总数', value: total },
    { label: '评估中方案', value: rows.filter((row) => row.status === '评估中').length },
    { label: '已结项方案', value: rows.filter((row) => row.status === '已结项').length },
  ]), [rows, total])

  return (
    <section className="page" data-module="activity">
      <header className="page-head">
        <div>
          <h2>活动评估管理</h2>
          <p className="page-desc">
            结项结论只用已执行对比的数据快照重算；未执行对比不能支撑当前结项，草稿对比只进入审计明细。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出活动评估清单
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
        <span>已选 {selected.length} 条</span>
        <select value={batchAction} onChange={(event) => setBatchAction(event.target.value)}>
          {batchActions.map((action) => <option key={action} value={action}>{action}</option>)}
        </select>
        <button className="btn primary" type="button" onClick={() => void submitBatch()}>整组提交（逐条核对）</button>
        <button className="btn ghost" type="button" onClick={() => { setSelected([]); setChecks([]) }}>清空选择</button>
      </div>

      {checks.length > 0 && (
        <table className="data-table compact">
          <thead><tr><th>编号</th><th>方案编号</th><th>当前状态</th><th>核对结果</th><th>说明</th></tr></thead>
          <tbody>
            {checks.map((check) => (
              <tr key={check.id} className={check.ok ? '' : 'row-blocked'}>
                <td>{check.id}</td>
                <td>{check.方案编号 ?? '—'}</td>
                <td>{check.status ?? '—'}</td>
                <td>{check.ok ? '通过' : '阻断'}</td>
                <td>{check.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className={lookup ? 'split-layout' : ''}>
        <table className="data-table">
          <thead>
            <tr>
              <th className="col-select">选择</th>
              {columns.map((column) => <th key={column}>{columnLabels[column] ?? column}</th>)}
              <th>可执行动作</th>
              <th>明细</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const actions = availableActions(ACTIVITY_ACTIONS, row)
              return (
                <tr key={String(row.id)}>
                  <td className="col-select">
                    <input
                      type="checkbox"
                      checked={selected.includes(Number(row.id))}
                      onChange={() => toggleSelected(row)}
                      aria-label={`选择 ${row.方案编号}`}
                    />
                  </td>
                  {columns.map((column) => <td key={column}>{displayValue(row[column])}</td>)}
                  <td className="row-actions">
                    {actions.length === 0 && <span className="muted-text">—</span>}
                    {actions.map((action) => (
                      <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                        {action}
                      </button>
                    ))}
                  </td>
                  <td>
                    <button className="link" type="button" onClick={() => void openLookup(row)}>回查</button>
                  </td>
                </tr>
              )
            })}
            {rows.length === 0 && (
              <tr>
                <td className="empty-state" colSpan={columns.length + 3}>暂无活动评估数据，可先登记活动方案</td>
              </tr>
            )}
          </tbody>
        </table>
        {lookup && (
          <ActivityDetailPanel
            lookup={lookup}
            loading={lookupLoading}
            error={lookupError}
            onClose={() => setLookup(null)}
          />
        )}
      </div>

      <footer className="page-foot">
        <span>共 {total} 条活动评估记录</span>
        <span>
          {notice && <span className="notice-text">{notice}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
