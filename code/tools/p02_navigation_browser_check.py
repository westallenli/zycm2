"""P02-04 navigation checks on live isolated normal and empty previews."""
import argparse,json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--base',required=True);ap.add_argument('--empty-base',required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--chromium',required=True);a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    checks=[];errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=a.chromium)
        page=browser.new_page(viewport={'width':1440,'height':900});page.on('pageerror',lambda e:errors.append(str(e)))
        page.request.post(a.base+'/login',form={'secret':'p02-local-preview'})
        def ready():page.wait_for_selector('.sand-enhanced')
        def panel(id):
            if page.locator('#float-'+id).is_visible():return
            button=page.locator('[data-sand-toggle="'+id+'"]')
            if not button.is_visible():page.get_by_role('button',name='更多面板',exact=True).click()
            button.click()
        page.goto(a.base+'/sand',wait_until='domcontentloaded');ready();panel('sand-work-detail')
        expect(page.locator('.sand-work-empty')).to_be_visible()
        expect(page.locator('.sand-work-empty')).to_contain_text('从底部九项工作中选择一项')
        checks.append('first open of work navigation has explicit guidance rather than blank content')
        page.wait_for_function('!!window.__cmdMap')
        page.evaluate('window.__navMap=__cmdMap')
        def view():return page.evaluate('JSON.stringify([__cmdMap.getCenter(),__cmdMap.getZoom()])')
        baseline=view()
        for index in range(9):
            page.locator(f'[data-sand-step="{index}"]').click()
            section=page.locator(f'[data-sand-step-content="{index}"]');expect(section).to_be_visible()
            expect(page.locator(f'[data-sand-step="{index}"]')).to_have_attribute('aria-pressed','true')
            if index in (2,6,8):
                target='/review' if index==8 else '/materials'
                with page.expect_navigation(wait_until='domcontentloaded'):section.locator('[data-sand-related]').click()
                assert page.url.endswith(target),page.url
                with page.expect_navigation(wait_until='domcontentloaded'):page.locator('.lz-topbar a[href="/sand"]').click()
                ready();expect(page.locator(f'[data-sand-step-content="{index}"]')).to_be_visible()
                page.wait_for_function('!!window.__cmdMap');baseline=view();page.evaluate('window.__navMap=__cmdMap')
            elif index==7:
                expect(section).to_contain_text('暂无独立信息发布入口')
                assert section.locator('button,a').count()==0
            else:
                link=section.locator('[data-sand-open]');target=link.get_attribute('data-sand-open');link.click()
                box=page.locator('#float-'+target);expect(box).to_be_visible()
                box.get_by_role('button',name='返回工作导航',exact=True).click()
                expect(section).to_be_visible()
                assert view()==baseline and page.evaluate('__navMap===__cmdMap')
        checks.append('all nine work items: real panel round trips, materials/review navigation and scoped return, honest unavailable release entry')
        page.locator('[data-sand-step="3"]').click();page.locator('[data-sand-step-content="3"] [data-sand-open]').click()
        page.locator('#cmd-plans .cmd-noplans a[href="#drawer-selfopt"]').click()
        expect(page.locator('#drawer-selfopt')).to_have_attribute('open','')
        title=page.locator('#drawer-selfopt input[name=title]');title.fill('返回后保留的方案输入')
        page.locator('#float-cmd-plans .sand-work-return').click()
        page.locator('[data-sand-step-content="3"] [data-sand-open]').click()
        assert title.input_value()=='返回后保留的方案输入' and view()==baseline
        page.locator('#cmd-plans .cmd-planctx a').click();expect(page.locator('#float-cmd-advisor')).to_be_visible()
        page.locator('#float-cmd-advisor .sand-work-return').click()
        expect(page.locator('[data-sand-step-content="3"]')).to_be_visible()
        checks.append('empty-plan links open real advisor/nested proposal form; returning retains input and map view')
        # Actual incoming links and same-document hash changes must both open their panel.
        page.goto(a.base+'/sand#qa',wait_until='domcontentloaded');ready()
        expect(page.locator('#float-drawer-qa')).to_be_visible()
        page.evaluate("location.hash='intent'");expect(page.locator('#float-drawer-intent')).to_be_visible()
        page.evaluate("location.hash='sand-work-4'");expect(page.locator('[data-sand-step-content="4"]')).to_be_visible()
        page.evaluate("location.hash='unavailable-target'");expect(page.locator('.sand-nav-notice')).to_be_visible()
        expect(page.locator('.sand-nav-notice')).to_contain_text('不可用')
        page.evaluate("location.hash='drawer-fourcat'");expect(page.locator('.sand-nav-notice')).to_be_hidden()
        checks.append('initial/deferred hashes open nested panels or work items; missing target gives dismissible feedback and valid navigation recovers')
        # Restored navigation must not cross a new login.
        page.goto(a.base+'/sand',wait_until='domcontentloaded');ready();page.locator('[data-sand-step="2"]').click()
        with page.expect_navigation(wait_until='domcontentloaded'):page.locator('[data-sand-related="2"]').click()
        page.request.post(a.base+'/login',form={'secret':'p02-local-preview'})
        page.goto(a.base+'/sand',wait_until='domcontentloaded');ready()
        assert page.locator('[data-sand-step][aria-pressed="true"]').count()==0
        checks.append('navigation return context is isolated by login session')
        page.set_viewport_size({'width':390,'height':844});page.locator('[data-sand-step="3"]').click()
        page.locator('[data-sand-step-content="3"] [data-sand-open]').click()
        expect(page.locator('#float-cmd-plans')).to_be_visible();assert page.locator('.sand-float:visible').count()==1
        page.locator('#float-cmd-plans .sand-work-return').click()
        assert page.locator('.sand-float:visible').count()==1 and page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(a.output/'navigation-mobile.jpg'),type='jpeg',quality=86,full_page=True)
        page.goto(a.base+'/sand?projector=1#qa',wait_until='domcontentloaded');ready()
        assert page.locator('[data-sand-toggle="drawer-qa"]').count()==0
        expect(page.locator('.sand-nav-notice')).to_contain_text('公开屏不可用')
        assert page.locator('[data-sand-step]').count()==0
        checks.append('mobile return uses one panel with no overflow; public screen has no empty private-query entry')
        page.set_viewport_size({'width':1440,'height':900});page.request.post(a.empty_base+'/login',form={'secret':'p02-local-preview'})
        page.goto(a.empty_base+'/sand',wait_until='domcontentloaded');ready();panel('sand-work-detail')
        expect(page.locator('#sand-work-detail')).to_contain_text('选择并进入事件')
        expect(page.locator('#cmd-map-banner')).to_contain_text('无事件坐标')
        panel('cmd-plans');expect(page.locator('#cmd-plans')).to_contain_text('尚无在议方案')
        page.screenshot(path=str(a.output/'empty-plans.jpg'),type='jpeg',quality=86,full_page=True)
        page.locator('#float-cmd-plans .sand-float-head button').click()
        with page.expect_navigation(wait_until='domcontentloaded'):
            panel('sand-work-detail');page.locator('#sand-work-detail a[href="/events"]').click()
        assert page.url.endswith('/events')
        checks.append('no-event/no-record page offers real event entry and honest empty map/plans')
        blocked=browser.new_context();blocked.add_init_script("Storage.prototype.setItem=function(){throw new DOMException('blocked','SecurityError')}")
        bp=blocked.new_page();bp.on('pageerror',lambda e:errors.append(str(e)));bp.request.post(a.base+'/login',form={'secret':'p02-local-preview'})
        bp.goto(a.base+'/sand',wait_until='domcontentloaded');bp.wait_for_selector('.sand-enhanced');bp.locator('[data-sand-step="2"]').click()
        with bp.expect_navigation(wait_until='domcontentloaded'):bp.locator('[data-sand-related="2"]').click()
        assert bp.url.endswith('/materials')
        checks.append('unavailable browser storage does not block real cross-module navigation')
        assert not errors,errors
        (a.output/'verification.json').write_text(json.dumps({'checks':checks,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close();print(json.dumps({'passed':len(checks),'page_errors':errors},ensure_ascii=False))
if __name__=='__main__':main()
