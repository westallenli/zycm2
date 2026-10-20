"""Focused regression within the existing isolated root; no delivery data writes."""
from pathlib import Path
import json,os,sys,tempfile,time,unittest
W=Path(__file__).resolve().parents[1]
B=W/'.p00-runs/emergency-sand-20261019'
baseline=len(sys.argv)>1 and sys.argv[1]=='baseline'
final_ui=len(sys.argv)>1 and sys.argv[1]=='final-ui'
source=B/'baseline-source' if baseline else B/'source'
sys.dont_write_bytecode=True
for k in list(os.environ):
    if any(w in k.upper() for w in ('KEY','TOKEN','SECRET','PASSWORD','CREDENTIAL')) or k.upper().endswith('_PROXY') or k in ('PYTHONPATH','PYTHONSTARTUP'):os.environ.pop(k,None)
tempfile.tempdir=str(B/'tmp')
os.chdir(source);sys.path[:0]=[str(source),str(B/'guard')]
from p00_baseline import guard
guard(B)
names=['tests.test_upgrade_emergency_ui','tests.test_upgrade_compact_sop',
       'tests.test_upgrade_formal_task_feedback','tests.test_upgrade_task_relations',
       'tests.test_upgrade_task_review_assessment','tests.test_upgrade_task_material_inputs',
       'tests.test_upgrade_material_supply_draft_page','tests.test_upgrade_command_sop']
if baseline:
    previous=json.loads((B/'focused-tests.json').read_text(encoding='utf-8'))
    names=previous['failed']
if final_ui:names=['tests.test_upgrade_emergency_ui','tests.test_upgrade_compact_sop']
suite=unittest.defaultTestLoader.loadTestsFromNames(names)
started=time.monotonic()
prefix='baseline' if baseline else 'final-ui-tests' if final_ui else 'focused-tests'
with (B/(prefix+'.log')).open('w',encoding='utf-8') as out:
    result=unittest.TextTestRunner(stream=out,verbosity=2).run(suite)
summary=dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
             successful=result.wasSuccessful(),seconds=round(time.monotonic()-started,2),
             failed=[t.id() for t,_ in result.failures+result.errors])
(B/(prefix+'.json')).write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
raise SystemExit(0 if result.wasSuccessful() else 1)
