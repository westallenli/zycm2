"""Bounded local document extraction. No OCR, network, macros or formulas."""
import csv
import io
import re
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

MAX_FILE = 3 * 1024 * 1024
MAX_EXPANDED = 16 * 1024 * 1024


def _csv(rows):
    if not rows or len(rows) > 2001 or any(len(r) > 40 for r in rows):
        raise ValueError('表格为空或超过2000行/40列限制。')
    result = io.StringIO(); csv.writer(result).writerows(rows)
    return result.getvalue()


def _xml(archive, name):
    data = archive.read(name)
    if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
        raise ValueError('文档含不支持的实体声明。')
    return ET.fromstring(data)


def extract(filename, raw):
    try:
        return _extract(filename, raw)
    except (ValueError, KeyError, IndexError, UnicodeError, zipfile.BadZipFile, ET.ParseError) as error:
        raise ValueError('文件解析失败：' + str(error)) from error


def _extract(filename, raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_FILE:
        raise ValueError('原文件为空或超过3MiB。')
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix in {'.csv', '.txt'}:
        try: text = raw.decode('utf-8-sig')
        except UnicodeDecodeError: text = raw.decode('gb18030')
        if '\x00' in text: raise ValueError('文本文件含二进制内容。')
        if '\t' in text.splitlines()[0]: return _csv(list(csv.reader(io.StringIO(text), delimiter='\t')))
        return text
    if suffix in {'.docx', '.xlsx'}:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.infolist(); names = [p.filename for p in info]
            if (len(info) > 1000 or len(names) != len(set(names)) or sum(p.file_size for p in info) > MAX_EXPANDED
                    or any(p.flag_bits & 1 or '..' in PurePosixPath(p.filename).parts or p.filename.startswith('/') for p in info)):
                raise ValueError('文档包过大、重复、加密或路径异常。')
            if suffix == '.docx':
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                doc = _xml(archive, 'word/document.xml'); tables = doc.findall('.//w:tbl', ns)
                if len(tables) != 1 or doc.findall('.//w:gridSpan', ns) or doc.findall('.//w:vMerge', ns):
                    raise ValueError('Word须有一张明确表格，不支持合并单元格或多表猜测。')
                return _csv([[''.join(t.text or '' for t in c.findall('.//w:t', ns)).strip() for c in row.findall('w:tc', ns)]
                             for row in tables[0].findall('w:tr', ns)])
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            sheets = [n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)]
            if len(sheets) != 1: raise ValueError('Excel须明确保留一个工作表，不猜测多表。')
            shared = []
            if 'xl/sharedStrings.xml' in names:
                shared = [''.join(t.text or '' for t in si.findall('.//s:t', ns)) for si in _xml(archive, 'xl/sharedStrings.xml').findall('s:si', ns)]
            sheet = _xml(archive, sheets[0])
            if sheet.findall('.//s:f', ns) or sheet.findall('.//s:mergeCell', ns):
                raise ValueError('Excel含公式或合并单元格，请先明确固定值与列。')
            rows = []
            for row in sheet.findall('.//s:sheetData/s:row', ns):
                cells = {}
                for c in row.findall('s:c', ns):
                    ref = c.get('r', ''); match = re.fullmatch(r'([A-Z]{1,2})([1-9][0-9]*)', ref)
                    if not match: raise ValueError('Excel单元格坐标不明确。')
                    col = 0
                    for ch in match[1]: col = col * 26 + ord(ch) - 64
                    if col > 40 or col in cells: raise ValueError('Excel列过多或单元格重复。')
                    v = c.find('s:v', ns); text = v.text if v is not None and v.text else ''
                    if c.get('t') == 's': text = shared[int(text)]
                    elif c.get('t') == 'inlineStr': text = ''.join(t.text or '' for t in c.findall('.//s:t', ns))
                    elif c.get('t') not in {None, 'n', 'str'}: raise ValueError('Excel单元格类型待人工核对。')
                    cells[col] = text.strip()
                if cells: rows.append([cells.get(i, '') for i in range(1, max(cells) + 1)])
            return _csv(rows)
    if suffix == '.pdf':
        if not raw.startswith(b'%PDF-'): raise ValueError('PDF文件签名不正确。')
        try:
            from pypdf import PdfReader
            from pypdf.errors import PyPdfError
        except ImportError as error: raise ValueError('缺少PDF解析依赖，请安装requirements-document-imports.txt。') from error
        try:
            reader = PdfReader(io.BytesIO(raw), strict=True)
            if reader.is_encrypted or len(reader.pages) > 30: raise ValueError('加密PDF或超过30页不接收。')
            lines = []; expanded = 0
            for page in reader.pages:
                content = page.get_contents()
                if content: expanded += len(content.get_data())
                if expanded > MAX_EXPANDED: raise ValueError('PDF累计内容流过大。')
                text = page.extract_text(extraction_mode='layout')
                if not text.strip(): raise ValueError('PDF无可读文本；扫描件OCR未接入，不能假解析。')
                lines.extend(line.strip() for line in text.splitlines() if line.strip())
        except (PyPdfError, KeyError, TypeError, IndexError) as error:
            raise ValueError('PDF结构或内容损坏，解析失败。') from error
        if not lines: raise ValueError('PDF没有可读页面。')
        if ',' in lines[0] or '\t' in lines[0]: return '\n'.join(lines)
        return _csv([re.split(r' {2,}', line) for line in lines])
    raise ValueError('支持CSV/TXT、Word .docx、Excel .xlsx与文本PDF；旧.doc/.xls请转换，图片/扫描OCR未接入。')
