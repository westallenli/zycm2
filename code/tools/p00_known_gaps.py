"""Reproduce P00 legacy defects without writing business data."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as N
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'impl'))
from panda_brain.sandtable import answer_inquiry
from panda_brain.review_domain import event_digest

def run():
    ws=N(events=[],annotations={})
    ledger=N(snapshots={'other-event':N(event_id='other-event',squads={'team':N(lines={})})})
    answer=answer_inquiry('物资',ws,event=N(event_id='current-event',updated_at=''),ledger=ledger)
    evs=[N(kind='PersonCheckedIn',sequence=1),N(kind='OrderConfirmed',sequence=2),N(kind='PersonCheckedIn',sequence=3),N(kind='OrderConfirmed',sequence=4)]
    digest={name:value for name,value,_ in event_digest(None,N(events=evs))}
    return {'cross_event_material_fallback':{'reproduced':'snapshot:other-event' in answer.sources,'current_event':'current-event','actual_sources':answer.sources,'actual_answer':answer.conclusion,'expected':'No other-event source when current-event snapshot is absent','next_batch':'P01-01'},'first_milestone_uses_last':{'reproduced':digest['前指成立/首批到场']=='审计序列 #3' and digest['首批命令']=='审计序列 #4','actual_arrival':digest['前指成立/首批到场'],'expected_arrival':'审计序列 #1','actual_order':digest['首批命令'],'expected_order':'审计序列 #2','next_batch':'P11'}}

if __name__=='__main__':
    print(json.dumps(run(),ensure_ascii=False,indent=2))
