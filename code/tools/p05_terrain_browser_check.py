"""Check the optional terrain basemap in a fresh local synthetic preview."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=args.chromium)
        for width, height in ((1440, 900), (390, 844)):
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.request.post(args.base + "/login", form={"secret": "p02-local-preview"})
            page.goto(args.base + "/sand", wait_until="domcontentloaded")
            page.wait_for_function("!!window.__cmdMap && !!document.querySelector('.sand-enhanced')")
            original = page.evaluate("({lat:__cmdMap.getCenter().lat,lng:__cmdMap.getCenter().lng,zoom:__cmdMap.getZoom()})")

            toggle = page.locator('[data-sand-toggle="sand-layers"]')
            if not toggle.is_visible():
                page.get_by_role("button", name="更多面板", exact=True).click()
            toggle.click()
            legend = page.locator(".cmd-maplegend")
            if not legend.evaluate("el => el.open"):
                legend.locator("summary").click()
            page.locator('[data-base-key="opentopo"]').check()
            page.wait_for_function("document.querySelectorAll('.leaflet-tile-loaded[src*=" + json.dumps("opentopomap.org") + "]').length >= 2", timeout=30000)
            source = page.locator('[data-base-key="opentopo"]')
            notice = page.locator("#cmd-base-notice").inner_text()
            attribution = page.locator(".leaflet-control-attribution").inner_text()
            panel_credit = page.locator("#cmd-base-credit").inner_text()
            current = page.evaluate("({lat:__cmdMap.getCenter().lat,lng:__cmdMap.getCenter().lng,zoom:__cmdMap.getZoom()})")
            assert source.is_checked()
            assert "非航拍影像" in notice and "独立控制点" in notice
            assert "OpenTopoMap" in attribution and "CC BY-SA 3.0" in attribution
            assert "OpenTopoMap" in panel_credit and "CC BY-SA 3.0" in panel_credit
            assert current == original, (original, current)
            assert not errors, errors
            toggle.click()
            geometry = page.evaluate("""() => {
              const a = document.querySelector('.leaflet-control-attribution').getBoundingClientRect();
              const w = document.querySelector('.sand-workflow').getBoundingClientRect();
              return {attribution: {left:a.left, top:a.top, right:a.right, bottom:a.bottom},
                      workflow: {left:w.left, top:w.top, right:w.right, bottom:w.bottom}};
            }""")
            attr = geometry["attribution"]
            workflow = geometry["workflow"]
            assert attr["bottom"] <= workflow["top"], geometry
            page.screenshot(path=str(args.output / f"terrain-{width}.png"))
            results.append({"width": width, "loaded_tiles": page.locator('.leaflet-tile-loaded[src*="opentopomap.org"]').count(),
                            "notice": notice, "attribution": attribution, "center_unchanged": True,
                            "geometry": geometry, "page_errors": errors})
            page.close()
        browser.close()

    (args.output / "verification.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
