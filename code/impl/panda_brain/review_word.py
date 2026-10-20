"""Owner-previewed Word recovery: a new pending report, never ledger conclusions."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import secrets
import time
import zipfile
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 3 * 1024 * 1024
MAX_BODY_CHARS = 400000
BODY_BOOKMARK = 'PandaReviewBody'
BODY_LABEL = '报告正文开始（Word 回收请保留本行及版本标识，修改下方正文）'


def marker_text(report):
    return BODY_LABEL + '\n回收版本标识：' + identity(report)
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def identity(report):
    value = dict(event_id=report.event_id, report_id=report.report_id,
                 version=report.version, source_digest=report.source_digest,
                 body_sha256=hashlib.sha256(report.content_md.encode('utf-8')).hexdigest())
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'panda-review-v1:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _document(raw):
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError('请选择不超过 2MB 的本系统 Word 文件。')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if (len(entries) > 512 or len(names) != len(set(names))
                    or sum(item.file_size for item in entries) > 12 * 1024 * 1024
                    or any(item.flag_bits & 1 or item.file_size > 200 * max(item.compress_size, 1)
                           for item in entries)):
                raise ValueError('Word 压缩包结构或展开大小不受支持。')
            for item in entries:
                name = item.filename
                if ('\\' in name or name.startswith('/') or '..' in PurePosixPath(name).parts
                        or ':' in name or any(part.lower() in {'embeddings', 'activex'}
                                              for part in PurePosixPath(name).parts)
                        or 'vbaproject' in name.lower()):
                    raise ValueError('Word 含不支持的嵌入内容或文件路径。')
                if name.endswith(('.xml', '.rels')):
                    content = archive.read(item)
                    try:
                        xml_text = content.decode('utf-8-sig')
                    except UnicodeError as exc:
                        raise ValueError('Word XML 编码不受支持，请重新导出或人工合并。') from exc
                    if '<!DOCTYPE' in xml_text.upper() or '<!ENTITY' in xml_text.upper():
                        raise ValueError('Word 不支持外部实体或文档类型声明。')
                    if name == '[Content_Types].xml' and b'macroenabled' in content.lower():
                        raise ValueError('不支持含宏 Word。')
                    if name.endswith('.rels'):
                        tree = ET.fromstring(content)
                        if any(row.get('TargetMode', '').lower() == 'external' for row in tree):
                            raise ValueError('Word 含外部链接，请移除后重试或人工合并。')
        from docx import Document
        document = Document(io.BytesIO(raw))
    except ImportError:
        raise
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('文件不是可读取的 DOCX，请重新导出后编辑。') from exc
    return document


def read_body(raw, report):
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    document = _document(raw)
    if document.core_properties.identifier and document.core_properties.identifier != identity(report):
        raise ValueError('Word 的报告或正文版本不匹配，请下载当前版本后重新编辑；旧内容可人工合并。')
    body = document.element.body
    starts = list(body.iter(W + 'bookmarkStart'))
    markers = [row for row in starts if row.get(W + 'name') == BODY_BOOKMARK]
    if len(markers) != 1:
        raise ValueError('Word 正文标记缺失或重复，请重新下载或人工合并。')
    forbidden = {'ins', 'del', 'moveFrom', 'moveTo', 'drawing', 'pict', 'object',
                 'fldChar', 'fldSimple', 'footnoteReference', 'endnoteReference',
                 'commentRangeStart', 'commentReference', 'txbxContent', 'altChunk',
                 'sdt', 'gridSpan', 'vMerge'}
    if any(element.tag in {W + name for name in forbidden} for element in body.iter()):
        raise ValueError('Word 含修订、批注、图片、域或复杂表格；请先整理为文字和简单表格，或人工合并。')
    rows, found = [], False
    for element in body:
        if element.tag == W + 'sectPr':
            continue
        if not found:
            if markers[0] in list(element.iter()):
                if element.tag != W + 'p' or Paragraph(element, document).text != marker_text(report):
                    raise ValueError('Word 正文标记已修改，请重新下载或人工合并。')
                found = True
            continue
        if element.tag == W + 'p':
            paragraph = Paragraph(element, document)
            text = paragraph.text
            style = paragraph.style.name if paragraph.style else ''
            if style.startswith('Heading ') and style[-1:] in '123456':
                text = '#' * int(style[-1]) + ' ' + text
            rows.append(text)
        elif element.tag == W + 'tbl':
            if len(list(element.iter(W + 'tbl'))) != 1:
                raise ValueError('不支持嵌套表格，请人工合并。')
            table = Table(element, document)
            values = [[cell.text.replace('|', '\\|').replace('\n', ' / ') for cell in row.cells]
                      for row in table.rows]
            if not values or not values[0] or any(len(row) != len(values[0]) for row in values):
                raise ValueError('Word 表格结构不完整。')
            rows.append('| ' + ' | '.join(values[0]) + ' |')
            rows.append('| ' + ' | '.join('---' for _ in values[0]) + ' |')
            rows.extend('| ' + ' | '.join(row) + ' |' for row in values[1:])
        else:
            raise ValueError('Word 正文有不支持的内容块，请人工合并。')
    content = '\n'.join(rows).strip()
    if not content or len(content) > MAX_BODY_CHARS:
        raise ValueError('回收正文须为 1 至 400000 字，请核对后重试。')
    return content


def _owner(state, token):
    from .authorization import Action
    from .review_meeting import service
    return service(state).require(token, Action.APPROVE_REPORT, event_id=state.active_event_id)


def _gate(state, report_id, expected_version):
    from .review_replay import replay
    from .review_reports import checked_source, freshness_error
    replay(state.workspace, state.active_event_id)
    if str(expected_version) != str(state.workspace.version):
        raise ValueError('资料或报告已变化，请重新载入并预览；有冲突时生成最新草稿后人工合并。')
    report = state.review_ledger.reports.get(report_id)
    if report is None or report.event_id != state.active_event_id:
        raise ValueError('原报告不属于当前事件。')
    checked_source(report, state.active_event_id)
    error = freshness_error(state, report)
    if error:
        raise ValueError(error + ' 外部 Word 请合并到最新草稿，不自动覆盖。')
    if state.registry.events[state.active_event_id].status != 'closed':
        raise ValueError('事件结束后方可回收正式复盘报告。')
    return report


def preview(state, token, report_id, expected_version, raw):
    with state._event_authority_lock:
        actor = _owner(state, token)
        report = _gate(state, report_id, expected_version)
        content = read_body(raw, report)
        cache = getattr(state, '_word_previews', {})
        now = time.monotonic()
        cache = {key: row for key, row in cache.items() if now - row['created'] < 600}
        while len(cache) >= 32:
            cache.pop(next(iter(cache)))
        key = secrets.token_urlsafe(24)
        cache[key] = dict(actor=actor, session=hashlib.sha256(token.encode()).hexdigest(),
            event_id=state.active_event_id, report_id=report_id, identity=identity(report),
            expected_version=state.workspace.version, content_md=content,
            file_sha256=hashlib.sha256(raw).hexdigest(), created=now, imported_report_id='')
        state._word_previews = cache
        return dict(preview_id=key, report_id=report_id, next_version=report.version + 1,
                    content_md=content, file_sha256=cache[key]['file_sha256'])


def _persist(state, ledger, workspace):
    """Publish ledger last; compensate ordinary IO failures before changing memory."""
    from .persistence import load_workspace, save_workspace
    record = state.registry.events[state.active_event_id]
    targets = {Path(state.db_path).resolve()} if state.db_path else set()
    if record.snapshot_path and Path(record.snapshot_path).is_file():
        targets.add(Path(record.snapshot_path).resolve())
    originals = {path: load_workspace(path) if path.exists() else None for path in targets}
    attempted = []
    try:
        for path in targets:
            attempted.append(path)
            save_workspace(workspace, path)
        ledger.save(state.reviews_path)
    except Exception as exc:
        rollback_errors = []
        for path in attempted:
            try:
                if originals[path] is not None:
                    save_workspace(originals[path], path)
                elif path.exists():
                    path.unlink()
            except Exception as error:
                rollback_errors.append(str(error))
        if rollback_errors:
            raise RuntimeError('回收保存与恢复失败，请核对事件底账；未宣布回收成功。') from exc
        raise
    state.workspace, state.review_ledger = workspace, ledger


from .durable_storage import durable_action


@durable_action
def confirm(state, token, preview_id, expected_version):
    from .review_domain import ReviewReport
    with state._event_authority_lock:
        actor = _owner(state, token)
        item = getattr(state, '_word_previews', {}).get(preview_id)
        if (not item or item['event_id'] != state.active_event_id or item['actor'] != actor
                or item['session'] != hashlib.sha256(token.encode()).hexdigest()
                or time.monotonic() - item['created'] >= 600):
            raise ValueError('回收预览已过期或不属于当前会话，请重新预览。')
        if str(expected_version) != str(item['expected_version']):
            raise ValueError('确认请求与原预览版本不匹配，请重新预览。')
        if item['imported_report_id']:
            return item['imported_report_id']
        report = _gate(state, item['report_id'], expected_version)
        if item['expected_version'] != state.workspace.version or item['identity'] != identity(report):
            raise ValueError('预览后原报告已变化，请重新预览或人工合并。')
        stamp = datetime.now(timezone.utc).isoformat()
        version = state.review_ledger.next_report_version(state.active_event_id)
        recovered = ReviewReport(f'RPT-{state.active_event_id}-v{version}', state.active_event_id,
            version, status='pending_review', content_md=item['content_md'], created_at=stamp,
            edited_by=actor, edited_at=stamp,
            history=(f'{stamp} {actor}：预览确认 Word 回收，来源 {report.report_id}，新版本待审核；未改结构化结论。',),
            source_snapshot_json=report.source_snapshot_json, source_digest=report.source_digest,
            source_workspace_version=state.workspace.version,
            source_audit_sequence=max((event.sequence for event in state.workspace.events), default=0),
            imported_from_report_id=report.report_id, imported_file_sha256=item['file_sha256'],
              word_import_request_id=preview_id, stage_report=report.stage_report)
        ledger, workspace = deepcopy(state.review_ledger), deepcopy(state.workspace)
        ledger.add_report(recovered)
        workspace.append_event('ReviewReportWordImported', event_id=state.active_event_id,
            report_id=recovered.report_id, version=version, source_report_id=report.report_id,
            file_sha256=item['file_sha256'], body_sha256=hashlib.sha256(item['content_md'].encode()).hexdigest(),
            actor=actor, request_id=preview_id)
        _persist(state, ledger, workspace)
        item['imported_report_id'] = recovered.report_id
        return recovered.report_id


def decode_file(value):
    import base64, binascii
    try:
        return base64.b64decode(value, validate=True)
    except binascii.Error as exc:
        raise ValueError('Word 文件传输不完整，请重新选择。') from exc


def json_fields(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Word 回收字段不能重复。')
            value[key] = item
        return value
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Word 回收请求格式不正确。') from exc
    if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
        raise ValueError('Word 回收字段须为单一文本值。')
    return value


def card(report):
    from .workbench import esc
    return (f'<section id="word-recovery" class="sand-card"><h3>外部 Word 回收为新待审版本</h3>'
        '<p>仅回收本系统导出后编辑的 DOCX（不超过 2MB）。保留正文开始行及版本标识，预览核对后再确认。'
        '回收文字、标题和简单表格；批注、修订或复杂排版请先整理或人工合并。页眉、页脚及正文开始行上方的修改不回收。</p>'
        '<p>有更新版本或新资料时，先生成最新草稿，再将外部补充人工合并并重新审核。</p>'
        '<form method="post" action="/review/word-preview">'
        f'<input type="hidden" name="report_id" value="{esc(report.report_id)}">'
        '<input type="file" name="word_file" accept=".docx" aria-label="选择编辑后的 Word" required>'
        '<button type="submit" class="lz-btn lz-btn2">预览回收内容</button></form>'
        '<p class="word-error" role="alert"></p>'
        '<pre class="word-preview" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre>'
        '<button type="button" class="word-confirm lz-btn" disabled>确认创建新待审核版本</button></section>'
        + '''<script>(()=>{const card=document.getElementById('word-recovery'),form=card.querySelector('form'),
        confirm=card.querySelector('.word-confirm'),out=card.querySelector('.word-preview'),error=card.querySelector('.word-error');let preview='';
        const field=name=>form.elements.namedItem(name).value;
        const common=()=>({event_id:field('event_id'),csrf:field('csrf'),expected_version:field('expected_version')});
        const post=async(url,data)=>{const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
        const value=await response.json();if(!response.ok||!value.ok)throw new Error(value.message||'回收失败');return value;};
        form.word_file.addEventListener('change',()=>{preview='';confirm.disabled=true;out.textContent='';error.textContent='';});
        form.addEventListener('submit',async event=>{event.preventDefault();preview='';confirm.disabled=true;error.textContent='';out.textContent='';
        const button=form.querySelector('button');button.disabled=true;try{const file=form.word_file.files[0];if(!file||file.size>2097152)throw new Error('请选择不超过 2MB 的 Word');
        const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=16384)binary+=String.fromCharCode(...bytes.subarray(i,i+16384));
        const value=await post(form.action,{...common(),report_id:field('report_id'),file_b64:btoa(binary)});preview=value.preview_id;
        out.textContent=`原报告 ${value.report_id} → 新版本 v${value.next_version}（待审核）\n以下正文请逐项核对；旧版与结构化结论保留。\n\n${value.content_md}`;
        confirm.disabled=false;}catch(exc){error.textContent=exc.message+'；文件选择已保留。';}finally{button.disabled=false;}});
        confirm.addEventListener('click',async()=>{if(!preview)return;confirm.disabled=true;error.textContent='';try{
        const value=await post('/review/word-import',{...common(),preview_id:preview,confirm:'1'});
        location.assign('/review?tab=report&rid='+encodeURIComponent(value.report_id));
        }catch(exc){error.textContent=exc.message+'；预览已保留。';confirm.disabled=false;}});})();</script>''')
