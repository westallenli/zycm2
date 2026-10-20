"""P02-05 actual-browser check for shared resource and review shell."""
import argparse,json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--base',required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--chromium',required=True);a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    checks=[];errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=a.chromium)
        page=browser.new_page(viewport={'width':1440,'height':900});page.on('pageerror',lambda e:errors.append(str(e)))
        page.request.post(a.base+'/login',form={'secret':'p02-local-preview'})
        for route,title in [('/materials','人员与物资'),('/review','复盘与经验')]:
            page.goto(a.base+route,wait_until='domcontentloaded')
            expect(page.locator('body')).to_have_class('lz-page lz-work-ui')
            expect(page.locator('.lz-work-context h1')).to_have_text(title)
            expect(page.locator('.lz-work-context')).to_contain_text('P02隔离演示')
            assert page.locator('.lz-topbar > a').count()==5
            expect(page.locator(f'.lz-topbar > a.on[href="{route}"]')).to_be_visible()
            assert page.evaluate('getComputedStyle(document.querySelector(".lz-topbar")).backgroundColor')=='rgb(255, 255, 255)'
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(a.output/(route[1:]+'-desktop.jpg')),type='jpeg',quality=86,full_page=True)
            if route=='/materials':
                expect(page.get_by_text('长期人员库与编制')).to_be_visible()
                expect(page.locator('form[action="/materials/item"]')).to_be_visible()
            else:
                expect(page.locator('.rv-tabs')).to_be_visible()
                with page.expect_navigation(wait_until='domcontentloaded'):page.locator('.rv-tabs a').nth(1).click()
                expect(page.locator('body')).to_have_class('lz-page lz-work-ui')
                expect(page.locator('.lz-work-context h1')).to_have_text(title)
            checks.append(route+' shared light navigation and event context with original business controls')
        page.goto(a.base+'/sand',wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced')
        assert page.locator('body').get_attribute('class')=='lz-page sand-ui'
        expect(page.locator('.sand-context h1')).to_contain_text('P02隔离演示')
        checks.append('sandtable floating shell remains separate and intact')
        page.set_viewport_size({'width':390,'height':844})
        for route in ('/materials','/review'):
            page.goto(a.base+route,wait_until='domcontentloaded')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),route
            expect(page.locator('.lz-work-context h1')).to_be_visible()
            assert page.locator('.lz-topbar a').count()==5
            page.screenshot(path=str(a.output/(route[1:]+'-mobile.jpg')),type='jpeg',quality=85,full_page=True)
        checks.append('resource and review pages fit 390px width with usable navigation')
        page.goto(a.base+'/review?projector=1',wait_until='domcontentloaded')
        assert page.locator('.lz-topbar > a').count()==0
        expect(page.locator('.lz-work-context')).to_contain_text('公共屏 · 仅已发布摘要')
        checks.append('public review keeps no private navigation or event name in shared context')
        assert not errors,errors
        (a.output/'verification.json').write_text(json.dumps({'checks':checks,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close();print(json.dumps({'passed':len(checks),'page_errors':errors},ensure_ascii=False))
if __name__=='__main__':main()
