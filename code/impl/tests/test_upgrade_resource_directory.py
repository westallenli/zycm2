"""Directory imports preserve identity, permissions, history and file round trips."""
import hashlib
import http.client
import io
import threading
import unittest
from copy import deepcopy
from urllib.parse import urlencode
from panda_brain import resource_directory as directory
from panda_brain import resource_maintenance as daily
from panda_brain.document_tables import extract
from panda_brain.resource_directory_page import xlsx,columns,data_rows
from panda_brain.panel import make_server,render_page
from . import test_upgrade_manual_material_transfers as fixture


class DirectoryTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','new_state','event_space','setUp'):
        locals()[name]=getattr(fixture.NamedTransferTests,name)

    def text(self):
        return directory.csv_text(directory.PEOPLE_HEADERS,[['演示甲','13911110001','示范甲市','演示单位','演示第三中队','队长','是'],['演示乙','13911110002','示范甲市','演示单位','演示第三中队','队员','否']])

    def change(self,content=None,kind='person_directory',token=None,**override):
        text=content or self.text();n=len(self.state.ledger.daily_resource_records)
        values=dict(operation='directory_import',details=dict(data_type=kind,content=text,content_sha256=hashlib.sha256(text.encode()).hexdigest()),basis='模拟导入',expected_version=n,request_id='directory-'+str(n))
        return self.state.handle_daily_resources(member_token=token or self.tokens[self.DUTY],**(values|override))

    def test_import_groups_same_identity_without_arrival(self):
        arrival=deepcopy(self.state.ledger.snapshots);self.change()
        squad=next(s for s in self.state.ledger.squads.values() if s.name=='演示第三中队')
        self.assertEqual(('13911110001','13911110002'),squad.member_phones)
        self.assertEqual('13911110001',squad.leader_phone)
        self.assertEqual(arrival,self.state.ledger.snapshots)
        self.assertEqual('示范甲市',self.state.ledger.resource_metadata['people']['13911110001']['city'])
        self.state=self.new_state();daily.journal(self.state.ledger)
        self.assertIn('13911110002',self.state.registry.library)

    def test_xlsx_and_csv_round_trip_and_formula_as_text(self):
        self.change()
        for kind in ('person_directory','squad_directory','item','carry','supply_point','company','stock'):
            values=data_rows(self.state,kind)
            self.assertEqual(directory.csv_text(columns(kind),values),extract('table.xlsx',xlsx(columns(kind),values)))
        self.assertIn('=1+1',extract('safe.xlsx',xlsx(['姓名'],[['=1+1']])))

    def test_batch_invalid_leader_or_phone_keeps_both_files(self):
        before=(self.state.registry_path.read_bytes(),self.state.materials_path.read_bytes())
        for text in (self.text().replace('队长,是','队长,否'),self.text().replace('13911110002','bad'),self.text()+self.text().splitlines()[1]):
            with self.assertRaises((ValueError,KeyError)):self.change(text)
        self.assertEqual(before,(self.state.registry_path.read_bytes(),self.state.materials_path.read_bytes()))

    def test_inactive_person_blocked_and_recovered_without_erasing(self):
        self.change();token,error=self.state.member_login('13911110002','123456');self.assertFalse(error)
        self.change(operation='status',details=dict(kind='people',identifier='13911110002',enabled=False))
        with self.assertRaises(PermissionError):self.state.authorization_subject(token)
        self.assertIsNone(self.state.member_login('13911110002','123456')[0])
        self.assertIn('13911110002',self.state.registry.library)
        self.change(operation='status',details=dict(kind='people',identifier='13911110002',enabled=True))
        self.assertIsNotNone(self.state.member_login('13911110002','123456')[0])

    def test_active_team_and_self_cannot_be_disabled(self):
        for kind,key in [('squads',self.source_squad),('people',self.SOURCE),('people',self.DUTY)]:
            with self.assertRaises(ValueError):self.change(operation='status',details=dict(kind=kind,identifier=key,enabled=False))
        self.assertEqual(0,len(self.state.ledger.daily_resource_records))

    def test_readonly_cannot_import_people_or_download(self):
        with self.assertRaises(PermissionError):self.change(token=self.tokens[self.DEPUTY])
        code,page=render_page(self.state,'/materials/daily',member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200,code);self.assertNotIn('新增人员',page);self.assertNotIn('原人员安排与维护入口',page)

    def test_unrelated_legacy_squad_does_not_block_new_person(self):
        self.state.ledger.add_squad(name='旧待完善中队',member_phones=(),library=self.state.registry.library)
        self.state._save_ledger()
        text=directory.csv_text(directory.PEOPLE_HEADERS,[['模拟独立人员','13911110009','示范甲市','演示单位','','联络','否']])
        self.change(text);self.assertIn('13911110009',self.state.registry.library)

    def test_squad_edit_preserves_original_position_slots(self):
        from dataclasses import replace
        self.change();s=next(s for s in self.state.ledger.squads.values() if s.name=='演示第三中队')
        self.state.ledger.squads[s.squad_id]=replace(s,position_slots=('pjjz-1',));self.state._save_ledger()
        text=directory.csv_text(directory.SQUAD_HEADERS,[[s.name,','.join(s.member_phones),s.leader_phone,3,'模拟更新联系说明']])
        self.change(text,kind='squad_directory')
        self.assertEqual(('pjjz-1',),self.state.ledger.squads[s.squad_id].position_slots)

    def test_unchanged_called_squad_can_be_previewed_but_cannot_be_replaced(self):
        s=self.state.ledger.squads[self.source_squad]
        def content(contact):return directory.csv_text(directory.SQUAD_HEADERS,[[s.name,','.join(s.member_phones),s.leader_phone,s.headcount_planned,contact]])
        changes=directory.apply_squads(self.state,deepcopy(self.state.ledger),deepcopy(self.state.registry),content(s.contact))
        self.assertEqual(changes[0]['before'],changes[0]['after'])
        with self.assertRaisesRegex(ValueError,'活动火情'):self.change(content('改动现场编制'),kind='squad_directory')

    def test_arrival_projection_uses_captain_checkin_over_preplanned_roster(self):
        from panda_brain.org_float import projected_rows
        from panda_brain.panel import _deputy_event_context
        with _deputy_event_context(self.state,'/materials/event',member_token=self.tokens[self.DEPUTY]):
            rows={r['phone']:r for r in projected_rows(self.state,self.event_id)}
        self.assertTrue(rows[self.SOURCE]['is_arrived'])
        self.assertEqual('已到场',rows[self.SOURCE]['status_word'])

    def test_disabled_company_not_recommended_or_listed_on_event(self):
        from panda_brain.resource_advisor import candidates
        from panda_brain.resource_pages import company_directory
        c=self.state.ledger.add_external_company(name='演示已停用公司',services='模拟水带',source='模拟测试名录',recorded_by=self.DUTY);self.state._save_ledger()
        self.change(operation='status',details=dict(kind='companies',identifier=c.company_id,enabled=False))
        self.assertNotIn('演示已停用公司',company_directory(self.state))
        self.assertFalse(candidates(self.state.ledger,self.item_id,1,None)[1])
        self.assertIn(c.company_id,self.state.ledger.companies)

    def test_http_xlsx_upload_preview_confirm_and_download(self):
        from html.parser import HTMLParser
        class Inputs(HTMLParser):
            def __init__(self):super().__init__();self.values={}
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='input' and attrs.get('type')=='hidden':self.values[attrs['name']]=attrs.get('value','')
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        token=self.tokens[self.DUTY]
        def request(method,url,body=None,ctype=None,use=token):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            headers={'Cookie':'member_session='+use}
            if ctype:headers['Content-Type']=ctype
            c.request(method,url,body,headers);r=c.getresponse();result=r.status,r.read();c.close();return result
        fields=dict(csrf=self.state.member_session(token).custody_csrf,operation='directory_import',expected_version='0',request_id='upload-1',expected_source_digest=daily.source_digest(self.state),basis='模拟Excel',data_type='person_directory')
        boundary='directory-test-boundary';body=b''
        for k,v in fields.items():body+=('--'+boundary+'\r\nContent-Disposition: form-data; name="'+k+'"\r\n\r\n'+v+'\r\n').encode()
        import csv
        data=list(csv.reader(io.StringIO(self.text())))
        body+=('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="people.xlsx"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()+xlsx(data[0],data[1:])+('\r\n--'+boundary+'--\r\n').encode()
        code,page=request('POST','/materials/daily/preview',body,'multipart/form-data; boundary='+boundary)
        self.assertEqual(200,code,page.decode());self.assertNotIn('13911110002',self.state.registry.library)
        parser=Inputs();parser.feed(page.decode())
        code,page=request('POST','/materials/daily/confirm',urlencode(parser.values).encode(),'application/x-www-form-urlencoded');self.assertEqual(303,code,page.decode())
        self.assertIn('13911110002',self.state.registry.library)
        url='/materials/daily/download?kind=person_directory&mode=current&format=xlsx'
        code,raw=request('GET',url);self.assertEqual(200,code);self.assertIn('13911110002',extract('people.xlsx',raw))
        self.assertEqual(403,request('GET',url,use=self.tokens[self.DEPUTY])[0])

    def test_table_edit_dialog_submits_same_person_and_preserves_arrival(self):
        from html.parser import HTMLParser
        class Forms(HTMLParser):
            def __init__(self):super().__init__();self.forms=[];self.current=None
            def handle_starttag(self,tag,attrs):
                a=dict(attrs)
                if tag=='form':self.current=dict(action=a.get('action'),fields={})
                elif tag=='input' and self.current is not None:self.current['fields'][a.get('name')]=a.get('value','')
            def handle_endtag(self,tag):
                if tag=='form' and self.current is not None:self.forms.append(self.current);self.current=None
        self.change();before=deepcopy(self.state.ledger.snapshots)
        code,html=render_page(self.state,'/materials/daily',member_token=self.tokens[self.DUTY]);self.assertEqual(200,code)
        forms=Forms();forms.feed(html)
        form=next(f for f in forms.forms if f['fields'].get('operation')=='person_edit' and f['fields'].get('phone')=='13911110002')
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def send(url,values):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            c.request('POST',url,urlencode(values).encode(),{'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+self.tokens[self.DUTY]})
            r=c.getresponse();result=r.status,r.read().decode();c.close();return result
        c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
        c.request('GET','/static/resource-directory.js');asset=c.getresponse()
        self.assertEqual(200,asset.status);self.assertIn('showModal',asset.read().decode());c.close()
        fields=form['fields']|dict(name='演示乙更新',basis='模拟表格修改')
        code,preview=send(form['action'],fields);self.assertEqual(200,code)
        forms=Forms();forms.feed(preview);confirm=next(f for f in forms.forms if f['action']=='/materials/daily/confirm')
        self.assertEqual(303,send(confirm['action'],confirm['fields'])[0])
        self.assertEqual('演示乙更新',self.state.registry.library['13911110002'].name)
        self.assertEqual('演示甲',self.state.registry.library['13911110001'].name)
        self.assertEqual(before,self.state.ledger.snapshots)

    def test_duty_fixed_entry_does_not_grant_other_roles_maintenance(self):
        for who in (self.DUTY,self.DEPUTY,self.SOURCE):
            code,html=render_page(self.state,'/materials',member_token=self.tokens[who]);self.assertEqual(200,code)
            self.assertEqual(who==self.DUTY,'data-daily-maintenance-entry' in html)
        with self.assertRaises(PermissionError):self.change(token=self.tokens[self.SOURCE])


if __name__=='__main__':unittest.main()
