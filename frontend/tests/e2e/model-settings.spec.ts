import { expect, test, type Page } from '@playwright/test'

const PROFILE_DEFAULTS = { protocol:'openai', base_url:'', model:'', timeout_seconds:180, voice:'alloy', size:'1024x1536', json_mode:false, enabled:false, watermark:false, instruction:'', language_type:'Chinese', max_completion_tokens:null, enable_thinking:null }
const credential = (over:any={}) => ({ key_configured:false, key_source:'none', secure_store_available:true, ...over })

function fullSettings(): any {
  const model = (over:any={}) => ({ ...PROFILE_DEFAULTS, source:'third_party', ...credential({ key_base_url:'' }), local:{ ...PROFILE_DEFAULTS, ...credential({ key_base_url:'' }) }, ...over })
  return {
    models: { text:model(), image:model(), speech:model(), video:model({protocol:'minimax_h3'}) },
    video: { renderer:'ffmpeg', clip_duration:6, clip_resolution:'768P', width:1080, height:1920, fps:30, target_seconds:60, timing_mode:'natural', font_path:'', bgm_asset_id:null, logo_asset_id:null },
    limits: { daily_calls:40, daily_videos:1 },
    publishing: { auto_publish_after_approval:false },
    timezone: 'Asia/Shanghai',
    local_ai: { voice_enabled:false, avatar_enabled:false, reference_audio_asset_id:null, reference_text:'', portrait_asset_id:null, voice_model:'qwen3', emotion_strength:0.65 },
  }
}

function legacySettings(): any {
  const model = { protocol:'openai', base_url:'', model:'', timeout_seconds:180, voice:'alloy', size:'1024x1536', json_mode:false, enabled:false, key_configured:false }
  return { ...fullSettings(), models:{ text:{...model}, image:{...model}, speech:{...model} } }
}

async function installSettingsAPI(page: Page, options: { legacy?: boolean; legacyVoice?: boolean; legacyPatch?: boolean; keyError?: any; keyGate?: Promise<void> } = {}) {
  let settings = options.legacy ? legacySettings() : fullSettings()
  if (options.legacyVoice)
    settings.local_ai = { ...settings.local_ai, voice_enabled: true }
  const keyCalls: any[] = []
  const patchCalls: any[] = []
  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/health')) return route.fulfill({ json:{ status:'ok', version:'test' } })
    if (path.includes('/settings/model-keys/')) {
      const body = request.postDataJSON()
      keyCalls.push(body)
      if (options.keyGate) await options.keyGate
      if (options.keyError) return route.fulfill({ status:409, json:{ ...options.keyError, request_id:'test' } })
      const configured = body.action === 'set'
      return route.fulfill({ json:{ key_configured: configured, key_source: configured ? 'keychain' : 'none', secure_store_available:true } })
    }
    if (path.endsWith('/settings')) {
      if (request.method() === 'PATCH') {
        const body = request.postDataJSON()
        patchCalls.push(body)
        for (const kind of ['text','image','speech','video'])
          if (body.models?.[kind])
            settings.models[kind] = { ...settings.models[kind], ...body.models[kind], local:{ ...(settings.models[kind].local || {}), ...(body.models[kind].local || {}) } }
        for (const key of ['video','limits','publishing','local_ai','timezone'])
          if (body[key] !== undefined) settings[key] = body[key]
        if (options.legacyPatch) {
          // Simulate a pre-source backend echo: no source or local profile in the response.
          const stripped = JSON.parse(JSON.stringify(settings))
          for (const kind of ['text','image','speech']) {
            delete stripped.models[kind].source
            delete stripped.models[kind].local
          }
          return route.fulfill({ json: stripped })
        }
      }
      return route.fulfill({ json:settings })
    }
    if (path.endsWith('/templates')) return route.fulfill({ json:{ items:[], total:0 } })
    if (path.endsWith('/diagnostics')) return route.fulfill({ json:{ python:'3.11', ffmpeg:{available:true}, adb:{available:false}, storage:{free_bytes:1}, workers:{}, warnings:[] } })
    if (path.endsWith('/assets')) return route.fulfill({ json:{ items:[{ id:'ref-1', kind:'voice_reference', filename:'ref.wav', size:1, url:'/api/v1/assets/ref-1/file', created_at:'2026-01-01T00:00:00Z' }], total:1 } })
    if (path.endsWith('/schedules')) return route.fulfill({ json:{ items:[], total:0 } })
    if (path.endsWith('/local-ai/status')) return route.fulfill({ json:{ tts:{installed:true,model:'fixture',engine:'mlx'}, chatterbox:{installed:true,model:'fixture',engine:'mlx'}, avatar:{installed:true}, transcribe:{installed:true}, busy:false, revision:'fixture' } })
    return route.fulfill({ status:404, json:{ code:'NOT_FOUND', message:'unhandled mock', request_id:'test' } })
  })
  return { keyCalls, patchCalls, getSettings: () => settings }
}

