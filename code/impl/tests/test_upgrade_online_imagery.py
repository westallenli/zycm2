"""Same-origin imagery transport, permission rechecks, and narrow egress."""
import io
import socket
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch
from tests.test_upgrade_p16_offline import PNG


class ImageryTests(unittest.TestCase):
    def test_disabled_runtime_never_resolves_or_fetches(self):
        from panda_brain import online_imagery as imagery, runtime_access as access
        with patch.object(access, '_runtime_enabled', False), patch.object(access.socket,'getaddrinfo') as dns:
            with self.assertRaises(PermissionError):imagery.fetch_tile(5,26,12)
            dns.assert_not_called()

    def test_fixed_host_no_proxy_redirect_credentials_or_business_parameters(self):
        from panda_brain import online_imagery as imagery, runtime_access as access
        response=io.BytesIO(PNG)
        with patch.object(access,'_runtime_enabled',True), patch.object(access.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.4.4',443))]), patch.object(imagery.urllib.request,'build_opener') as build:
            build.return_value.open.return_value=response
            self.assertEqual((PNG,'image/png'),imagery.fetch_tile(5,26,12))
            handlers=build.call_args.args
            self.assertEqual({},handlers[0].proxies)
            from panda_brain.model_connections import NoRedirect
            self.assertIsInstance(handlers[1],NoRedirect)
            req=build.return_value.open.call_args.args[0]
            self.assertRegex(req.full_url,r'^https://webst0[1-4]\.is\.autonavi\.com/appmaptile\?style=6&x=26&y=12&z=5$')
            self.assertEqual('GET',req.get_method())
            self.assertNotIn('Authorization',req.headers)
            self.assertNotIn('Cookie',req.headers)
            self.assertNotIn('Referer',req.headers)

    def test_invalid_coordinates_private_dns_and_non_images_are_refused(self):
        from panda_brain import online_imagery as imagery, runtime_access as access
        for values in ((19,1,1),(5,32,1),(5,-1,1),(True,1,1),(5,1,'2')):
            with self.subTest(values=values),self.assertRaises(ValueError):imagery.fetch_tile(*values)
        with patch.object(access,'_runtime_enabled',True),patch.object(access.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError):imagery.fetch_tile(5,26,12)
        for raw in (b'<html>unavailable</html>',b'x'*(1024*1024+1)):
            with patch.object(access,'_runtime_enabled',True),patch.object(access.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.4.4',443))]),patch.object(imagery.urllib.request,'build_opener') as build:
                build.return_value.open.return_value=io.BytesIO(raw)
                with self.assertRaises(ValueError):imagery.fetch_tile(5,26,12)

    def test_image_egress_is_thread_scoped_official_host_only_and_resets(self):
        from panda_brain import runtime_access as a
        with patch.object(a,'_runtime_enabled',True),patch.object(a.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.4.4',443))]):
            for host in ('evil.test','api.deepseek.com','webst01.is.autonavi.com.evil.test'):
                with self.assertRaises(ValueError):
                    with a.map_image_egress(host):pass
            with a.map_image_egress('webst01.is.autonavi.com'):
                self.assertTrue(a.permit_socket_event('socket.getaddrinfo',('webst01.is.autonavi.com',443)))
                self.assertTrue(a.permit_socket_event('socket.connect',(None,('8.8.4.4',443))))
                for event,args in (('socket.getaddrinfo',('api.deepseek.com',443)),('socket.getaddrinfo',('webst02.is.autonavi.com',443)),('socket.connect',(None,('8.8.4.4',80))),('socket.connect',(None,('127.0.0.1',443))),('socket.bind',(None,('8.8.4.4',443)))):
                    self.assertFalse(a.permit_socket_event(event,args))
                other=[];thread=threading.Thread(target=lambda:other.append(a.permit_socket_event('socket.connect',(None,('8.8.4.4',443)))));thread.start();thread.join();self.assertEqual([False],other)
            self.assertFalse(a.permit_socket_event('socket.connect',(None,('8.8.4.4',443))))


class ImageryHttpTests(unittest.TestCase):
    def setUp(self):
        from tests.test_upgrade_task_release import TaskReleaseTests
        from panda_brain.panel import make_server
        self.f=TaskReleaseTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.state=self.f.state;self.token=self.f.tokens[self.f.DEPUTY];self.eid=self.f.event_id
        self.server=make_server(self.state,port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def get(self,path=None,token=None):
        conn=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            conn.request('GET',path or '/sand/online-imagery/5/26/12.jpg?event_id='+self.eid,headers={'Cookie':'member_session='+(token or self.token)})
            r=conn.getresponse();return r.status,r.read(),dict(r.getheaders())
        finally:conn.close()

    def test_named_current_event_image_is_readonly_and_does_not_hold_business_lock(self):
        from panda_brain import online_imagery as imagery
        before=self.f.files();entered=threading.Event();release=threading.Event();output=[]
        def fetch(*args):entered.set();self.assertTrue(release.wait(4));return PNG,'image/png'
        with patch.object(imagery,'fetch_tile',side_effect=fetch):
            client=threading.Thread(target=lambda:output.append(self.get()));client.start()
            try:
                self.assertTrue(entered.wait(3))
                acquired=self.state._event_authority_lock.acquire(timeout=.3)
                self.assertTrue(acquired,'remote I/O must not lock business requests')
                if acquired:self.state._event_authority_lock.release()
            finally:release.set();client.join(5)
        self.assertEqual((200,PNG),output[0][:2]);self.assertEqual('no-store',output[0][2]['Cache-Control'])
        self.assertEqual(before,self.f.files())

    def test_foreign_event_guest_invalid_path_and_extra_query_never_fetch(self):
        from panda_brain import online_imagery as imagery
        with patch.object(imagery,'fetch_tile') as fetch:
            for path,token in (('/sand/online-imagery/5/26/12.jpg?event_id=other',None),('/sand/online-imagery/19/26/12.jpg?event_id='+self.eid,None),('/sand/online-imagery/5/26/12.jpg?event_id='+self.eid+'&url=https://evil.test',None),(None,'unknown-session')):
                self.assertEqual(404,self.get(path,token)[0])
            fetch.assert_not_called()

    def test_logout_during_fetch_discards_result_and_upstream_failure_returns_error(self):
        from panda_brain import online_imagery as imagery
        def logout(*args):self.state.member_logout(self.token);return PNG,'image/png'
        with patch.object(imagery,'fetch_tile',side_effect=logout):self.assertEqual(404,self.get()[0])

    def test_upstream_timeout_returns_error_without_touching_business(self):
        from panda_brain import online_imagery as imagery
        before=self.f.files()
        with patch.object(imagery,'fetch_tile',side_effect=TimeoutError('anonymous timeout')):
            self.assertEqual(503,self.get()[0])
        self.assertEqual(before,self.f.files())

