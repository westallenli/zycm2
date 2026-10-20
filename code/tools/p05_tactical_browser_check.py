"""Verify P05 tactical rendering against a freshly built P00 scenario."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "impl"))

from panda_brain.auth import GeneralEntryAuth  # noqa: E402
from panda_brain.campaign_runner import DRILL_SECRET  # noqa: E402
from panda_brain.panel import load_state, make_server  # noqa: E402


def check(page):
    page.wait_for_function("!!window.__cmdMap && !!document.querySelector('.sand-enhanced')")
    data = page.evaluate("JSON.parse(document.getElementById('cmd-map-data').textContent)")
    marks = {m["title"]: m for m in data["marks"]}
    belt = marks["北侧已开设隔火带"]
    assert len(belt["gap_geometry"]) == 1, belt
    assert {m["route_type"] for m in data["marks"] if m["kind"] == "team_route"} == {
        "plan", "confirmed", "actual"}
    assert ["证据编号", "ev-demo-source-01"] in marks["东线重点火线"]["linked_evidence"]
    assert any(row[0] == "命令编号" for row in marks["扑救一分队确认路线"]["linked_command"])

    rendered = page.evaluate("""() => {
      const lines=[]; __cmdMap.eachLayer(layer => {
        if(layer instanceof L.Polyline && !(layer instanceof L.Polygon))
          lines.push({color:layer.options.color, dash:layer.options.dashArray||'',
                      pts:layer.getLatLngs().map(p=>[p.lat,p.lng])});
      });
      return {lines, gapIcons:document.querySelectorAll('.cmd-gapin').length,
              arrows:document.querySelectorAll('.cmd-arrowin').length,
              routeLabels:document.querySelectorAll('.cmd-route-label').length};
    }""")
    belt_lines = [line for line in rendered["lines"] if line["color"] == "#C8A27A"]
    assert len(belt_lines) >= 3, rendered
    assert rendered["gapIcons"] == 1, rendered
    assert rendered["routeLabels"] >= 2 and rendered["arrows"] >= 4, rendered
    assert any(line["color"] == "#176A5D" for line in rendered["lines"]), rendered
    assert any(line["dash"] == "14 8" for line in rendered["lines"]), rendered

    # Two sections of the opened belt must terminate on opposite sides of the gap.
    pts = belt["pts"]
    nearby = [line for line in belt_lines if any(
        abs(p[0] - pts[0][0]) < 0.00001 and abs(p[1] - pts[0][1]) < 0.00001
        for p in line["pts"])]
    assert nearby, belt_lines
    gap_point = page.evaluate("""() => {
      const icon=document.querySelector('.cmd-gapin');
      const marker=Object.values(__cmdMap._layers).find(x=>x._icon&&x._icon.contains(icon));
      const p=marker.getLatLng(); return [p.lat,p.lng];
    }""")
    assert not any(any(abs(p[0] - gap_point[0]) < 0.00001 and
                           abs(p[1] - gap_point[1]) < 0.00001 for p in line["pts"])
                   for line in belt_lines), belt_lines

    def click_line(mark):
        matched = page.evaluate("""mark => {
          let target=null;
          __cmdMap.eachLayer(layer => {
            if(!(layer instanceof L.Polyline) || layer instanceof L.Polygon) return;
            const pts=layer.getLatLngs();
            if(pts.length===mark.pts.length && pts.every((p,i)=>
              Math.abs(p.lat-mark.pts[i][0])<0.000001 &&
              Math.abs(p.lng-mark.pts[i][1])<0.000001)) target=layer;
          });
          if(target)target.fire('click');
          return !!target;
        }""", mark)
        assert matched, mark["title"]

    click_line(marks["扑救一分队确认路线"])
    assert "关联命令执行" in page.locator("#cmd-bottom-info").inner_text()
    page.locator("#float-cmd-bottom .sand-float-head button").click()
    click_line(marks["东线重点火线"])
    assert "ev-demo-source-01" in page.locator("#cmd-bottom-info").inner_text()
    page.locator("#float-cmd-bottom .sand-float-head button").click()
    page.locator("#cmd-conflict-note").click()
    assert "路线冲突（记录仍有效）" in page.locator("#cmd-bottom-info").inner_text()
    page.locator("#float-cmd-bottom .sand-float-head button").click()

    def sim_layer_count():
        return page.evaluate("""() => {
          let n=0; __cmdMap.eachLayer(layer=>{
            if(layer.getPopup && layer.getPopup() &&
               String(layer.getPopup().getContent()).startsWith('方案')) n++;
          }); return n;
        }""")

    buttons = page.locator("[data-sim]")
    assert buttons.count() >= 3
    original_switches = page.evaluate("""() => Object.fromEntries(
      [...document.querySelectorAll('[data-cmd-layer],[data-layer-group]')]
      .map(x => [x.dataset.cmdLayer || x.dataset.layerGroup, x.checked]))""")
    page.locator('[data-sand-toggle="cmd-plans"]').click()
    buttons.nth(0).click()
    assert page.locator("#cmd-simstrip .sim-chip").count() == 1
    assert sim_layer_count() == len(data["sims"][0]["shapes"])
    buttons.nth(1).click()
    assert page.locator("#cmd-simstrip .sim-chip").count() == 1
    assert sim_layer_count() == len(data["sims"][1]["shapes"])
    page.locator("#cmd-sim-compare").check()
    buttons.nth(0).click()
    assert page.locator("#cmd-simstrip .sim-chip").count() == 2
    assert sim_layer_count() == len(data["sims"][0]["shapes"]) + len(data["sims"][1]["shapes"])
    page.locator("#cmd-sim-clear").click()
    assert page.locator("#cmd-simstrip .sim-chip").count() == 0
    assert sim_layer_count() == 0
    assert len(page.evaluate("JSON.parse(document.getElementById('cmd-map-data').textContent).marks")) == len(data["marks"])
    page.locator("#cmd-sim-restore").click()
    assert page.locator("#cmd-simstrip .sim-chip").count() == 0
    restored_switches = page.evaluate("""() => Object.fromEntries(
      [...document.querySelectorAll('[data-cmd-layer],[data-layer-group]')]
      .map(x => [x.dataset.cmdLayer || x.dataset.layerGroup, x.checked]))""")
    assert restored_switches == original_switches, (original_switches, restored_switches)
    page.locator('[data-sand-toggle="cmd-plans"]').click()
    layers = page.locator('[data-sand-toggle="sand-layers"]')
    if not layers.is_visible():
        page.get_by_role("button", name="更多面板", exact=True).click()
    layers.click()
    legend = page.locator(".cmd-maplegend")
    if not legend.evaluate("el => el.open"):
        legend.locator("summary").click()
    page.locator('[data-base-key="opentopo"]').check()
    needed_tiles = 12 if page.viewport_size["width"] >= 700 else 6
    page.wait_for_function("n => document.querySelectorAll('.leaflet-tile-loaded[src*=\"opentopomap.org\"]').length >= n", arg=needed_tiles, timeout=30000)
    tiles = page.locator('.leaflet-tile-loaded[src*="opentopomap.org"]').count()
    assert "OpenTopoMap" in page.locator(".leaflet-control-attribution").inner_text()
    layers.click()
    return {"map_objects": len(data["marks"]), "belt_sections": len(belt_lines),
            "gap_icons": rendered["gapIcons"], "route_labels": rendered["routeLabels"],
            "arrows": rendered["arrows"], "plans": len(data["sims"]),
            "terrain_tiles": tiles}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    seed = args.run.resolve() / "impl" / "data" / "demo"
    if not seed.is_relative_to(ROOT / ".p00-runs") or not (seed / "demo_hanjiang.db").exists():
        parser.error("--run must be a fresh P00 run with a built synthetic scenario")
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="p05-browser-", dir=ROOT) as tmp_name:
        tmp = Path(tmp_name)
        for source in seed.iterdir():
            shutil.copy2(source, tmp / source.name)
        state = load_state(db_path=tmp / "demo_hanjiang.db",
                           auth=GeneralEntryAuth(secret=DRILL_SECRET))
        for event_id, event in state.registry.events.items():
            state.registry.events[event_id] = replace(event, snapshot_path="")
        state.active_event_id = sorted(state.registry.events)[-1]
        server = make_server(state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True,
                                                     executable_path=args.chromium)
                for width, height in ((1440, 900), (390, 844)):
                    page = browser.new_page(viewport={"width": width, "height": height})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.request.post(base + "/login", form={"secret": DRILL_SECRET})
                    page.goto(base + "/sand", wait_until="domcontentloaded")
                    facts = check(page)
                    assert not errors, errors
                    page.screenshot(path=str(args.output / f"tactical-{width}.png"))
                    results.append({"viewport": [width, height], **facts,
                                    "page_errors": errors})
                    page.close()
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=10)
    (args.output / "verification.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
