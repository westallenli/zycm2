"""Audited, event-scoped in-app information requests; never command receipts.

Trusted local policy enables background issuance. HTTP cannot enable it, select a
recipient, confer a role, or write source business records. Hashes detect damage;
they do not authenticate a database administrator who rewrites a whole journal.
"""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import time
from . import sop_assistance as sa


def now(): return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, path):
        self.path=Path(path); self.entries=[]; self.questions={}; self.error=''
        if self.path.exists():
            try:
                if self.path.stat().st_size>16*1024*1024: raise ValueError('问询留痕文件过大，需维护核对。')
                raw=json.loads(self.path.read_text(encoding='utf-8'))
                if set(raw)!={'version','entries'} or raw['version']!=1 or not isinstance(raw['entries'],list):
                    raise ValueError('问询留痕格式不一致。')
                self._replay(raw['entries']); self.entries=raw['entries']
            except (ValueError,KeyError,TypeError,OSError):
                self.error='问询留痕不可核对，保留原文件；自动补问与答复保存已暂停。'

    def _replay(self, entries):
        questions={}; previous=''
        for sequence,e in enumerate(entries,1):
            if set(e)!={'sequence','kind','at','data','previous','sha256'} or type(e['sequence']) is not int:
                raise ValueError('问询留痕结构无效。')
            if e['sequence']!=sequence or e['previous']!=previous or sa.digest({k:v for k,v in e.items() if k!='sha256'})!=e['sha256']:
                raise ValueError('问询留痕校验失败。')
            d=e['data'];qid=d['id']
            if e['kind']=='issued':
                if qid in questions or set(d)!={'id','event_id','key','step','recipient','team','scope','question','basis','kind','version','status','created_at','channel'}:
                    raise ValueError('补问记录无效。')
                if d['status']!='pending' or d['version']!=1 or not d['recipient'] or not d['event_id'] or d['step'] not in range(9):
                    raise ValueError('补问归属无效。')
                questions[qid]=copy.deepcopy(d)
            elif e['kind'] in {'reply','resolved','cancelled'}:
                q=questions[qid]
                if d['expected_version']!=q['version']: raise ValueError('问询版本链不一致。')
                if e['kind']=='reply':
                    if set(d)!={'id','expected_version','by','outcome','content','source','occurred_at','reported_at'} or q['status']!='pending' or d['by']!=q['recipient'] or d['outcome'] not in {'provided','unavailable'}:
                        raise ValueError('回复来源无效。')
                    q.update(status='answered' if d['outcome']=='provided' else 'escalated',reply=copy.deepcopy(d))
                else: q['status']=e['kind']
                q['version']+=1
            else: raise ValueError('未知问询留痕类型。')
            previous=e['sha256']
        self.questions=questions

    def append(self,kind,data):
        if self.error: raise ValueError(self.error)
        old_questions=copy.deepcopy(self.questions)
        entry=dict(sequence=len(self.entries)+1,kind=kind,at=now(),data=data,
                   previous=self.entries[-1]['sha256'] if self.entries else '')
        entry['sha256']=sa.digest(entry); entries=self.entries+[entry]
        self._replay(entries)
        temp=self.path.with_name(self.path.name+'.'+secrets.token_hex(8)+'.tmp')
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with temp.open('x',encoding='utf-8',newline='\n') as f:
                f.write(sa.canonical(dict(version=1,entries=entries)));f.flush();os.fsync(f.fileno())
            os.replace(temp,self.path)
        except OSError:
            self.questions=old_questions
            if temp.exists():temp.unlink()
            raise
        self.entries=entries


def permitted_recipient(state,q):
    eid=q['event_id'];record=state.registry.events.get(eid)
    if not record or record.status not in {'active','upgraded'} or q['recipient'] not in state.registry.library:
        return False
    if q['team']:
        from .task_recipients import recipient_route
        space,_,_=state._appointment_space(eid)
        return recipient_route(space,eid,q['team'],registry=state.registry,ledger=state.ledger).leader_id==q['recipient']
    return any(g.subject_id==q['recipient'] for g in state._duty_grants)


