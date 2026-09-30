import { expect, test } from '@playwright/test'

const baseContent = (facts:any[] = []) => ({
  id:'content-1', topic_id:'topic-1', title:'原始标题', version:4, media_version:4, status:'REVIEW_REQUIRED', template_id:null,
  script:{angle:'原角度',selection_reason:'原理由',facts,platforms:{douyin:{title:'抖音测试',description:'测试正文，AI辅助制作。',tags:[]},xiaohongshu:{title:'小红书测试',description:'测试正文，AI辅助制作。',tags:[]}}},
  scenes:[],video_asset_id:'video-1',cover_asset_id:null,subtitle_asset_id:null,latest_quality_review:{status:'PASS',video_asset_id:'video-1',report:{issues:[],warnings:[],recommended_action:'present_for_review'}},created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z'
})

async function installContentApi(page:any, initial:any, calls:string[], automatic=false) {
  let content=initial
  await page.route('**/api/v1/**', async (route:any) => {
    const request=route.request()
    const pathname=new URL(request.url()).pathname
    const method=request.method()
    if (pathname.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (pathname.endsWith('/settings')) return route.fulfill({json:{publishing:{auto_publish_after_approval:automatic},video:{clip_duration:6,clip_resolution:'768P'}}})
    if (pathname.endsWith('/templates')) return route.fulfill({json:{items:[],total:0}})
    if (pathname.endsWith('/contents') && method==='GET') return route.fulfill({json:{items:[content],total:1}})
    if (pathname.endsWith('/contents/content-1') && method==='GET') return route.fulfill({json:content})
    if (pathname.endsWith('/assets') && method==='GET') return route.fulfill({json:{items:[],total:0}})
    if (pathname.endsWith('/contents/content-1') && method==='PATCH') {
      const body=request.postDataJSON()
      calls.push(`patch:${body.title}`)
      content={...content,...body,version:content.version + 1,media_version:content.version + 1,updated_at:'2026-01-01T00:01:00Z'}
      return route.fulfill({json:content})
    }
    if (pathname.endsWith('/contents/content-1/brief/select')) {
      const body=request.postDataJSON()
      calls.push(`brief:${body.version}`)
      content={...content,version:content.version + 1,brief:{...content.brief,selected_concept_id:body.concept_id,selected_at:'2026-01-01T00:02:00Z'},updated_at:'2026-01-01T00:02:00Z'}
      return route.fulfill({json:content})
    }
    if (pathname.endsWith('/contents/content-1/generate')) {
      calls.push(`generate:${request.postDataJSON().stage}`)
      return route.fulfill({status:202,json:{job_id:'job-1',status:'QUEUED'}})
    }
    if (pathname.endsWith('/contents/content-1/approve')) {
      calls.push(`approve:${request.postDataJSON().version}`)
      content={...content,status:'APPROVED'}
      return route.fulfill({json:content})
    }
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
}

test('saves dirty content before generation so the server never receives stale copy', async ({ page }) => {
  const calls:string[]=[]
  await installContentApi(page,baseContent(),calls)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.locator('.title-input input').fill('已保存的新标题')
  await page.getByRole('button',{name:'生成'}).click()
  await page.getByRole('menuitem',{name:'仅脚本'}).click()
  await expect.poll(() => calls).toEqual(['patch:已保存的新标题','generate:script'])
  await expect(page.getByText('未保存')).toHaveCount(0)
})

test('submits full generation with the configured personal voice and corner avatar', async ({ page }) => {
  const calls:string[]=[]
  await installContentApi(page,baseContent(),calls)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.getByRole('button',{name:'生成',exact:true}).click()
  const submitted=page.waitForRequest(request=>request.url().endsWith('/contents/content-1/generate'))
  await page.getByRole('menuitem',{name:'使用本人声音和右下角数字人生成全部'}).click()
  expect((await submitted).postDataJSON()).toEqual({stage:'local_avatar_all',avatar_layout:'corner'})
  await expect.poll(()=>calls).toEqual(['generate:local_avatar_all'])
})

test('full-screen avatar generation requires paid-call confirmation and freezes video options', async ({ page }) => {
  const calls:string[]=[]
  const initial:any=baseContent()
  initial.scenes=[{id:'scene-1',order:0,narration:'Codex CLI 更新。',caption:'全屏数字人',image_prompt:'Presenter explains the update',image_asset_id:null,audio_asset_ids:[],scene_type:'image'}]
  await installContentApi(page,initial,calls)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.getByRole('button',{name:'生成',exact:true}).click()
  await page.getByRole('menuitem',{name:'使用本人声音和全屏数字人生成全部'}).click()
  const dialog=page.getByRole('dialog',{name:'使用全屏数字人生成全部'})
  await expect(dialog).toContainText('MiniMax H3')
  await expect(dialog).toContainText('预计调用 1 次')
  await expect(dialog).toContainText('¥0.54')
  await dialog.getByRole('button',{name:'取消',exact:true}).click()
  expect(calls).toEqual([])
  await page.getByRole('button',{name:'生成',exact:true}).click()
  await page.getByRole('menuitem',{name:'使用本人声音和全屏数字人生成全部'}).click()
  const submitted=page.waitForRequest(request=>request.url().endsWith('/contents/content-1/generate'))
  await page.getByRole('dialog',{name:'使用全屏数字人生成全部'}).getByRole('button',{name:'确认生成'}).click()
  expect((await submitted).postDataJSON()).toEqual({stage:'local_avatar_all',clip_duration:6,clip_resolution:'768P',avatar_layout:'fullscreen'})
  await expect.poll(()=>calls).toEqual(['generate:local_avatar_all'])
})

test('saves dirty content before selecting a Brief concept at the current version', async ({ page }) => {
  const calls:string[]=[]
  const initial:any=baseContent()
  initial.brief={version:'1.0',concepts:[{id:'c1',title:'方案一',hook:'开场钩子',narrative_structure:'comparison',visual_approach:'数据卡片',core_message:'核心信息',why_this_works:'明确对比',estimated_calls:{text:1,image:2,speech:2},estimated_cost_usd:null}],selected_concept_id:null,selected_at:null}
  await installContentApi(page,initial,calls)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.locator('.title-input input').fill('选择前保存的新标题')
  await page.getByRole('button',{name:'选择此方案'}).click()
  await expect.poll(()=>calls).toEqual(['patch:选择前保存的新标题','brief:5'])
})

test('requires a sourced verified fact and saves it before approval', async ({ page }) => {
  const calls:string[]=[]
  await installContentApi(page,baseContent(),calls)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.getByRole('tab',{name:'预览与审核'}).click()
  await expect(page.getByRole('button',{name:'通过审核'})).toBeDisabled()
  await page.getByRole('tab',{name:'脚本与事实'}).click()
  await page.getByRole('button',{name:'添加事实'}).click()
  await page.locator('.facts input').nth(0).fill('真实事实')
  await page.locator('.facts input').nth(1).fill('https://example.com/evidence')
  await page.locator('.facts .el-checkbox').click()
  await page.getByRole('tab',{name:'预览与审核'}).click()
  await expect(page.getByRole('button',{name:'通过审核'})).toBeEnabled()
  await page.getByRole('button',{name:'通过审核'}).click()
  const dialog=page.getByRole('dialog',{name:'确认通过'})
  await dialog.getByPlaceholder('审核说明').fill('已核验来源')
  const complianceBoxes=dialog.locator('.el-checkbox')
  for (let index=0; index<await complianceBoxes.count(); index++) await complianceBoxes.nth(index).click()
  await dialog.getByRole('button',{name:'已核对，通过'}).click()
  await expect.poll(() => calls).toEqual(['patch:原始标题','approve:5'])
})

test('persists keyword-only edits before rendering', async ({ page }) => {
  const calls:string[]=[]
  const initial:any=baseContent()
  initial.scenes=[{id:'scene-1',order:0,narration:'模型保留自然语速。',caption:'自然语速',image_prompt:'测试画面',image_asset_id:null,audio_asset_ids:[]}]
  await installContentApi(page,initial,calls)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.getByRole('tab',{name:'镜头与素材'}).click()
  const field=page.locator('.el-form-item').filter({has:page.getByText('黄色关键词',{exact:true})})
  await field.getByRole('textbox').fill('自然语速')
  await field.getByRole('textbox').press('Enter')
  await expect(page.getByText('未保存',{exact:true})).toBeVisible()
  const patch=page.waitForRequest(request=>request.method()==='PATCH'&&request.url().endsWith('/contents/content-1'))
  await page.getByRole('button',{name:'生成',exact:true}).click()
  await page.getByRole('menuitem',{name:'仅渲染视频'}).click()
  expect((await patch).postDataJSON().scenes[0].highlight_terms).toEqual(['自然语速'])
  await expect.poll(()=>calls).toEqual(['patch:原始标题','generate:render'])
})

test('saves dirty content before redrawing the selected scene image once', async ({ page }) => {
  const calls:string[]=[]
  const initial:any=baseContent()
  initial.scenes=[
    {id:'scene-a',order:1,narration:'第一段真实旁白。',caption:'第一镜头',image_prompt:'第一画面',image_asset_id:'image-a',audio_asset_ids:['audio-a']},
    {id:'scene-b',order:2,narration:'第二段真实旁白。',caption:'第二镜头',image_prompt:'第二画面',image_asset_id:'image-b',audio_asset_ids:['audio-b']}
  ]
  await installContentApi(page,initial,calls)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.locator('.title-input input').fill('已更新的图片叙事标题')
  await page.getByRole('tab',{name:'镜头与素材'}).click()
  await page.getByRole('button',{name:'重绘本镜头配图'}).click()
  const dialog=page.getByRole('dialog',{name:'重绘本镜头配图'})
  await expect(dialog.getByText('镜头 1（第一镜头）',{exact:false})).toBeVisible()
  await expect(page.getByRole('button',{name:'生成',exact:true})).toBeDisabled()
  const patch=page.waitForRequest(request=>request.method()==='PATCH'&&request.url().endsWith('/contents/content-1'))
  const generateRequest=page.waitForRequest(request=>request.method()==='POST'&&request.url().endsWith('/contents/content-1/generate'))
  await dialog.getByRole('button',{name:'开始重绘'}).click()
  expect((await patch).postDataJSON().title).toBe('已更新的图片叙事标题')
  expect((await generateRequest).postDataJSON()).toEqual({stage:'images',scene_id:'scene-a'})
  await expect.poll(()=>calls).toEqual(['patch:已更新的图片叙事标题','generate:images'])
})

test('saves motion once and blocks overlapping animation composition confirmation', async ({ page }) => {
  const calls:string[]=[]
  const initial:any=baseContent()
  initial.scenes=[{id:'scene-1',order:0,narration:'已有配音。',caption:'已有画面',image_prompt:'测试画面',image_asset_id:'image-1',audio_asset_ids:['audio-1']}]
  await installContentApi(page,initial,calls)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.getByRole('tab',{name:'镜头与素材'}).click()
  await expect(page.getByText('未保存',{exact:true})).toHaveCount(0)
  const controls=page.locator('.motion-controls')
  await expect(controls.locator('.el-select__selected-item').nth(1)).toHaveText('自动')
  await expect(controls.locator('.el-select__selected-item').nth(3)).toHaveText('淡入淡出')
  await controls.locator('.el-select').nth(0).click()
  await page.getByRole('option',{name:'缓慢推进'}).click()
  await controls.locator('.el-select').nth(1).click()
  await page.getByRole('option',{name:'直接切换'}).click()
  await expect(page.getByText('未保存',{exact:true})).toBeVisible()
  const generateButton=page.getByRole('button',{name:'生成',exact:true})
  await generateButton.click()
  await page.getByRole('menuitem',{name:'AI 编排动画并合成'}).click()
  const dialog=page.getByRole('dialog',{name:'AI 编排动画并合成'})
  await expect(dialog.getByText('不会生成图片或声音')).toBeVisible()
  await expect(generateButton).toBeDisabled()
  await expect(page.getByRole('dialog',{name:'AI 编排动画并合成'})).toHaveCount(1)
  await dialog.getByRole('button',{name:'取消'}).click()
  await expect(generateButton).toBeEnabled()
  await generateButton.click()
  await page.getByRole('menuitem',{name:'AI 编排动画并合成'}).click()
  const confirmedDialog=page.getByRole('dialog',{name:'AI 编排动画并合成'})
  const patch=page.waitForRequest(request=>request.method()==='PATCH'&&request.url().endsWith('/contents/content-1'))
  await confirmedDialog.getByRole('button',{name:'开始编排'}).click()
  expect((await patch).postDataJSON().scenes[0]).toMatchObject({ motion:'push_in', transition:'cut' })
  await expect.poll(()=>calls).toEqual(['patch:原始标题','generate:motion'])
})

test('discloses automatic posting before approving and binds the viewed mode', async ({ page }) => {
  const calls:string[]=[]
  await installContentApi(page,baseContent([{claim:'测试事实',source_url:'https://example.com/evidence',verified:true,notes:''}]),calls,true)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.getByRole('tab',{name:'预览与审核'}).click()
  await page.getByRole('button',{name:'通过审核'}).click()
  const dialog=page.getByRole('dialog',{name:'确认通过'})
  await expect(dialog.getByText('结构化合规清单')).toBeVisible()
  await expect(dialog.getByText('通过后将自动上传并发布到已绑定账号。',{exact:false})).toBeVisible()
  const complianceBoxes=dialog.locator('.el-checkbox')
  for (let index=0; index<await complianceBoxes.count(); index++) await complianceBoxes.nth(index).click()
  const request=page.waitForRequest(value=>value.url().endsWith('/contents/content-1/approve'))
  await dialog.getByRole('button',{name:'已核对，通过',exact:true}).click()
  const payload=(await request).postDataJSON()
  expect(payload.auto_publish_expected).toBe(true)
  expect(payload.compliance_confirmed).toBe(true)
  expect(Object.values(payload.compliance_checks)).toEqual([true,true,true,true,true,true,true,true])
})

test('keeps the newest list query result when an earlier search resolves late', async ({ page }) => {
  await page.route('**/api/v1/**', async (route:any) => {
    const request=route.request()
    const url=new URL(request.url())
    const pathname=url.pathname
    if (pathname.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (pathname.endsWith('/templates')) return route.fulfill({json:{items:[],total:0}})
    if (pathname.endsWith('/presets')) return route.fulfill({json:{items:[],total:0}})
    if (pathname.endsWith('/contents') && request.method()==='GET') {
      if (url.searchParams.get('q')==='慢查询') {
        await new Promise(resolve=>setTimeout(resolve,1200))
        return route.fulfill({json:{items:[{...baseContent(),id:'slow-1',title:'慢查询结果'}],total:1}})
      }
      return route.fulfill({json:{items:[{...baseContent(),id:'fast-1',title:'快速结果'}],total:1}})
    }
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
  await page.goto('/contents')
  const search=page.getByPlaceholder('搜索内容')
  await search.fill('慢查询')
  await search.blur()
  await search.fill('快速')
  await search.blur()
  await expect(page.locator('.content-item b')).toHaveText('快速结果',{timeout:4000})
  await page.waitForTimeout(1400)
  await expect(page.locator('.content-item b')).toHaveText('快速结果')
  await expect(page.getByText('慢查询结果')).toHaveCount(0)
})

test('AI video generation requires cost confirmation and saves edited prompt first', async ({ page }) => {
  const calls:string[]=[]
  const initial:any=baseContent()
  initial.scenes=[{id:'scene-1',order:0,narration:'机器人移动箱子。',caption:'演示画面',image_prompt:'Robot moves a box',image_asset_id:null,audio_asset_ids:[],scene_type:'image'}]
  await installContentApi(page,initial,calls)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.getByRole('tab',{name:'镜头与素材'}).click()
  await page.getByLabel('画面提示词').fill('Robot moves a blue box slowly')
  await page.getByRole('button',{name:'生成本镜头 AI 视频'}).click()
  const dialog=page.getByRole('dialog',{name:'生成 AI 视频片段'})
  await expect(dialog).toContainText('6 秒 768P')
  await expect(dialog).toContainText('¥0.54')
  expect(calls).toEqual([])
  await dialog.getByRole('button',{name:'取消',exact:true}).click()
  expect(calls).toEqual([])
  await page.getByRole('button',{name:'生成本镜头 AI 视频'}).click()
  const submitted = page.waitForRequest(request=>request.url().endsWith('/contents/content-1/generate'))
  await page.getByRole('dialog',{name:'生成 AI 视频片段'}).getByRole('button',{name:'确认生成'}).click()
  expect((await submitted).postDataJSON()).toEqual({stage:'video',scene_id:'scene-1',clip_duration:6,clip_resolution:'768P'})
  await expect.poll(()=>calls).toEqual(['patch:原始标题','generate:video'])
})

test('AI video configuration failure is visible and never submits a paid task', async ({ page }) => {
  const calls:string[]=[]
  const initial:any=baseContent()
  initial.scenes=[{id:'scene-1',order:0,narration:'技术演示。',caption:'演示',image_prompt:'A robot',image_asset_id:null,audio_asset_ids:[],scene_type:'image'}]
  await installContentApi(page,initial,calls)
  await page.goto('/contents')
  await page.locator('.content-item').click()
  await page.getByRole('tab',{name:'镜头与素材'}).click()
  await page.route('**/api/v1/settings',route=>route.fulfill({status:503,json:{code:'UNAVAILABLE',message:'视频配置暂不可用'}}))
  await page.getByRole('button',{name:'生成本镜头 AI 视频'}).click()
  await expect(page.getByText('视频配置暂不可用',{exact:true})).toBeVisible()
  expect(calls).toEqual([])
  await expect(page.getByRole('button',{name:'生成本镜头 AI 视频'})).toBeEnabled()
})
