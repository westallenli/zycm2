"""Actual-browser checks for P02-02 draft continuity. Uses isolated preview only."""
import argparse,json
from pathlib import Path
from playwright.sync_api import sync_playwright, Error


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--base',required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--chromium',required=True);a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    checks=[];errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=a.chromium)
        context=browser.new_context(viewport={'width':1440,'height':900});page=context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.request.post(a.base+'/login',form={'secret':'p02-local-preview'})
        def sand():
            page.goto(a.base+'/sand',wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced')
        def toggle(id):
            button=page.locator('[data-sand-toggle="'+id+'"]')
            if not button.is_visible():page.get_by_role('button',name='更多面板',exact=True).click()
            button.click()
        def open_panel(id):
            if not page.locator('#float-'+id).is_visible():toggle(id)
        def restore(form):
            form.get_by_role('button',name='恢复输入',exact=True).click()
        def abandon(form):
            page.once('dialog',lambda d:d.accept());form.get_by_role('button',name='放弃输入草稿',exact=True).click()
        def submit(form):
            form.locator('button:not([type="button"])').first.click()
        sand();open_panel('cmd-advisor')
        goal=page.locator('#cmd-advisor form[action="/sand/goal"]');ask=page.locator('#cmd-advisor form[action="/sand/ask"]')
        goal.locator('[name=statement]').fill('P02 草稿目标');ask.locator('[name=question]').fill('当前物资情况')
        assert '2 份' in page.locator('.sand-draft-summary').inner_text()
        toggle('cmd-advisor');toggle('cmd-advisor');assert goal.locator('[name=statement]').input_value()=='P02 草稿目标'
        page.reload(wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced');open_panel('cmd-advisor')
        assert goal.locator('[name=statement]').is_disabled()
        assert ask.locator('[name=question]').input_value()==''
        restore(goal);assert goal.locator('[name=statement]').input_value()=='P02 草稿目标'
        assert ask.locator('[name=question]').is_disabled()
        checks.append('two drafts survive reload; explicit restore affects one form only')
        page.goto(a.base+'/materials',wait_until='domcontentloaded');sand();open_panel('cmd-advisor');restore(ask)
        assert ask.locator('[name=question]').input_value()=='当前物资情况'
        checks.append('draft survives real cross-module navigation within the same tab')
        with page.expect_navigation(wait_until='domcontentloaded'):submit(ask)
        page.wait_for_selector('.sand-enhanced');open_panel('cmd-advisor')
        assert not ask.locator('[name=question]').is_disabled()
        assert not ask.get_by_role('button',name='恢复输入',exact=True).is_visible()
        assert goal.get_by_role('button',name='恢复输入',exact=True).is_visible()
        assert '当前物资情况' in page.locator('#drawer-qa').text_content(), page.locator('#drawer-qa').text_content()
        checks.append('real successful POST clears only its own draft; another draft remains')
        restore(goal);goal.locator('[name=statement]').fill('   ');submit(goal)
        page.wait_for_function("document.querySelector('form[action=\"/sand/goal\"] .sand-draft-controls').textContent.includes('不能为空')")
        assert goal.locator('[name=statement]').input_value()=='   '
        assert not goal.locator('[name=statement]').is_disabled()
        checks.append('server validation error stays beside form and preserves input')
        abandon(goal);assert goal.locator('[name=statement]').input_value()==''
        page.reload(wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced');open_panel('cmd-advisor')
        assert not goal.get_by_role('button',name='恢复输入',exact=True).is_visible()
        checks.append('explicit discard clears this input and it does not return on reload')
        # A dropped response must never cause an automatic duplicate business request.
        calls=[]
        def drop(route):calls.append(route.request.url);route.abort()
        page.route('**/sand/goal',drop)
        goal.locator('[name=statement]').fill('网络中断待核对');submit(goal)
        page.wait_for_function("document.querySelector('form[action=\"/sand/goal\"] .sand-draft-controls').textContent.includes('未确认提交结果')")
        assert len(calls)==1
        page.unroute('**/sand/goal',drop);page.reload(wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced');open_panel('cmd-advisor')
        assert '上次提交结果未确认' in goal.locator('.sand-draft-controls').inner_text()
        abandon(goal)
        checks.append('network uncertainty retains inputs, warns on return and performs no automatic retry')
        # Draw a real point; hidden geometry is part of the recoverable draft.
        open_panel('sand-tools')
        group=page.locator('.cmd-toolgroup').filter(has=page.locator('#cmd-draw-point'))
        group.locator('[data-group-tab]').click();page.locator('#cmd-draw-point').click()
        toggle('sand-tools');page.locator('#cmd-map').click(position={'x':650,'y':280})
        anno=page.locator('#sand-anno-form');page.wait_for_selector('#float-cmd-anno-float:not([hidden])')
        anno.locator('[name=title]').fill('仅供恢复验证的标绘')
        geo=anno.locator('[name=geo]').input_value();assert geo
        kind=anno.locator('[name=kind]').input_value()
        page.reload(wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced');open_panel('cmd-anno-float')
        restore(anno)
        assert anno.locator('[name=geo]').input_value()==geo
        assert anno.locator('[name=kind]').input_value()==kind
        assert anno.locator('[name=title]').input_value()=='仅供恢复验证的标绘'
        assert '已恢复未提交坐标' in page.locator('#cmd-anno-preview').inner_text()
        abandon(anno)
        checks.append('real drawing coordinates, kind and text survive reload without submitting a map object')
        # Reauthentication changes the namespace even in the same browser tab.
        open_panel('cmd-advisor');ask.locator('[name=question]').fill('旧登录会话草稿')
        page.request.post(a.base+'/login',form={'secret':'p02-local-preview'})
        sand();open_panel('cmd-advisor');assert not ask.get_by_role('button',name='恢复输入',exact=True).is_visible()
        assert ask.locator('[name=question]').input_value()==''
        checks.append('new login does not inherit prior-session drafts')
        ask.locator('[name=question]').fill('截图草稿')
        page.reload(wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced');open_panel('cmd-advisor')
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(a.output/'draft-restore.jpg'),type='jpeg',quality=88,full_page=True)
        page.set_viewport_size({'width':390,'height':844});open_panel('cmd-advisor')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(a.output/'draft-mobile.jpg'),type='jpeg',quality=88,full_page=True)
        page.goto(a.base+'/sand?projector=1',wait_until='domcontentloaded')
        assert page.locator('.sand-draft-controls').count()==0
        checks.append('public projector renders no draft content or controls; mobile has no page overflow')
        # Storage denial must preserve the original leave-page warning.
        blocked=browser.new_context(viewport={'width':1440,'height':900})
        blocked.add_init_script("Storage.prototype.setItem=function(){throw new DOMException('blocked','SecurityError')}")
        bp=blocked.new_page();bp.request.post(a.base+'/login',form={'secret':'p02-local-preview'});bp.goto(a.base+'/sand',wait_until='domcontentloaded');bp.wait_for_selector('.sand-enhanced')
        bp.locator('[data-sand-toggle="cmd-advisor"]').click();bp.locator('#cmd-advisor form[action="/sand/ask"] input[name=question]').fill('不能保存时留页')
        assert '不可用' in bp.locator('.sand-draft-summary').inner_text()
        dialogs=[]
        def dismiss(d):dialogs.append(d.type);d.dismiss()
        bp.on('dialog',dismiss)
        try:bp.goto(a.base+'/materials',wait_until='domcontentloaded')
        except Error:pass
        assert 'beforeunload' in dialogs and '/sand' in bp.url
        checks.append('blocked storage reports failure and keeps unsaved-input leave-page protection')
        assert not errors,errors
        result={'passed':checks,'pageErrors':errors,'scope':'seven new-entry form actions; same tab and same login, no browser restart or file recovery'}
        (a.output/'draft-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
        print(json.dumps(result,ensure_ascii=False));browser.close()


if __name__=='__main__':main()
