/** 活动评估 / 对比分析共用的类型与展示口径：后端口径直接映射，避免页面各写一套。 */
export type Row = Record<string, string | number | null | undefined | Record<string, unknown>>

export type Assessment = {
  明细编号: string
  活动方案编号: string
  对比编号: string
  执行编号: string
  快照版本?: string
  显著性结论: string
  p值: number
  z值: number
  显著性阈值: number
  提升率: number | null
  结项状态: string
  数据口径: string
  告警时点: string
  数据快照?: Record<string, string | number | null>
  被拒绝的对比引用?: { 对比编号: string; status: string; 拒绝原因: string }[]
  审计明细?: Record<string, unknown>[]
}

export type Execution = {
  执行编号: string
  对比编号: string
  执行时间: string
  快照版本: string
  对照组样本量: number
  对照组转化数: number
  实验组样本量: number
  实验组转化数: number
  显著性阈值: number
  数据快照: Record<string, string | number | null>
} | null

export type Lookup = {
  活动方案编号: string
  活动状态: string
  结项状态: string
  对照方案: string | null
  引用的执行编号: string | null
  结项明细编号: string | null
  对比组状态: string | null
  可引用执行: boolean
  执行回执: Execution
  结项评估明细: Assessment[]
  被拒绝的对比引用: { 对比编号: string; status: string; 拒绝原因: string }[]
  审计明细: { 审计编号: string; 审计类型: string; 审计说明: string; 记录时间: string }[]
  口径说明: string
}

/** 允许在某状态下执行的动作：与后端 ACTION_SOURCES 对齐，页面不再无条件展示全部按钮。 */
export const ACTIVITY_ACTIONS: Record<string, string[]> = {
  提交方案评审: ['草稿'],
  启动评估: ['方案评审中'],
  结项评估: ['评估中', '已结项'],
  取消方案: ['草稿', '方案评审中', '评估中'],
}

export const COMPARISON_ACTIONS: Record<string, string[]> = {
  提交对比审批: ['草稿'],
  执行对比: ['对比审批中', '已执行'],
  归档对比: ['已执行'],
}

export function availableActions(
  actionSources: Record<string, string[]>,
  row: Row,
): string[] {
  const status = String(row.status ?? '')
  return Object.entries(actionSources)
    .filter(([, sources]) => sources.includes(status))
    .map(([action]) => action)
}

export function displayValue(value: Row[string]): string | number {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return value
}

export function formatRate(value: unknown): string {  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  if (Number.isNaN(number)) return String(value)
  return `${(number * 100).toFixed(2)}%`
}

export function formatP(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  if (Number.isNaN(number)) return String(value)
  return number < 0.0001 ? '<0.0001' : number.toFixed(6)
}