const card = (page: Page, name: string) => page.locator('.el-card').filter({ hasText:name })

async function selectSource(page: Page, cardName: string, option: string) {
  await card(page, cardName).locator('.el-select').first().click()
  await page.locator('.el-select-dropdown:visible').last().getByRole('option', { name:option }).click()
}

test('saves a key through the dedicated endpoint and settings stay secret-free', async ({ page }) => {
  const { keyCalls, patchCalls } = await installSettingsAPI(page)
  await page.goto('/setup?tab=settings')
  const text = card(page, '文本模型')
  await expect(text.locator('input[type="password"]')).toBeVisible()
  await text.getByLabel('Base URL').fill('https://api.example.test/v1')
  await text.getByLabel('模型名称').fill('fixture-model')
  await text.locator('input[type="password"]').fill('sk-fixture-123')
  await text.getByRole('button', { name:'保存密钥', exact:true }).click()
  await expect.poll(() => keyCalls).toEqual([{ source:'third_party', base_url:'https://api.example.test/v1', action:'set', api_key:'sk-fixture-123' }])
  await expect(text.locator('input[type="password"]')).toHaveValue('')
  await expect(text.getByText('钥匙串已保存密钥')).toBeVisible()
  await page.getByRole('button', { name:'保存设置' }).click()
  await expect.poll(() => patchCalls.length).toBe(1)
  const payload = JSON.stringify(patchCalls[0])
  expect(payload).not.toContain('sk-fixture-123')
  expect(payload).not.toContain('api_key')
  expect(patchCalls[0].models.text.key_configured).toBeUndefined()
  expect(patchCalls[0].models.text.key_source).toBeUndefined()
  expect(patchCalls[0].models.text.local.key_source).toBeUndefined()
  expect(patchCalls[0].models.text.source).toBe('third_party')
  const browserStorage = await page.evaluate(() => JSON.stringify([localStorage, sessionStorage]))
  expect(browserStorage).not.toContain('sk-fixture-123')
})

test('key save failure keeps the typed secret and reports the sanitized error', async ({ page }) => {
  const { keyCalls } = await installSettingsAPI(page, { keyError:{ code:'MODEL_KEY_STORE_LOCKED', message:'macOS 钥匙串当前已锁定或需要交互授权，无法访问模型密钥；请解锁后重试' } })
  await page.goto('/setup?tab=settings')
  const text = card(page, '文本模型')
  await text.getByLabel('Base URL').fill('https://api.example.test/v1')
  await text.locator('input[type="password"]').fill('sk-fixture-123')
  await text.getByRole('button', { name:'保存密钥', exact:true }).click()
  await expect.poll(() => keyCalls.length).toBe(1)
  await expect(page.locator('.el-message').filter({ hasText:'钥匙串当前已锁定' })).toBeVisible()
  await expect(text.locator('input[type="password"]')).toHaveValue('sk-fixture-123')
  await expect(text.getByText('钥匙串已保存密钥')).toHaveCount(0)
})

