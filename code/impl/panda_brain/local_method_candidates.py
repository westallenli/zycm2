"""Review-pending source cards, not an approved tactical method library."""

import re

from .sandtable import InquiryAnswer


SOURCE_URL = 'https://xzfg.moj.gov.cn/front/law/detail?LawID=1785'
SOURCE_DATE = '2025-11-16'
SOURCE_EFFECTIVE_DATE = '2026-01-01'
STANDARD_URL = 'https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/202511/W020251103550694299778.pdf'
STANDARD_DATE = '2025-10-15'
TRAINING_URL = 'https://www.mem.gov.cn/fw/flfgbz/bz/bzwb/202511/W020260116712847594775.pdf'
TRAINING_DATE = '2025-10-15'
TRAINING_TITLE = '地方森林草原消防队伍火灾扑救训练大纲'
REGIONAL_GUIDE_URL = 'https://yjglt.yn.gov.cn/html/2026/qtwj_0303/4032595.html'
REGIONAL_GUIDE_DATE = '2026-03-03'
STANDARD_SOURCE_MARKERS = ('YJ/T 35', 'YJ/T35', 'YJT35',
                           '地方森林草原消防队伍火灾扑救安全指南')
_OTHER_STANDARD_TITLES = (
    '地方森林草原消防队伍评价规范',
    '地方森林草原消防队伍建设规范',
    '地方应急指挥部建设基本要求',
)
_STANDARD_ID_RE = re.compile(r'YJ[\s/_-]*T[\s/_-]*(\d+)', re.IGNORECASE)

CARDS = {
    'front-command': {
        'keywords': ('指挥', '方案', '方法', '扑救'),
        'title': '前线指挥与专业指挥核对',
        'source_clause': '《森林草原防灭火条例》第三十八条',
        'source_requirement': '按需要设立火场前线指挥部并设置专业指挥；各救援力量接受统一指挥。',
        'assistant_check': '核对现场指挥关系和专业负责人是否已有具名记录；缺失时提示补核，不自动指派。',
        'required_inputs': ('前线指挥关系', '专业指挥人', '参加救援力量'),
    },
    'situation-before-plan': {
        'keywords': ('火情', '风', '位置', '范围', '方案', '方法'),
        'title': '方案前现场数据核对',
        'source_clause': '《森林草原防灭火条例》第三十八条',
        'source_requirement': '核实火灾准确位置、范围、风力、风向、火势，并结合天气、地理条件确定方案、地段和责任人。',
        'assistant_check': '逐项列出记录来源、时间和未知项；资料不足时只提示核查，不生成确定的扑救部署。',
        'required_inputs': ('位置', '范围', '风力', '风向', '火势', '天气', '地理条件'),
    },
    'people-safety': {
        'keywords': ('安全', '撤离', '人员', '扑救', '方法'),
        'title': '群众和扑救人员安全前置核对',
        'source_clause': '《森林草原防灭火条例》第三十九条',
        'source_requirement': '及时解救、疏散、撤离受威胁群众，做好扑救人员管理与防护；不得安排未经相关专业培训的人员直接灭火。',
        'assistant_check': '提示核对受威胁人员、撤离情况、队伍培训与防护记录；不能凭缺失信息判定现场安全。',
        'required_inputs': ('受威胁人员', '疏散撤离状态', '扑救人员培训', '防护状况'),
    },
    'major-hazard': {
        'keywords': ('重大危险源', '危化', '油气管道', '铁路', '重要目标'),
        'title': '重大危险源专业指导核对',
        'source_clause': '《森林草原防灭火条例》第三十八条',
        'source_requirement': '重要目标物和重大危险源受到火灾威胁时，应在专业人员指导下消除威胁、科学施救。',
        'assistant_check': '核对受威胁对象、专业人员及指导记录；缺项时提示补核，不代替专业判断。',
        'required_inputs': ('受威胁目标', '专业人员', '指导记录'),
    },
    'emergency-measures': {
        'keywords': ('隔离带', '清除障碍', '应急取水', '交通管制'),
        'title': '应急措施决定权核对',
        'source_clause': '《森林草原防灭火条例》第四十一条',
        'source_requirement': '因扑救需要，县级以上人民政府可以决定开设防火隔离带、清除障碍物、应急取水、局部交通管制等措施。',
        'assistant_check': '核对措施需要、决定主体和原始记录；候选方案不能冒充政府决定或已执行措施。',
        'required_inputs': ('措施需要', '决定主体', '决定记录'),
    },
    'postfire-guard': {
        'keywords': ('余火', '看守火场', '撤出看守', '清理火场'),
        'title': '余火清理与撤守验收核对',
        'source_clause': '《森林草原防灭火条例》第四十三条',
        'source_requirement': '明火扑灭后，扑救队伍全面检查、清理余火；当地人民政府组织足够人员看守，按规定检查验收合格后方可撤出看守人员。',
        'assistant_check': '核对明火状态、余火清理、看守和验收记录；不能凭口头完结自动撤守。',
        'required_inputs': ('明火状态', '余火清理', '看守安排', '验收记录'),
    },
}

