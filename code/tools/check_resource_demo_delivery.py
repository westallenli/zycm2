"""Read/preview-only smoke test of the clean delivery data."""
import json,sys
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(sys.argv[1]);port=sys.argv[2];base='http://127.0.0.1:'+port
sys.path.insert(0,str(root/'source'))
from panda_brain.resource_directory_page import columns,xlsx
from panda_brain.document_tables import extract
checks=[];errors=[]
def check(label,value):
    checks.append(dict(label=label,passed=bool(value)))
    (root/'delivery-browser.json').write_text(json.dumps(dict(checks=checks,errors=errors),ensure_ascii=False,indent=2),encoding='utf-8')
    if not value:raise AssertionError(label)
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True,args=['--disable-gpu'])
    context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    def login(phone):
        context.clear_cookies();page.goto(base+'/login');page.locator('[name=account]').fill(phone);page.locator('[name=password]').fill('123456');page.locator('.login-submit').click();page.wait_for_load_state()
    page.goto(base+'/login');check('登录页显示演示账号',page.get_by_text('13900000031',exact=True).count()==1);page.screenshot(path=str(root/'delivery-login.png'))
    login('13900000119');page.goto(base+'/materials/daily');check('日常120人库','共 120 人' in page.inner_text('body'));check('中文导入及新增公司入口',page.locator('#company').get_by_text('新增外协公司',exact=True).count()==1)
    page.screenshot(path=str(root/'delivery-daily-desktop.png'))
    company=page.locator('#company');company.locator('summary').filter(has_text='文件导入 / 下载模板').click()
    with page.expect_download() as download:company.get_by_text('下载当前Excel（含示例）',exact=True).click()
    downloaded=root/'delivery-company-download.xlsx';download.value.save_as(str(downloaded));text=extract(downloaded.name,downloaded.read_bytes())
    check('下载公司中文表头和6家公司',text.startswith('公司名称,服务/物资,服务区域,联系人,联系电话,资料来源,来源更新时间') and len(text.splitlines())==7)
    data=json.loads((root/'data/materials.json').read_text(encoding='utf-8'))
    upload=root/'delivery-preview.xlsx';upload.write_bytes(xlsx(columns('company'),[['演示预览公司','饮用水','示范甲市','演示联系人','13900009999','仅用于最终预览校验','2026-10-17T10:00:00+08:00']]))
    form=company.locator('form[enctype="multipart/form-data"]');form.locator('[name=file]').set_input_files(str(upload));form.locator('[name=basis]').fill('虚构中文Excel预览，不确认写入');form.get_by_role('button',name='上传并预览',exact=True).click();page.wait_for_load_state()
    check('最终中文Excel可预览','演示预览公司' in page.inner_text('body') and page.get_by_role('button',name='确认导入',exact=True).count()==1)
    check('预览没有修改物资资料',data==json.loads((root/'data/materials.json').read_text(encoding='utf-8')))
    login('13900000120');page.goto(base+'/');check('人员框架有统一签到数据',page.get_by_role('heading',name='人员与扫码签到演示').count()==1)
    check('旧人员安排维护入口移除',page.locator('[data-org-url^="/personnel/arrangements"],a[href="/personnel/arrangements"]').count()==0)
    page.screenshot(path=str(root/'delivery-personnel-desktop.png'))
    page.goto(base+'/materials/event');check('到场32人',page.locator('[data-resource-arrived]').get_attribute('data-resource-arrived')=='32')
    for heading in ['应急餐食 · 当前缺 30 份','照明灯组 · 当前缺 4 套','饮用水 · 预计还需补 120 瓶']:check(heading,page.get_by_role('heading',name=heading,exact=True).count()==1)
    check('提前两小时提醒','约 2 小时后不足' in page.inner_text('body'))
    page.screenshot(path=str(root/'delivery-event-desktop.png'))
    with page.expect_response(lambda response:'/materials/adviser-feed' in response.url,timeout=25000) as feed:pass
    check('20秒自动重评服务正常',feed.value.status==200 and 'data-event="fire-001"' in feed.value.text())
    page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(root/'delivery-event-mobile.png'));check('手机事件页无横向溢出',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
    login('13900000031');page.goto(base+'/personnel/arrival');check('待到场队长扫码表单有效',page.locator('form[action="/captain-arrival/scan"]').count()==1)
    check('显示当前有效二维码',page.locator('svg[aria-label^="事件二维码"]').count()==1);page.screenshot(path=str(root/'delivery-scan-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1000});login('13900000119');page.goto(base+'/materials/daily');page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(root/'delivery-daily-mobile.png'));check('手机日常页无横向溢出',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
    ledger=json.loads((root/'data/materials.json').read_text(encoding='utf-8'));demo=json.loads((root/'data/resource-demo.json').read_text(encoding='utf-8'));teams=ledger['snapshots'][demo['event_id']]['squads']
    check('交付保留3支到场和1支待到场中队',len(teams)==3 and demo['waiting_squad'] not in teams)
    check('交付保留6家公司',len(ledger['companies'])==6)
    check('脚本无异常',not errors);browser.close()
print(json.dumps(dict(passed=len(checks),errors=errors),ensure_ascii=False))
