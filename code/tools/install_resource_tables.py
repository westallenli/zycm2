"""Limited UI-source update of the approved independent demo; data stays byte-identical."""
from pathlib import Path
from datetime import datetime
import hashlib,json,shutil

W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/人员物资签到演示_20261017_110916'
E=N/'docs/开发/资源表格与维护入口证据_20261017'
FILES=['panda_brain/resource_pages.py','panda_brain/resource_directory_page.py','panda_brain/resource_directory_tables.py',
       'panda_brain/resource_advice_page.py','panda_brain/panel.py','panda_brain/unified_ui.py',
       'static/resource-directory.js','static/resource-adviser.js','tests/test_upgrade_resource_directory.py']

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def hashes(folder):return {str(p.relative_to(folder)).replace('\\','/'):sha(p) for p in folder.rglob('*') if p.is_file()}

assert R.resolve().is_relative_to((N/'release').resolve())
manifest=json.loads((R/'source-manifest.json').read_text(encoding='utf-8'))
assert all(sha(R/'source'/p)==value for p,value in manifest.items()),'Existing delivery sources changed; retain and inspect first.'
assert all((W/'impl'/p).is_file() for p in FILES)
unexpected=[p for p,value in manifest.items() if p!='config/settings.yaml' and (W/'impl'/p).is_file() and sha(W/'impl'/p)!=value and p not in FILES]
assert not unexpected,unexpected
for name in FILES:assert (R/'source'/name).resolve().is_relative_to((R/'source').resolve())
backup=E/('install-backup-'+datetime.now().strftime('%H%M%S'))
before=hashes(R/'data')
with (R/'process.lock').open('x',encoding='utf-8') as lock:
    lock.write('UI source update');lock.flush()
    try:
        shutil.copytree(R,backup/'root')
        for name in FILES:
            destination=R/'source'/name;destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(W/'impl'/name,destination);manifest[name]=sha(destination)
        temporary=R/'source-manifest.next.json'
        temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(R/'source-manifest.json')
        assert hashes(R/'data')==before,'Data changed during UI update'
        assert all(sha(R/'source'/p)==value for p,value in manifest.items())
        result=dict(delivery=str(R),backup=str(backup),updated=FILES,source_files=len(manifest),data_unchanged=True,data_hashes=before)
        (E/'installation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(dict(updated=len(FILES),source_files=len(manifest),data_unchanged=True,backup=str(backup)),ensure_ascii=False))
    except BaseException:
        if (backup/'root/source-manifest.json').is_file():
            failed=E/('failed-source-'+datetime.now().strftime('%H%M%S'));failed.mkdir()
            for name in FILES:
                dest=R/'source'/name
                if dest.exists():
                    proof=failed/name;proof.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,proof)
                old=backup/'root/source'/name
                if old.exists():shutil.copy2(old,dest)
                elif dest.exists():dest.unlink()
            shutil.copy2(backup/'root/source-manifest.json',R/'source-manifest.json')
        raise
(R/'process.lock').unlink()
