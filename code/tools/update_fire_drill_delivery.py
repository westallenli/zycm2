"""Install only verified source differences after normal delivery shutdown."""
from pathlib import Path
import hashlib,json,shutil,sys
W=Path(__file__).resolve().parents[1];N=W.parent
E=N/'docs/开发/灭火场景恢复与性能证据_20261017'
B=Path((E/'build-root.txt').read_text(encoding='utf-8'))
R=Path((E/'delivery-root.txt').read_text(encoding='utf-8'))
assert R.resolve().is_relative_to((N/'release').resolve()) and not (R/'process.lock').exists()
assert json.loads((R/'last-exit.json').read_text(encoding='utf-8'))['normal']
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
old=json.loads((R/'source-manifest.json').read_text(encoding='utf-8'))
source={name:sha(B/'source'/name) for name in old}
assert all(name=='config/settings.yaml' or sha(W/'impl'/name)==digest for name,digest in source.items())
changed=[name for name in source if source[name]!=old[name]]
map_read='--map-read' in sys.argv
expected={'panda_brain/online_imagery.py'} if map_read else {'panda_brain/formal_task_reads.py','panda_brain/map_brief.py','panda_brain/command_sop.py','panda_brain/review_flow_view.py','panda_brain/sandview.py','tests/test_upgrade_fire_drill.py'}
assert set(changed)==expected,changed
backup=E/('delivery-map-read-backup' if map_read else 'delivery-view-backup');backup.mkdir()
shutil.copy2(R/'source-manifest.json',backup/'source-manifest.json')
for name in changed:
    destination=backup/'source'/name;destination.parent.mkdir(exist_ok=True,parents=True)
    shutil.copy2(R/'source'/name,destination)
    shutil.copy2(B/'source'/name,R/'source'/name)
(R/'source-manifest.json').write_text(json.dumps(source,ensure_ascii=False,indent=2),encoding='utf-8')
assert all(sha(R/'source'/name)==digest for name,digest in source.items())
(E/('delivery-map-read-update.json' if map_read else 'delivery-source-update.json')).write_text(json.dumps(dict(root=str(R),backup=str(backup),changed=changed,source_manifest_sha256=sha(R/'source-manifest.json')),ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(changed=changed,backup=str(backup)),ensure_ascii=False))
