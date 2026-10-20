"""Actual document bytes, named catalog versions and safe rollback/recovery."""
import base64
import http.client
import io
import json
import threading
import unittest
import zipfile
from unittest.mock import patch
from urllib.parse import urlencode
from xml.sax.saxutils import escape
from panda_brain import material_catalog as catalog
from panda_brain.document_tables import extract
from panda_brain.panel import make_server,render_page
from panda_brain.persistence import save_workspace
from . import test_upgrade_material_supply_contacts as fixture
from . import test_upgrade_material_maintenance_authority as authority_fixture


def word(rows):
    xml='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl>'
    for row in rows:xml+='<w:tr>'+''.join('<w:tc><w:p><w:r><w:t>'+escape(c)+'</w:t></w:r></w:p></w:tc>' for c in row)+'</w:tr>'
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('word/document.xml',xml+'</w:tbl></w:body></w:document>')
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    return stream.getvalue()


def excel(rows,formula=False):
    xml='<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    for i,row in enumerate(rows,1):
        xml+=f'<row r="{i}">'+''.join(f'<c r="{chr(65+j)}{i}" t="inlineStr"><is><t>{escape(c)}</t></is>'+('<f>1+1</f>' if formula else '')+'</c>' for j,c in enumerate(row))+'</row>'
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('xl/worksheets/sheet1.xml',xml+'</sheetData></worksheet>')
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="资料" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    return stream.getvalue()


def pdf(lines):
    from reportlab.pdfgen.canvas import Canvas
    stream=io.BytesIO(); c=Canvas(stream)
    for i,line in enumerate(lines):c.drawString(30,800-i*20,line)
    c.save();return stream.getvalue()


class CatalogTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','report','files'):
        locals()[name]=getattr(fixture.SupplyContactTests,name)
    grant=authority_fixture.MaintenanceAuthorityTests.grant
    def setUp(self):fixture.SupplyContactTests.setUp(self);self.grant()
    def change(self,operation='edit',oid='edit-item',version=0,request='catalog-edit',basis='匿名核对的资料依据',details=None,token=None):
        return self.state.handle_material_catalog(member_token=token or self.tokens[self.OTHER],operation=operation,object_id=oid,expected_version=version,request_id=request,basis=basis,
            details=details if details is not None else dict(data_type='item',row=dict(name='匿名长期物资',unit='盘',quantity='4')))
    def upload(self,kind,raw,filename='anonymous.csv',oid='import-test',request='upload'):
        return self.change('upload',oid=oid,request=request,details=dict(data_type=kind,filename=filename,original_base64=base64.b64encode(raw).decode()))
    def stage(self,op,version,oid='import-test',request=None):return self.change(op,oid,version,request or op,details={})
    def checked(self):return catalog.inspect(self.state.ledger,self.state.supply_event_spaces())
    def test_named_edits_old_routes_and_units_finite_unknown_scope(self):
        before=self.files()
        with self.assertRaises(PermissionError):self.change(token=self.tokens[self.DEPUTY])
        with self.assertRaises(PermissionError):self.state.handle_material_item(name='旧写口',member_token=self.tokens[self.OTHER])
        self.assertEqual(before,self.files());r=self.change();self.assertEqual(self.OTHER,r.actor_id)
        for qty in ['nan','inf','-1',True]:
            before=self.files()
            with self.assertRaises(ValueError):self.change(oid='edit-bad',request='bad-'+str(qty),details=dict(data_type='item',row=dict(name='错量',unit='盘',quantity=qty)))
            self.assertEqual(before,self.files())
        with self.assertRaises(ValueError):self.change(oid='edit-staff',request='bad-staff',details=dict(data_type='personnel',row=dict(name='越权人员')))
        fields=dict(point=self.state.ledger.points[self.point_id].name,item_name=self.state.ledger.items[self.item_id].name)
        self.change(oid='edit-stock',request='stock-zero',details=dict(data_type='stock',row=fields|dict(quantity='0')))
        self.assertEqual(0,self.state.ledger.points[self.point_id].supplies[self.item_id])
        self.change(oid='edit-stock',version=1,request='stock-unknown',details=dict(data_type='stock',row=fields|dict(quantity='未知')))
        self.assertIsNone(self.state.ledger.points[self.point_id].supplies[self.item_id]);self.assertEqual(3,len(self.checked()))
    def test_actual_word_excel_pdf_csv_txt_and_raw_hash_retained(self):
        rows=[['物资名称','单位','数量'],['文件物资','盘','3']]
        for i,(name,raw) in enumerate([('a.docx',word(rows)),('a.xlsx',excel(rows)),('a.csv','物资名称,单位,数量\n文件物资,盘,3'.encode()),('a.txt','物资名称\t单位\t数量\n文件物资\t盘\t3'.encode()),('a.pdf',pdf(['name,unit,quantity','PDF-item,piece,3']))]):
            oid=f'import-format-{i}';record=self.upload('item',raw,name,oid,request=f'format-{i}');value=json.loads(record.content_json)['details']
            self.assertEqual(raw,base64.b64decode(value['original_base64']));self.assertEqual('预览',value['state']);self.assertEqual('3',value['rows'][0]['quantity'])
        self.assertFalse(any(i.name in {'文件物资','PDF-item'} for i in self.state.ledger.items.values()));self.assertEqual(5,len(self.checked()))
    def test_edit_preserves_existing_status_validity_owner_and_omitted_fields(self):
        from dataclasses import replace
        original=replace(self.state.ledger.items[self.item_id],status='损坏',valid_until='2027-01-01',owner='匿名产权单位',last_inventory_at='2026-10-10T08:00:00+08:00')
        self.state.ledger.items[self.item_id]=original;self.state.ledger.save(self.state.materials_path)
        self.change(details=dict(data_type='item',row=dict(name=original.name,spec=' '+original.spec+' ',quantity='7')))
        updated=self.state.ledger.items[self.item_id]
        self.assertEqual(replace(original,default_carry_qty=7),updated);self.assertEqual(1,len(self.checked()))
        before=self.files()
        with self.assertRaisesRegex(ValueError,'文本'):
            self.change(version=1,request='bad-text',details=dict(data_type='item',row=dict(name=7,quantity='1')))
        self.assertEqual(before,self.files())
    def test_empty_pdf_and_late_pdf_content_error_are_rejected_without_writes(self):
        from pypdf import PdfWriter
        from pypdf.errors import PdfReadError
        empty=io.BytesIO();PdfWriter().write(empty);before=self.files()
        with self.assertRaisesRegex(ValueError,'可读页面'):self.upload('item',empty.getvalue(),'empty.pdf')
        with patch('pypdf._page.PageObject.extract_text',side_effect=PdfReadError('anonymous damaged stream')):
            with self.assertRaisesRegex(ValueError,'内容损坏'):self.upload('item',pdf(['name,unit','A,piece']),'broken.pdf')
        self.assertEqual(before,self.files())
    def test_bad_binary_formula_scan_mapping_duplicates_and_invalid_rows(self):
        before=self.files()
        for filename,raw in [('a.docx',b'PK fake'),('a.xlsx',excel([['物资名称','单位'],['A','盘']],True)),('a.pdf',pdf([])),('a.doc',b'legacy')]:
            with self.assertRaises(ValueError):self.upload('item',raw,filename)
        self.assertEqual(before,self.files())
        self.upload('item','物资名称,单位,数量,未映射\n错,盘,nan,x\n错,盘,2,x'.encode());d=json.loads(self.checked()[-1][0].content_json)['details'];self.assertEqual('失败',d['state']);self.assertGreaterEqual(len(d['errors']),3)
        before=self.files()
        with self.assertRaises(ValueError):self.stage('draft',1)
        self.assertEqual(before,self.files())
    def test_preview_publish_safe_rollback_keeps_unrelated_later_records(self):
        self.upload('supply_point','物资点,位置\n新匿名库,匿名A'.encode());self.stage('draft',1);self.stage('publish',2)
        self.change(oid='edit-point',request='later-other',details=dict(data_type='supply_point',row=dict(name='后续另一库',location='匿名B')))
        self.stage('rollback',3);self.assertFalse(any(p.name=='新匿名库' for p in self.state.ledger.points.values()));self.assertTrue(any(p.name=='后续另一库' for p in self.state.ledger.points.values()));self.assertEqual(5,len(self.checked()))
        self.state=self.new_state();self.assertEqual(5,len(catalog.inspect(self.state.ledger,self.state.supply_event_spaces())))
    def test_same_record_later_edit_and_referenced_addition_block_rollback(self):
        self.upload('supply_point','物资点,位置\n新匿名库,匿名A'.encode());self.stage('draft',1);self.stage('publish',2)
        self.change(oid='edit-point',request='later-same',details=dict(data_type='supply_point',row=dict(name='新匿名库',location='匿名新值')));before=self.files()
        with self.assertRaisesRegex(ValueError,'后续变更'):self.stage('rollback',3)
        self.assertEqual(before,self.files())
        self.upload('item','物资名称,单位,数量\n已引用新物资,盘,2'.encode(),oid='import-item',request='up-item');self.stage('draft',1,'import-item','draft-item');self.stage('publish',2,'import-item','pub-item')
        self.change(oid='edit-carry',request='later-carry',details=dict(data_type='carry',row=dict(squad=self.state.ledger.squads[self.source_squad].name,item_name='已引用新物资',quantity='2')))
        before=self.files()
        with self.assertRaisesRegex(ValueError,'引用'):self.stage('rollback',3,'import-item','rollback-item')
        self.assertEqual(before,self.files())
    def test_stale_preview_replay_and_revocation_do_not_mutate(self):
        first=self.upload('company','公司名称,资料来源,服务/物资\n匿名公司,匿名表,水带'.encode());before=self.files();self.assertEqual(first,self.upload('company','公司名称,资料来源,服务/物资\n匿名公司,匿名表,水带'.encode()));self.assertEqual(before,self.files())
        with self.assertRaisesRegex(ValueError,'请求号'):
            self.upload('company','公司名称,资料来源,服务/物资\n匿名公司,匿名表,水带'.encode(),oid='import-other')
        with self.assertRaisesRegex(ValueError,'编号'):
            self.change(oid='import-test',version=1,request='edit-import-batch')
        with self.assertRaisesRegex(ValueError,'编号'):
            self.upload('company','公司名称,资料来源\n别名,匿名表'.encode(),oid='edit-hidden-batch',request='wrong-kind-upload')
        self.assertEqual(before,self.files())
        self.stage('draft',1);self.change();before=self.files()
        with self.assertRaisesRegex(ValueError,'库已变化'):self.stage('publish',2)
        self.assertEqual(before,self.files());self.grant(active=False,expected_version=1,request_id='catalog-revoke');before=self.files()
        with self.assertRaises(PermissionError):self.stage('publish',2)
        self.assertEqual(before,self.files())
    def test_company_publish_is_unverified_and_keeps_physical_inventory(self):
        initial=self.state.ledger.snapshots[self.event_id].to_dict() if hasattr(self.state.ledger.snapshots[self.event_id],'to_dict') else self.state.ledger.to_dict()['snapshots']
        self.upload('company','公司名称,资料来源,服务/物资\n匿名公司,匿名表,水带'.encode());self.stage('draft',1);self.stage('publish',2)
        company=next(c for c in self.state.ledger.companies.values() if c.name=='匿名公司');self.assertEqual('unverified',company.verification_state);self.assertEqual(self.OTHER,company.recorded_by)
        self.assertEqual(initial,self.state.ledger.to_dict()['snapshots']);self.assertFalse(self.state.ledger.supply_records)
    def test_granted_long_term_scope_survives_event_close_without_reopening_combat(self):
        from dataclasses import replace
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='closed');self.state._save_registry()
        r=self.change();self.assertEqual('closed',self.state.registry.events[self.event_id].status);self.assertEqual(1,len(self.checked()))
        with self.assertRaises(Exception):self.state.handle_formal_task_feedback(self.event_id,member_token=self.tokens[self.OTHER],order_id='missing',action='progress',basis='越界',expected_version=0,request_id='bad-combat')
    def test_mirror_catalog_tamper_and_generic_forgery_fail_closed(self):
        r=self.change();space=self.state._appointment_space(self.event_id)[0]
        with self.assertRaises(PermissionError):space.record_object_revision(r,expected_version=0)
        space.object_revisions.pop(r.id);save_workspace(space,self.root/'snapshots'/f'{self.event_id}.db');before=self.files()
        with self.assertRaises(ValueError):self.change(version=1,request='mirror-broken')
        self.assertEqual(before,self.files())
    def test_postwrite_failure_and_interrupt_restore_before_retry(self):
        import panda_brain.persistence as persistence
        before=self.files();save=persistence.save_workspace
        def fail(*args,**kw):save(*args,**kw);raise OSError('anonymous catalog postwrite')
        with patch.object(persistence,'save_workspace',side_effect=fail):
            with self.assertRaises(OSError):self.change()
        self.assertEqual(before,self.files())
        class Interrupted(BaseException):pass
        def interrupt(*args,**kw):save(*args,**kw);raise Interrupted()
        with patch.object(persistence,'save_workspace',side_effect=interrupt):
            with self.assertRaises(Interrupted):self.change()
        with self.assertRaises(Exception):self.change()
        self.state=self.new_state();self.tokens[self.OTHER],_=self.state.member_login(self.OTHER,'123456');self.assertEqual(before,self.files());self.change();self.assertEqual(1,len(self.checked()))
    def test_real_http_actual_multipart_csrf_unknown_duplicate_and_readonly(self):
        server=make_server(self.state,port=0);threading.Thread(target=server.serve_forever,daemon=True).start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        token=self.tokens[self.OTHER];common=dict(csrf=self.state.member_session(token).custody_csrf,operation='upload',object_id='import-http',expected_version='0',request_id='http-file',basis='匿名真实上传依据',data_type='item')
        def send(fields,raw=b'',ctype='application/x-www-form-urlencoded',use=token):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);c.request('POST','/materials/records',raw or urlencode(fields,doseq=True).encode(),{'Content-Type':ctype,'Cookie':'member_session='+use});resp=c.getresponse();status=resp.status;resp.read();c.close();return status
        parts=[]
        for k,v in common.items():parts.append(f'--anonymous-boundary\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        raw=b''.join(parts)+b'--anonymous-boundary\r\nContent-Disposition: form-data; name="file"; filename="a.docx"\r\nContent-Type: application/octet-stream\r\n\r\n'+word([['物资名称','单位'],['HTTP文件物资','盘']])+b'\r\n--anonymous-boundary--\r\n'
        self.assertEqual(303,send({},raw,'multipart/form-data; boundary=anonymous-boundary'));self.assertEqual('预览',self.checked()[-1][1]['details']['state'])
        fields=dict(csrf=common['csrf'],operation='draft',object_id='import-http',expected_version='1',request_id='http-draft',basis='匿名预览查错确认')
        self.assertEqual(403,send(fields|dict(csrf='bad')));self.assertEqual(409,send(fields|dict(expected_version=['1','1'])));self.assertEqual(409,send(fields|dict(scope='personnel')));self.assertEqual(403,send(fields,use=self.tokens[self.DEPUTY]));self.assertEqual(303,send(fields))
        self.assertEqual(200,render_page(self.state,route='/materials/records',member_token=token)[0]);self.assertIn('只读',render_page(self.state,route='/materials/records',member_token=self.tokens[self.DEPUTY])[1])
