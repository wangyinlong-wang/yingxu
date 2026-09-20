import { describe, expect, it, vi } from 'vitest'
import { canApproveContent, contentFingerprint, contentPatch, platformDescription, platformTextIssues, platformTitleUnits, reorderedScenes, saveBeforeAction } from '../src/content-editor'
import type { Content } from '../src/types'

const content = ():Content => ({
  id:'content-1', topic_id:'topic-1', title:'原题', version:4, media_version:4, status:'REVIEW_REQUIRED', template_id:null,
  latest_quality_review:{status:'PASS',video_asset_id:'video-1',report:{}},script:{angle:'角度',selection_reason:'理由',facts:[{claim:'已核验事实',source_url:'https://example.com',verified:true,notes:''}],platforms:{douyin:{title:'测试标题',description:'测试正文，AI辅助制作。',tags:['测试']},xiaohongshu:{title:'测试标题',description:'测试正文，AI辅助制作。',tags:['测试']}}},
  scenes:[{id:'a',order:1,narration:'一',caption:'',image_prompt:'',image_asset_id:'image-a',audio_asset_ids:[]},{id:'b',order:2,narration:'二',caption:'',image_prompt:'',image_asset_id:null,audio_asset_ids:[]}],
  video_asset_id:'video-1',cover_asset_id:null,subtitle_asset_id:null,created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z'
})

describe('content editor safeguards', () => {
  it('saves a dirty editor before submitting generation', async () => {
    const save=vi.fn(async()=>true), generate=vi.fn(async()=>undefined)
    await expect(saveBeforeAction(true,save,generate)).resolves.toBe(true)
    expect(save).toHaveBeenCalledOnce(); expect(generate).toHaveBeenCalledOnce(); expect(save.mock.invocationCallOrder[0]).toBeLessThan(generate.mock.invocationCallOrder[0])
  })
  it('blocks approval without facts or a current-version video', () => {
    const candidate=content(); expect(canApproveContent(candidate)).toBe(true)
    candidate.media_version=3; expect(canApproveContent(candidate)).toBe(false)
    candidate.media_version=4; candidate.script.facts=[]; expect(canApproveContent(candidate)).toBe(false)
    candidate.script.facts=[{claim:'未核验',source_url:'',verified:false,notes:''}]; expect(canApproveContent(candidate)).toBe(false)
    candidate.script.facts=[{claim:'已核验',source_url:'https://example.com',verified:true,notes:''}]; candidate.latest_quality_review={status:'REVISE',video_asset_id:'video-1',report:{}}; expect(canApproveContent(candidate)).toBe(false)
    candidate.latest_quality_review={status:'PASS',video_asset_id:'old-video',report:{}}; expect(canApproveContent(candidate)).toBe(false)
  })
  it('counts platform copy with full-width and hashtag rules', () => {
    expect(platformTitleUnits('中A')).toBe(3)
    const text={title:'测试',description:'正文，AI辅助制作。',tags:['AI','技术']}
    expect(platformDescription(text)).toBe('正文，AI辅助制作。\n#AI #技术')
    expect(platformTextIssues('xiaohongshu',text)).toEqual([])
    expect(platformTextIssues('xiaohongshu',{...text,title:'中'.repeat(21)})).toContain('小红书标题超过20个中文字符')
    expect(platformTextIssues('douyin',{...text,description:'未标识',tags:[]})).toContain('抖音描述需包含AI辅助制作标识')
  })
  it('normalizes cleared image material and preserves display order', () => {
    const candidate=content(); candidate.scenes[0].image_asset_id=''
    expect(contentPatch(candidate).scenes[0].image_asset_id).toBeNull()
    expect(reorderedScenes(candidate.scenes,1,0).map(scene=>[scene.id,scene.order])).toEqual([['b',1],['a',2]])
  })
  it('uses motion defaults without marking old scenes as changed and includes them in patches', () => {
    const oldContent=content()
    const explicitDefaults=content()
    explicitDefaults.scenes.forEach(scene => { scene.motion='auto'; scene.transition='fade' })
    expect(contentFingerprint(oldContent)).toBe(contentFingerprint(explicitDefaults))
    expect(contentPatch(oldContent).scenes[0]).toMatchObject({ motion:'auto', transition:'fade' })
  })
})
