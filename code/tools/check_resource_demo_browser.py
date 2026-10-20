"""Exercise demo-only CRUD and actual scan/confirm. Leaves evidence, never resets data."""
import json,sys
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(sys.argv[1]);port=sys.argv[2];base='http://127.0.0.1:'+port
sys.path.insert(0,str(root/'source'))
from panda_brain.resource_directory_page import xlsx,columns
checks=[];errors=[]
def check(label,value):
    checks.append(dict(label=label,passed=bool(value)))
    (root/'browser-verified.json').write_text(json.dumps(dict(checks=checks,errors=errors),ensure_ascii=False,indent=2),encoding='utf-8')
    if not value:raise AssertionError(label)
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True,args=['--disable-gpu'])
    context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page()
    page.on('pageerror',lambda e:errors.append(str(e)))
    def login(phone):
        context.clear_cookies();page.goto(base+'/login');page.locator('[name=account]').fill(phone);page.locator('[name=password]').fill('123456');page.locator('.login-submit').click();page.wait_for_load_state()
    page.goto(base+'/login');check('登录页给出演示账号',page.get_by_text('13900000031',exact=True).count()==1)
    page.screenshot(path=str(root/'verified-login.png'))
    login('13900000120');page.goto(base+'/materials/event')
    check('到场32人含30名中队成员及2名后勤',page.locator('[data-resource-arrived]').get_attribute('data-resource-arrived')=='32')
    for label in ['应急餐食 · 当前缺 30 份','照明灯组 · 当前缺 4 套','饮用水 · 预计还需补 120 瓶']:
        check(label,page.get_by_role('heading',name=label,exact=True).count()==1)
    check('饮水提前两小时提醒','约 2 小时后不足' in page.inner_text('body'))
    check('物资库和外协有具体联系人','演示东岭前置物资库' in page.inner_text('body') and '演示暖食保障公司' in page.inner_text('body'))
    page.screenshot(path=str(root/'verified-event-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(root/'verified-event-mobile.png'))
    check('手机事件页无横向溢出',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
    page.set_viewport_size({'width':1440,'height':1000});page.goto(base+'/')
    check('人员框架有扫码演示',page.get_by_role('heading',name='人员与扫码签到演示',exact=True).count()==1)
    check('人员框架不再暴露旧维护入口',page.locator('[data-org-url^="/personnel/arrangements"],a[href="/personnel/arrangements"]').count()==0)
    page.screenshot(path=str(root/'verified-personnel-desktop.png'))
    login('13900000031');page.goto(base+'/personnel/arrival')
    form=page.locator('form[action="/captain-arrival/scan"]');check('待到队长有扫码表单',form.count()==1)
    qr=page.locator('svg[aria-label^="事件二维码"]').first.get_attribute('aria-label').split()[-1]
    form.locator('[name=qr_id]').fill(qr);form.get_by_role('button').click();page.wait_for_load_state()
    check('扫码后尚未计到场','名册已载入，尚未确认任何人到场' in page.inner_text('body'))
    before=json.loads((root/'data/materials.json').read_text(encoding='utf-8'))
    sid=json.loads((root/'data/resource-demo.json').read_text(encoding='utf-8'))['waiting_squad']
    eid=json.loads((root/'data/resource-demo.json').read_text(encoding='utf-8'))['event_id']
    check('仅扫码没有物资快照',sid not in before['snapshots'][eid]['squads'])
    form=page.locator('form[action="/captain-arrival/report"]')
    form.get_by_role('button',name='确认本队到场并一次计入随队清单').click();page.wait_for_load_state()
    check('确认整队10人到场','队长已申报到场 10/10 人' in page.inner_text('body'))
    after=json.loads((root/'data/materials.json').read_text(encoding='utf-8'));squad=after['snapshots'][eid]['squads'][sid]
    check('签到名册与到场一致',len(squad['arrived_phones'])==10)
    check('同次自动带入三种标配',len(squad['lines'])==3)
    page.reload();again=json.loads((root/'data/materials.json').read_text(encoding='utf-8'))
    check('刷新不重复到场或标配',after==again)
    page.screenshot(path=str(root/'verified-arrival-desktop.png'))
    login('13900000120');page.goto(base+'/materials/event')
    check('火情页立即同步42人',page.locator('[data-resource-arrived]').get_attribute('data-resource-arrived')=='42')
    check('新到队伍标配显示',page.get_by_text('示范甲市应急救援中心二中队',exact=True).count()>=1)
    login('13900000119');page.goto(base+'/materials/daily')
    check('120人日常库','共 120 人' in page.inner_text('body'))
    page.screenshot(path=str(root/'verified-daily-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(root/'verified-daily-mobile.png'))
    check('手机日常页无横向溢出',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
    page.set_viewport_size({'width':1440,'height':1000})
    company=page.locator('#company');company.locator('summary').filter(has_text='文件导入 / 下载模板').click()
    form=company.locator('form[enctype="multipart/form-data"]')
    file=root/'browser-company.xlsx';file.write_bytes(xlsx(columns('company'),[['演示浏览器新公司','饮用水','示范甲市','演示联系人','13900009999','虚构浏览器导入测试','2026-10-17T10:00:00+08:00']]))
    form.locator('[name=file]').set_input_files(str(file));form.locator('[name=basis]').fill('虚构Excel导入测试');form.get_by_role('button',name='上传并预览',exact=True).click();page.wait_for_load_state()
    check('Excel导入有预览','导入预览' in page.inner_text('body') and '演示浏览器新公司' in page.inner_text('body'))
    check('预览没有写入公司',len(json.loads((root/'data/materials.json').read_text(encoding='utf-8'))['companies'])==6)
    page.get_by_role('button',name='确认导入',exact=True).click();page.wait_for_load_state()
    check('确认后外协名录新增',len(json.loads((root/'data/materials.json').read_text(encoding='utf-8'))['companies'])==7)
    company=page.locator('#company');detail=company.locator('details').filter(has=page.locator('summary',has_text='编辑 演示浏览器新公司'))
    detail.locator('summary').first.click();edit=detail.locator('form').first
    edit.locator('[name=services]').fill('饮用水、照明灯组');edit.locator('[name=basis]').fill('虚构公司编辑测试');edit.get_by_role('button').click();page.wait_for_load_state()
    check('公司可编辑','饮用水、照明灯组' in page.locator('#company').inner_text())
    detail=page.locator('#company details').filter(has=page.locator('summary',has_text='编辑 演示浏览器新公司'));detail.locator('summary').first.click()
    disable=detail.locator('form').last;disable.locator('[name=basis]').fill('模拟删除采用停用保留历史');disable.get_by_role('button').click();page.wait_for_load_state()
    ledger=json.loads((root/'data/materials.json').read_text(encoding='utf-8'))
    check('删除采用有记录停用',any(not v['enabled'] for k,v in ledger['resource_metadata']['directory_status'].items() if k.startswith('companies:')))
    check('页面脚本无异常',not errors)
    browser.close()
(root/'browser-verified.json').write_text(json.dumps(dict(checks=checks,errors=errors),ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(passed=len(checks),errors=errors),ensure_ascii=False))
