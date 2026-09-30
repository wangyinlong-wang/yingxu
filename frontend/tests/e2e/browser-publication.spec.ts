import { expect, test, type Page, type Route } from '@playwright/test'

type Overrides = { accounts?: unknown; devices?: unknown; publications?: unknown; browserStatus?: unknown }
const baseRoutes = async (page: Page, overrides: Overrides = {}) => {
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/health')) return route.fulfill({ json: { status: 'ok', version: 'test' } })
    if (path.endsWith('/accounts') && request.method() === 'GET') return route.fulfill({ json: overrides.accounts ?? { items: [], total: 0 } })
    if (path.endsWith('/devices')) return route.fulfill({ json: overrides.devices ?? { items: [], total: 0 } })
    if (path.endsWith('/contents')) return route.fulfill({ json: { items: [], total: 0 } })
    if (path.endsWith('/settings')) return route.fulfill({ json: { publishing: { auto_publish_after_approval: false } } })
    if (path.endsWith('/publications') && request.method() === 'GET') return route.fulfill({ json: overrides.publications ?? { items: [], total: 0 } })
    if (path.endsWith('/browser/status')) return route.fulfill({ json: overrides.browserStatus ?? { installed: true, version: '0.13.10', chrome_available: true, ready: true, message: '本地 browser-use 与 Chrome 已就绪' } })
    return route.fulfill({ status: 404, json: { code: 'NOT_FOUND', message: 'unhandled mock', request_id: 'test' } })
  })
}

test('creates a browser account sending only writable fields and shows install guidance when runtime is missing', async ({ page }) => {
  const accountCreates: unknown[] = []
  await baseRoutes(page, { browserStatus: { installed: false, version: null, chrome_available: false, ready: false, message: '请运行 ./scripts/setup_browser_use.sh 安装锁定版本' } })
  await page.route('**/api/v1/accounts', async route => {
    if (route.request().method() === 'POST') { accountCreates.push(route.request().postDataJSON()); return route.fulfill({ json: {} }) }
    return route.fallback()
  })
  await page.goto('/publications')
  await expect(page.getByText('./scripts/setup_browser_use.sh', { exact: false }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: '打开 Chrome 登录' })).toHaveCount(2)
  await expect(page.getByRole('button', { name: '打开 Chrome 登录' }).first()).toBeDisabled()
  await expect(page.getByText('未发现 ADB 设备')).toHaveCount(0)
  await expect(page.getByText('已发现设备')).toHaveCount(0)
  await page.getByRole('button', { name: '账号设置' }).click()
  const dialog = page.getByRole('dialog', { name: '配置发布账号' })
  await expect(dialog.getByText('设备序列号')).toHaveCount(0)
  await dialog.locator('.el-form-item').filter({ hasText: '显示名称' }).locator('input').fill('浏览器号')
  await dialog.locator('.el-form-item').filter({ hasText: '账号标识' }).locator('input').fill('browser-user')
  await dialog.getByRole('button', { name: '保存' }).click()
  await expect.poll(() => accountCreates).toEqual([{ channel: 'browser', platform: 'douyin', display_name: '浏览器号', account_identifier: 'browser-user' }])
})

test('opens Chrome login and inspects browser accounts, surfacing warnings', async ({ page }) => {
  const opens: unknown[] = []
  const inspects: unknown[] = []
  await baseRoutes(page)
  await page.route('**/api/v1/browser/douyin/open', async route => { opens.push(route.request().postDataJSON()); return route.fulfill({ json: { ready: false, account_identifier: null, warnings: ['尚未登录抖音创作者账号'], channel: 'browser', platform: 'douyin' } }) })
  await page.route('**/api/v1/browser/douyin/inspect', async route => { inspects.push(route.request().postDataJSON()); return route.fulfill({ json: { ready: true, account_identifier: 'browser-user', warnings: [], channel: 'browser', platform: 'douyin' } }) })
  await page.goto('/publications')
  await expect(page.getByText('运行环境已就绪')).toBeVisible()
  await page.getByRole('button', { name: '打开 Chrome 登录' }).first().click()
  await expect(page.getByText('尚未登录抖音创作者账号').first()).toBeVisible()
  await page.getByRole('button', { name: '检查账号' }).first().click()
  await expect(page.getByText('抖音：已就绪')).toBeVisible()
  await expect(page.getByText('账号 browser-user')).toBeVisible()
  await expect.poll(() => opens).toEqual([{}])
  await expect.poll(() => inspects).toEqual([{}])
})

