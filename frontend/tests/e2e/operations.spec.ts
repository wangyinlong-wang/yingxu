import { expect, test } from '@playwright/test'

test('shows model capabilities, template versions, and rolls back a rejected schedule switch', async ({ page }) => {
  let patchAttempts=0
  await page.route('**/api/v1/**', async route => {
    const request=route.request()
    const path=new URL(request.url()).pathname
    if (path.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (path.endsWith('/templates/template-1/versions')) return route.fulfill({json:{items:[{version:2,name:'快报',style:'brief',updated_at:'2026-01-01T00:00:00Z'}],total:1}})
    if (path.endsWith('/templates')) return route.fulfill({json:{items:[{id:'template-1',name:'快报',description:'说明',prompt:'提示',style:'brief',version:2,updated_at:'2026-01-01T00:00:00Z'}],total:1}})
    if (path.endsWith('/settings')) return route.fulfill({json:{models:{text:{base_url:'',model:'',timeout_seconds:90,voice:'alloy',size:'1024x1536',json_mode:false,enabled:false,key_configured:false},image:{base_url:'',model:'',timeout_seconds:180,voice:'alloy',size:'1024x1536',json_mode:false,enabled:false,key_configured:false},speech:{base_url:'',model:'',timeout_seconds:180,voice:'alloy',size:'1024x1536',json_mode:false,enabled:false,key_configured:false}},video:{width:1080,height:1920,fps:30,target_seconds:60,font_path:'',bgm_asset_id:null,logo_asset_id:null},limits:{daily_calls:40,daily_videos:1},timezone:'Asia/Shanghai'}})
    if (path.endsWith('/diagnostics')) return route.fulfill({json:{python:'3.11',ffmpeg:{available:true},adb:{available:false},storage:{free_bytes:1},workers:{},warnings:[]}})
    if (path.endsWith('/assets')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/schedules') && request.method()==='GET') return route.fulfill({json:{items:[{id:'schedule-1',name:'早间任务',kind:'pipeline',time:'09:00',timezone:'Asia/Shanghai',enabled:false,daily_limit:1,next_run_at:null}],total:1}})
    if (path.endsWith('/schedules/schedule-1') && request.method()==='PATCH') { patchAttempts++; return route.fulfill({status:422,json:{code:'MODEL_NOT_CONFIGURED',message:'模型未配置',request_id:'test'}}) }
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
  await page.goto('/setup')
  await page.getByRole('tab',{name:'模型与视频'}).click()
  await expect(page.getByText('音色',{exact:true})).toBeVisible()
  await expect(page.getByText('接入协议')).toHaveCount(3)
  await expect(page.getByText('图像尺寸')).toBeVisible()
  await expect(page.getByText('结构化 JSON 输出')).toBeVisible()
  await page.getByRole('tab',{name:'内容模板'}).click()
  await page.getByRole('button',{name:'版本'}).click()
  await expect(page.getByRole('dialog',{name:'模板版本记录'}).getByText('快报')).toBeVisible()
  await page.getByRole('dialog',{name:'模板版本记录'}).getByRole('button',{name:'Close this dialog'}).click()
  await page.getByRole('tab',{name:'定时计划'}).click()
  const toggle=page.getByRole('switch')
  await page.locator('.el-switch').click()
  await expect.poll(() => patchAttempts).toBe(1)
  await expect(toggle).toHaveAttribute('aria-checked','false')
})

test('confirms an actual VERIFYING publication with URL and optional work id, and exposes both device checks', async ({ page }) => {
  const confirmation:any[]=[]
  const detections:string[]=[]
  await page.route('**/api/v1/**', async route => {
    const request=route.request()
    const path=new URL(request.url()).pathname
    if (path.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (path.endsWith('/accounts')) return route.fulfill({json:{items:[{id:'account-1',platform:'douyin',display_name:'主账号',account_identifier:'user-1',device_serial:'device-1',created_at:'2026-01-01T00:00:00Z'}],total:1}})
    if (path.endsWith('/devices')) return route.fulfill({json:{items:[{serial:'device-1',state:'device',model:'Test Phone'}],total:1}})
    if (path.endsWith('/contents')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/settings')) return route.fulfill({json:{publishing:{auto_publish_after_approval:true}}})
    if (path.endsWith('/publications') && request.method()==='GET') return route.fulfill({json:{items:[{id:'pub-wait',content_id:'c1',content_version:1,content_title:'等待处理',platform:'douyin',account_id:'account-1',status:'WAITING_USER',job_id:'job-1',work_url:null,work_id:null,evidence:{current_step:'已打开编辑器'},created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z'},{id:'pub-verify',content_id:'c2',content_version:1,content_title:'已实际发布',platform:'xiaohongshu',account_id:'account-1',status:'VERIFYING',job_id:'job-2',work_url:null,work_id:null,evidence:{reason:'等待回传',error_code:'SHORT_LINK'},created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z'}],total:2}})
    if (path.endsWith('/devices/device-1/inspect')) return route.fulfill({json:{serial:'device-1',platform:request.postDataJSON().platform,app_version:'1.2.3',account_identifier:'user-1',ready:true,warnings:[]}})
    if (path.endsWith('/publications/pub-verify/confirm')) { confirmation.push(request.postDataJSON()); return route.fulfill({json:{}}) }
    if (path.endsWith('/publications/pub-wait/detect')) { detections.push(path); return route.fulfill({json:{}}) }
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
  await page.goto('/publications')
  await expect(page.getByRole('button',{name:'继续准备'})).toHaveCount(1)
  await page.getByRole('button',{name:'检查抖音'}).click()
  await page.getByRole('button',{name:'检查小红书'}).click()
  await expect(page.getByText('版本 1.2.3 · 账号 user-1')).toHaveCount(2)
  await expect(page.getByText('审核后自动发布已开启', {exact:false})).toBeVisible()
  await page.locator('tr').filter({hasText:'等待处理'}).getByRole('button',{name:'从手机识别'}).click()
  await page.getByRole('dialog',{name:'从手机识别已发作品'}).getByRole('button',{name:'开始识别'}).click()
  await expect.poll(() => detections).toEqual(['/api/v1/publications/pub-wait/detect'])
  await page.locator('tr').filter({hasText:'已实际发布'}).getByRole('button',{name:'确认已发布'}).click()
  const dialog=page.getByRole('dialog',{name:'确认手机端已发布'})
  await dialog.getByPlaceholder('https://…').fill('https://short.example/a')
  await dialog.getByRole('textbox').nth(1).fill('note-123')
  await dialog.locator('.el-checkbox').click()
  await dialog.getByRole('button',{name:'记录确认'}).click()
  await expect.poll(() => confirmation).toEqual([{work_url:'https://short.example/a',work_id:'note-123',confirmed:true}])
})
