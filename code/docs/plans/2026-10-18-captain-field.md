# Captain field App Implementation Plan

**Goal:** Approved 0.2 Android field workflow: full-squad arrival scan, sand table, team commands with completion replies, names-only material shortages, event information assistant.

**Architecture:** Native three-tab app (scene/commands/assistant), native QR camera reader and tile canvas; five narrow authenticated JSON operations. Reuse current captain/event scopes and transactional persistence. Scene uses existing event map and visible marks. Team commands are projected by responsible group, actor identity only records the captain who operates. Completion reports remain explicit pending-verification claims and never silently close published commands. Names-only shortages are reports, not inventory deductions or inferred quantities. Surface reports in existing computer task-feedback and event-material modules.

**Tech Stack:** Existing verified JDK/Android SDK, Java platform views, ZXing QR decoder with verified dependency checksum/license, Python standard library and current business modules. Same signing identity, versionCode2.

1. Preserve current source/data/config/map hashes, Git diff, 0.1 Android source/APK. Use a new isolated current-data root and read-only map hardlink.
2. Implement scoped field projection/actions and append-only audited shortage/reply records under existing lock/transactions; validate duplicate IDs, team/event/command versions, invalid QR and closed events. Full arrival uses initial full-roster report only, no missing-person or supplement inputs.
3. Add desktop readback in existing material and task-feedback modules. Add v2 API for state/arrival/reply/shortage/ask/tiles, reuse v1 session lifecycle, enforce exact input schemas.
4. Replace native main UI, add camera permission/preview/QR decoding, native tile panning/zoom/marker detail, team command reply, single material-name input, assistant question/answer with sources/time. No browser/WebView or personal-command UI.
5. Test current data/authority/idempotence/restart/readback, QR encoding-decoding with actual payloads, coordinate math, APK build/signatures/manifest. Check actual-device availability; do not present static build or backend HTTP tests as real-device validation.
6. Normally stop and backup current delivery, install only approved changes/APK/guide, restart same Wi-Fi services, verify existing data preserved and computer contracts intact. Update dual logs/latest spec/evidence.

No offline queue, background push, 5G/iOS, GPS collection, extra personnel/quantity/destination/reason input, or restoration of removed desktop modules. No commits or additional agents.