test('handles browser-channel publications without any ADB device', async ({ page }) => {
  const detections: string[] = []
  await baseRoutes(page, {
    accounts: { items: [{ id: 'a-browser', platform: 'douyin', channel: 'browser', display_name: '浏览器号', account_identifier: 'bu', device_serial: 'browser:douyin', created_at: '2026-01-01T00:00:00Z' }], total: 1 },
    publications: { items: [{ id: 'pub-browser', content_id: 'c1', content_version: 1, content_title: '浏览器作品', platform: 'douyin', account_id: 'a-browser', channel: 'browser', status: 'WAITING_USER', job_id: null, work_url: null, work_id: null, evidence: { current_step: '已打开创作者页' }, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' }], total: 1 },
  })
  await page.route('**/api/v1/publications/pub-browser/detect', async route => { detections.push(new URL(route.request().url()).pathname); return route.fulfill({ json: {} }) })
  await page.goto('/publications')
  await expect(page.getByRole('cell', { name: '浏览器确认' })).toBeVisible()
  await expect(page.locator('code').filter({ hasText: '本机 Chrome' })).toHaveCount(1)
  await expect(page.getByText(/ADB 手机/)).toHaveCount(0)
  const detect = page.getByRole('button', { name: '从浏览器识别' })
  await expect(detect).toBeEnabled()
  await expect(page.getByRole('button', { name: '从手机识别' })).toHaveCount(0)
  await detect.click()
  await page.getByRole('dialog', { name: '从浏览器识别已发作品' }).getByRole('button', { name: '开始识别' }).click()
  await expect.poll(() => detections).toEqual(['/api/v1/publications/pub-browser/detect'])
})

test('labels browser metric sources and collection text in analytics', async ({ page }) => {
  await baseRoutes(page, {
    accounts: { items: [{ id: 'a1', platform: 'douyin', channel: 'browser', display_name: '浏览器号', account_identifier: 'bu', device_serial: 'browser:douyin' }], total: 1 },
    publications: { items: [{ id: 'pub-1', content_id: 'c1', content_version: 1, content_title: '浏览器作品', platform: 'douyin', account_id: 'a1', channel: 'android', status: 'PUBLISHED', job_id: null, work_url: 'https://example.com/w', work_id: null, evidence: {}, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' }], total: 1 },
  })
  await page.route('**/api/v1/analytics', async route => route.fulfill({ json: { metrics: [], reports: [] } }))
  await page.route('**/api/v1/metrics**', async route => route.fulfill({ json: { items: [{ id: 'm1', publication_id: 'pub-1', platform: 'douyin', content_title: '浏览器作品', views: 10, likes: 2, comments: null, favorites: null, shares: null, source: 'browser', raw: {}, observed_at: '2026-01-02T00:00:00Z' }], total: 1 } }))
  await page.goto('/analytics')
  await page.locator('.el-select').first().click()
  await page.locator('.el-select-dropdown:visible').last().getByRole('option', { name: 'douyin · 浏览器作品' }).click()
  await expect(page.getByRole('button', { name: '从浏览器读取指标' })).toBeVisible()
  await expect(page.getByRole('button', { name: '从手机读取指标' })).toHaveCount(0)
  await expect(page.getByRole('cell', { name: '浏览器' })).toBeVisible()
})
