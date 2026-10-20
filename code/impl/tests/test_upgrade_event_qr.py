"""Decode actual generated symbols and verify real record/event validity on every GET."""
from dataclasses import replace
import re
import unittest
from unittest.mock import patch
from panda_brain.event_qr import qr_svg
from panda_brain.fire_demo import confirmed_demo_message
from panda_brain.fire_events import EventRegistry
from tests import test_upgrade_confirmed_fire as support


class EventQRTests(unittest.TestCase):
    DUTY=support.ConfirmedFireTests.DUTY;MEMBER=support.ConfirmedFireTests.MEMBER
    setUp=support.ConfirmedFireTests.setUp
    start_http=support.ConfirmedFireTests.start_http
    request=support.ConfirmedFireTests.request
    login=support.ConfirmedFireTests.login
    bytes=support.ConfirmedFireTests.bytes

    def create_event(self):
        m=confirmed_demo_message(source_record_id='full-fields-fixture',timestamp='2026-10-12T10:00:00+08:00',report_no='DEMO-ALARM-001')
        result=self.state.handle_confirmed_fire_event(m)
        self.assertEqual('CREATED',result['code']);return result['event_id'],m

    def start_event(self):
        eid,m=self.create_event()
        token,error=self.state.member_login(self.DUTY,'123456');self.assertEqual('',error)
        qr=self.state.handle_event_action(eid,'start',duty_token=token)['qr_id']
        return eid,qr,m

    def test_complete_structured_demo_fields_without_starting(self):
        eid,m=self.create_event();event=self.state.registry.events[eid]
        for field in ('name','report_no','discovered_at','source','location','level','admin_region','initial_report','key_targets'):
            self.assertTrue(getattr(event,field),field)
        self.assertEqual((30.1234,103.5678),(event.lat,event.lon))
        self.assertIn('演示',event.name);self.assertIn('虚构',event.location)
        self.assertEqual('registered',event.status);self.assertEqual({},self.state.registry.qrs)
        self.assertEqual('DEMO_CONSOLE',m['sourceSystem'])
        self.assertEqual('多模态火情预警系统（演示数据）', event.source)

    def test_generated_svg_decodes_to_actual_registry_id(self):
        import cv2,numpy as np
        for qid in ('qr-001','qr-123456789','qr-'+('9'*40)):
            svg=qr_svg(qid)
            size=int(re.search(r'viewBox="0 0 (\d+)',svg).group(1))
            image=np.full((size,size),255,dtype=np.uint8)
            for x,y in re.findall(r'M(\d+),(\d+)h1v1h-1z',svg):image[int(y),int(x)]=0
            image=cv2.resize(image,None,fx=8,fy=8,interpolation=cv2.INTER_NEAREST)
            decoded,points,_=cv2.QRCodeDetector().detectAndDecode(image)
            self.assertEqual(qid,decoded);self.assertIsNotNone(points)

    def test_svg_rejects_markup_and_ids_outside_registry_format(self):
        for qid in ('<svg onload=alert(1)>','qr-1"','http://example.test', 'qr-'+'1'*65):
            with self.assertRaises(ValueError):qr_svg(qid)

    def test_pending_detail_has_full_fields_and_explicit_no_qr(self):
        eid,_=self.create_event()
        self.state.registry.events[eid] = replace(self.state.registry.events[eid], source='人工模拟登记')
        self.start_http();admin=self.login('admin','confirmed-fire-test');before=self.bytes()
        status,_,body=self.request('GET',f'/events/{eid}/details',cookie=admin)
        self.assertEqual(200,status);self.assertNotIn('未提供',body)
        self.assertIn('等待值班人员启动处置',body);self.assertNotIn('event-qr-symbol',body)
        self.assertIn('多模态火情预警系统（演示数据）', body);self.assertNotIn('人工模拟登记', body)
        self.assertEqual('人工模拟登记', self.state.registry.events[eid].source)
        self.assertEqual(before, self.bytes())

    def test_active_real_code_visible_to_admin_and_duty_without_writes(self):
        eid,qid,_=self.start_event();self.start_http()
        for account,password in [('admin','confirmed-fire-test'),(self.DUTY,'123456')]:
            cookie=self.login(account,password);before=self.bytes()
            for path in ('/events',f'/events/{eid}/details'):
                status,_,body=self.request('GET',path,cookie=cookie)
                self.assertEqual(200,status);self.assertIn(f'aria-label="事件二维码 {qid}"',body)
                self.assertIn('有效期至',body)
                if account!= 'admin' or path.endswith('/details'):
                    self.assertNotIn('name="action" value="revoke-qr"',body)
            self.assertEqual(before,self.bytes())

    def test_revoked_expired_and_closed_codes_are_not_scannable(self):
        eid,qid,_=self.start_event();self.start_http();admin=self.login('admin','confirmed-fire-test')
        original=self.state.registry.qrs[qid]
        for status in ('revoked','expired','closed'):
            self.state.registry.qrs[qid]=original
            if status=='revoked':self.state.registry.revoke_qr(qid,revoked_by='test')
            if status=='expired':self.state.registry.qrs[qid]=replace(original,issued_at='2000-01-01T00:00:00+08:00')
            if status=='closed':self.state.registry.transition(eid,'close',operator='test')
            body=self.request('GET',f'/events/{eid}/details',cookie=admin)[2]
            self.assertNotIn('event-qr-symbol',body);self.assertIn('此码不可用于入岗',body)
            self.assertIn({'revoked':'已撤销','expired':'已过期','closed':'事件已关闭'}[status],body)

    def test_cached_symbol_does_not_bypass_revoke_after_render(self):
        eid,qid,_=self.start_event();self.start_http();admin=self.login('admin','confirmed-fire-test')
        self.assertIn('event-qr-symbol',self.request('GET',f'/events/{eid}/details',cookie=admin)[2])
        self.state.registry.revoke_qr(qid,revoked_by='test')
        self.assertNotIn('event-qr-symbol',self.request('GET',f'/events/{eid}/details',cookie=admin)[2])

    def test_symbol_never_contains_login_credentials_or_identity(self):
        eid,qid,_=self.start_event();svg=qr_svg(qid)
        for forbidden in ('123456','confirmed-fire-test',self.DUTY,'member_session','panel_session','href=','src='):
            self.assertNotIn(forbidden,svg)
        self.start_http();status,_,body=self.request('GET',f'/events/{eid}/details')
        self.assertNotEqual(200,status);self.assertNotIn('event-qr-symbol',body)


if __name__=='__main__':unittest.main()
