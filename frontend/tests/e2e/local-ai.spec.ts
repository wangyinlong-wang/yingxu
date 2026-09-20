import { expect, test } from '@playwright/test'

async function installLocalAPI(page:any, installed=true, chatterboxInstalled=installed) {
  let settings:any={models:{text:{enabled:false},image:{enabled:false},speech:{enabled:false}},video:{width:1080,height:1920,fps:30,target_seconds:60},limits:{daily_calls:40,daily_videos:1},publishing:{auto_publish_after_approval:false},timezone:'Asia/Shanghai',local_ai:{voice_enabled:false,avatar_enabled:false,reference_audio_asset_id:'reference-1',reference_text:'',portrait_asset_id:'portrait-1',voice_model:'qwen3',emotion_strength:0.65}}
  const calls:any[]=[]
  const assets=[{id:'reference-1',kind:'voice_reference',filename:'本人录音.wav',size:1000,url:'/api/v1/assets/reference-1/file'}, {id:'portrait-1',kind:'portrait',filename:'本人照片.png',size:1000,url:'/api/v1/assets/portrait-1/file'}, {id:'voice-1',kind:'audio',filename:'local-voice.wav',size:1000,url:'/api/v1/assets/voice-1/file'}]
  await page.route('**/api/v1/**',async(route:any)=>{
    const request=route.request(),path=new URL(request.url()).pathname
    if(path.endsWith('/health'))return route.fulfill({json:{status:'ok'}})
    if(path.endsWith('/local-ai/status'))return route.fulfill({json:{tts:{installed,reason:installed?undefined:'manifest_missing'},chatterbox:{installed:chatterboxInstalled,model:'mlx-community/chatterbox-multilingual-v3',engine:'mlx',reason:chatterboxInstalled?undefined:'manifest_missing'},avatar:{installed},transcribe:{installed},busy:false,revision:'fixture'}})
    if(path.endsWith('/settings')){
      if(request.method()==='PATCH'){settings={...settings,...request.postDataJSON()};calls.push({type:'save',local_ai:settings.local_ai})}
      return route.fulfill({json:settings})
    }
    if(path.endsWith('/assets'))return route.fulfill({json:{items:assets,total:assets.length}})
    if(path.endsWith('/templates')||path.endsWith('/schedules'))return route.fulfill({json:{items:[],total:0}})
    if(path.endsWith('/diagnostics'))return route.fulfill({json:{warnings:[]}})
    if(path.endsWith('/local-ai/transcribe')){calls.push({type:'transcribe',...request.postDataJSON()});return route.fulfill({status:202,json:{job_id:'transcribe-1'}})}
    if(path.endsWith('/local-ai/preview')){const body=request.postDataJSON();calls.push({type:'preview',...body});return route.fulfill({status:202,json:{job_id:body.kind==='voice'?'voice-job':'avatar-job'}})}
    if(path.endsWith('/jobs/transcribe-1'))return route.fulfill({json:{status:'SUCCEEDED',progress:100,result:{transcript:'这是我的参考录音。',source_asset_id:'reference-1',local:true}}})
    if(path.endsWith('/jobs/voice-job'))return route.fulfill({json:{status:'SUCCEEDED',progress:100,result:{asset_id:'voice-1',kind:'audio',local:true,elapsed_seconds:2.1}}})
    if(path.endsWith('/jobs/avatar-job'))return route.fulfill({json:{status:'FAILED',error_message:'测试夹具：未生成真实数字人'}})
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'fixture route'}})
  })
  return calls
}

test('missing installation disables inference while reference uploads remain available',async({page})=>{
  const calls=await installLocalAPI(page,false)
  await page.goto('/setup?tab=local-ai')
  await expect(page.getByText('等待安装验证',{exact:true})).toHaveCount(4)
  await expect(page.locator('button').filter({hasText:'上传本人音频'})).toBeEnabled()
  await expect(page.getByRole('button',{name:'生成本人声音试听'})).toBeDisabled()
  await expect(page.getByRole('button',{name:'生成照片数字人预览'})).toBeDisabled()
  expect(calls).toEqual([])
})

test('transcribes locally, saves reviewed text, and reuses the generated voice for avatar preview',async({page})=>{
  const calls=await installLocalAPI(page)
  await page.goto('/setup?tab=local-ai')
  await page.getByRole('button',{name:'自动识别录音文字'}).click()
  await expect(page.getByRole('textbox',{name:'录音对应文字'})).toHaveValue('这是我的参考录音。')
  await page.getByRole('textbox',{name:'录音对应文字'}).fill('这是核对后的参考录音。')
  await page.getByRole('button',{name:'生成本人声音试听'}).click()
  await expect(page.getByText('本地声音试听',{exact:true})).toBeVisible()
  await page.getByRole('button',{name:'生成照片数字人预览'}).click()
  await expect.poll(()=>calls.filter(call=>call.type==='preview')).toHaveLength(2)
  expect(calls.filter(call=>call.type==='preview')[1].audio_asset_id).toBe('voice-1')
  const saved=calls.filter(call=>call.type==='save')
  expect(saved[1].local_ai.reference_text).toBe('这是核对后的参考录音。')
  await expect(page.getByText('测试夹具：未生成真实数字人',{exact:true})).toBeVisible()
})

test('uses a Chatterbox preview override without changing the future-video model',async({page})=>{
  const calls=await installLocalAPI(page,true,true)
  await page.goto('/setup?tab=local-ai')
  await expect(page.getByRole('button',{name:'生成本人声音试听'})).toBeDisabled()
  await expect(page.locator('.future-voice-switch .el-switch__input')).toBeDisabled()
  await page.locator('.preview-model .el-select').click()
  await page.locator('.el-select-dropdown:visible').last().getByRole('option',{name:'Chatterbox（仅需参考录音）'}).click()
  await expect(page.locator('.preview-emotion')).toBeVisible()
  await expect(page.getByRole('button',{name:'生成本人声音试听'})).toBeEnabled()
  await page.locator('.preview-model .el-select').click()
  await page.locator('.el-select-dropdown:visible').last().getByRole('option',{name:'Qwen3（需录音对应文字）'}).click()
  await expect(page.getByRole('button',{name:'生成本人声音试听'})).toBeDisabled()
  await page.locator('.preview-model .el-select').click()
  await page.locator('.el-select-dropdown:visible').last().getByRole('option',{name:'Chatterbox（仅需参考录音）'}).click()
  await page.getByRole('button',{name:'生成本人声音试听'}).click()
  await expect.poll(()=>calls.filter(call=>call.type==='preview')).toHaveLength(1)
  expect(calls.find(call=>call.type==='preview')).toMatchObject({kind:'voice',voice_model:'chatterbox',emotion_strength:0.65})
  expect(calls.find(call=>call.type==='save').local_ai.voice_model).toBe('qwen3')
})
