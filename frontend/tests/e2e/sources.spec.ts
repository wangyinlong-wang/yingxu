import { expect, test } from '@playwright/test'

test('keeps a dismissed topic hidden after a refresh from the server', async ({ page }) => {
  let visibleDismissed=false
  await page.route('**/api/v1/**', async route => {
    const request=route.request(), path=new URL(request.url()).pathname
    if (path.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (path.endsWith('/sources')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/topics')) return route.fulfill({json:{items:[{id:'visible',source_id:'s',title:'保留选题',url:'https://example.com/a',summary:'摘要',published_at:null,score:1,relevance_score:90,selection_reason:'相关',status:visibleDismissed?'DISMISSED':'NEW',created_at:'2026-01-01T00:00:00Z'},{id:'dismissed',source_id:'s',title:'已忽略选题',url:'https://example.com/b',summary:'摘要',published_at:null,score:1,relevance_score:80,selection_reason:'相关',status:'DISMISSED',created_at:'2026-01-01T00:00:00Z'}],total:2}})
    if (path.endsWith('/topics/visible/dismiss')) { visibleDismissed=true; return route.fulfill({json:{}}) }
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
  await page.goto('/sources')
  await page.getByRole('tab',{name:'候选选题'}).click()
  await expect(page.getByText('保留选题')).toBeVisible()
  await expect(page.getByText('已忽略选题')).toHaveCount(0)
  await page.getByRole('button',{name:'忽略'}).click()
  await page.getByRole('button',{name:'刷新'}).click()
  await expect(page.getByText('保留选题')).toHaveCount(0)
  await expect(page.getByText('已忽略选题')).toHaveCount(0)
})

test('creates an X.com profile source with its profile URL', async ({ page }) => {
  let created:any
  await page.route('**/api/v1/**', async route => {
    const request=route.request(), path=new URL(request.url()).pathname
    if (path.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (path.endsWith('/sources') && request.method()==='GET') return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/sources') && request.method()==='POST') {
      created=request.postDataJSON()
      return route.fulfill({json:{id:'x-openai',...created,last_collected_at:null,last_error:null,created_at:'2026-01-01T00:00:00Z'}})
    }
    if (path.endsWith('/topics')) return route.fulfill({json:{items:[],total:0}})
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
  await page.goto('/sources')
  await page.getByRole('button',{name:'添加来源'}).click()
  await page.locator('.el-radio').filter({hasText:'X公开时间线'}).click()
  await expect(page.getByText('采集近 30 天公开可见的技术/AI 帖子',{exact:false})).toBeVisible()
  await page.getByLabel('名称').fill('OpenAI on X')
  await page.getByLabel('地址').fill('https://x.com/OpenAI')
  await page.getByRole('button',{name:'保存',exact:true}).click()
  await expect.poll(()=>created).toMatchObject({name:'OpenAI on X',kind:'x',url:'https://x.com/OpenAI',enabled:true})
})

test('converts an X source to TikHub, protects paid collection, and preserves RSS kind', async ({ page }) => {
  let sources:any[]=[
    {id:'x-openai',name:'OpenAI on X',kind:'x',url:'https://x.com/OpenAI',enabled:true,last_collected_at:null,last_error:null,created_at:'2026-01-01T00:00:00Z'},
    {id:'rss-tech',name:'Tech RSS',kind:'rss',url:'https://example.com/feed.xml',enabled:true,last_collected_at:null,last_error:null,created_at:'2026-01-01T00:00:00Z'}
  ]
  const patches:any[]=[]
  const collectionBodies:any[]=[]
  await page.route('**/api/v1/**', async route => {
    const request=route.request(), path=new URL(request.url()).pathname
    if (path.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (path.endsWith('/sources') && request.method()==='GET') return route.fulfill({json:{items:sources,total:sources.length}})
    if (path.endsWith('/topics')) return route.fulfill({json:{items:[],total:0}})
    if (request.method()==='PATCH' && path.startsWith('/api/v1/sources/')) {
      const body=request.postDataJSON(), id=path.split('/').at(-1)!
      patches.push(body)
      const index=sources.findIndex(source=>source.id===id)
      sources[index]={...sources[index],...body}
      return route.fulfill({json:sources[index]})
    }
    if (request.method()==='POST' && path.endsWith('/collect')) {
      collectionBodies.push(request.postDataJSON())
      return route.fulfill({status:202,json:{job_id:'collect-1',status:'QUEUED'}})
    }
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
  await page.goto('/sources')
  await page.getByRole('button',{name:'编辑'}).first().click()
  await page.locator('.el-radio').filter({hasText:'X（TikHub）'}).click()
  await page.getByRole('button',{name:'保存',exact:true}).click()
  await expect.poll(()=>patches).toHaveLength(1)
  expect(patches[0]).toMatchObject({kind:'x_tikhub',url:'https://x.com/OpenAI'})
  await page.getByRole('button',{name:'编辑'}).nth(1).click()
  await page.getByRole('button',{name:'保存',exact:true}).click()
  await expect.poll(()=>patches).toHaveLength(2)
  expect(patches[1]).not.toHaveProperty('kind')
  const collectButton=page.getByRole('button',{name:'采集'}).first()
  await collectButton.click()
  const dialog=page.getByRole('dialog',{name:'确认 TikHub 采集'})
  await expect(dialog.getByText('成功请求按服务商规则计费并计入每日服务调用额度')).toBeVisible()
  await dialog.getByRole('button',{name:'取消'}).click()
  await expect(collectButton).toBeEnabled()
  expect(collectionBodies).toHaveLength(0)
  await collectButton.click()
  const confirmedDialog=page.getByRole('dialog',{name:'确认 TikHub 采集'})
  const request=page.waitForRequest(value=>value.method()==='POST'&&value.url().endsWith('/sources/x-openai/collect'))
  await confirmedDialog.getByRole('button',{name:'继续采集'}).click()
  expect((await request).postDataJSON()).toEqual({expected_kind:'x_tikhub'})
  await expect.poll(()=>collectionBodies).toEqual([{expected_kind:'x_tikhub'}])
})

test('collects a free source without an expected provider payload', async ({ page }) => {
  const source={id:'rss-tech',name:'Tech RSS',kind:'rss',url:'https://example.com/feed.xml',enabled:true,last_collected_at:null,last_error:null,created_at:'2026-01-01T00:00:00Z'}
  await page.route('**/api/v1/**', async route => {
    const request=route.request(), path=new URL(request.url()).pathname
    if (path.endsWith('/health')) return route.fulfill({json:{status:'ok',version:'test'}})
    if (path.endsWith('/sources') && request.method()==='GET') return route.fulfill({json:{items:[source],total:1}})
    if (path.endsWith('/topics')) return route.fulfill({json:{items:[],total:0}})
    if (path.endsWith('/sources/rss-tech/collect')) return route.fulfill({status:202,json:{job_id:'collect-1',status:'QUEUED'}})
    return route.fulfill({status:404,json:{code:'NOT_FOUND',message:'unhandled mock',request_id:'test'}})
  })
  await page.goto('/sources')
  const request=page.waitForRequest(value=>value.method()==='POST'&&value.url().endsWith('/sources/rss-tech/collect'))
  await page.getByRole('button',{name:'采集'}).click()
  expect((await request).postData()).toBeNull()
})
