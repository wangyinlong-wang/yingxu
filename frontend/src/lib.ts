import type { ApiErrorBody, Source, SourceKind } from './types'
export class ApiError extends Error { constructor(public status:number, public body:ApiErrorBody) { super(body.message) } }
export const actionKey = () => crypto.randomUUID()
export const formatDate = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { dateStyle:'medium', timeStyle:'short' }).format(new Date(value)) : '—'
export const formatBytes = (value?: number | null) => value == null ? '—' : value < 1024 * 1024 ? `${Math.round(value / 1024)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`
export const isConflict = (error: unknown) => error instanceof ApiError && error.status === 409 && error.body.code === 'VERSION_CONFLICT'

export const jobLabel = (kind: string) => ({COLLECT_SOURCE:'热点采集',GENERATE_CONTENT:'内容生成',PREPARE_PUBLICATION:'发布上传准备',COLLECT_METRICS:'作品数据采集',GENERATE_REPORT:'运营复盘',PIPELINE_BATCH:'每日内容计划',COLLECT_BATCH:'定时资讯采集',LOCAL_AI_PREVIEW:'本地声音与数字人测试',LOCAL_AI_TRANSCRIBE:'本地录音转文字'}[kind] || kind)
export const statusLabel = (value: string) => ({QUEUED:'排队中',RUNNING:'执行中',WAITING_USER:'待人工处理',SUCCEEDED:'已完成',FAILED:'未完成',CANCELED:'已取消',DRAFT:'草稿',GENERATING:'生成中',REVIEW_REQUIRED:'待审核',APPROVED:'已通过',REJECTED:'需修改'}[value] || value)
export const platformLabel = (value: string) => ({douyin:'抖音',xiaohongshu:'小红书'}[value] || value)
export const whitelistedStatus = (value: unknown, allowed: readonly string[]) => typeof value === 'string' && allowed.includes(value) ? value : ''
export const isPaidSource = (kind: SourceKind) => kind === 'x_tikhub' || kind === 'x_tikhub_search'
export const isXProfileSource = (kind: SourceKind) => kind === 'x' || kind === 'x_tikhub'
export const sourceKindLabel = (kind: SourceKind) => ({rss:'RSS / Atom',hackernews:'Hacker News',x:'X公开时间线',x_tikhub:'X（TikHub）',x_tikhub_search:'X搜索（TikHub）',html_list:'网页列表'}[kind] || kind)
export const scheduleKindLabel = (kind: string) => ({pipeline:'内容流水线',metrics:'作品数据采集',collect:'资讯采集'}[kind] || kind)
export const parseKeywords = (value: string) => { const seen = new Set<string>(); return value.split(/[,，、\n]/).map(item => item.trim()).filter(item => item && !seen.has(item.toLowerCase()) && seen.add(item.toLowerCase())) }
export const xSearchUrl = (query: string, mode: 'live' | 'top' = 'live') => `https://x.com/search?q=${encodeURIComponent(query.trim())}&f=${mode}`
export const xSearchParts = (url: string): { query: string; mode: 'live' | 'top' } => { try { const parsed = new URL(url); return { query: parsed.searchParams.get('q') || '', mode: parsed.searchParams.get('f') === 'top' ? 'top' : 'live' } } catch { return { query: '', mode: 'live' } } }
export const sourceHealth = (source: Pick<Source, 'enabled' | 'consecutive_failures' | 'next_eligible_at' | 'last_success_at'>, instant = new Date()): { label: string; type: 'success' | 'warning' | 'danger' | 'info' } => {
  if (!source.enabled) return { label: '已停用', type: 'info' }
  const failures = source.consecutive_failures || 0
  if (!failures) return source.last_success_at ? { label: '正常', type: 'success' } : { label: '尚未采集', type: 'info' }
  const cooling = source.next_eligible_at && new Date(source.next_eligible_at) > instant
  return { label: `连续失败 ${failures} 次${cooling ? '，自动采集暂缓' : ''}`, type: failures >= 3 ? 'danger' : 'warning' }
}
export const intervalLabel = (minutes?: number | null) => { const value = minutes || 180; return value % 60 ? `${value} 分钟` : `${value / 60} 小时` }
export const tikhubKeyText = (status?: { key_source: string; secure_store_available: boolean } | null) => !status ? '读取中' : status.key_source === 'keychain' ? '钥匙串已保存密钥' : status.key_source === 'environment' ? '使用环境变量密钥（.env）' : status.secure_store_available === false ? '钥匙串不可用' : '未配置密钥'
