"""Browser acceptance for the real P02 /sand page using the local synthetic preview."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8767')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks = []; errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=args.chromium)
        page = browser.new_page(viewport={'width':1440,'height':900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.request.post(args.base+'/login', form={'secret':'p02-local-preview'})
        page.goto(args.base+'/sand', wait_until='domcontentloaded')
        page.wait_for_function('!!window.__cmdMap && !!document.querySelector(".sand-enhanced")')
        page.evaluate('window.__savedMap=window.__cmdMap;window.__savedMapNode=document.querySelector("#cmd-map")')
        def view():
            return page.evaluate('({lat:__cmdMap.getCenter().lat,lng:__cmdMap.getCenter().lng,zoom:__cmdMap.getZoom(),width:document.querySelector("#cmd-map").clientWidth})')
        def toggle(id):
            button=page.locator('[data-sand-toggle="'+id+'"]')
            if not button.is_visible():
                page.get_by_role('button',name='更多面板',exact=True).click()
            button.click()
        def close_all():
            if not page.get_by_role('button',name='收起全部面板',exact=True).is_visible():
                page.get_by_role('button',name='更多面板',exact=True).click()
            page.get_by_role('button',name='收起全部面板',exact=True).click()
        assert page.locator('.sand-dock [data-sand-toggle]').count()==6
        assert page.locator('.sand-dock [data-sand-toggle] svg').count()==6
        assert page.locator('.sand-context h1').inner_text()
        assert page.locator('.sand-brand').inner_text().startswith('破局智援参谋')
        assert page.locator('.sand-app-nav a').count()==5
        assert page.locator('#float-cmd-situation #cmd-situation').count()==1
        assert page.locator('#cmd-advisor #cmd-situation').count()==0
        checks.append('reviewed visual shell: white header, event heading and six primary icon panels; existing extra actions retained')
        close_all()
        old=view()
        page.mouse.move(850,470);page.mouse.down();page.mouse.move(970,530,steps=8);page.mouse.up()
        page.wait_for_function('(a)=>Math.abs(__cmdMap.getCenter().lng-a.lng)>.0001',arg=old)
        page.locator('.leaflet-control-zoom-in').click()
        page.wait_for_function('(n)=>__cmdMap.getZoom()>n',arg=old['zoom'])
        checks.append('real Leaflet drag and zoom')
        close_all()
        page.locator('.leaflet-marker-icon').first.click()
        page.wait_for_selector('#float-cmd-bottom:not([hidden])')
        selected=page.locator('#cmd-bottom-info').inner_text();assert '演示' in selected
        stable=view()
        toggle('cmd-bottom');toggle('cmd-bottom')
        assert page.locator('#cmd-bottom-info').inner_text()==selected
        assert view()==stable
        checks.append('map object click opens evidence; minimize preserves selection and view')
        close_all();toggle('sand-layers')
        layer=page.locator('[data-layer-group]').first
        layer.set_checked(not layer.is_checked())
        layer_value=layer.is_checked()
        toggle('sand-layers');toggle('sand-layers')
        assert layer.is_checked()==layer_value and view()==stable
        layer.set_checked(not layer_value)
        close_all();toggle('sand-event-summary')
        box=page.locator('#float-sand-event-summary')
        before=box.bounding_box();head=box.locator('.sand-float-head').bounding_box()
        page.mouse.move(head['x']+50,head['y']+20);page.mouse.down()
        page.mouse.move(head['x']+170,head['y']+60,steps=8);page.mouse.up()
        moved=box.bounding_box();assert moved['x']>before['x']+90
        toggle('sand-event-summary');toggle('sand-event-summary')
        assert box.bounding_box()==moved and view()==stable, {'before':before,'moved':moved,'restored':box.bounding_box(),'map':view(),'expectedMap':stable}
        checks.append('layer selection and dragged panel position survive collapse without panning')
        close_all()
        toggle('cmd-advisor')
        question=page.locator('#cmd-advisor form[action="/sand/ask"] input[name=question]')
        question.fill('当前物资情况')
        toggle('cmd-advisor');toggle('cmd-advisor')
        assert question.input_value()=='当前物资情况'
        assert view()==stable
        page.locator('[data-sand-step="3"]').click()
        page.locator('[data-sand-step-content="3"] [data-sand-open]').click()
        assert page.locator('#float-cmd-plans').is_visible()
        assert view()==stable
        assert page.evaluate('__savedMap===__cmdMap && __savedMapNode===document.querySelector("#cmd-map")')
        checks.append('SOP opens real plan panel; form/map instances preserved')
        close_all();toggle('drawer-annotate')
        unmanaged=page.locator('form[action="/sand/annotate-geo"] input[name=geo]').first;unmanaged.fill('0,0 10,0 10,10')
        dialogs=[]
        def dismiss(dialog):
            dialogs.append(dialog.type);dialog.dismiss()
        page.on('dialog',dismiss)
        page.evaluate("location.href='/materials'")
        page.wait_for_function("location.pathname==='/sand'")
        assert 'beforeunload' in dialogs
        checks.append('dirty draft blocks leaving when browser confirmation is dismissed')
        # A second dirty form must not be silently lost when submitting another one.
        close_all();toggle('cmd-advisor')
        goal=page.locator('#cmd-advisor input[name=statement]');goal.fill('未提交目标')
        page.locator('#cmd-advisor form[action="/sand/ask"] button:not([type="button"])').click()
        assert 'confirm' in dialogs and question.input_value()=='当前物资情况'
        goal.fill('')
        close_all();toggle('drawer-annotate');unmanaged.fill('')
        close_all();toggle('cmd-advisor')
        checks.append('submitting another form asks before discarding unrelated dirty inputs')
        # Real read-only business query submission, not a fake response.
        with page.expect_navigation(wait_until='domcontentloaded'):
            page.locator('#cmd-advisor form[action="/sand/ask"] button:not([type="button"])').click()
        page.wait_for_selector('.sand-enhanced')
        assert page.locator('#drawer-qa').text_content().find('当前物资情况')>=0
        checks.append('existing /sand/ask POST round trip succeeds')
        close_all()
        stable=view()
        page.evaluate("const n=document.getElementById('cmd-sit-live');n.hidden=false;n.textContent='验证注入：新的待复核变化';")
        page.wait_for_selector('.sand-update-notice:not([hidden])')
        assert page.locator('.sand-float:visible').count()==0 and view()==stable
        checks.append('injected passive notice updates summary without opening/panning')
        page.evaluate("document.getElementById('cmd-sit-live').hidden=true")
        viewport_checks=[]
        for width,height in [(1920,1080),(1440,900),(1366,768),(390,844)]:
            page.set_viewport_size({'width':width,'height':height})
            page.wait_for_function('document.querySelector(".sand-workflow").getBoundingClientRect().bottom<=innerHeight')
            close_all()
            for id in ['cmd-advisor','cmd-plans','sand-layers','drawer-annotate']:
                toggle(id)
                fit=page.evaluate('''() => {const r=document.querySelector('#cmd-page').getBoundingClientRect(),m=document.querySelector('#cmd-map').getBoundingClientRect(),s=document.querySelector('.sand-workflow').getBoundingClientRect();return {noOverflow:document.documentElement.scrollWidth<=innerWidth,mapFull:Math.abs(r.width-m.width)<2,panelsFit:[...document.querySelectorAll('.sand-float:not([hidden])')].every(n=>n.getBoundingClientRect().bottom<s.top),stepsVisible:s.bottom<=innerHeight}}''')
                assert all(fit.values()),(width,id,fit)
            viewport_checks.append({'size':[width,height],**fit})
        page.set_viewport_size({'width':1440,'height':900});close_all();toggle('sand-event-summary')
        page.screenshot(path=str(args.output/'formal-sand.jpg'),type='jpeg',quality=86,full_page=True)
        # Missing CDN must retain usable navigation and honest map-unavailable messaging.
        offline=browser.new_page(viewport={'width':1366,'height':768})
        offline.request.post(args.base+'/login',form={'secret':'p02-local-preview'})
        offline.route('https://**/*',lambda route:route.abort())
        offline.goto(args.base+'/sand',wait_until='domcontentloaded')
        offline.wait_for_selector('.sand-enhanced')
        assert '未能加载' in offline.locator('#cmd-map-banner').inner_text()
        offline.locator('[data-sand-toggle="cmd-advisor"]').click()
        assert offline.locator('#float-cmd-advisor').is_visible()
        checks.append('CDN unavailable: clear error and business panels remain accessible')
        page.goto(args.base+'/sand?projector=1',wait_until='domcontentloaded')
        assert page.locator('[data-sand-step]').count()==0
        assert page.locator('form[action="/sand/ask"]').count()==0
        checks.append('public projector adds no workflow/action controls')
        assert not errors,errors
        result={'passed':checks,'viewports':viewport_checks,'pageErrors':errors,
                'scope':'P02-01 shell and existing read-only query, not new SOP rules or full business acceptance'}
        (args.output/'browser-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
        print(json.dumps(result,ensure_ascii=False))
        browser.close()


if __name__=='__main__':
    main()
