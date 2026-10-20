"""P16-T18 验收：文件导入流水线（定版 04 号 §8）。

覆盖：上传解析→自动映射→预览；逐行错误清单→失败态；生成草稿（库级查重）；
授权发布写长期库+回滚快照；回滚恢复；Word/Excel/PDF 入口显式拒绝（待接入）；
留痕字段齐全（哈希/上传人/时间/版本/发布人）；活跃事件时审计镜像入哈希链；
成员 403。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.data_imports import ImportLedger
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p16-import-secret"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _http(method, url, form=None, cookie=""):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8"), resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers.get("Set-Cookie", "")


CSV_OK = "姓名,手机号,单位,岗位\r\n张三,13912340001,林场哨所,扑救一组\r\n李四,13912340002,林场哨所,扑救二组"
CSV_BAD = "姓名,手机号,单位,岗位\r\n,13912340003,X,Y\r\n王五,abc,X,Y"


class ImportPipelineE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.imports_path = tmp / "imports.json"
        self.state = PanelState(
            ScenarioWorkspace("p16-import", general_auth=GeneralEntryAuth(secret=_SECRET)),
            auth=GeneralEntryAuth(secret=_SECRET),
            registry_path=tmp / "reg.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
            imports_path=self.imports_path,
        )
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)
        _, _, raw = _http("POST", f"{self.base}/login",
                          {"username": "admin", "secret": _SECRET})
        self.cmd = raw.split(";")[0]

    def _post(self, route, form):
        return _http("POST", f"{self.base}{route}", form, cookie=self.cmd)[:2]

    # -- 状态机主路径 ---------------------------------------------------------

    def test_upload_preview_draft_publish(self):
        st, body = self._post("/imports/upload",
                              {"data_type": "personnel", "filename": "r.csv", "content": CSV_OK})
        self.assertEqual(303, st)
        batch = self.state.imports.batches[0]
        self.assertEqual("预览", batch.status)
        self.assertEqual({"name": "姓名", "phone": "手机号",
                          "unit": "单位", "home_position": "岗位"}, batch.mapping)
        st, _ = self._post("/imports/draft", {"batch_id": batch.batch_id})
        self.assertEqual(303, st)
        self.assertEqual("已生成草稿", batch.status)
        st, _ = self._post("/imports/publish", {"batch_id": batch.batch_id})
        self.assertEqual(303, st)
        self.assertEqual("已发布", batch.status)
        self.assertEqual(2, len(self.state.registry.library))
        self.assertEqual(1, batch.version)
        self.assertTrue(batch.published_at)
        # 留痕（§8.2）：哈希/上传人/文件名
        self.assertEqual(64, len(batch.content_sha256))
        self.assertEqual("指挥员", batch.uploaded_by)

    def test_row_errors_block_whole_batch(self):
        self._post("/imports/upload",
                   {"data_type": "personnel", "filename": "bad.csv", "content": CSV_BAD})
        batch = self.state.imports.batches[0]
        self.assertEqual("失败", batch.status)
        self.assertTrue(any("为空" in e for e in batch.errors))
        self.assertTrue(any("手机号格式错" in e for e in batch.errors))
        # 失败批次不可直接生成草稿
        st, body = self._post("/imports/draft", {"batch_id": batch.batch_id})
        self.assertEqual(409, st)
        self.assertEqual(0, len(self.state.registry.library))

    def test_publish_requires_draft_state(self):
        self._post("/imports/upload",
                   {"data_type": "personnel", "filename": "r.csv", "content": CSV_OK})
        bid = self.state.imports.batches[0].batch_id
        st, _ = self._post("/imports/publish", {"batch_id": bid})
        self.assertEqual(409, st)  # 预览态跳步被拒

    def test_binary_format_rejected_honestly(self):
        st, body = self._post("/imports/upload",
                              {"data_type": "personnel", "filename": "花名册.docx",
                               "content": "PK\x03\x04"})
        self.assertEqual(409, st)
        self.assertIn("待接入", body)
        self.assertEqual(0, len(self.state.imports.batches))  # 拒绝不入库

    def test_rollback_restores_and_returns_to_draft(self):
        self._post("/imports/upload",
                   {"data_type": "personnel", "filename": "r.csv", "content": CSV_OK})
        b = self.state.imports.batches[0]
        self._post("/imports/draft", {"batch_id": b.batch_id})
        self._post("/imports/publish", {"batch_id": b.batch_id})
        self.assertEqual(2, len(self.state.registry.library))
        st, _ = self._post("/imports/rollback", {"batch_id": b.batch_id})
        self.assertEqual(303, st)
        self.assertEqual(0, len(self.state.registry.library))
        self.assertEqual("已生成草稿", b.status)
        self.assertTrue(b.rolled_back)
        # 修正后可重新发布（版本递增）
        st, _ = self._post("/imports/publish", {"batch_id": b.batch_id})
        self.assertEqual(2, len(self.state.registry.library))
        self.assertEqual(2, b.version)

    # -- 审计镜像与持久化 -----------------------------------------------------

    def test_publish_mirrors_into_active_event_chain(self):
        rec = self.state.registry.create_event(
            name="导入演练事件", report_no="RPT-IMP-1", discovered_at="2026-08-24T09:00:00Z",
            source="演示数据", location="某山林场")
        from tests.duty_support import duty_token
        self.state.handle_event_action(rec.event_id, "start",
                                       duty_token=duty_token(self.state))
        self.state.handle_enter_event(rec.event_id)
        status,_=self._post("/imports/upload", {"data_type":"personnel","filename":"r.csv","content":CSV_OK})
        self.assertEqual(403,status)
        self.assertFalse(self.state.imports.batches)
        from tests.personnel_support import named_actor
        token=named_actor(self.state,rec.event_id)
        status,_,_=_http("POST",self.base+"/personnel/manage",dict(csrf=self.state.member_session(token).custody_csrf,
            event_id=rec.event_id,operation="import_library",csv_text=CSV_OK,basis="匿名具名人员导入",
            expected_version="0",request_id="named-import-mirror"),cookie="member_session="+token)
        self.assertEqual(303,status)
        from panda_brain.personnel_management import inspect
        self.assertEqual("import_library",inspect(self.state.workspace)[-1][2]["operation"])
        self.assertEqual((), self.state.workspace.verify_audit_chain())

    def test_ledger_persists_across_reload(self):
        self._post("/imports/upload",
                   {"data_type": "personnel", "filename": "r.csv", "content": CSV_OK})
        reloaded = ImportLedger.load(self.imports_path)
        self.assertEqual(1, len(reloaded.batches))
        again = reloaded.batches[0]
        self.assertEqual("预览", again.status)
        self.assertEqual({"name": "姓名", "phone": "手机号",
                         "unit": "单位", "home_position": "岗位"}, again.mapping)

    def test_anonymous_and_member_forbidden_and_page_render(self):
        # 导入维护与物资底账同门：非指挥员一律 403（fail-closed）
        st, _ = _http("POST", f"{self.base}/imports/upload",
                      {"data_type": "personnel", "content": CSV_OK})[:2]
        self.assertEqual(403, st)
        st, _ = _http("POST", f"{self.base}/imports/upload",
                      {"data_type": "personnel", "content": CSV_OK},
                      cookie="member_session=fake")[:2]
        self.assertEqual(403, st)
        self.assertEqual(0, len(self.state.imports.batches))
        st, body = _http("GET", f"{self.base}/materials", cookie=self.cmd)[:2]
        self.assertEqual(200, st)
        self.assertIn("文件导入", body)
        self.assertIn("上传并解析", body)


if __name__ == "__main__":
    unittest.main()
