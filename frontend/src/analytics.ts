import type { Metric } from './types'

export type MetricField = 'views'|'likes'|'comments'|'favorites'|'shares'
export const metricLabels:Record<MetricField,string>={views:'播放',likes:'点赞',comments:'评论',favorites:'收藏',shares:'分享'}

export function observedAtIso(value:Date|null):string|undefined {
  return value ? value.toISOString() : undefined
}

export function orderedMetrics(metrics:Metric[]):Metric[] {
  return [...metrics].sort((left,right) => new Date(left.observed_at).getTime() - new Date(right.observed_at).getTime())
}

export function metricSeries(metrics:Metric[],field:MetricField) {
  return orderedMetrics(metrics).filter(metric => metric[field] !== null).map(metric => ({at:metric.observed_at,value:metric[field] as number}))
}

export function latestMetricChange(metrics:Metric[],field:MetricField):{latest:number|null; previous:number|null; delta:number|null} {
  const series=metricSeries(metrics,field)
  const latest=series.at(-1)?.value ?? null
  const previous=series.at(-2)?.value ?? null
  return {latest,previous,delta:latest === null || previous === null ? null : latest - previous}
}
