import type { ApiErrorBody } from './types'
export class ApiError extends Error { constructor(public status:number, public body:ApiErrorBody) { super(body.message) } }
export const actionKey = () => crypto.randomUUID()
export const formatDate = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { dateStyle:'medium', timeStyle:'short' }).format(new Date(value)) : '—'
export const formatBytes = (value?: number | null) => value == null ? '—' : value < 1024 * 1024 ? `${Math.round(value / 1024)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`
export const isConflict = (error: unknown) => error instanceof ApiError && error.status === 409 && error.body.code === 'VERSION_CONFLICT'

export const jobLabel = (kind: string) => ({COLLECT_SOURCE:'热点采集',GENERATE_CONTENT:'内容生成',PREPARE_PUBLICATION:'手机上传准备',COLLECT_METRICS:'作品数据采集',GENERATE_REPORT:'运营复盘',PIPELINE_BATCH:'每日内容计划',LOCAL_AI_PREVIEW:'本地声音与数字人测试',LOCAL_AI_TRANSCRIBE:'本地录音转文字'}[kind] || kind)
export const statusLabel = (value: string) => ({QUEUED:'排队中',RUNNING:'执行中',WAITING_USER:'待人工处理',SUCCEEDED:'已完成',FAILED:'未完成',CANCELED:'已取消',DRAFT:'草稿',GENERATING:'生成中',REVIEW_REQUIRED:'待审核',APPROVED:'已通过',REJECTED:'需修改'}[value] || value)