class Runtime:
    def __init__(self,state,*,clock=time.monotonic,interval=3):
        self.state=state;self.clock=clock;self.interval=interval;self.last_tick=None;self.reports={};self.errors={}
        self.policy_path=state.registry_path.with_name('assistant-policy.json')
        self.journal=Journal(state.registry_path.with_name('assistant-followups.json'))
        self.enabled=False;self.policy_error=''
        if self.policy_path.exists():
            try:
                policy=json.loads(self.policy_path.read_text(encoding='utf-8'))
                if policy!={'version':1,'automatic_followups':True,'channel':'in-app'}:
                    raise ValueError('自动补问策略无效。')
                self.enabled=True
            except (OSError,ValueError): self.policy_error='本机自动补问策略不可核对，补问已暂停。'

    def tick(self):
        stamp=self.clock()
        # 数据源没变就不重跑；变了就立刻跑，不必等到 interval。
        from .background_refresh import due as _due
        try:
            if not _due(self, stamp):return
        except Exception:  # noqa: BLE001 — 指纹异常时退回纯时间判断
            if self.last_tick is not None and stamp-self.last_tick<self.interval:return
            self.last_tick=stamp
        state=self.state
        from .read_scope import read_scope
        with state._event_authority_lock, read_scope():
            for eid,record in tuple(state.registry.events.items()):
                if record.status not in {'active','upgraded'} or not record.snapshot_path:continue
                prior=state.workspace,state.active_event_id,state.db_path
                try:
                    space,target,_=state._appointment_space(eid)
                    state.workspace,state.active_event_id,state.db_path=space,eid,target
                    result=sa.report(state,eid)
                    if self.enabled and not self.journal.error:self.synchronize(result)
                    self.reports[eid]=result;self.errors.pop(eid,None)
                except (ValueError,KeyError,TypeError,OSError,RuntimeError) as exc:
                    self.errors[eid]='自动核对暂不可用，旧结果不作为当前依据。'
                    self.reports.pop(eid,None)
                finally:state.workspace,state.active_event_id,state.db_path=prior

    def synchronize(self,result):
        eid=result['event_id'];candidates=result['candidates']
        keys={(q['key'],q['recipient'],q['scope'],q['basis']) for q in candidates}
        for q in tuple(self.journal.questions.values()):
            if q['event_id']!=eid or q['status'] in {'resolved','cancelled'}:continue
            if not permitted_recipient(self.state,q):
                self.journal.append('cancelled',dict(id=q['id'],expected_version=q['version']))
            elif (q['key'],q['recipient'],q['scope'],q['basis']) not in keys:
                if not result['steps'][q['step']]['available']:
                    continue
                if q['key'].startswith('task-feedback:'):
                    oid=q['key'].split(':',1)[1]
                    order=self.state.workspace.orders.get(oid)
                    if order and getattr(order.status,'value',order.status)=='superseded':
                        self.journal.append('cancelled',dict(id=q['id'],expected_version=q['version']))
                        continue
                    if sa.task_sop.flow_issue(self.state.workspace,oid,eid):
                        continue
                # A changed source/version supersedes the old request; a validated
                # absence of the gap resolves it. An unavailable projection does neither.
                replaced=any(c['key']==q['key'] for c in candidates)
                self.journal.append('cancelled' if replaced else 'resolved',dict(id=q['id'],expected_version=q['version']))
        for candidate in candidates:
            if not permitted_recipient(self.state,dict(event_id=eid,**candidate)):continue
            identity=(candidate['key'],candidate['recipient'],candidate['scope'],candidate['basis'])
            matches=[q for q in self.journal.questions.values() if q['event_id']==eid and
                     (q['key'],q['recipient'],q['scope'],q['basis'])==identity]
            if any(q['status'] not in {'resolved','cancelled'} for q in matches):continue
            q=dict(candidate,id='assist-'+sa.digest([eid,identity,len(matches)])[:24],event_id=eid,
                   version=1,status='pending',created_at=now(),channel='系统内补问')
            self.journal.append('issued',q)

    def questions(self,eid,*,recipient=None):
        if self.journal.error:return []
        return [copy.deepcopy(q) for q in self.journal.questions.values() if q['event_id']==eid
                and (recipient is None or q['recipient']==recipient) and permitted_recipient(self.state,q)]

    def reply(self,token,fields):
        from .workflow import context
        expected={'event_id','csrf','question_id','expected_version','outcome','content','source','occurred_at'}
        if set(fields)!=expected or any(len(v)!=1 for v in fields.values()):raise ValueError('补问回复字段无效。')
        values={k:v[0] for k,v in fields.items()};member,record=context(self.state,token)
        if values['event_id']!=record.event_id or values['csrf']!=member.custody_csrf:raise PermissionError('本人会话或事件已变化。')
        sa.checked_workspace(self.state,record.event_id)
        if self.journal.error:raise ValueError(self.journal.error)
        q=self.journal.questions.get(values['question_id'])
        if not q or q['event_id']!=record.event_id or q['recipient']!=member.phone or not permitted_recipient(self.state,q):
            raise PermissionError('只能答复本人本事件的资料补问。')
        if values['expected_version']!=str(q['version']) or q['status']!='pending':raise ValueError('补问版本已变化，请刷新后查看。')
        fresh=sa.report(self.state,record.event_id)
        if not any((c['key'],c['recipient'],c['scope'],c['basis'])==(q['key'],q['recipient'],q['scope'],q['basis']) for c in fresh['candidates']):
            raise ValueError('补问对应的原始资料或任务版本已变化，请等待重新核对。')
        if values['outcome'] not in {'provided','unavailable'}:raise ValueError('答复结果无效。')
        if not 1<=len(values['content'].strip())<=2000 or not 1<=len(values['source'].strip())<=400:
            raise ValueError('请填写资料或无法取得的原因，并注明来源。')
        if values['occurred_at']:
            from .record_contracts import explicit_time
            explicit_time(values['occurred_at'])
        self.journal.append('reply',dict(id=q['id'],expected_version=q['version'],by=member.phone,
            outcome=values['outcome'],content=values['content'].strip(),source=values['source'].strip(),
            occurred_at=values['occurred_at'],reported_at=now()))
        self.last_tick=None
        return q['id']
