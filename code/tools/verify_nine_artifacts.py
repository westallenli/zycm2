"""Offline source/data/report verification; does not drive or access a browser."""
from pathlib import Path
import hashlib,json,shutil,sys
from html.parser import HTMLParser
W=Path(__file__).resolve().parents[1];N=W.parent;E=N/'docs/开发/九步连贯修复证据_20261017'
B=Path((E/'build-root.txt').read_text(encoding='utf-8'))
sys.dont_write_bytecode=True;sys.path.insert(0,str(B/'source'))
root=B/'artifact-data'
shutil.copytree(B/'delivery-data',root)
reg=root/'registry.json';v=json.loads(reg.read_text(encoding='utf-8'))
for e in v['events'].values():e['snapshot_path']=str(root/'snapshots'/Path(e['snapshot_path']).name)
reg.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
from panda_brain.resource_demo import open_state,DEPUTY
from panda_brain import nine_flow,review_exports,review_reports,formal_task_reads,assistant_view
from panda_brain.panel import render_page
state=open_state(root);token,error=state.member_login(DEPUTY,'123456');assert not error
assert state.workspace.version==112 and not state.workspace.verify_audit_chain()
rows=nine_flow.summary(state);assert len(rows)==9 and all(x['value'] and not x['stale'] for x in rows)
assert [x['done'] for x in rows]==[False,False,False,True,False,False,True,True,True]
report=state.review_ledger.reports['RPT-fire-001-v3'];assert report.stage_report and report.status=='edited'
assert not review_reports.freshness_error(state,report)
out=E/'artifacts';out.mkdir(exist_ok=True)
for fmt in ('md','docx','pdf'):(out/('report-v3.'+fmt)).write_bytes(review_exports.export_version(report,fmt))
from docx import Document
import fitz
text='\n'.join(p.text for p in Document(out/'report-v3.docx').paragraphs)
pdf=fitz.open(out/'report-v3.pdf');pdf_text='\n'.join(page.get_text() for page in pdf)
for needle in ('九步办理与交接','模拟 · 已接收遗留事项','餐食30份','照明4套','32 人','阶段复盘'):
    assert needle in text,needle
    assert needle in pdf_text,needle
assert 'revision-' not in report.content_md
pdf[0].get_pixmap(matrix=fitz.Matrix(1.3,1.3)).save(out/'report-first-page.png')
class Forms(HTMLParser):
    def __init__(self,html):super().__init__();self.forms=[];self.current=None;self.feed(html)
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='form':self.current=dict(action=a.get('action'),names=[],disabled=False)
        if self.current is not None:
            if a.get('name'):self.current['names'].append(a['name'])
            if tag=='fieldset' and 'disabled' in a:self.current['disabled']=True
    def handle_endtag(self,tag):
        if tag=='form' and self.current is not None:self.forms.append(self.current);self.current=None
pages={}
for step in range(9):
    status,html=render_page(state,'/workflow/records',query={'step':[str(step)]},member_token=token)
    assert status==200,(step,status)
    assert rows[step]['status'] in html
    forms=[f for f in Forms(html).forms if f['action']=='/workflow/records']
    assert len(forms)==2 and all(not f['disabled'] and 'csrf' in f['names'] for f in forms)
    assert any('source_digest' in f['names'] for f in forms)
    (out/('step-'+str(step+1)+'.html')).write_text(html,encoding='utf-8')
    pages[step]=dict(status=status,named_forms=len(forms))
for path in ('/materials/event','/workflow/guide','/review'):
    status,html=render_page(state,path,member_token=token)
    assert status==200,(path,status)
    assert 'data-workflow-bar' in html
    if path=='/materials/event':assert all(not f['disabled'] for f in Forms(html).forms if f['action']=='/materials/planning')
    (out/(path.strip('/').replace('/','-')+'.html')).write_text(html,encoding='utf-8')
facts=formal_task_reads.cards(state.workspace,state.ledger)
assert any('本任务当前版已由指定人员核验' in '；'.join(v['facts']) for v in facts.values())
result=dict(event_id=state.active_event_id,audit_count=112,statuses=[x['status'] for x in rows],
    pages=pages,report=report.report_id,pdf_pages=len(pdf),docx_text_length=len(text),pdf_text_length=len(pdf_text),
    original_report_versions=sorted(state.review_ledger.reports),browser_visual='not verified: browser auto-review usage-limit failure',
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()})
(E/'artifact-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k not in {'files','pages'}},ensure_ascii=False))
