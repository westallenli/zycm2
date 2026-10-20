"""Read-only OSMDroid source, coverage, authenticated HTTP and event isolation."""
import hashlib
import json
import math
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from panda_brain.local_satellite import LocalSatellite, index, decode, load_installed
from panda_brain import sandview, offline_resources
from tests.test_upgrade_p16_offline import PNG
from tests import test_upgrade_business_modules as fixtures
Fixture = fixtures.BusinessModulesTests


def database(path):
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE tiles(key INTEGER PRIMARY KEY,provider TEXT,tile BLOB);'
                     'CREATE TABLE ATAK_metadata(key TEXT,value TEXT);')
    db.execute('INSERT INTO ATAK_metadata VALUES (?,?)', ('srid','3857'))
    db.execute('INSERT INTO tiles VALUES (?,?,?)',(0,'Google Satellite Only',PNG))
    # A coarse real coordinate grid; image content is expressly synthetic.
    z=4;n=2**z;x=int((103.5678+180)/360*n);y=int((1-math.asinh(math.tan(math.radians(30.1234)))/math.pi)/2*n)
    for a in range(x-1,x+2):
        for b in range(y-1,y+2):
            db.execute('INSERT INTO tiles VALUES (?,?,?)',(index(z,a,b),'Google Satellite Only',PNG))
    db.commit();db.close()
    return LocalSatellite(path,sha256=hashlib.sha256(path.read_bytes()).hexdigest())


class SatelliteTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'map with space.sqlite'
        self.source=database(self.path)

    def test_index_roundtrip_all_levels_and_non_square_coordinates(self):
        for z in range(23):
            n=2**z
            for x,y in {(0,0),(n-1,0),(0,n-1),(n-1,n-1),(n//2,n//3)}:
                self.assertEqual((z,x,y),decode(index(z,x,y)))
        for invalid in (-1,3,23<<(2*23)):
            with self.assertRaises(ValueError):decode(invalid)

    def test_content_readonly_no_database_sidecars_or_byte_changes(self):
        before=self.path.read_bytes()
        self.assertEqual((PNG,'image/png'),self.source.tile(0,0,0))
        with self.source._db() as db:
            with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM tiles')
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual([self.path],list(self.path.parent.iterdir()))

    def test_sparse_coverage_and_missing_tiles_do_not_stretch_or_fake(self):
        self.assertEqual(4,self.source.coverage_zoom(30.1234,103.5678))
        self.assertEqual(0,self.source.coverage_zoom(-60,-100))
        for coords in ((4,0,0),(5,1,1),(-1,0,0),(0,1,0),(0,0,-1)):
            self.assertIsNone(self.source.tile(*coords))

    def test_digest_and_changed_source_fail_closed(self):
        with self.assertRaises(ValueError):LocalSatellite(self.path,sha256='0'*64)
        self.path.write_bytes(self.path.read_bytes()+b'changed')
        with self.assertRaises(ValueError):self.source.tile(0,0,0)
        with self.assertRaises(ValueError):self.source.coverage_zoom(30,103)

    def test_unknown_projection_and_views_are_rejected(self):
        db=sqlite3.connect(self.path);db.execute("UPDATE ATAK_metadata SET value='4326'");db.commit();db.close()
        with self.assertRaisesRegex(ValueError,'投影'):LocalSatellite(self.path,sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())
        db=sqlite3.connect(self.path);db.executescript("UPDATE ATAK_metadata SET value='3857'; ALTER TABLE tiles RENAME TO original; CREATE VIEW tiles AS SELECT * FROM original;");db.close()
        with self.assertRaisesRegex(ValueError,'普通'):LocalSatellite(self.path,sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_p00_guard_accepts_only_inside_readonly_uri(self):
        # The isolated runner installs the real guard before these tests.
        import sys
        if 'p00_baseline' not in sys.modules:
            self.fail('Run this suite in the guarded P00 runner.')
        with self.source._db() as db:self.assertTrue(db.execute('SELECT count(*) FROM tiles').fetchone()[0])
        with self.assertRaises(PermissionError):sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True)
        with self.assertRaises(PermissionError):sqlite3.connect(Path('C:/outside-p00-satellite.sqlite').as_uri()+'?mode=ro',uri=True)

    def test_explicit_installation_and_no_client_path_selection(self):
        root=self.path.parent
        self.assertIsNone(load_installed(root))
        maps=root/'maps';maps.mkdir();target=maps/'google-satellite.sqlite'
        target.write_bytes(self.path.read_bytes())
        value=dict(file=target.name,sha256=self.source.revision,provider=self.source.provider,basis='模拟用户明确确认本机只读使用')
        manifest=maps/'local-satellite.json';manifest.write_text(json.dumps(value),encoding='utf-8')
        self.assertEqual(PNG,load_installed(root).tile(0,0,0)[0])
        value['file']='../map with space.sqlite';manifest.write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaises(ValueError):load_installed(root)


class SatelliteHttpTests(unittest.TestCase):
    DUTY,DEPUTY,CAPTAIN=Fixture.DUTY,Fixture.DEPUTY,Fixture.CAPTAIN
    MEMBER_A,MEMBER_B,TEMP=Fixture.MEMBER_A,Fixture.MEMBER_B,Fixture.TEMP
    OTHER,OTHER_MEMBER=Fixture.OTHER,Fixture.OTHER_MEMBER
    _demo_event,bytes,server,request,foreign_event,page=Fixture._demo_event,Fixture.bytes,Fixture.server,Fixture.request,Fixture.foreign_event,Fixture.page

    def setUp(self):
        fixtures.BusinessModulesTests.setUp(self)
        self.root = Path(self.state.registry_path).parent
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],lat=30.1234,lon=103.5678)
        self.state._save_registry()
        self.state.local_satellite=database(self.root/'satellite.sqlite')
        self.url='/sand/local-map/'+self.state.local_satellite.revision+'/0/0/0.jpg?event_id='+self.event_id

    def test_all_business_roles_read_same_source_and_preserve_coordinates_and_bytes(self):
        before=self.bytes();payloads=[]
        for phone in (self.DUTY,self.DEPUTY,self.CAPTAIN):
            status,html=self.page('/sand',phone);self.assertEqual(200,status)
            payload=json.loads(re.search(r'id="cmd-map-data">(.*?)</script>',html,re.S)[1]);payloads.append(payload)
            self.assertEqual('satellite',payload['base']);self.assertEqual(4,payload['initialZoom'])
            self.assertEqual([30.1234,103.5678],payload['center'])
            self.assertIn('放大后可能缺图',html);self.assertNotIn(str(self.root),html)
        self.assertEqual(payloads[0],payloads[1]);self.assertEqual(payloads[1],payloads[2])
        self.assertEqual(before,self.bytes())
    def test_http_tiles_authenticated_readonly_and_no_export_or_external_fallback(self):
        from http.client import HTTPConnection
        server=self.server();before=self.bytes()
        for phone in (self.DUTY,self.DEPUTY,self.CAPTAIN):
            conn=HTTPConnection('127.0.0.1',server.server_port,timeout=8)
            try:
                conn.request('GET',self.url,headers={'Cookie':'member_session='+self.tokens[phone]})
                response=conn.getresponse();status,headers,body=response.status,dict(response.getheaders()),response.read()
                with self.state._event_authority_lock:pass
            finally:conn.close()
            self.assertEqual(200,status);self.assertEqual(PNG,body)
            self.assertEqual('image/png',headers['Content-Type']);self.assertEqual('no-store',headers['Cache-Control'])
        self.assertEqual(404,self.request(server,self.url,cookie='invalid=')[0])
        self.assertEqual(before,self.bytes())

    def test_missing_foreign_stale_and_invalid_paths_are_404(self):
        server=self.server();foreign=self.foreign_event();before=self.bytes()
        for url in (self.url.replace(self.event_id,foreign),self.url+'&event_id='+self.event_id,
                    self.url.replace('/0/0/0.jpg','/4/0/0.jpg'),self.url.replace('/0/0/0.jpg','/0/1/0.jpg'),
                    self.url.replace(self.state.local_satellite.revision,'0'*64),
                    self.url.replace('/0/0/0.jpg','/../map.sqlite'),self.url+'&path=C:/other.sqlite'):
            self.assertEqual(404,self.request(server,url,phone=self.CAPTAIN)[0],url)
        self.assertEqual(before,self.bytes())

    def test_event_resource_errors_block_host_source(self):
        with patch.object(offline_resources,'view_status',return_value=dict(resource=None,error='来源异常')):
            html=self.page('/sand',self.CAPTAIN)[1]
            payload=json.loads(re.search(r'id="cmd-map-data">(.*?)</script>',html,re.S)[1])
            self.assertEqual('unavailable',payload['base']);self.assertIsNone(payload['satellite'])
            self.assertEqual(404,self.request(self.server(),self.url,phone=self.CAPTAIN)[0])

    def test_event_resource_revocation_history_cannot_be_overridden(self):
        with patch.object(offline_resources,'inspect',return_value=((0,None,{'operation':'revoke'}),)):
            self.assertIsNone(sandview._local_satellite.description(self.state,[30.1234,103.5678]))

    def test_expired_event_package_is_not_revived_and_new_host_source_is_independent(self):
        with patch.object(offline_resources,'inspect',return_value=((0,None,{'operation':'import'}),)), \
                patch.object(offline_resources,'view_status',return_value=dict(resource=None,error='')), \
                patch.object(offline_resources,'active_for',return_value=None):
            status,html=self.page('/sand',self.CAPTAIN);self.assertEqual(200,status)
            payload=json.loads(re.search(r'id="cmd-map-data">(.*?)</script>',html,re.S)[1])
            self.assertEqual('satellite',payload['base']);self.assertIsNone(payload['offline'])
            self.assertIn('/sand/local-map/',payload['satellite']['url'])
            self.assertEqual(404,self.request(self.server(),'/offlinemap/0/0/0.png',phone=self.CAPTAIN)[0])

    def test_broken_host_source_reports_unavailable_without_business_changes(self):
        self.state.local_satellite.path.write_bytes(self.state.local_satellite.path.read_bytes()+b'changed')
        before=self.bytes();html=self.page('/sand',self.CAPTAIN)[1]
        self.assertIn('本机离线地图暂不可读取',html)
        self.assertEqual(404,self.request(self.server(),self.url,phone=self.CAPTAIN)[0])
        self.assertEqual(before,self.bytes())


del Fixture
