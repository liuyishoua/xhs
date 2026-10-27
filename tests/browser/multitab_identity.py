import asyncio,json,sys,mimetypes
from pathlib import Path
from urllib.parse import urlsplit
from playwright.async_api import async_playwright
async def main():
 async with async_playwright() as p:
  b=await p.chromium.launch(args=['--no-sandbox']);c=await b.new_context();owner={'id':101,'username':'offline-user-a'};exchanges=0;errors=[];logout_completed=False
  if len(sys.argv)>1:
   build=Path(sys.argv[1])
   async def static(route):
    rel=urlsplit(route.request.url).path.lstrip('/');file=build/rel
    if not file.is_file():file=build/'index.html'
    await route.fulfill(path=str(file),content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
   await c.route('https://d.58nft.net:9450/**',static)
  async def api(route):
   nonlocal exchanges,owner,logout_completed
   path=urlsplit(route.request.url).path
   if path.endswith('/auth/logout'):
    await asyncio.sleep(.3);owner=None;logout_completed=True;await route.fulfill(json={'status':'ok'});return
   if '/auth/' in path:
    exchanges+=1
    if owner is None:await route.fulfill(status=401,json={'detail':'Expired'});return
    await route.fulfill(json={'access_token':'offline-access','refresh_token':'offline-refresh-'+str(exchanges),'user':owner})
   elif '/accounts' in path:await route.fulfill(json={'items':[],'total':0,'page':1,'page_size':20})
   elif path.endswith('/network'):await route.fulfill(json={'state':'online'})
   else:await route.fulfill(json={'items':[]})
  await c.route('**/api/**',api)
  pages=[];url='https://d.58nft.net:9450/platforms/xhs/messages'
  for i in range(2):
   page=await c.new_page();page.on('pageerror',lambda e:errors.append(str(e)));await page.goto(url)
   await page.get_by_text('offline-user-a',exact=True).first.wait_for();pages.append(page)
  await pages[-1].wait_for_timeout(3500)
  assert all(page.url==url for page in pages),[page.url for page in pages]
  before=exchanges
  await pages[0].evaluate("localStorage.setItem('spider_xhs_refresh_token','same-user-rotation')")
  await pages[-1].wait_for_timeout(1000)
  assert all(page.url==url for page in pages)
  assert exchanges-before<=2,exchanges-before
  owner={'id':102,'username':'offline-user-b'}
  await pages[0].evaluate("localStorage.setItem('spider_xhs_refresh_token','other-user-rotation');window.dispatchEvent(new Event('focus'))")
  for page in pages:await page.get_by_text('offline-user-b',exact=True).first.wait_for()
  assert all(page.url==url for page in pages)
  await pages[0].get_by_role('button',name='退出登录',exact=True).first.click()
  await pages[0].wait_for_timeout(500)
  assert logout_completed
  if len(sys.argv)==1:
   for page in pages:await page.wait_for_url('https://d.58nft.net:9461/login')
  failed_context=await b.new_context()
  if len(sys.argv)>1:await failed_context.route('https://d.58nft.net:9450/**',static)
  await failed_context.route('**/api/**',lambda route:route.fulfill(status=503,json={'detail':'temporary outage'}))
  failed_page=await failed_context.new_page();await failed_page.goto(url)
  await failed_page.get_by_text('暂时无法验证登录状态',exact=True).wait_for()
  assert failed_page.url==url
  owner={'id':103,'username':'offline-recovered'}
  await failed_context.route('**/api/**',api)
  await failed_page.locator('.ant-result-extra button').click()
  await failed_page.get_by_text('offline-recovered',exact=True).first.wait_for()
  assert failed_page.url==url
  await failed_context.close()
  assert not errors,errors
  print(json.dumps({'outage_retry_keeps_original_route':True,'two_tabs_keep_messages_route':True,'token_rotation_does_not_redirect':True,'owner_switch_updates_both_tabs':True,'logout_request_completed':logout_completed,'logout_verified':len(sys.argv)==1,'js_errors':errors,'exchanges':exchanges}))
  await b.close()
asyncio.run(main())
