"""Read-only rendering profile over an existing isolated rehearsal snapshot."""
from pathlib import Path
import cProfile,json,pstats,sys,time
sys.dont_write_bytecode=True
root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root/'source'));data=root/'data'
from panda_brain.panel import PanelState,render_page
from panda_brain.workspace import ScenarioWorkspace
from panda_brain.auth import GeneralEntryAuth
from panda_brain.persistence import load_workspace
from panda_brain.resource_demo import DUTY,DEPUTY,SECRET
from panda_brain.fire_drill import load_scene
from panda_brain import workbench
from panda_brain.read_scope import read_scope
workbench.SNAPSHOT_DIR=data/'snapshots';auth=GeneralEntryAuth(secret=SECRET)
state=PanelState(ScenarioWorkspace('profile',general_auth=auth),auth=auth,registry_path=data/'registry.json',
    materials_path=data/'materials.json',reviews_path=data/'reviews.json',imagery_path=data/'imagery.json',
    imports_path=data/'imports.json',alarm_inbox_path=data/'alarms.json',upload_dir=data/'uploads',duty_subjects=(DUTY,))
state.active_event_id='fire-001';state.db_path=data/'snapshots/fire-001.db'
state.workspace=load_workspace(state.db_path,general_auth=auth)
state.resource_demo=json.loads((data/'resource-demo.json').read_text(encoding='utf-8'));load_scene(state)
token,error=state.member_login(DEPUTY,'123456');assert not error
profile=cProfile.Profile();profile.enable();started=time.perf_counter()
with state._event_authority_lock,read_scope():code,body=render_page(state,'/sand',member_token=token)
elapsed=time.perf_counter()-started;profile.disable()
print(json.dumps(dict(status=code,characters=len(body),seconds=round(elapsed,4)),ensure_ascii=False),flush=True)
pstats.Stats(profile).sort_stats('cumulative').print_stats(16)
