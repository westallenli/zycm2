"""Per-request, commander-approved DeepSeek analysis of a local inquiry."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .providers import OpenAICompatibleProvider


ENDPOINT = 'https://api.deepseek.com/chat/completions'
MODEL = 'deepseek-flash'
SYSTEM = (
    '你是应急事件问答的辅助分析工具。仅依据用户消息中的匿名事实摘要回答；'
    '不得编造现场事实、人员、位置、法规条款或行动指令。'
    '只输出 JSON 对象，字段为 answer（简短分析）、source_ids（引用的 S 编号数组）、'
    'unknown（尚需核实的信息）。资料不足时明确说不足。'
)
_PHONE = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')
_LANDLINE = re.compile(r'(?<!\d)0\d{2,3}[-－ ]?\d{7,8}(?!\d)')
_COORD = re.compile(r'(?<!\d)[+-]?\d{1,3}\.\d{4,}\s*[,，/ ]\s*[+-]?\d{1,3}\.\d{4,}(?!\d)')
_COORD_LABEL = re.compile(r'(?:北纬|南纬|东经|西经|纬度|经度)\s*[:：]?\s*\d{1,3}(?:\.\d+|[°度]\d{1,2}[′分]\d{1,2}(?:\.\d+)?[″秒]?)')
_PRECISE_DECIMAL = re.compile(r'(?<!\d)[+-]?\d{1,3}\.\d{4,}(?!\d)')
_SENSITIVE = ('通话', '对讲', '录音', '音频', '语音', '电话记录')
_CURRENT_EVENT_TOPICS = frozenset({
    'personnel', 'annotations', 'water', 'materials', 'areas',
    'decisions', 'orders', 'gaps', 'pending', 'tasks', 'plan_comparison',
})


def is_eligible_topic(topic: str) -> bool:
    return topic in _CURRENT_EVENT_TOPICS


@dataclass(frozen=True)
class Preview:
    request_body: dict
    source_map: dict[str, str]


def _clean(value: str, *, names: tuple[str, ...], event_tokens: tuple[str, ...]) -> str:
    result = str(value or '')[:2000]
    for token in sorted(set(names + event_tokens), key=len, reverse=True):
        if token and len(token) >= 2:
            result = result.replace(token, '[已隐去]')
    result = _PHONE.sub('[手机号已隐去]', result)
    result = _LANDLINE.sub('[电话已隐去]', result)
    result = _COORD.sub('[精确坐标已隐去]', result)
    result = _COORD_LABEL.sub('[精确坐标已隐去]', result)
    return _PRECISE_DECIMAL.sub('[高精度数值已隐去]', result)


def build_preview(record: dict, *, names: tuple[str, ...],
                  event_tokens: tuple[str, ...], model: str = MODEL) -> Preview:
    answer = record['answer']
    if not is_eligible_topic(answer.get('topic', '')):
        raise ValueError('仅可选择当前事件事实问答；历史、规范或模型答案不能外发。')
    raw = ' '.join((record['question'], answer.get('conclusion', ''),
                    answer.get('unconfirmed', '')))
    if answer.get('topic') == 'radio' or any(word in raw for word in _SENSITIVE):
        raise ValueError('通话、语音及录音内容不得发送给模型。')
    sources = tuple(answer.get('sources') or ())
    if not sources or len(sources) > 12:
        raise ValueError('当前回答缺少可核对来源，不能发送给模型。')
    aliases = {f'S{i}': source for i, source in enumerate(sources, 1)}
    substitutions = sorted(
        ((source, alias) for alias, source in aliases.items()),
        key=lambda item: len(item[0]), reverse=True)

    def scrub(value: str) -> str:
        for source, alias in substitutions:
            value = value.replace(source, alias)
        return _clean(value, names=names, event_tokens=event_tokens)

    question = scrub(record['question'])
    summary = scrub(answer.get('conclusion', ''))
    if not question.strip() or not summary.strip():
        raise ValueError('匿名问句或事实摘要为空。')
    prompt = json.dumps({
        '匿名问题': question,
        '已选择的当前事件事实摘要': summary,
        '匿名来源编号': list(aliases),
    }, ensure_ascii=False, separators=(',', ':'))
    return Preview({
        'model': model,
        'messages': [{'role': 'system', 'content': SYSTEM},
                     {'role': 'user', 'content': prompt}],
        'temperature': 0, 'max_tokens': 1024, 'stream': False,
        'response_format': {'type': 'json_object'},
    }, aliases)


def provider(api_key: str, timeout_seconds: float) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url='https://api.deepseek.com', api_key=api_key,
        model=MODEL, timeout_seconds=timeout_seconds,
        response_format={'type': 'json_object'})


def parse_reply(raw: str, aliases: dict[str, str]) -> dict:
    try:
        reply = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError('模型未返回规定的 JSON 结构。') from exc
    if not isinstance(reply, dict) or not isinstance(reply.get('answer'), str):
        raise ValueError('模型回答结构不完整。')
    ids = reply.get('source_ids')
    if not isinstance(ids, list) or not ids or any(
            not isinstance(x, str) or x not in aliases for x in ids):
        raise ValueError('模型引用缺失或超出已预览的匿名来源。')
    if len(reply['answer'].strip()) < 2 or len(reply['answer']) > 4000:
        raise ValueError('模型回答为空或过长。')
    unknown = reply.get('unknown', '')
    if not isinstance(unknown, str) or len(unknown) > 1000:
        raise ValueError('模型待核实字段不正确。')
    return {'answer': reply['answer'].strip(), 'unknown': unknown.strip(),
            'source_ids': list(dict.fromkeys(ids))}
