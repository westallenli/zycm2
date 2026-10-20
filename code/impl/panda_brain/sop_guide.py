"""Read-only visual work guide. Nodes describe actions, never certify completion."""
from html.parser import HTMLParser
from .workbench import esc

# label, instruction, responsible role, existing destination, button label
PATHS = (
    (("核对火情", "核对来源、位置、初报和启动记录。", "值班人员 / 指定副总指挥", "/events", "查看事件资料"),
     ("整理处置", "核对已有任务和现场记录，整理尚缺的资料。", "指定副总指挥", "drawer-fourcat", "查看处置记录"),
     ("确认依据", "结合处置任务、专业意见和核验记录，由负责人确认。", "相应专业人员 / 负责人", "basis", "查看完成依据")),
    (("核对资料", "核对地图、气象地形、观测来源和资料时效。", "指定副总指挥 / 相应专业人员", "cmd-advisor", "打开智能参谋"),
     ("复核变化", "查看待复核变化与信息缺口，结合原始证据研判。", "指定副总指挥 / 相应专业人员", "cmd-situation", "查看态势复核"),
     ("确认意见", "由相应人员确认专业意见与方案依据。", "相应专业人员 / 指挥负责人", "basis", "查看确认依据")),
    (("核对可派", "核对人员到场、当前可派力量和物资缺口。", "指定副总指挥 / 后勤负责人", "/materials/event", "核对人员物资"),
     ("安排力量", "使用已有选队、编组入口安排力量；安排与实际到场分别核对。", "指定副总指挥", "/personnel/arrangements", "打开人员安排"),
     ("核对落实", "核对人员与保障落实记录；登记数量不代表可派数量。", "队长 / 指定副总指挥 / 后勤负责人", "/materials/event", "查看落实记录")),
    (("编制草案", "编制总任务与子任务，补齐目标、分工、时间和反馈要求。", "指定副总指挥", "draft", "打开任务草案"),
     ("按类别复核", "常规任务、需专业意见和重大会商分别沿用各自复核规则。", "相应审定人员", "/tasks/release", "核对复核与发布条件"),
     ("正式发布", "由有权限的本人核对当前版本并正式发布。", "指定副总指挥", "/tasks/release", "打开正式发布"),
     ("核对回执", "核对正确接收人对当前版本的回执；送达或已阅不能代替接收。", "执行队长 / 组长", "/tasks/formal", "查看当前版回执")),
    (("核对任务", "核对正式任务当前版本、分工和接收依据。", "指定副总指挥 / 执行队长或组长", "/tasks/formal", "查看正式任务"),
     ("跟进执行", "报告实际进展、困难与异常，保留现场来源。", "执行队长 / 组长", "/tasks/feedback", "打开任务反馈"),
     ("核验结果", "由指定核验负责人核对结果与整体目标，自报完成仍需核验。", "指定核验负责人", "/tasks/feedback", "查看核验记录")),
    (("核对范围", "核对清理范围、热点、看守分工和既有任务。", "指定副总指挥 / 执行队长或组长", "drawer-fourcat", "查看地图与任务"),
     ("跟进复查", "结合现场反馈跟进异常，核对复查与交接安排。", "执行队长 / 组长", "/tasks/feedback", "查看任务反馈"),
     ("核验交接", "由负责人核对清理、复查与交接依据，专门入口尚待接入。", "指定核验负责人", "basis", "查看交接依据")),
    (("核对险情", "核对紧急、异常或支援线索的原文、对象与位置。", "现场队长 / 组长", "basis", "查看应急线索"),
     ("协调资源", "结合具体救援任务核对可用人员、装备与专业保障。", "指定副总指挥 / 相应专业人员", "/materials/event", "核对救援保障"),
     ("核验救援", "核对实际执行、人员情况和转运交接，由负责人核验。", "指定核验负责人", "basis", "查看救援依据")),
    (("核对内容", "区分内部通报与对外新闻，核对来源、版本和接收范围。", "按本单位制度确定", "/workflow/records?step=7", "查看沟通资料"),
     ("确认职责", "依据已确认制度明确编制、审核与发布责任人。", "按本单位制度确定", "/workflow/records?step=7", "查看制度缺项"),
     ("核对发布", "按本单位制度在原通道发布，在环节办理页登记审核、发布与接收依据。", "制度明确的发布责任人", "/workflow/records?step=7", "查看发布依据")),
    (("核验任务", "核对任务实际结果、指定负责人核验和整体目标。", "指定核验负责人 / 指挥负责人", "/tasks/feedback", "查看任务核验"),
     ("核对收尾", "核对撤离、物资去向和仍需跟进的未归还事项。", "指挥负责人 / 后勤负责人", "/materials/closeout", "核对物资收尾"),
     ("交接遗留", "明确交接责任，保留整改遗留和复盘依据。", "交接负责人 / 整改验收负责人", "/review", "查看遗留与复盘")),
)

COMPLETION = (
    '处置任务、专业意见与核验依据经负责人确认；精确规则待专业审核。',
    '当前资料、专业意见和方案依据经相应人员确认。',
    '可派力量、保障落实与本项任务依据经负责人核对。',
    '部署内容、必要复核和正确执行方对当前版的接收有依据，并由人确认。',
    '实际执行、异常处置和整体目标有依据，并由指定负责人核验。',
    '清理、看守、复查和交接有依据，并由指定负责人核验。',
    '救援执行、人员情况与必要交接有依据，并由指定负责人核验。',
    '按已确认制度核对审核、发布与送达依据；制度和独立入口仍待接入。',
    '任务结果、整体目标、撤离与物资去向经负责人核对，明确交接并保留遗留事项。',
)


class FactsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.state = "状态待核对"
        self.heading = ""
        self.in_heading = False
        self.in_item = False
        self.parts = []
        self.missing = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get('data-sop-state'):
            self.state = attributes['data-sop-state']
        if tag == 'h4':
            self.in_heading = True
            self.heading = ""
        if tag == 'li' and self.heading == '还缺什么':
            self.in_item = True
            self.parts = []

    def handle_data(self, data):
        if self.in_heading:
            self.heading += data
        if self.in_item:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'h4':
            self.in_heading = False
        if tag == 'li' and self.in_item:
            text = ''.join(self.parts).strip()
            if text:
                self.missing.append(text)
            self.in_item = False


def facts(fragment):
    parser = FactsParser()
    parser.feed(fragment)
    parser.close()
    return parser


INVALID = {'未进入事件', '依据待核对', '状态待核对'}
PRIORITIES = ((6, '有应急相关记录待核对'), (1, '有变化待复核'),
              (4, '有执行事项待核对'), (3, '草案待编制'))


def recommendation(states):
    """Priority is a navigation suggestion, not risk diagnosis or work approval."""
    if len(states) != 9 or states[0] in INVALID:
        return dict(step=None, reason='当前事件依据待核对，请先核对事件与资料。')
    for step, state in PRIORITIES:
        if states[step] == state:
            return dict(step=step, reason='依据：' + state)
    return dict(step=0, reason='先核对事件基础资料；可同时进入其他工作。')


def action(step, target, label):
    if target.startswith('/'):
        return f'<a class="lz-btn" data-sand-related="{step}" href="{target}">{esc(label)}</a>'
    if target in {'basis', 'draft'}:
        return f'<button type="button" class="lz-btn" data-sop-reveal="{target}">{esc(label)}</button>'
    return f'<button type="button" class="lz-btn" data-sand-open="{target}">{esc(label)}</button>'


def guide(step, fragment, *, action_renderer=None, phase_override=None):
    view = facts(fragment)
    phase = 1 if view.state in {'有变化待复核', '有执行事项待核对',
                                '有清理看守资料待核对', '有收尾事项待核对'} else 0
    if phase_override is not None:
        phase = phase_override
    render_action = action_renderer or action
    nodes, bodies = [], []
    for number, (label, instruction, owner, target, button) in enumerate(PATHS[step]):
        current = number == phase
        nodes.append(f'<li><button type="button" data-sop-node="{number}" aria-pressed="{str(current).lower()}"'
                     f' aria-controls="sop-node-{step}-{number}"><span>{number + 1}</span><b>{label}</b>'
                     f'<small>{"建议当前" if current else "可查看"}</small></button></li>')
        bodies.append(f'<section id="sop-node-{step}-{number}" data-sop-node-body="{number}"'
                      f'{"" if current else " hidden"}><h4>下一步：{label}</h4><p>{instruction}</p>'
                      f'<p class="sop-owner">办理职责：{owner}</p>'
                      + (render_action(step, target, button) if view.state not in INVALID else
                         '<p role="status">依据不可用，请先核对事件与原始资料。</p>') + '</section>')
    gaps = ''.join(f'<li>{esc(item)}</li>' for item in view.missing)
    from .sand_shell import WORK_ITEMS
    following = ((step + 1) if step < 5 else 8 if step in (5, 6, 7) else 0)
    sequence = (f'<div class="sop-sequence"><button type="button" class="lz-btn lz-btn2" data-sop-work="{max(0,step-1)}">查看上一项</button>'
                f'<button type="button" class="lz-btn" data-sop-work="{following}">后续：{WORK_ITEMS[following][0]}</button></div>'
                '<p class="lz-note">办理顺序供组织工作参考；可并行、可回看，应急救援随时进入。选择节点不代表完成。</p>')
    return (f'<div class="sop-guide" data-sop-guide="{step}" data-sop-phase="{phase}">'
            f'<p class="sop-step-heading">第 {step + 1} 项 · {WORK_ITEMS[step][0]}</p>'
            f'<div class="sop-focus"><span>记录提示</span><b>{esc(view.state)}</b></div>'
            '<ol class="sop-path" aria-label="本项操作路径，节点选择不代表完成">' + ''.join(nodes) + '</ol>'
            '<div class="sop-action-card">' + ''.join(bodies) + '</div>'
            + (f'<details class="sop-gaps"><summary>当前资料提醒 · {len(view.missing)} 条</summary><ul>{gaps}</ul></details>' if gaps else '')
            + f'<details class="sop-confirm"><summary>完成要求</summary><p>{COMPLETION[step]} 本页不自动判定完成。</p></details>{sequence}</div>')


def recommendation_html(states):
    from .sand_shell import WORK_ITEMS
    suggestion = recommendation(states)
    step = suggestion['step']
    title = WORK_ITEMS[step][0] if step is not None else '先核对事件资料'
    button = (f'<button type="button" class="lz-btn" data-sop-start="{step}">继续：{title}</button>'
              if step is not None else '<a class="lz-btn" href="/events">查看事件</a>')
    return (f'<div data-sop-recommendation data-sop-recommended="{step if step is not None else ""}">'
            f'<span>现在先做什么 · 建议入口</span><b>{"第 " + str(step + 1) + " 项 · " if step is not None else ""}{title}</b><small>{esc(suggestion["reason"])}</small>{button}</div>')
