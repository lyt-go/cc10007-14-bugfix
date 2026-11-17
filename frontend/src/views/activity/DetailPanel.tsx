import type { Assessment, Execution, Lookup } from './types'
import { formatP, formatRate } from './types'

type DetailProps = {
  lookup: Lookup | null
  loading: boolean
  error: string
  onClose: () => void
}

function SnapshotTable({ snapshot }: { snapshot?: Record<string, string | number | null> }) {
  if (!snapshot) return <p className="muted-text">无数据快照</p>
  const entries = Object.entries(snapshot)
  return (
    <table className="data-table compact">
      <thead>
        <tr><th>快照字段</th><th>取值</th></tr>
      </thead>
      <tbody>
        {entries.map(([key, value]) => (
          <tr key={key}><td>{key}</td><td>{String(value ?? '—')}</td></tr>
        ))}
      </tbody>
    </table>
  )
}

function ExecutionBlock({ execution }: { execution: Execution }) {
  if (!execution) {
    return <p className="warn-text">暂无已执行回执：未执行对比不能支撑任何当前结项结论。</p>
  }
  return (
    <div className="detail-block">
      <h4>执行回执 {execution.执行编号}（快照 {execution.快照版本}）</h4>
      <table className="data-table compact">
        <thead><tr><th>组别</th><th>样本量</th><th>转化数</th><th>转化率</th></tr></thead>
        <tbody>
          <tr>
            <td>对照组</td>
            <td>{execution.对照组样本量}</td>
            <td>{execution.对照组转化数}</td>
            <td>{formatRate(Number(execution.对照组转化数) / Number(execution.对照组样本量))}</td>
          </tr>
          <tr>
            <td>实验组</td>
            <td>{execution.实验组样本量}</td>
            <td>{execution.实验组转化数}</td>
            <td>{formatRate(Number(execution.实验组转化数) / Number(execution.实验组样本量))}</td>
          </tr>
        </tbody>
      </table>
      <p className="muted-text">执行时间：{execution.执行时间}，显著性阈值：{execution.显著性阈值}</p>
    </div>
  )
}

function AssessmentBlock({ item }: { item: Assessment }) {
  return (
    <div className="detail-block">
      <h4>
        结项明细 {item.明细编号}
        <span className={`tag ${item.显著性结论.startsWith('显著') ? 'tag-ok' : 'tag-muted'}`}>
          {item.显著性结论}
        </span>
        <span className="tag tag-muted">{item.数据口径}</span>
      </h4>
      <table className="data-table compact">
        <tbody>
          <tr><th>引用执行</th><td>{item.执行编号}（快照 {item.快照版本 ?? '—'}）</td>
              <th>对比组</th><td>{item.对比编号}</td></tr>
          <tr><th>p 值</th><td>{formatP(item.p值)}</td>
              <th>z 值</th><td>{Number(item.z值).toFixed(6)}</td></tr>
          <tr><th>提升率</th><td>{formatRate(item.提升率)}</td>
              <th>显著性阈值</th><td>{item.显著性阈值}</td></tr>
          <tr><th>告警时点</th><td>{item.告警时点}</td>
              <th>结项状态</th><td>{item.结项状态}</td></tr>
        </tbody>
      </table>
      <details>
        <summary>数据快照（结论重算依据）</summary>
        <SnapshotTable snapshot={item.数据快照} />
      </details>
      {item.被拒绝的对比引用 && item.被拒绝的对比引用.length > 0 && (
        <div className="detail-block">
          <h5>被拒绝的对比引用（仅审计，不进入结论）</h5>
          <ul className="plain-list">
            {item.被拒绝的对比引用.map((rejected) => (
              <li key={rejected.对比编号}>
                {rejected.对比编号} · 状态 {rejected.status} · {rejected.拒绝原因}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

export default function ActivityDetailPanel({ lookup, loading, error, onClose }: DetailProps) {
  return (
    <aside className="detail-panel">
      <header className="detail-head">
        <h3>结果回查 {lookup?.活动方案编号 ?? ''}</h3>
        <button className="btn ghost" type="button" onClick={onClose}>关闭</button>
      </header>
      {loading && <p className="muted-text">正在读取执行回执与结项明细…</p>}
      {error && <p className="error-text">{error}</p>}
      {lookup && (
        <>
          <table className="data-table compact">
            <tbody>
              <tr><th>活动状态</th><td>{lookup.活动状态}</td>
                  <th>结项状态</th><td>{lookup.结项状态}</td></tr>
              <tr><th>对照方案</th><td>{lookup.对照方案 ?? '—'}</td>
                  <th>对比组状态</th><td>{lookup.对比组状态 ?? '—'}</td></tr>
              <tr><th>引用执行</th><td>{lookup.引用的执行编号 ?? '—'}</td>
                  <th>结项明细</th><td>{lookup.结项明细编号 ?? '—'}</td></tr>
            </tbody>
          </table>
          <p className="scope-note">{lookup.口径说明}</p>

          <ExecutionBlock execution={lookup.执行回执} />

          <section className="detail-block">
            <h4>结项评估明细（{lookup.结项评估明细.length}）</h4>
            {lookup.结项评估明细.length === 0
              ? <p className="muted-text">尚无结项评估明细，当前版本可凭执行回执契约材料对照。</p>
              : lookup.结项评估明细.map((item) => <AssessmentBlock key={item.明细编号} item={item} />)}
          </section>

          <section className="detail-block">
            <h4>被拒绝的对比引用（{lookup.被拒绝的对比引用.length}）</h4>
            {lookup.被拒绝的对比引用.length === 0
              ? <p className="muted-text">无</p>
              : (
                <ul className="plain-list">
                  {lookup.被拒绝的对比引用.map((item) => (
                    <li key={item.对比编号}>{item.对比编号} · 状态 {item.status} · {item.拒绝原因}</li>
                  ))}
                </ul>
              )}
          </section>

          <section className="detail-block">
            <h4>审计明细（{lookup.审计明细.length}）</h4>
            {lookup.审计明细.length === 0
              ? <p className="muted-text">无</p>
              : (
                <table className="data-table compact">
                  <thead><tr><th>审计编号</th><th>类型</th><th>说明</th><th>时间</th></tr></thead>
                  <tbody>
                    {lookup.审计明细.map((item) => (
                      <tr key={item.审计编号}>
                        <td>{item.审计编号}</td>
                        <td>{item.审计类型}</td>
                        <td>{item.审计说明}</td>
                        <td>{item.记录时间}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
          </section>
        </>
      )}
    </aside>
  )
}