test('a delayed key response cannot mark an edited Base URL as configured', async ({ page }) => {
  let releaseKey!: () => void
  const keyGate = new Promise<void>(resolve => { releaseKey = resolve })
  const { keyCalls } = await installSettingsAPI(page, { keyGate })
  await page.goto('/setup?tab=settings')
  const text = card(page, '文本模型')
  await text.getByLabel('Base URL').fill('https://a.example.test/v1')
  await text.locator('input[type="password"]').fill('sk-fixture-123')
  await text.getByRole('button', { name:'保存密钥', exact:true }).click()
  await expect.poll(() => keyCalls.length).toBe(1)
  // While the key save is in flight, an ordinary settings save is blocked and the URL changes.
  await expect(page.getByRole('button', { name:'保存设置' })).toBeDisabled()
  await text.getByLabel('Base URL').fill('https://b.example.test/v1')
  releaseKey()
  await expect(page.locator('.el-message').filter({ hasText:'密钥已保存' })).toBeVisible()
  // The returned metadata stays bound to the submitted URL: the edited URL is marked stale
  // instead of inheriting the saved-key success.
  await expect(text.locator('.key-stale')).toBeVisible()
  await expect(page.getByRole('button', { name:'保存设置' })).toBeEnabled()
})

test('a delayed key response never clears a newer draft', async ({ page }) => {
  let releaseKey!: () => void
  const keyGate = new Promise<void>(resolve => { releaseKey = resolve })
  const { keyCalls } = await installSettingsAPI(page, { keyGate })
  await page.goto('/setup?tab=settings')
  const text = card(page, '文本模型')
  await text.getByLabel('Base URL').fill('https://api.example.test/v1')
  await text.locator('input[type="password"]').fill('sk-first')
  await text.getByRole('button', { name:'保存密钥', exact:true }).click()
  await expect.poll(() => keyCalls.length).toBe(1)
  await text.locator('input[type="password"]').fill('sk-second')
  releaseKey()
  await expect(page.locator('.el-message').filter({ hasText:'密钥已保存' })).toBeVisible()
  await expect(text.locator('input[type="password"]')).toHaveValue('sk-second')
})

test('switching sources preserves both profile drafts until the explicit save', async ({ page }) => {
  const { patchCalls } = await installSettingsAPI(page)
  await page.goto('/setup?tab=settings')
  const text = card(page, '文本模型')
  await text.getByLabel('Base URL').fill('https://api.example.test/v1')
  await text.getByLabel('模型名称').fill('fixture-remote')
  await selectSource(page, '文本模型', '本地 URL 服务')
  await expect(text.getByLabel('Base URL')).toHaveValue('')
  await expect(text.getByText('仅限本机回环地址')).toBeVisible()
  await text.getByLabel('Base URL').fill('http://127.0.0.1:8000/v1')
  await text.getByLabel('模型名称').fill('fixture-local')
  await selectSource(page, '文本模型', '第三方模型')
  await expect(text.getByLabel('Base URL')).toHaveValue('https://api.example.test/v1')
  await expect(text.getByLabel('模型名称')).toHaveValue('fixture-remote')
  await selectSource(page, '文本模型', '本地 URL 服务')
  await expect(text.getByLabel('Base URL')).toHaveValue('http://127.0.0.1:8000/v1')
  await expect(text.getByLabel('模型名称')).toHaveValue('fixture-local')
  await page.getByRole('button', { name:'保存设置' }).click()
  await expect.poll(() => patchCalls.length).toBe(1)
  expect(patchCalls[0].models.text.source).toBe('local')
  expect(patchCalls[0].models.text.base_url).toBe('https://api.example.test/v1')
  expect(patchCalls[0].models.text.model).toBe('fixture-remote')
  expect(patchCalls[0].models.text.local).toMatchObject({ base_url:'http://127.0.0.1:8000/v1', model:'fixture-local', protocol:'openai' })
})

