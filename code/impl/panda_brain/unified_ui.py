"""One business framework. View selection never grants write authority."""
from html.parser import HTMLParser
from html import escape
from urllib.parse import urlsplit
import re
from .workbench import esc
from . import business_modules as business

# Paths identify children of the original five pages, not role workbenches.
CHILDREN = {
    '/': (('/personnel/callouts','选队召集与回执'),('/personnel/arrival','到场确认'),
          ('/personnel/arrangements','岗位与人员安排'),('/personnel/temporary','临时人员与编组'),
          ('/personnel/groups','编组与调组'),('/personnel/professionals','职责指定')),
    '/sand': (('/sand/task-review','任务分类与复核'),('/sand/task-release','任务发布'),
              ('/sand/task-receipts','任务接收'),('/sand/task-feedback','执行反馈'),
              ('/sand/offline-tasks','线下指令'),('/sand/task-materials','任务物资依据'),('/sand/supply-draft','补给任务附件')),
    '/materials': (('/materials/transfers','调拨'),('/materials/supply','补给联系'),
                   ('/materials/supply-receipts','补给实收'),('/materials/supply-execution','保障执行'),
                   ('/materials/records','长期资料'),('/materials/closeout','收尾核实'),
                   ('/materials/custody','撤离接收与结清'),('/materials/accounts','底账调整'),('/materials/maintenance-authority','长期维护授权')),
    '/events': (), '/review': (),
}
ALIASES = {
    '/dispatch':'/personnel/callouts', '/callouts':'/personnel/callouts',
    '/captain-arrival':'/personnel/arrival', '/personnel/manage':'/personnel/arrangements',
    '/temporary/assign':'/personnel/temporary', '/temporary/leader':'/personnel/temporary',
    '/temporary':'/personnel/temporary', '/dispatch/professionals':'/personnel/professionals',
    '/dispatch/task-review':'/sand/task-review', '/tasks/release':'/sand/task-release',
    '/tasks/formal':'/sand/task-receipts', '/tasks/feedback':'/sand/task-feedback',
    '/tasks/relations':'/sand/task-feedback', '/tasks/offline':'/sand/offline-tasks',
    '/task-materials':'/sand/task-materials', '/supply-draft':'/sand/supply-draft',
    '/material-custody':'/materials/custody', '/material-transfers':'/materials/transfers',
    '/material-supply':'/materials/supply', '/material-supply/receipts':'/materials/supply-receipts',
    '/material-supply/execution':'/materials/supply-execution',
}
LEGACY = {v:k for k,v in ALIASES.items()}
LEGACY.update({'/personnel/callouts':'/dispatch','/personnel/temporary':'/temporary/assign',
               '/sand/task-feedback':'/tasks/feedback'})
CHILD_LABELS = {path:label for children in CHILDREN.values() for path,label in children}
SAND_COMMANDS = frozenset('/sand/'+path for path in (
    'goal','confirm-goal','gap-defer','select-plan','adopt','decide','option','pause',
    'annotate','annotate-geo','annotate-status','annotate-edit','conflict-resolve',
    'review-current','change-action','measure','image-upload','image-status','image-analyze',
    'image-decision','ai-candidate','intent','candidate','draft-confirm','draft-discard',
    'restaff-sand','self-option','plan-decision','task-draft'))


def parent(route):
    route = ALIASES.get(route, route)
    if route in business.MODULES:
        return route
    for main, children in CHILDREN.items():
        if route in dict(children):
            return main
    if route.startswith('/events/'):
        return '/events'
    if route.startswith('/review/'):
        return '/review'
    return ''


def can_read(state, token):
    return bool(business.role(state,token) and state.active_event_id in business.events(state,token))


def deputy_command(state, token):
    return bool(token and state.active_event_id in state.deputy_events(token))


