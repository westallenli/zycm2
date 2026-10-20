# Qinling offline demonstration Implementation Plan

**Goal:** Build the approved independent Qinling north simulated demonstration at 33.945638 N, 108.778381 E and inventory all high-level offline tile coverage.

**Architecture:** Preserve the existing release, its data and audit history. Add an explicit fixed scene template derived from the retained synthetic geometry, preserving approximate metre-scale offsets. Generate new clearly simulated records in a new empty data root through existing domain APIs. Keep the latest Android 0.3 and business semantics. Copy the map once into the new delivery; QA may use a read-only hardlink to that new copy.

**Tech Stack:** Existing Python domain APIs/SQLite, raster tile reader, browser UI and signed Android APK. No new external service, public network, model call, database architecture or performance change.

1. Save source/data/config/map/APK digests and Git diff/status before edits; retain all existing evidence. Append this bounded task to both worklogs.
2. Add an allowlisted Qinling template with translated synthetic points and explicit terrain/route verification limits. Parameterize only fresh rehearsal building and strict scene loading. Shift simulated warehouses, companies and supply reception coordinates coherently; retain original Qinglan template behavior.
3. Test template geometry, original-template compatibility, tamper rejection and full 16-level coverage of the proposed scene bounding rectangle. Freeze current sources, create new empty Qinling data using existing workflows, validate its audit and 22 scene objects/three plans, 120-person roster, arrivals and outstanding supplies. Never seed into the original release.
4. Inventory connected high-level tile components and complete square subregions, exporting exact coordinates/counts and explicitly approximate place labels. Tile availability does not certify native image resolution, acquisition date or surveyed accuracy.
5. Validate in isolation with actual HTTP and browser map loading/minute demo and native read protocol. Package latest source manifest, unchanged APK, launchers and usage instructions. Check source/data/maps before switching the normal ports; verify original process identity and stop normally. Keep original root restorable.
6. Start the new delivery at the existing local/LAN ports, verify Qinling coordinates and local tiles, original data preservation and no QA writes carried over. Record tests, limitations, coverage inventory, report and both worklogs. No Git commit/push, agents, firewall changes, performance fixes or historical compatibility queue.