test('legacy settings responses normalize to the third-party profile', async ({ page }) => {
  const { patchCalls } = await installSettingsAPI(page, { legacy:true })
  await page.goto('/setup?tab=settings')
  const text = card(page, '文本模型')
  await expect(text.locator('.el-select__selected-item:not(.is-hidden)').first()).toHaveText('第三方模型')
  await expect(page.getByText('接入协议')).toHaveCount(3)
  await expect(page.getByLabel('Base URL')).toHaveCount(3)
  await page.getByRole('button', { name:'保存设置' }).click()
  await expect.poll(() => patchCalls.length).toBe(1)
  expect(patchCalls[0].models.speech.source).toBe('third_party')
  expect(patchCalls[0].models.speech.local).toMatchObject({ protocol:'openai', enabled:false })
})

test('a legacy native-voice response selects the builtin speech source', async ({ page }) => {
  await installSettingsAPI(page, { legacy:true, legacyVoice:true })
  await page.goto('/setup?tab=settings')
  const speech = card(page, '语音模型')
  await expect(speech.locator('.el-select__selected-item:not(.is-hidden)').first()).toHaveText('本地声音引擎')
  await expect(speech.getByText('不产生 API 调用')).toBeVisible()
})

test('a legacy PATCH response still allows switching to a local profile', async ({ page }) => {
  const { patchCalls } = await installSettingsAPI(page, { legacyPatch:true })
  await page.goto('/setup?tab=settings')
  const text = card(page, '文本模型')
  await text.getByLabel('Base URL').fill('https://api.example.test/v1')
  await page.getByRole('button', { name:'保存设置' }).click()
  await expect.poll(() => patchCalls.length).toBe(1)
  // The legacy-shaped PATCH response is normalized again, so the local profile still exists.
  await selectSource(page, '文本模型', '本地 URL 服务')
  await expect(text.getByLabel('Base URL')).toHaveValue('')
  await text.getByLabel('Base URL').fill('http://127.0.0.1:8000/v1')
  await expect(text.getByLabel('Base URL')).toHaveValue('http://127.0.0.1:8000/v1')
})

test('local profiles treat the key as optional and never send Authorization implicitly', async ({ page }) => {
  const { keyCalls } = await installSettingsAPI(page)
  await page.goto('/setup?tab=settings')
  await selectSource(page, '文本模型', '本地 URL 服务')
  const text = card(page, '文本模型')
  await expect(text.getByText('未保存密钥（本地服务可选）')).toBeVisible()
  await expect(text.getByText('未保存时请求不携带 Authorization')).toBeVisible()
  await expect(text.getByRole('button', { name:'清除已保存密钥' })).toBeDisabled()
  await text.getByLabel('Base URL').fill('http://127.0.0.1:8000/v1')
  await text.getByRole('button', { name:'保存密钥', exact:true }).click()
  await expect(page.locator('.el-message').filter({ hasText:'请先输入 API Key' })).toBeVisible()
  expect(keyCalls).toEqual([])
})