def can_post(state, token, action):
    role = business.role(state,token)
    if action == '/logout/member':return bool(state.member_session(token))
    if action == '/materials/planning':
        from .resource_advisor import require_planner
        try:require_planner(state,token,state.active_event_id);return True
        except PermissionError:return False
    if action == '/workflow/mode':
        from .workflow import closed_event
        return bool(role or closed_event(state,token))
    if action in {'/logout/member','/events/select','/workflow/mode','/workflow/assistance-reply','/workflow/records'}:
        return bool(role)
    if action in {'/events/action','/events/appointment','/events/alarm/simulate','/events/deputy-select','/events/local-notices/prepare'}:
        return role == 'duty'
    if action in {'/callouts/reply'} or action.startswith('/captain-arrival'):
        return role == 'captain'
    if action == '/tasks/release':
        return bool(state.member_session(token) and state.task_release_views(token))
    if action in {'/tasks/formal','/tasks/relations','/tasks/feedback'}:
        return role in {'deputy','captain'}
    if action in {'/sand/ask','/sand/source-check','/sand/intent','/sand/observe','/review/ask'}:
        return bool(role)
    if action.startswith('/review/'):
        from .review_meeting import is_owner
        return is_owner(state,token) or (role == 'deputy' and action == '/review/owner')
    if action.startswith(('/sand/','/dispatch/','/personnel/','/temporary/','/material-','/tasks/','/imports/')):
        return role == 'deputy'
    return False


class FormGate(HTMLParser):
    """Keep identical read content; disable controls the current actor cannot use."""
    def __init__(self,state,token):
        super().__init__(convert_charrefs=False)
        self.state,self.token,self.out,self.forms = state,token,[],[]
    def handle_starttag(self,tag,attrs):
        values = dict(attrs)
        if tag == 'a' and values.get('href'):
            target = urlsplit(values['href'])
            if target.path in ALIASES:
                values['href'] = ALIASES[target.path] + ('?'+target.query if target.query else '') + ('#'+target.fragment if target.fragment else '')
                attrs = list(values.items())
        self.out.append('<'+tag+''.join(' '+k+('="'+escape(v,quote=True)+'"' if v is not None else '') for k,v in attrs)+'>')
        if tag == 'form':
            action = values.get('action','')
            disabled = values.get('method','get').lower() == 'post' and not can_post(self.state,self.token,action)
            self.forms.append(disabled)
            if disabled:
                self.out.append('<fieldset disabled style="border:0;padding:0;margin:0;min-width:0"><p class="lz-note">本项由对应职责人员办理，当前账号可查看。</p>')
            if action in SAND_COMMANDS and values.get('method','get').lower() == 'post':
                member = self.state.member_session(self.token)
                self.out.append('<input type="hidden" name="_business_csrf" value="'+esc(member.custody_csrf)+'">')
    def handle_endtag(self,tag):
        if tag == 'form' and self.forms and self.forms.pop():
            self.out.append('</fieldset>')
        self.out.append('</'+tag+'>')
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:
            self.handle_endtag(tag)
    def handle_data(self,data): self.out.append(data)
    def handle_entityref(self,name): self.out.append('&'+name+';')
    def handle_charref(self,name): self.out.append('&#'+name+';')
    def handle_comment(self,data): self.out.append('<!--'+data+'-->')
    def handle_decl(self,data): self.out.append('<!'+data+'>')


def controls(state,token,html):
    gate = FormGate(state,token); gate.feed(html); gate.close()
    return daily_entry(state,token,''.join(gate.out))


def daily_entry(state,token,html):
    try: state.duty_subject(token)
    except PermissionError:return html
    if 'data-daily-maintenance-entry' in html:return html
    return html.replace('<span class="spacer"></span>','<a href="/materials/daily" data-daily-maintenance-entry>日常数据维护</a><span class="spacer"></span>',1)


