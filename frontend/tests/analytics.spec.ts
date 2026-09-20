import { describe, expect, it } from 'vitest'
import { latestMetricChange, metricSeries, observedAtIso, orderedMetrics } from '../src/analytics'
import type { Metric } from '../src/types'

const metric=(at:string,views:number|null):Metric => ({id:at,publication_id:'p',platform:'douyin',content_title:'作品',views,likes:null,comments:null,favorites:null,shares:null,source:'manual',raw:{},observed_at:at})

describe('analytics snapshots', () => {
  it('keeps null distinct from a real zero and never sums snapshots', () => {
    const values=[metric('2026-01-03T00:00:00Z',0),metric('2026-01-01T00:00:00Z',null),metric('2026-01-02T00:00:00Z',12)]
    expect(orderedMetrics(values).map(item=>item.views)).toEqual([null,12,0])
    expect(metricSeries(values,'views').map(item=>item.value)).toEqual([12,0])
    expect(latestMetricChange(values,'views')).toEqual({latest:0,previous:12,delta:-12})
  })
  it('serializes a local Date as its actual UTC instant and omits absent observations', () => {
    const value=new Date('2026-01-01T08:30:00+08:00')
    expect(observedAtIso(value)).toBe('2026-01-01T00:30:00.000Z')
    expect(observedAtIso(null)).toBeUndefined()
  })
})
