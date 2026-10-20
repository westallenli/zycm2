"""P02-03 real browser checks against a fresh isolated preview."""
import argparse, json
from pathlib import Path
from playwright.sync_api import sync_playwright, Error

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--base',required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--chromium',required=True);a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    checks=[];errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=a.chromium)
        page=browser.new_page(viewport={'width':1440,'height':900});page.on('pageerror',lambda e:errors.append(str(e)))
        page.request.post(a.base+'/login',form={'secret':'p02-local-preview'})
        def sand():
            page.goto(a.base+'/sand',wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced')
        def panel(id):
            if page.locator('#float-'+id).is_visible():return
            button=page.locator('[data-sand-toggle="'+id+'"]')
            if not button.is_visible():page.get_by_role('button',name='更多面板',exact=True).click()
            button.click()
        def wait_text(form,text):
            from playwright.sync_api import expect
            expect(form.locator('.sand-draft-controls')).to_contain_text(text)
        def click(form):form.locator('button:not([type="button"])').first.click()
        def storage():return page.evaluate('JSON.stringify({...sessionStorage})')
        sand();panel('drawer-measure')
        measure=page.locator('form[action="/sand/measure"]');points=measure.locator('[name=points]')
        points.fill('bad polygon');click(measure);wait_text(measure,'不是 x,y')
        assert points.input_value()=='bad polygon' and not points.is_disabled()
        points.fill('0,0 10,0 10,10 0,10');click(measure);wait_text(measure,'面积约 1,600')
        assert points.input_value()=='0,0 10,0 10,10 0,10'
        page.screenshot(path=str(a.output/'measure-inline.jpg'),type='jpeg',quality=88,full_page=True)
        checks.append('measurement validation preserves input; actual 1600 m2 result stays inline and labelled illustrative')
        page.reload(wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced');panel('drawer-measure')
        assert points.is_disabled();measure.get_by_role('button',name='恢复输入',exact=True).click()
        assert points.input_value()=='0,0 10,0 10,10 0,10'
        with page.expect_navigation(wait_until='domcontentloaded'):measure.locator('button[name=save]').click()
        page.wait_for_selector('.sand-enhanced');panel('drawer-measure')
        assert not measure.get_by_role('button',name='恢复输入',exact=True).is_visible()
        checks.append('measurement input explicitly restores and save acknowledges then clears only its draft')
        panel('drawer-imagery');upload=page.locator('form[action="/sand/image-upload"]')
        upload.locator('[name=source_system]').fill('P02-03 模拟无人机')
        upload.locator('[name=collected_at]').fill('2026-09-26T10:00:00+08:00')
        upload.locator('[name=file]').set_input_files({'name':'invalid.png','mimeType':'image/png','buffer':b'not-an-image'})
        click(upload);wait_text(upload,'JPG/PNG')
        assert upload.locator('[name=file]').evaluate('(el)=>el.files[0].name')=='invalid.png'
        assert upload.locator('[name=source_system]').input_value()=='P02-03 模拟无人机'
        assert 'invalid.png' not in storage() and 'not-an-image' not in storage()
        page.screenshot(path=str(a.output/'upload-failure.jpg'),type='jpeg',quality=88,full_page=True)
        dialogs=[]
        def dismiss(d):dialogs.append(d.type);d.dismiss()
        page.on('dialog',dismiss)
        try:page.reload(wait_until='domcontentloaded',timeout=3000)
        except Error:pass
        assert 'beforeunload' in dialogs, {'dialogs':dialogs,'url':page.url,'files':upload.locator('[name=file]').evaluate('(el)=>el.files.length'),'activation':page.evaluate('navigator.userActivation.hasBeenActive')}
        page.remove_listener('dialog',dismiss);page.once('dialog',lambda d:d.accept())
        page.reload(wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced');panel('drawer-imagery')
        wait_text(upload,'须重新选择文件');upload.get_by_role('button',name='恢复输入',exact=True).click()
        assert upload.locator('[name=source_system]').input_value()=='P02-03 模拟无人机'
        assert upload.locator('[name=file]').evaluate('(el)=>el.files.length')==0
        checks.append('upload rejection retains live file; file bytes/names never stored; leave warns; reload restores metadata only')
        import base64
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB1sAAAAASUVORK5CYII=')
        upload.locator('[name=file]').set_input_files({'name':'synthetic.png','mimeType':'image/png','buffer':png})
        with page.expect_navigation(wait_until='domcontentloaded'):click(upload)
        page.wait_for_selector('.sand-enhanced');panel('drawer-imagery')
        assert not upload.get_by_role('button',name='恢复输入',exact=True).is_visible()
        assert 'P02-03 模拟无人机' in page.locator('#drawer-imagery').inner_text()
        checks.append('actual multipart upload passes session/event gate and appears in current-event ledger')
        panel('drawer-annotate');geo=page.locator('form[action="/sand/annotate-geo"]').first
        geo.locator('[name=geo]').fill('invalid location');click(geo);wait_text(geo,'坐标')
        assert geo.locator('[name=geo]').input_value()=='invalid location'
        assert 'invalid location' not in storage()
        calls=[]
        def drop(route):calls.append(route.request.url);route.abort()
        page.route('**/sand/annotate-geo',drop);click(geo);wait_text(geo,'未确认提交结果')
        assert len(calls)==1
        page.once('dialog',lambda d:d.dismiss());click(geo);assert len(calls)==1
        page.unroute('**/sand/annotate-geo',drop)
        page.once('dialog',lambda d:d.accept());page.reload(wait_until='domcontentloaded');page.wait_for_selector('.sand-enhanced');panel('drawer-annotate')
        assert geo.locator('[name=geo]').input_value()==''
        checks.append('existing-object errors retain live input; network failure never retries; stale object input is not restored after reload')
        # Inject a late-created form to exercise the dynamic-detail binding path explicitly.
        geo.evaluate('(f)=>{const copy=f.cloneNode(true);copy.id="late-form";copy.querySelector(".sand-draft-controls").remove();f.after(copy)}')
        late=page.locator('#late-form');late.locator('[name=geo]').fill('late invalid');click(late);wait_text(late,'坐标')
        assert late.locator('[name=geo]').input_value()=='late invalid'
        late.locator('[name=geo]').fill('')
        checks.append('injected late-created existing-object form receives the same feedback handling')
        page.set_viewport_size({'width':390,'height':844});panel('drawer-imagery')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(a.output/'forms-mobile.jpg'),type='jpeg',quality=88,full_page=True)
        assert not errors,errors
        (a.output/'verification.json').write_text(json.dumps({'checks':checks,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close();print(json.dumps({'passed':len(checks),'page_errors':errors},ensure_ascii=False))
if __name__=='__main__':main()
