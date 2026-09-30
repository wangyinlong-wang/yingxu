import { expect, test } from '@playwright/test'

const dashboardPayload = { counts:{topics:3,contents:4,review_required:2,published:1,failed_jobs:1}, recent_jobs:[], recent_contents:[{id:'c9',title:'最近内容条目',status:'REVIEW_REQUIRED',updated_at:'2026-01-01T00:00:00Z'}], usage:{calls_today:5,tokens_today:100,cost:null}, environment:{timezone:'Asia/Shanghai',warnings:[]} }

type ExtraHandler = (path:string, url:URL, route:any) => Promise<boolean>

async function installBaseApi(page:any, extra?: ExtraHandler) {
  await page.route('**/api/v1/**', async (route:any) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (extra && await extra(path, url, route)) return
    if (path.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (path.endsWith('/dashboard')) return route.fulfill({json:dashboardPayload})
    if (path.endsWith('/templates')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/presets')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/contents')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/jobs')) return route.fulfill({json:{items:[],total:0}})
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
}

test('dashboard status cards link to filtered destinations that actually apply the filter', async ({ page }) => {
  const contentsQueries:string[] = []
  const jobsQueries:string[] = []
  await installBaseApi(page, async (path, url, route) => {
    if (path === '/api/v1/contents') { contentsQueries.push(url.search); await route.fulfill({json:{items:[],total:0}}); return true }
    if (path === '/api/v1/jobs') { jobsQueries.push(url.search); await route.fulfill({json:{items:[],total:0}}); return true }
    return false
  })
  await page.goto('/')
  await expect(page.getByText('2 条内容待人工复核', {exact:false})).toBeVisible()
  await page.getByRole('link', {name:/待复核内容/}).click()
  await expect(page).toHaveURL(/\/contents\?status=REVIEW_REQUIRED/)
  await expect.poll(() => contentsQueries.some(q => q.includes('status=REVIEW_REQUIRED'))).toBe(true)
  await page.goto('/')
  await page.getByRole('link', {name:'失败任务 1'}).click()
  await expect(page).toHaveURL(/\/jobs\?status=FAILED/)
  await expect.poll(() => jobsQueries.some(q => q.includes('status=FAILED'))).toBe(true)
})

test('dashboard recent content links open the workspace on that content', async ({ page }) => {
  await installBaseApi(page, async (path, _url, route) => {
    if (path.endsWith('/contents/c9')) { await route.fulfill({json:{id:'c9',topic_id:null,title:'最近内容条目',version:1,media_version:null,status:'REVIEW_REQUIRED',template_id:null,script:{angle:'',selection_reason:'',facts:[],platforms:{douyin:{title:'',description:'',tags:[]},xiaohongshu:{title:'',description:'',tags:[]}}},scenes:[],video_asset_id:null,cover_asset_id:null,subtitle_asset_id:null,created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z'}}); return true }
    if (path.endsWith('/assets')) { await route.fulfill({json:{items:[],total:0}}); return true }
    if (path.endsWith('/quality-reviews')) { await route.fulfill({json:{items:[],total:0}}); return true }
    return false
  })
  await page.goto('/')
  await page.getByRole('link', {name:'最近内容条目'}).click()
  await expect(page).toHaveURL(/\/contents\?content_id=c9/)
  await expect(page.locator('.title-input input')).toHaveValue('最近内容条目')
})

test('status query params are whitelisted on jobs and contents', async ({ page }) => {
  const requests:URL[] = []
  await installBaseApi(page, async (_path, url, route) => {
    requests.push(url)
    await route.fulfill({json:{items:[],total:0}})
    return true
  })
  await page.goto('/jobs?status=INJECTED')
  await expect.poll(() => requests.filter(u => u.pathname === '/api/v1/jobs').length).toBeGreaterThan(0)
  expect(requests.find(u => u.pathname === '/api/v1/jobs')!.searchParams.get('status')).toBeNull()
  await page.goto('/jobs?status=FAILED')
  await expect.poll(() => requests.filter(u => u.pathname === '/api/v1/jobs' && u.searchParams.get('status') === 'FAILED').length).toBeGreaterThan(0)
  await page.goto('/contents?status=REVIEW_REQUIRED')
  await expect.poll(() => requests.filter(u => u.pathname === '/api/v1/contents' && u.searchParams.get('status') === 'REVIEW_REQUIRED').length).toBeGreaterThan(0)
  await page.goto('/contents?status=INJECTED')
  await expect.poll(() => requests.filter(u => u.pathname === '/api/v1/contents' && !u.searchParams.get('status')).length).toBeGreaterThan(0)
})

test('jobs background polling keeps the rendered list on failure and clears the warning after recovery', async ({ page }) => {
  test.setTimeout(40_000)
  let failing = false
  const job = { id:'job-1', kind:'GENERATE_CONTENT', content_id:null, status:'RUNNING', progress:40, current_step:'渲染中', error_code:null, error_message:null, steps:[], result:null, created_at:'2026-01-01T00:00:00Z', updated_at:'2026-01-01T00:00:00Z' }
  await installBaseApi(page, async (path, _url, route) => {
    if (path === '/api/v1/jobs') {
      if (failing) { await route.fulfill({status:500, json:{code:'INTERNAL', message:'模拟的刷新失败', request_id:'test'}}); return true }
      await route.fulfill({json:{items:[job], total:1}})
      return true
    }
    return false
  })
  await page.goto('/jobs')
  await expect(page.getByText('内容生成')).toBeVisible()
  failing = true
  const warning = page.getByText('自动刷新失败', {exact:false})
  await expect(warning).toBeVisible({timeout:15_000})
  await expect(page.getByText('内容生成')).toBeVisible()
  failing = false
  await expect(warning).toHaveCount(0, {timeout:15_000})
  await expect(page.getByText('内容生成')).toBeVisible()
})

test('jobs filter change during a slow load lets the latest request win', async ({ page }) => {
  const requests:string[] = []
  const base = { content_id:null, error_code:null, error_message:null, steps:[], result:null, created_at:'2026-01-01T00:00:00Z', updated_at:'2026-01-01T00:00:00Z' }
  const runningJob = { ...base, id:'job-1', kind:'GENERATE_CONTENT', status:'RUNNING', progress:40, current_step:'渲染中' }
  const failedJob = { ...base, id:'job-2', kind:'GENERATE_CONTENT', status:'FAILED', progress:40, current_step:'生成失败' }
  await installBaseApi(page, async (path, url, route) => {
    if (path === '/api/v1/jobs') {
      const requested = url.searchParams.get('status') || ''
      requests.push(requested || 'ALL')
      if (!requested) {
        await new Promise(resolve => setTimeout(resolve, 1200))
        await route.fulfill({json:{items:[runningJob],total:1}})
      } else {
        await route.fulfill({json:{items:[failedJob],total:1}})
      }
      return true
    }
    return false
  })
  await page.goto('/jobs')
  await page.locator('.jobs-toolbar .el-select').click()
  await page.getByRole('option', {name:'未完成'}).click()
  await expect(page.getByText('生成失败')).toBeVisible({timeout:3000})
  await expect.poll(() => requests, {timeout:3000}).toContain('FAILED')
  await page.waitForTimeout(1400)
  await expect(page.getByText('渲染中')).toHaveCount(0)
  await expect(page.getByText('生成失败')).toBeVisible()
})

test('mobile navigation drawer opens and closes and the page does not overflow horizontally', async ({ page }) => {
  await page.setViewportSize({width:390, height:844})
  await installBaseApi(page)
  const noOverflow = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)
  await page.goto('/')
  expect(await noOverflow()).toBe(true)
  const menuButton = page.getByRole('button', {name:'打开导航菜单'})
  await expect(menuButton).toBeVisible()
  await expect(menuButton).toHaveAttribute('aria-expanded', 'false')
  await menuButton.click()
  const drawer = page.getByRole('dialog', {name:'导航菜单'})
  await expect(drawer.getByRole('link', {name:'任务中心'})).toBeVisible()
  await expect(menuButton).toHaveAttribute('aria-expanded', 'true')
  const closeButton = drawer.getByRole('button', {name:'关闭导航菜单'})
  await expect(closeButton).toBeVisible()
  await drawer.getByRole('link', {name:'总览'}).click()
  await expect(drawer).toBeHidden()
  await expect(page).toHaveURL(/\/$/)
  await menuButton.click()
  await closeButton.click()
  await expect(drawer).toBeHidden()
  await expect(menuButton).toHaveAttribute('aria-expanded', 'false')
  await menuButton.click()
  await expect(drawer.getByRole('link', {name:'任务中心'})).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await menuButton.click()
  await drawer.getByRole('link', {name:'任务中心'}).click()
  await expect(page).toHaveURL(/\/jobs$/)
  await expect(drawer).toBeHidden()
  expect(await noOverflow()).toBe(true)
})