_LAW_CLAUSE_CARDS = {
    '38': ('front-command', 'situation-before-plan', 'major-hazard'),
    '三十八': ('front-command', 'situation-before-plan', 'major-hazard'),
    '39': ('people-safety',), '三十九': ('people-safety',),
    '41': ('emergency-measures',), '四十一': ('emergency-measures',),
    '43': ('postfire-guard',), '四十三': ('postfire-guard',),
}


# YJ/T 35—2025 applies to local forest and grassland fire brigades. These are
# source-backed review cues, not a claim that a brigade or tactic fits an event.
STANDARD_CARDS = {
    'site-safety-role': {
        'keywords': ('现场安全员', '安全员职责', '专职指挥员'),
        'title': '现场安全职责核对',
        'source_clause': 'YJ/T 35—2025 第5.1.2—5.1.3条',
        'source_requirement': '现场专职指挥员应掌握安全扑救的组织指挥程序、火场天气和火行为变化，明确撤离路线与避险措施；现场安全员协助观察并报告安全隐患。',
        'assistant_check': '核对具名指挥、安全员、风险报告和通信记录；缺失时提示补核，不自动任命或判断安全。',
        'required_inputs': ('队伍类型', '专职指挥员', '现场安全员', '风险报告'),
    },
    'site-risk-assessment': {
        'keywords': ('风险评估', '危险地形', '危险时段', '危险火行为'),
        'title': '力量部署前风险评估核对',
        'source_clause': 'YJ/T 35—2025 第6.2.2条',
        'source_requirement': '力量部署前评估可能影响扑火安全的危险地形、时段、气象、可燃物和火行为，制定安全防范措施并发布预警信息。',
        'assistant_check': '逐项核对现场评估来源、时间、责任人与预警记录；未知项保留未知，不据此给出部署位置。',
        'required_inputs': ('队伍类型', '地形', '时段', '气象', '可燃物', '火行为', '预警记录'),
    },
    'retreat-route': {
        'keywords': ('安全撤离路线', '避险区', '火场情况不明', '接近火场'),
        'title': '接近火场前撤离与避险核对',
        'source_clause': 'YJ/T 35—2025 第6.3.1—6.3.2条',
        'source_requirement': '接近火场前预选安全撤离路线和避险区并制定撤离方案；安全员反馈地形、气象、可燃物、火行为等信息，火场环境不明时不盲目接近火线。',
        'assistant_check': '核对路线、避险区、现场信息和安全员反馈是否已记录；不根据地图草稿推定路线安全。',
        'required_inputs': ('队伍类型', '撤离路线', '避险区', '安全员反馈', '现场环境'),
    },
    'withdrawal-count': {
        'keywords': ('撤离清点', '清点人员装备', '伤员转运', '撤离火场'),
        'title': '撤离归建前清点与交接核对',
        'source_clause': 'YJ/T 35—2025 第7.1—7.3条',
        'source_requirement': '撤离前收拢队伍、清点人员和装备，做好火场交接、伤员转运及统计记录；撤离按规定时间和路线组织，途中机动安全要求与进场一致。',
        'assistant_check': '核对具名清点、交接对象、伤员处置、路线和归建记录；不能以撤离报告自动认定人员或物资已结清。',
        'required_inputs': ('队伍类型', '人员清点', '装备清点', '交接记录', '伤员转运', '撤离路线'),
    },
}

_STANDARD_CLAUSE_CARDS = {
    '5.1.2': 'site-safety-role', '5.1.3': 'site-safety-role',
    '6.2.2': 'site-risk-assessment',
    '6.3.1': 'retreat-route', '6.3.2': 'retreat-route',
    '7.1': 'withdrawal-count', '7.2': 'withdrawal-count',
    '7.3': 'withdrawal-count',
}


TRAINING_CARDS = {
    'training-levels': {
        'keywords': ('适用范围', '训练层级', '单兵训练', '班组训练', '分队综合演练'),
        'title': '训练大纲适用范围与层级',
        'source_clause': 'YJ/T 37—2025 第1条、第4.1—4.2条',
        'source_requirement': '本大纲用于指导地方森林草原消防队伍开展扑救训练；训练分单兵训练、班组训练、分队综合演练三个层级，均应开展扑救安全训练。',
        'assistant_check': '核对队伍类型及训练记录；仅凭大纲不能认定本事件队伍已完成训练或具备现场能力。',
    },
    'training-safety': {
        'keywords': ('训练安全', '训练伤害', '安全员'),
        'title': '训练安全组织核对',
        'source_clause': 'YJ/T 37—2025 第4.3条',
        'source_requirement': '组训单位应按训练大纲落实训练制度，做好器材、防护和场地准备，预防训练伤害；训练中应设安全员监督全过程。',
        'assistant_check': '核对组训单位、训练准备和安全员记录；不据此判断现场部署安全。',
    },
}


