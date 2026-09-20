import type { Content, PlatformText, Scene } from './types'

export const platformTextRules = {
  douyin:{ label:'抖音', titleUnits:60, titleHanChars:30, descriptionChars:1000 },
  xiaohongshu:{ label:'小红书', titleUnits:40, titleHanChars:20, descriptionChars:1000 }
} as const

export type PublishingPlatform = keyof typeof platformTextRules

export function platformTitleUnits(value:string):number {
  return Array.from(value).reduce((total, character) => total + (character.codePointAt(0)! < 128 ? 1 : 2), 0)
}

export function platformDescription(text:PlatformText):string {
  const tags=text.tags.map(tag=>`#${tag.replace(/^#+/,'')}`).join(' ')
  return [text.description,tags].filter(Boolean).join('\n')
}

export function platformTextIssues(platform:PublishingPlatform,text:PlatformText):string[] {
  const rule=platformTextRules[platform]
  const issues:string[]=[]
  if (!text.title.trim()) issues.push(`${rule.label}标题不能为空`)
  if (platformTitleUnits(text.title)>rule.titleUnits) issues.push(`${rule.label}标题超过${rule.titleHanChars}个中文字符`)
  if (!text.description.trim()) issues.push(`${rule.label}描述不能为空`)
  if (Array.from(platformDescription(text)).length>rule.descriptionChars) issues.push(`${rule.label}描述与话题合计超过${rule.descriptionChars}个字符`)
  if (!/(?:AI\s*(?:辅助制作|生成|创作)|人工智能(?:辅助)?(?:生成|制作|创作)|AIGC)/iu.test(text.description)) issues.push(`${rule.label}描述需包含AI辅助制作标识`)
  return issues
}

export function contentPlatformIssues(content:Content):string[] {
  return (Object.keys(platformTextRules) as PublishingPlatform[]).flatMap(platform=>platformTextIssues(platform,content.script.platforms[platform]))
}

export const cloneContent = <T>(value:T):T => JSON.parse(JSON.stringify(value))

export function normalizedScene(scene:Scene):Scene {
  return {
    ...scene,
    motion:scene.motion ?? 'auto',
    transition:scene.transition ?? 'fade',
    scene_type:scene.scene_type ?? 'image',
    visual_data:{ headline:'', body:'', value:'', source_url:'', emphasis:'', ...scene.visual_data },
    hero_moment:scene.hero_moment ?? false,
    shot_intent:scene.shot_intent ?? ''
  }
}

export function normalizedContent(content:Content):Content {
  const cloned=cloneContent(content)
  return { ...cloned, scenes:cloned.scenes.map(normalizedScene) }
}

export function contentFingerprint(content:Content):string {
  return JSON.stringify({
    title:content.title,
    template_id:content.template_id,
    preset_id:content.preset_id ?? 'tech_brief_video_v1',
    script:content.script,
    scenes:content.scenes.map(({id, order, narration, caption, highlight_terms, image_prompt, image_asset_id, audio_asset_ids, motion, transition, scene_type, visual_data, hero_moment, shot_intent}) => ({ id, order, narration, caption, highlight_terms:highlight_terms || [], image_prompt, image_asset_id:image_asset_id || null, audio_asset_ids, motion:motion ?? 'auto', transition:transition ?? 'fade', scene_type:scene_type ?? 'image', visual_data:{ headline:'', body:'', value:'', source_url:'', emphasis:'', ...visual_data }, hero_moment:hero_moment ?? false, shot_intent:shot_intent ?? '' }))
  })
}

export function contentPatch(content:Content) {
  return {
    version:content.version,
    title:content.title,
    template_id:content.template_id,
    preset_id:content.preset_id ?? 'tech_brief_video_v1',
    script:cloneContent(content.script),
    scenes:content.scenes.map(scene => ({ ...cloneContent(normalizedScene(scene)), image_asset_id:scene.image_asset_id || null }))
  }
}

export function canApproveContent(content:Content):boolean {
  return Boolean(content.video_asset_id)
    && content.media_version === content.version
    && content.script.facts.length > 0
    && content.script.facts.every(fact => fact.verified && Boolean(fact.source_url.trim()))
    && contentPlatformIssues(content).length === 0
    && content.latest_quality_review?.status === 'PASS'
    && content.latest_quality_review.video_asset_id === content.video_asset_id
}

export function reorderedScenes(scenes:Scene[], from:number, to:number):Scene[] {
  const result = cloneContent(scenes)
  const [scene] = result.splice(from, 1)
  if (!scene) return result
  result.splice(to, 0, scene)
  return result.map((item, index) => ({ ...item, order:index + 1 }))
}

export async function saveBeforeAction(isDirty:boolean, save:()=>Promise<boolean>, action:()=>Promise<void>):Promise<boolean> {
  if (isDirty && !await save()) return false
  await action()
  return true
}
