# Android Captain App Implementation Plan

> Implementation in the current approved workspace; no additional agents or commits.

**Goal:** Installable Android captain app with in-app login and the existing four duties, connected to the current computer over the same Wi-Fi.

**Architecture:** Android native Activity/widgets render structured JSON forms; no WebView or browser login. An authenticated, allowlisted adapter exposes existing captain presentation and handlers, preserving their event/version/authority checks. Computer data stays authoritative. App saves only the server address; session and password stay in memory.

**Tech Stack:** Java Android platform APIs, HttpURLConnection, Python standard-library JSON/HTMLParser adapter; official Android build tools/JDK, signed APK. No external app runtime libraries.

1. Preserve current source/data/manifests/Git evidence in a new Android evidence directory; use isolated current delivery data and read-only shared map for checks.
2. Add `/app/v1/login`, `/app/v1/view`, `/app/v1/action`, `/app/v1/logout`: strict JSON, bearer sessions, captain-only scope, opaque form actions bound to event/session/version and immutable original fields. Forward approved operations to existing handlers under the existing lock and event context. Reject foreign/expired actions, unknown routes and modified hidden fields.
3. Implement Android server-address/login, event selection, four native tabs, form controls, date picker, refresh, loading/error feedback, one-request-at-a-time and logout. Accept only explicit private IPv4 server addresses. No background submission, password persistence, arbitrary navigation or external website content.
4. Download only official portable JDK/Android build components into an isolated tools directory. Build, align, sign, verify and inspect APK. Preserve the signing identity privately for later updates, exclude keys and build outputs from Git.
5. Run adapter HTTP/domain flows on isolated current data, native pure Java model checks, APK signature/manifest/dex checks. Check available device/emulator support; report actual-device limitations honestly.
6. Stop delivery normally, backup and install only changed backend sources/manifest plus signed APK and instructions. Restart the same Wi-Fi entry. Verify original data/config/maps, computer routes and native API. Record dual logs, latest specification and delivery evidence.

Acceptance: APK installs as a named launcher app; login and duties render as Android widgets; no WebView. Phone actions share current computer state, scan/read do not imply arrival, supplement does not double inventory, formal completion remains pending verification. No iOS/5G/public deployment/offline queue or changes to the existing desktop layout.