test('builtin speech syncs the voice flag both ways and links to the voice tab', async ({ page }) => {
  const { patchCalls } = await installSettingsAPI(page)
  await page.goto('/setup?tab=settings')
  await selectSource(page, '语音模型', '本地声音引擎')
  const speech = card(page, '语音模型')
  await expect(speech.getByText('不产生 API 调用')).toBeVisible()
  await page.getByRole('button', { name:'保存设置' }).click()
  await expect.poll(() => patchCalls.length).toBe(1)
  expect(patchCalls[0].models.speech.source).toBe('builtin')
  expect(patchCalls[0].local_ai.voice_enabled).toBe(true)
  await speech.getByRole('button', { name:'打开「本人声音与数字人」' }).click()
  await expect(page.getByRole('tab', { name:'本人声音与数字人' })).toHaveAttribute('aria-selected', 'true')
  // The native voice toggle on that tab drives the same speech source in the parent settings.
  // Selecting a reference clears its transcript, so pick the asset before filling the text.
  await page.locator('.local-ai-grid .el-select').first().click()
  await page.locator('.el-select-dropdown:visible').last().getByRole('option', { name:'ref.wav' }).click()
  await page.getByRole('textbox', { name:'录音对应文字' }).fill('这是参考录音文字。')
  const voiceSwitch = page.locator('.future-voice-switch .el-switch__input')
  // The settings-tab builtin selection already synchronized the native voice flag on.
  await expect(voiceSwitch).toBeChecked()
  await page.locator('.future-voice-switch .el-switch').click()
  await page.getByRole('button', { name:'保存参考资料与设置' }).click()
  await expect.poll(() => patchCalls.length).toBe(2)
  expect(patchCalls[1].models.speech.source).toBe('third_party')
  expect(patchCalls[1].local_ai.voice_enabled).toBe(false)
  await expect(voiceSwitch).toBeEnabled()
  await page.locator('.future-voice-switch .el-switch').click()
  await page.getByRole('button', { name:'保存参考资料与设置' }).click()
  await expect.poll(() => patchCalls.length).toBe(3)
  expect(patchCalls[2].models.speech.source).toBe('builtin')
  expect(patchCalls[2].local_ai.voice_enabled).toBe(true)
})

test('model cards and key actions stay usable on a 390px mobile layout', async ({ page }) => {
  await page.setViewportSize({ width:390, height:844 })
  await installSettingsAPI(page)
  await page.goto('/setup?tab=settings')
  const text = card(page, '文本模型')
  const image = card(page, '图像模型')
  await expect(text.getByText('模型来源')).toBeVisible()
  const textBox = await text.boundingBox()
  const imageBox = await image.boundingBox()
  expect(textBox && imageBox && imageBox.y > textBox.y).toBeTruthy()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  await expect(text.locator('input[type="password"]')).toBeVisible()
  await expect(text.getByRole('button', { name:'保存密钥', exact:true })).toBeVisible()
  // The key row spans the full card width: input and stacked buttons match the Base URL field.
  const urlBox = await text.getByLabel('Base URL').locator('..').boundingBox()
  const keyBox = await text.locator('input[type="password"]').locator('..').boundingBox()
  const saveBox = await text.getByRole('button', { name:'保存密钥', exact:true }).boundingBox()
  const clearBox = await text.getByRole('button', { name:'清除已保存密钥' }).boundingBox()
  for (const box of [keyBox, saveBox, clearBox])
    expect(box && urlBox && Math.abs(box.width - urlBox.width) <= 1 && Math.abs(box.x - urlBox.x) <= 1).toBeTruthy()
})

test('video model and local Hypit settings save without dispatching a generation', async ({ page }) => {
  const { keyCalls, patchCalls } = await installSettingsAPI(page)
  await page.goto('/setup?tab=settings')
  const video = card(page, '视频模型 · MiniMax H3')
  await expect(video).toBeVisible()
  await video.getByLabel('Base URL').fill('https://metaso.cn/api/minimax')
  await video.getByLabel('模型名称').fill('MiniMax-H3')
  await video.locator('input[type="password"]').fill('fixture-h3-key')
  await video.getByRole('button', {name:'保存密钥',exact:true}).click()
  await expect.poll(()=>keyCalls.length).toBe(1)
  await page.locator('.el-form-item').filter({hasText:'画面渲染方式'}).locator('.el-select').click()
  await page.getByRole('option',{name:'Hypit 动态画面（本地）'}).click()
  await page.getByRole('button',{name:'保存设置'}).click()
  await expect.poll(()=>patchCalls.length).toBe(1)
  expect(patchCalls[0].video.renderer).toBe('hypit')
  expect(patchCalls[0].models.video.protocol).toBe('minimax_h3')
  expect(JSON.stringify(patchCalls[0])).not.toContain('fixture-h3-key')
})