REGIONAL_CARDS = {
    'yn-early-fire-conditions': {
        'title': '云南省基层早期火情处置条件核对',
        'source_clause': '《云南省森林草原火情早期处理与安全扑救手册》第一章二（二）',
        'source_requirement': '手册将较小火势、较好气象、平缓地形、周边无居民点和重要设施、人员已受训且能够确保安全列为基层先期处置条件；火势较大、气象较差或地形复杂且基层人员无法处置时，应在安全位置监测并上报，由上级组织处置。',
        'assistant_check': '逐项核对火势、气象、地形、受威胁目标、人员培训和安全依据；缺项保留未知，不据此生成扑救点或进入路线。',
    },
}


def answer_method_question(question: str) -> InquiryAnswer:
    named_ids = {int(number) for number in _STANDARD_ID_RE.findall(question)}
    training_query = 37 in named_ids or TRAINING_TITLE in question
    safety_query = any(marker in question for marker in STANDARD_SOURCE_MARKERS)
    if (any(number not in (35, 37) for number in named_ids)
            or any(title in question for title in _OTHER_STANDARD_TITLES)
            or (training_query and (35 in named_ids or safety_query or '条例' in question))):
        return InquiryAnswer(
            'knowledge', '本地待审方法资料尚未对所问标准建立逐条核验卡，不能以其他标准或法规代答。',
            '', (), '本地待审资料', '—',
            '该标准的适用范围和条款须另行核对，不能据此推断现场能力或安全结论。')
    if training_query:
        requested_clauses = [int(number) for number in re.findall(
            r'第\s*(\d+)(?:\.\d+)*\s*(?:章|条|节)', question)]
        requested_subclauses = re.findall(r'第\s*4\.(\d+)\s*条', question)
        matches = [(key, card) for key, card in TRAINING_CARDS.items()
                   if any(word in question for word in card['keywords'])]
        if (any(number not in (1, 4) for number in requested_clauses)
                or any(number not in ('1', '2', '3') for number in requested_subclauses)):
            matches = []
        elif requested_subclauses:
            allowed = {'training-levels' if number in ('1', '2')
                       else 'training-safety' for number in requested_subclauses}
            matches = [(key, card) for key, card in TRAINING_CARDS.items()
                       if key in allowed]
        elif not matches:
            matches = list(TRAINING_CARDS.items())
        if not matches:
            return InquiryAnswer(
                'knowledge', 'YJ/T 37—2025 所问条款尚未建立逐条核验卡。',
                TRAINING_DATE, (), '本地待审资料', '—',
                '不能把已核对的训练总则推断为其他章节要求或现场能力。')
        lines = [f"{card['title']}：原文要求摘要——{card['source_requirement']}"
                 f" 系统待审检查建议——{card['assistant_check']}"
                 for _, card in matches]
        return InquiryAnswer(
            'knowledge', '；'.join(lines), TRAINING_DATE,
            tuple(f'method:{key}' for key, _ in matches),
            TRAINING_URL + ' · ' + '、'.join(card['source_clause'] for _, card in matches),
            '标准来源已核，专业适用性未审',
            'YJ/T 37—2025 于 2025-10-15 发布并实施；仅适用于标准所述队伍范围。'
            '以上为待专业人员评审的训练核对项，不代表本事件队伍已完成训练，'
            '也不自动发布指令。')
    if ('云南' in question and '火情' in question
            and any(word in question for word in ('早期', '先期'))):
        if named_ids or safety_query or '条例' in question:
            return InquiryAnswer(
                'knowledge', '所问地方手册与法规或标准的关系尚未逐条核对，不能混合作答。',
                '', (), '本地待审资料', '—', '请分别核对具体资料及适用范围。')
        card = REGIONAL_CARDS['yn-early-fire-conditions']
        return InquiryAnswer(
            'knowledge', f"{card['title']}：原文要求摘要——{card['source_requirement']}"
            f" 系统待审检查建议——{card['assistant_check']}",
            REGIONAL_GUIDE_DATE, ('method:yn-early-fire-conditions',),
            REGIONAL_GUIDE_URL + ' · ' + card['source_clause'],
            '地方手册来源已核，跨地区及现场适用性待审',
            '该手册为云南省基层早期火情流程，不代表本事件满足所列条件；'
            '未经专业复核不得据此部署，也不自动发布指令。')
    source_query = safety_query
    law_query = '条例' in question
    if law_query and ('森林草原防灭火条例' not in question or source_query):
        return InquiryAnswer(
            'knowledge', '所问法规或跨资料关系尚未建立逐条核验卡。',
            '', (), '本地待审资料', '—', '请明确单一法规和条款，不用其他资料代答。')
    standard_only = ('标准' in question or bool(named_ids) or source_query) and '条例' not in question
    allowed_standard_cards = None
    if source_query:
        sections = re.findall(r'第\s*([^第，。；;]{1,48}?)\s*(条|章|节)', question)
        clauses = [number for expression, _ in sections for number in
                   re.findall(r'\d+(?:\.\d+)*', expression)]
        if not sections:
            clauses = re.findall(r'(?<![\d.])\d+(?:\.\d+)+(?![\d.])', question)
        if sections or clauses:
            if (any(unit != '条' for _, unit in sections)
                    or not clauses
                    or any(number not in _STANDARD_CLAUSE_CARDS for number in clauses)):
                return InquiryAnswer(
                    'knowledge', 'YJ/T 35—2025 所问条款尚未建立逐条核验卡。',
                    STANDARD_DATE, (), '本地待审资料', '—',
                    '不能用已核对的其他条款代答。')
            allowed_standard_cards = {_STANDARD_CLAUSE_CARDS[number]
                                      for number in clauses}
    allowed_law_cards = None
    if law_query:
        sections = re.findall(r'第\s*([^第，。；;]{1,48}?)\s*(条|章|节)', question)
        if sections:
            if (any(unit != '条' for _, unit in sections)
                    or any(expression.strip() not in _LAW_CLAUSE_CARDS
                           for expression, _ in sections)):
                return InquiryAnswer(
                    'knowledge', '《森林草原防灭火条例》所问条款尚未建立逐条核验卡。',
                    SOURCE_DATE, (), '本地待审资料', '—',
                    '不能用已核对的其他条款代答。')
            allowed_law_cards = {key for expression, _ in sections
                                 for key in _LAW_CLAUSE_CARDS[expression.strip()]}
    law_matches = [(key, card, 'law') for key, card in CARDS.items()
                   if not standard_only
                   and (allowed_law_cards is None or key in allowed_law_cards)
                   and any(word in question for word in card['keywords'])]
    if allowed_law_cards is not None and not law_matches:
        law_matches = [(key, card, 'law') for key, card in CARDS.items()
                       if key in allowed_law_cards]
    standard_matches = [(key, card, 'standard') for key, card in STANDARD_CARDS.items()
                        if not law_query
                        and (key in allowed_standard_cards if allowed_standard_cards is not None
                            else any(word in question for word in card['keywords']))]
    if source_query and allowed_standard_cards is None and not standard_matches:
        standard_matches = [(key, card, 'standard') for key, card in STANDARD_CARDS.items()]
    matches = law_matches + standard_matches
    if not matches:
        return InquiryAnswer('knowledge', '本地待审资料没有匹配条目，请明确要核对的主题。',
                             SOURCE_DATE, (), '本地待审资料', '—',
                             '不能据此推断现场适用方法。')
    lines = [f"{card['title']}：原文要求摘要——{card['source_requirement']}"
             f" 系统待审检查建议——{card['assistant_check']}"
             for _, card, _ in matches]
    law = [(key, card) for key, card, kind in matches if kind == 'law']
    standard = [(key, card) for key, card, kind in matches if kind == 'standard']
    source_parts = []
    if law:
        source_parts.append(f'{SOURCE_URL} · ' + '、'.join(dict.fromkeys(
            card['source_clause'] for _, card in law)))
    if standard:
        source_parts.append(f'{STANDARD_URL} · ' + '、'.join(dict.fromkeys(
            card['source_clause'] for _, card in standard)))
    boundary = []
    if law:
        boundary.append(f'法规于 {SOURCE_DATE} 公布、{SOURCE_EFFECTIVE_DATE} 施行。')
    if standard:
        boundary.append(f'YJ/T 35—2025 于 {STANDARD_DATE} 发布并实施，仅适用于标准所述队伍范围。')
    return InquiryAnswer(
        'knowledge', '；'.join(lines), SOURCE_DATE if law else STANDARD_DATE,
        tuple(f'method:{key}' for key, _, _ in matches),
        '；'.join(source_parts),
        ('法规/标准' if law and standard else '法规' if law else '标准') + '来源已核，专业适用性未审',
        ''.join(boundary) + '以上为待专业人员评审的检查项；'
        '不代表现场战术已核定，也不自动发布指令。')