def navigation(route):
    # The sandtable uses its map and command panels instead of the old link row.
    if route == "/sand" or parent(route) == '/materials': return ""
    main = parent(route)
    if main == '/sand':
        return ('<nav class="emergency-nav" aria-label="模块内功能"><a href="/sand">‹ 返回沙盘</a>'
                '<label>办理事项<select data-emergency-nav>'
                + ''.join('<option value="'+path+'"'+(' selected' if path==route else '')+'>'+label+'</option>'
                          for path,label in CHILDREN['/sand'])+'</select></label></nav>')
    children = tuple((path,label) for path,label in CHILDREN.get(main,()) if path!='/personnel/arrangements')
    if not children: return ''
    return ('<nav class="lz-card unified-children" data-module="'+esc(main)+'" aria-label="模块内功能" '
            'style="display:flex;flex-wrap:wrap;gap:12px;max-width:100%;margin-bottom:12px">'
            + ''.join('<a class="lz-link" href="'+path+'">'+label+'</a>' for path,label in children) + '</nav>')


def decorate(state,route,html,token):
    from .panel import _light_nav, _COMMANDER_LIGHT_NAV
    main = parent(route)
    if not main: return html
    # Preserve branding/user/logout, replace only the role-specific top navigation.
    match = re.search(r'(<header class="lz-topbar">)(.*?)(</header>)',html,re.S)
    if match:
        brand = re.match(r'<b>.*?</b>',match[2],re.S)
        user = re.search(r'<span class="spacer"></span>.*',match[2],re.S)
        if brand and user:
            header = match[1]+brand[0]+_light_nav(_COMMANDER_LIGHT_NAV,main)+user[0]+match[3]
            html = html[:match.start()]+header+html[match.end():]
    if not (main == '/' and 'id="org-stage"' in html):
        html = html.replace('<div class="lz-wrap">','<div class="lz-wrap">'+navigation(route),1)
    from .workflow import decorate as workflow_decorate
    html=controls(state,token,workflow_decorate(state,route,html,token))
    if main=='/sand' and route!='/sand':
        from .emergency_ui import decorate as emergency_decorate
        html=emergency_decorate(state,route,html)
    return html


def table(title,heads,rows):
    return ('<section class="lz-card"><h2>'+esc(title)+'</h2><div style="overflow-x:auto">'
            '<table class="lz-table"><tr>'+''.join('<th>'+esc(h)+'</th>' for h in heads)+'</tr>'
            + ''.join('<tr>'+''.join('<td style="overflow-wrap:anywhere">'+esc(str(v))+'</td>' for v in row)+'</tr>' for row in rows)
            + '</table></div>'+('' if rows else '<p>暂无本事件记录。</p>')+'</section>')


def event_records(state, main):
    """Common event detail projection, independent of reader identity."""
    eid = state.active_event_id
    if not eid or state.registry.events[eid].status == 'registered':
        return '<div class="lz-empty">请先选择已启动事件。</div>'
    if main == '/':
        calls = state.event_callouts(eid)
        snapshot = state.ledger.snapshots.get(eid)
        rows=[]
        for sid, squad in sorted(state.ledger.squads.items()):
            call = calls.get(sid)
            if not call: continue
            arrived = snapshot.squads.get(sid) if snapshot else None
            for phone in squad.member_phones:
                person=state.registry.library.get(phone)
                rows.append((squad.name,person.name if person else phone,phone,
                    '已确认到场' if arrived and phone in arrived.arrived_phones else '未确认到场',
                    '有召集回执' if call.reply else '待回执'))
        temp=state.temporary_people(eid)
        groups=state.event_group_people(eid)
        temporary=[(p.name,p.unit,groups.get(p.phone,p).pos_code or '待安排','已确认到场' if p.arrived_at else '未确认到场') for p in temp.values()]
        return table('本事件全部召集队伍与人员',('队伍','姓名','联系电话','到场','召集'),rows)+table('临时人员库与编组',('姓名','单位','编组','到场'),temporary)
    if main == '/sand':
        from .emergency_ui import overview
        return overview(state)
    if main == '/materials':
        from .material_inventory_reads import read_event
        rows=[]
        for iid,inventory in read_event(state.workspace,state.ledger,eid).items():
            item=state.ledger.items[iid]
            for balance in inventory.team_balances:
                squad=state.ledger.squads.get(balance.squad_id)
                rows.append((squad.name if squad else balance.squad_id,item.name,balance.arrival_base,balance.available,item.unit))
        return table('本事件全部队伍物资',('队伍','物资','到场基数','当前可用','单位'),rows)
    return ''


