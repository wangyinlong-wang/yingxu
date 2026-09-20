import { expect, test } from '@playwright/test'

test('creates a demand signal and recovers from a strategy API validation error', async ({ page }) => {
  const creations:any[]=[]
  let fail = true
  await page.route('**/api/v1/**', async route => {
    const request=route.request(), path=new URL(request.url()).pathname
    if (path.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (path.endsWith('/strategy/demand-signals') && request.method()==='GET') return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/strategy/benchmarks') && request.method()==='GET') return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/strategy/experiments') && request.method()==='GET') return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/contents') || path.endsWith('/publications')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/strategy/demand-signals') && request.method()==='POST') {
      creations.push(request.postDataJSON())
      if (fail) { fail=false; return route.fulfill({status:422,json:{code:'INVALID_URL',message:'来源链接必须为公网 HTTP(S)',request_id:'test'}}) }
      return route.fulfill({json:{id:'signal-1',...request.postDataJSON(),created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z'}})
    }
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
  await page.goto('/strategy')
  await page.getByRole('button',{name:'新建需求信号'}).click()
  const dialog=page.getByRole('dialog',{name:'新建需求信号'})
  await dialog.getByLabel('标题').fill('用户反复询问的功能')
  await dialog.getByLabel('公开来源链接').fill('https://example.com/signal')
  await dialog.getByRole('button',{name:'保存'}).click()
  await expect(page.getByText('来源链接必须为公网 HTTP(S)')).toBeVisible()
  await dialog.getByRole('button',{name:'保存'}).click()
  await expect.poll(()=>creations).toHaveLength(2)
  expect(creations[1].scores).toEqual({demand:0,production:0,delivery:0,presentation:0,compliance:0,iteration:0})
})

test('shows all three strategy workbenches', async ({ page }) => {
  await page.route('**/api/v1/**', async route => {
    const path=new URL(route.request().url()).pathname
    if (path.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (path.includes('/strategy/')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/contents') || path.endsWith('/publications')) return route.fulfill({json:{items:[],total:0}})
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
  await page.goto('/strategy')
  await page.getByRole('tab',{name:'对标拆解'}).click()
  await expect(page.getByRole('button',{name:'新建对标拆解'})).toBeVisible()
  await page.getByRole('tab',{name:'内容实验'}).click()
  await expect(page.getByRole('button',{name:'新建内容实验'})).toBeVisible()
})

test('edits an existing multi-publication experiment without sending read-only server fields', async ({ page }) => {
  const patches:any[]=[]
  const experiment={id:'experiment-1',name:'原实验',hypothesis:'原假设',variable:'angle',status:'RUNNING',content_ids:['content-1'],publication_ids:['publication-1','publication-2'],observation_windows:['24h'],conclusion:'',next_action:'undecided',publication_metrics:[{publication_id:'publication-1',latest_metric:{views:120,likes:8,comments:null,favorites:null,shares:null},delta:{views:20,likes:null,comments:null,favorites:null,shares:null}},{publication_id:'publication-2',latest_metric:null,delta:{views:null,likes:null,comments:null,favorites:null,shares:null}}],created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-02T00:00:00Z'}
  await page.route('**/api/v1/**', async route => {
    const request=route.request(), path=new URL(request.url()).pathname
    if (path.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (path.endsWith('/strategy/demand-signals')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/strategy/benchmarks')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/strategy/experiments') && request.method()==='GET') return route.fulfill({json:{items:[experiment],total:1}})
    if (path.endsWith('/strategy/experiments/experiment-1') && request.method()==='PATCH') { patches.push(request.postDataJSON()); return route.fulfill({json:{...experiment,...request.postDataJSON()}}) }
    if (path.endsWith('/contents')) return route.fulfill({json:{items:[{id:'content-1',title:'关联内容'}],total:1}})
    if (path.endsWith('/publications')) return route.fulfill({json:{items:[{id:'publication-1',content_title:'关联作品',platform:'douyin'}],total:1}})
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
  await page.goto('/strategy')
  await page.getByRole('tab',{name:'内容实验'}).click()
  await expect(page.getByText('作品 1：播放 120、赞 8，播放增量 20；作品 2：尚无观测')).toBeVisible()
  await page.locator('tr').filter({hasText:'原实验'}).getByRole('button',{name:'编辑'}).click()
  const dialog=page.getByRole('dialog',{name:'编辑内容实验'})
  await dialog.getByLabel('实验名称').fill('更新实验')
  await dialog.getByRole('button',{name:'保存'}).click()
  await expect.poll(()=>patches).toHaveLength(1)
  expect(patches[0]).toMatchObject({name:'更新实验',content_ids:['content-1'],publication_ids:['publication-1','publication-2']})
  expect(patches[0]).not.toHaveProperty('id')
  expect(patches[0]).not.toHaveProperty('created_at')
  expect(patches[0]).not.toHaveProperty('updated_at')
  expect(patches[0]).not.toHaveProperty('publication_metrics')
})