# Same validated read projection for each role; original action forms keep their subject.
RECORD_KINDS = {
 '/personnel/arrangements': ('personnel_management',),
 '/personnel/professionals': ('professional_assignment',),
 '/sand/task-review': ('task_review_assessment',),
 '/sand/task-release': ('formal_task_release',),
 '/sand/task-receipts': ('formal_task_release','formal_task_receipt'),
 '/sand/task-feedback': ('formal_task_feedback','formal_task_relations','formal_task_control'),
 '/sand/task-materials': ('task_material_inputs',),
 '/materials/transfers': ('material_transfer',),
 '/materials/supply': ('material_supply_contact',),
 '/materials/supply-receipts': ('formal_supply_receipt',),
 '/materials/supply-execution': ('verified_point_stock','formal_supply_order'),
 '/sand/supply-draft': ('material_supply_draft',),
 '/materials/custody': ('loan_custody_receipt',),
 '/materials/accounts': ('material_closeout_account',),
 '/materials/maintenance-authority': ('material_maintenance_grant',),
 '/materials/records': ('material_catalog_change',),
 '/materials/closeout': ('material_closeout_account','loan_custody_receipt'),
}
FIELD_WORDS = dict(operation='办理事项',basis='依据',note='说明',before='此前',after='本次',
    status='状态',state='状态',content='内容',result='结果',order_id='任务',squad_id='队伍',
    group_id='编组',phone='人员',actor_id='办理人',quantity='数量',item_id='物资',
    classification='分类',action='动作',decision='决定',details='明细',reason='理由')


def child_records(state,route):
    from .audited_records import records
    from .workbench import esc
    from . import personnel_management_page
    def human(value):
        if isinstance(value,dict):
            return '；'.join(FIELD_WORDS.get(k,'')+('：' if k in FIELD_WORDS else '')+human(v)
                            for k,v in value.items())
        if isinstance(value,(list,tuple)): return '、'.join(human(v) for v in value)
        return str(value)
    RESULT_WORDS = {'accept': '核验通过', 'return': '退回补充', 'received': '确认接收',
                    'started': '开始执行', 'progress': '进展报告', 'completed': '完成报告',
                    'difficulty': '困难报告', 'resolved': '问题已处理', 'pause': '暂停',
                    'resume': '恢复', 'ack': '已阅', 'publish': '正式发布',
                    'dispatch': '已发出', 'receive': '已接收', 'submit': '已提交',
                    'verify': '已核验', 'arrival': '已到场'}
    def result_word(value):
        """现场只看结果：收到没有、到位没有、完成没有；原因与依据留在复盘里。"""
        for key in ('action', 'result', 'decision', 'operation', 'stage', 'state'):
            word = value.get(key)
            if isinstance(word, str) and word:
                return RESULT_WORDS.get(word, word)
        return '已办理'
    rows=[]
    for kind in RECORD_KINDS.get(route,()):
        for index,r,value in records(state.workspace,kind):
            actor=state.registry.library.get(r.actor_id)
            rows.append((r.times.recorded_at,actor.name if actor else r.actor_id,result_word(value)))
    content='<div data-shared-child="'+esc(route)+'">'+table('本事件'+CHILD_LABELS[route]+'记录',('时间','办理人','结果'),rows)+'</div>'
    if parent(route)=='/sand':
        return '<details class="emergency-raw"><summary>查看办理记录 · '+str(len(rows))+' 条</summary>'+content+'</details>'
    return content
