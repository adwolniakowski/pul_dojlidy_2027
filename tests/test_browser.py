"""Browser acceptance tests with local data and deliberately unavailable tiles.

Run: /tmp/opencode/pul-venv/bin/python -m unittest discover -s tests -p test_browser.py -v
No availability, imagery, licensing, or accuracy claim about external services is made.
"""

from collections import defaultdict
import csv
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from shapely.geometry import shape
from shapely.strtree import STRtree


ROOT = Path(__file__).resolve().parents[1]
STORAGE_KEY = "pul-dojlidy-list-v1"
SCREENSHOTS = Path("/tmp/opencode")

# Leaflet may publish L before assigning its map factory. Capture either order.
CAPTURE_MAP = """(() => {
  let leaflet;
  Object.defineProperty(window, 'L', {
    configurable: true,
    get() { return leaflet; },
    set(value) {
      leaflet = value;
      let factory = value.map;
      const wrap = fn => function(...args) {
        const map = fn.apply(this, args);
        window.__testMap = map;
        return map;
      };
      if (factory) value.map = wrap(factory);
      else Object.defineProperty(value, 'map', {
        configurable: true,
        get() { return factory; },
        set(fn) { factory = wrap(fn); }
      });
    }
  });
})();"""


def address_key(address):
    return "".join(address.split())


def pretty(address):
    return "-".join(part.strip() for part in address.split("-"))


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "web/layers/manifest.json").read_text())
        cls.features = {
            name: json.loads((ROOT / f"web/layers/{name}.geojson").read_text())["features"]
            for name in ("bdl", "rozplan")
        }
        cls.geometries = {
            name: [shape(f["geometry"]) for f in features]
            for name, features in cls.features.items()
        }
        cls.trees = {name: STRtree(shapes) for name, shapes in cls.geometries.items()}
        bdl_addresses = {address_key(f["properties"]["address"]) for f in cls.features["bdl"]}
        # Use an interior point of an actual intersection, not a guessed map pixel.
        cls.overlap = None
        for i, plan in enumerate(cls.features["rozplan"]):
            if address_key(plan["properties"]["address"]) in bdl_addresses:
                continue
            geometry = cls.geometries["rozplan"][i]
            for j in cls.trees["bdl"].query(geometry, predicate="intersects"):
                intersection = geometry.intersection(cls.geometries["bdl"][j])
                if intersection.area > 1e-9:
                    cls.overlap = intersection.representative_point()
                    cls.plan = plan
                    cls.bdl = cls.features["bdl"][int(j)]
                    break
            if cls.overlap is not None:
                break
        if cls.overlap is None:
            raise AssertionError("No unmatched ROZPLAN/BDL overlap in published data")
        bdl_by_address = {address_key(f["properties"]["address"]): f for f in cls.features["bdl"]}
        cls.matched_plan = next(f for f in cls.features["rozplan"]
                                if address_key(f["properties"]["address"]) in bdl_by_address)
        cls.matched_bdl = bdl_by_address[address_key(cls.matched_plan["properties"]["address"])]
        grouped = defaultdict(list)
        for feature in cls.features["rozplan"]:
            grouped[address_key(feature["properties"]["address"])].append(feature)
        cls.repeated_plan = next(group for group in grouped.values() if len(group) > 1)
        # Build the rich fixture independently from the source tables, not the
        # published inventory (which could itself have lost secondary records).
        bdl_source = ROOT / "data/BDL_01_08_DOJLIDY_2026"

        def table(filename):
            with (bdl_source / filename).open(encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle, delimiter="\t"))

        dictionaries = {
            kind: {r[code].strip(): r[name].strip() for r in table(filename)}
            for kind, filename, code, name in (
                ("layer", "f_storey_dic.txt", "storey_cd", "storey_name"),
                ("species", "f_tree_species_dic.txt", "species_cd", "species_name"),
                ("share", "f_part_dic.txt", "part_cd", "part_name"),
                ("density", "f_density_dic.txt", "density_cd", "density_name"),
                ("mixture", "f_mixture_dic.txt", "mixture_cd", "mixture_name"),
            )
        }
        metadata, species = {}, defaultdict(list)
        for row in table("f_arod_storey.txt"):
            if row["a_year"] == "2026":
                metadata[row["arodes_int_num"], row["storey_cd"].strip()] = row
        for row in table("f_storey_species.txt"):
            if row["a_year"] == "2026":
                species[row["arodes_int_num"], row["storey_cd"].strip()].append(row)
        published_ids = {f["properties"]["id"] for f in cls.features["bdl"]}
        candidates = defaultdict(list)
        for (identifier, code), rows in species.items():
            if identifier in published_ids:
                candidates[identifier].extend(rows)
        rich_ids = [identifier for identifier, rows in candidates.items()
                    if {"MJS", "PJD"} <= {r["part_cd_act"].strip() for r in rows}
                    and len({r["storey_cd"] for r in rows}) > 1
                    and len({r["species_age"] for r in rows}) > 1]
        if not rich_ids:
            raise AssertionError("No multi-layer, multi-age source fixture with both minor shares")
        identifier = max(rich_ids, key=lambda key: len(candidates[key]))
        cls.inventory_bdl = next(f for f in cls.features["bdl"] if f["properties"]["id"] == identifier)
        subarea = next(r for r in table("f_subarea.txt")
                       if r["arodes_int_num"] == identifier and r["a_year"] == "2026")

        def number(raw):
            return float(raw) if raw.strip() else None

        cls.source_inventory = {"structure": subarea["stand_struct_cd"].strip() or None,
                                "rotationAge": number(subarea["rotation_age"]), "layers": []}
        keys = sorted((k for k in metadata.keys() | species.keys() if k[0] == identifier),
                      key=lambda k: (float(metadata[k]["st_rank_order_act"]) if k in metadata else float("inf"), k[1]))
        for key in keys:
            meta = metadata.get(key, {})
            layer = {"code": key[1], "name": dictionaries["layer"][key[1]],
                     "density": dictionaries["density"].get(meta.get("density_cd", "").strip()),
                     "mixture": dictionaries["mixture"].get(meta.get("mixture_cd", "").strip()),
                     "stocking": number(meta.get("standdensity_index", "")), "species": []}
            for row in sorted(species[key], key=lambda r: int(r["sp_rank_order_act"])):
                code = row["species_cd"].strip()
                layer["species"].append({
                    "code": code, "name": dictionaries["species"][code],
                    "share": dictionaries["share"].get(row["part_cd_act"].strip()),
                    "siteClass": row["site_class_cd"].strip() or None,
                    "age": number(row["species_age"]), "height": number(row["height"]),
                    "diameter": number(row["bhd"]),
                })
            cls.source_inventory["layers"].append(layer)
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "web"))
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.contexts = []
        self.errors = []
        self.blocked_requests = []
        self.page = self.open_page()

    def tearDown(self):
        for context in self.contexts:
            context.close()
        self.assertEqual(self.errors, [], "Unexpected browser JavaScript errors")

    def open_page(self, mobile=False):
        context = self.browser.new_context(
            viewport={"width": 390 if mobile else 1440, "height": 844 if mobile else 1000},
            is_mobile=mobile, has_touch=mobile, device_scale_factor=1,
        )
        self.contexts.append(context)

        def route_request(route):
            if urlsplit(route.request.url).netloc == urlsplit(self.url).netloc:
                route.continue_()
            else:
                self.blocked_requests.append(route.request.url)
                route.abort("failed")

        context.route("**/*", route_request)
        context.add_init_script(CAPTURE_MAP)
        page = context.new_page()
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto(self.url)
        self.wait_ready(page)
        return page

    def wait_ready(self, page=None):
        page = page or self.page
        expect(page.locator("#status")).to_contain_text("6105")
        expect(page.locator("#visible-count")).to_have_text("826 / 826")
        page.wait_for_function("!!window.__testMap && !!window.PulCore")
        self.wait_map_idle(page)

    def test_header_and_sources_copy(self):
        """UI-02/04/05: project title, forest section and source-only attribution."""
        expect(self.page).to_have_title("PUL Dojlidy 2027-2037")
        expect(self.page.locator("h1")).to_contain_text("PUL Dojlidy 2027-2037")
        expect(self.page.locator(".eyebrow")).to_have_count(0)
        expect(self.page.locator("#layers-panel > .intro")).to_have_count(0)
        for label in self.page.locator(".basemaps label").all():
            expect(label).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            expect(label).to_have_css("border-top-width", "0px")
        expect(self.page.locator("#layers-panel legend")).to_have_text(["01 / Podkład mapy", "02 / Dane leśne"])
        forest = self.page.locator("#layers-panel fieldset").nth(1)
        expect(forest.locator("#bdl-visible, #mbdl-visible, #plan-visible, #lozfs-visible")).to_have_count(4)
        self.assert_boundary_visible(self.page)
        expect(forest.locator("small")).to_have_text(["Stan na 2026 rok", "Oddziały i wydzielenia; widoczne po przybliżeniu", "Plan rębni"])
        expect(forest.locator("label:has(#plan-visible) small")).to_have_text("Plan rębni")
        expect(forest).to_contain_text("BULIG")
        expect(forest.locator("#types [data-type]")).to_have_count(13)
        expect(self.page.locator("#list-tab")).to_contain_text("Uwagi")
        expect(self.page.locator("#list-panel h2, #list-panel > .intro, #list-panel > .notice")).to_have_count(0)
        expect(self.page.locator(".sources summary")).to_have_text("Źródła")
        self.page.locator(".sources summary").click()
        expect(self.page.locator(".sources p")).to_have_text([
            "BDL: Bank Danych o Lasach / Lasy Państwowe. Stan 2026, wytworzenie 23.06.2026.",
            "OSM: © OpenStreetMap contributors.",
            "Ortofotomapa: GUGiK / Geoportal.",
        ])

    def lozfs_collections(self):
        return [json.loads((ROOT / f"web/layers/lozfs-{name}.geojson").read_bytes())
                for name in ("proposed", "established")]

    def lozfs_overlap(self):
        # Only the intersection fixture skips invalid shapes; rendering must retain all 42.
        for collection in self.lozfs_collections():
            for feature in collection["features"]:
                geometry = shape(feature["geometry"])
                if not geometry.is_valid:
                    continue
                for i in self.trees["rozplan"].query(geometry, predicate="intersects"):
                    plan_part = geometry.intersection(self.geometries["rozplan"][int(i)])
                    for j in self.trees["bdl"].query(plan_part, predicate="intersects"):
                        common = plan_part.intersection(self.geometries["bdl"][int(j)])
                        if common.area > 1e-9:
                            return common.representative_point(), self.features["rozplan"][int(i)]
        self.fail("No published LoZFS/BDL/BULIG interior intersection")

    def assert_lozfs_rendered(self, page):
        expect(page.locator(".leaflet-lozfs-pane > svg")).to_have_count(1)
        expect(page.locator(".leaflet-lozfs-pane path")).to_have_count(42)
        expect(page.locator("#lozfs-hatch")).to_have_count(1)
        pattern = page.locator("#lozfs-hatch")
        for attribute, value in (("patternUnits", "userSpaceOnUse"), ("width", "8"),
                                 ("height", "8"), ("patternTransform", "rotate(45)")):
            expect(pattern).to_have_attribute(attribute, value)
        line = pattern.locator("line")
        for attribute, value in (("stroke", "#287a3c"), ("stroke-width", "1.4"),
                                 ("stroke-opacity", "0.8"), ("x1", "0"), ("x2", "0"),
                                 ("y1", "0"), ("y2", "8")):
            expect(line).to_have_attribute(attribute, value)
        self.assertTrue(page.evaluate("""() => {
          const layers = Object.values(__testMap._layers);
          const groups = layers.filter(l => l instanceof L.GeoJSON && l.options.pane === 'lozfsPane');
          const parent = layers.filter(l => l instanceof L.LayerGroup && !(l instanceof L.GeoJSON) &&
            l.getLayers().length === 2 && l.getLayers().every(c => groups.includes(c)));
          const renderer = groups[0]?.options.renderer;
          return groups.length === 2 && parent.length === 1 && renderer instanceof L.SVG &&
            renderer.options.pane === 'lozfsPane' &&
            groups.every((g, i) => g.options.renderer === renderer && !g.options.onEachFeature &&
              g.options.interactive === false && g.getLayers().length === (i === 0 ? 34 : 8) &&
              g.getLayers().every(c => {
                const p = c._path;
                return c._renderer === renderer && c.options.pane === 'lozfsPane' &&
                  c.options.interactive === false && !c.getPopup() && !c.getTooltip() &&
                  !c.listens('mouseover') && !c.listens('mouseout') && !c.listens('click') &&
                  !p.classList.contains('leaflet-interactive') &&
                  getComputedStyle(p).pointerEvents === 'none' &&
                  p.getAttribute('stroke') === '#287a3c' &&
                  p.getAttribute('fill') === 'url(#lozfs-hatch)' &&
                  p.getAttribute('fill-opacity') === '1' &&
                  c.options.dashArray === (i === 0 ? '6 4' : null) &&
                  (i === 0 ? p.getAttribute('stroke-dasharray') === '6 4' : !p.hasAttribute('stroke-dasharray'));
              }));
        }"""), "Both complete LoZFS datasets must share one noninteractive SVG hatch renderer")
        self.assertTrue(page.evaluate("""() => {
          const pane = name => getComputedStyle(__testMap.getPane(name));
          const groups = Object.values(__testMap._layers).filter(l => l instanceof L.GeoJSON);
          const core = groups.filter(g => g.getLayers().some(c =>
            ['bdl', 'rozplan'].includes(c.feature?.properties.source)) && g.options.pane !== 'highlightPane');
          const highlights = groups.filter(g => g.options.pane === 'highlightPane');
          return pane('overlayPane').zIndex === '400' && pane('lozfsPane').zIndex === '410' &&
            pane('lozfsPane').pointerEvents === 'none' && pane('highlightPane').zIndex === '420' &&
            pane('highlightPane').pointerEvents === 'none' && core.length === 2 &&
            core.every(g => g.getLayers().every(c => c._renderer instanceof L.Canvas &&
              c._renderer.options.pane === 'overlayPane')) && highlights.length === 3 &&
            highlights.every(g => g.options.interactive === false &&
              g.getLayers().every(c => c.options.interactive === false && c.options.pane === 'highlightPane'));
        }"""), "Canvas core layers must stay below hatch, with noninteractive highlights above")
        self.assert_boundary_visible(page)

    def test_lozfs_svg_hatch_complete_datasets_toggle_pan_zoom_desktop_mobile(self):
        """LOZFS-01: actual SVG paths, hatch, layer order and noninteractive styling."""
        collections = self.lozfs_collections()
        self.assertEqual([len(c["features"]) for c in collections], [34, 8])
        self.assertFalse(shape(collections[0]["features"][3]["geometry"]).is_valid)
        point, _ = self.lozfs_overlap()
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                checkbox = page.locator("#lozfs-visible")
                expect(checkbox).not_to_be_checked()
                expect(page.locator("#lozfs-legend")).to_be_hidden()
                expect(checkbox).to_be_enabled()
                expect(page.locator(".leaflet-lozfs-pane path")).to_have_count(0)
                checkbox.check()
                self.click_point(point, page)
                page.evaluate("__testMap.closePopup()")
                expect(page.locator(".leaflet-popup")).to_have_count(0)
                self.assert_lozfs_rendered(page)
                groups = page.evaluate("""() => Object.values(__testMap._layers)
                  .filter(g => g instanceof L.GeoJSON && g.options.pane === 'lozfsPane')
                  .map(g => ({features: g.getLayers().map(c => c.feature)}))""")
                for actual, original in zip(groups, collections):
                    self.assertEqual(actual["features"], original["features"])
                pattern = page.locator("#lozfs-hatch").element_handle()
                styles = page.locator(".leaflet-lozfs-pane path").evaluate_all("els => els.map(e => e.outerHTML)")
                pixel = page.evaluate("""() => {
                  const r = document.querySelector('#map').getBoundingClientRect();
                  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }""")
                page.mouse.move(pixel["x"], pixel["y"])
                self.assertEqual(page.locator(".leaflet-lozfs-pane path").evaluate_all(
                    "els => els.map(e => e.outerHTML)"), styles)
                expect(page.locator(".leaflet-tooltip, .leaflet-popup")).to_have_count(0)
                for action in ("off", "on", "pan", "zoom"):
                    if action == "off":
                        checkbox.uncheck()
                        expect(page.locator(".leaflet-lozfs-pane path")).to_have_count(0)
                        expect(page.locator("#lozfs-legend")).to_be_hidden()
                        continue
                    if action == "on":
                        checkbox.check()
                    elif action == "pan":
                        page.evaluate("__testMap.panBy([35, 20], {animate: false})")
                    else:
                        page.evaluate("__testMap.setZoom(16, {animate: false})")
                    self.wait_map_idle(page)
                    self.assert_lozfs_rendered(page)
                    self.assertTrue(pattern.evaluate("el => el.isConnected && el === document.querySelector('#lozfs-hatch')"))
                expect(page.locator("#lozfs-legend")).to_be_visible()
                expect(page.locator("#lozfs-legend > span")).to_have_text(["Proponowane", "Ustalone"])
                for sample in page.locator("#lozfs-legend i").all():
                    expect(sample).to_have_css("border-top-color", "rgb(40, 122, 60)")
                    self.assertIn("repeating-linear-gradient", sample.evaluate("el => getComputedStyle(el).backgroundImage"))
                self.assert_no_overflow(page)
                page.screenshot(path=str(SCREENSHOTS / f"pul-{'mobile' if mobile else 'desktop'}-lozfs.png"), full_page=True)
                pattern.dispose()

    def test_lozfs_click_through_and_toggle_preserves_popup_lists_filters_and_view(self):
        """LOZFS-01/SEL-03/05/06: hatch adds no hits and toggling touches no core state."""
        page = self.page
        point, plan = self.lozfs_overlap()
        expect(page.locator("#lozfs-visible")).to_be_enabled()
        self.load_entries([plan], ["LoZFS must preserve this note"], page)
        expect(page.locator("#lozfs-visible")).to_be_enabled()
        self.click_point(point, page)
        before_hits = page.locator(".result-button").all_text_contents()
        expect(page.locator(".popup section")).to_have_count(2)
        expect(page.locator(".result-button")).to_have_count(
            len(self.hits(point, "bdl")) + len(self.hits(point, "rozplan")))
        page.locator("#lozfs-visible").check()
        self.click_point(point, page)
        self.assertEqual(page.locator(".result-button").all_text_contents(), before_hits)
        page.locator("#no-types").click()
        page.locator(f'[data-type="{plan["properties"]["type"]}"]').check()
        page.locator("#color-mode").select_option("type")
        page.locator('[name=base][value=ortho]').check()
        self.click_point(point, page)
        self.choose(plan, page)
        popup = page.locator(".leaflet-popup").element_handle()
        saved = self.saved(page)
        snapshot = """() => ({
          center: __testMap.getCenter(), zoom: __testMap.getZoom(),
          objects: document.querySelector('#objects-list').innerHTML,
          notes: document.querySelector('#entries').innerHTML,
          popup: document.querySelector('.leaflet-popup').innerHTML,
          filters: [...document.querySelectorAll('[data-type]')].map(e => [e.dataset.type, e.checked]),
          controls: [...document.querySelectorAll('[name=base], #bdl-visible, #mbdl-visible, #plan-visible')].map(e => e.checked),
          mode: document.querySelector('#color-mode').value,
          coreLayers: Object.values(__testMap._layers).filter(l => l.feature && l.options.pane !== 'lozfsPane').map(l => L.stamp(l))
        })"""
        before = page.evaluate(snapshot)
        self.assert_active_object(plan, page)
        self.assert_active_note(plan, page)
        for enabled in (False, True, False, True):
            page.locator("#lozfs-visible").set_checked(enabled)
            self.assertTrue(popup.evaluate("el => el.isConnected && el === document.querySelector('.leaflet-popup')"))
            self.assertEqual(page.evaluate(snapshot), before)
            self.assertEqual(self.saved(page), saved)
            self.assert_boundary_visible(page)
        page.locator("#objects-tab").click()
        self.assert_objects([f for f in self.features["rozplan"]
                             if f["properties"]["type"] == plan["properties"]["type"]], page)
        page.locator("#list-tab").click()
        expect(page.locator(".entry")).to_have_count(1)
        expect(page.locator(".entry textarea")).to_have_value("LoZFS must preserve this note")
        popup.dispose()

    def test_lozfs_optional_pending_does_not_block_core_and_starts_disabled(self):
        """LOZFS-01: hold both optional responses until core and saved list are usable."""
        page = self.page
        self.load_entries([self.plan], ["Saved before optional loading"], page)
        pending = []
        page.route("**/layers/lozfs-*.geojson", lambda route: pending.append(route))
        page.reload(wait_until="domcontentloaded")
        self.wait_ready(page)
        page.wait_for_function("performance.getEntriesByType('resource').some(e => e.name.endsWith('bdl.geojson'))")
        expect(page.locator("#lozfs-visible")).to_be_disabled()
        expect(page.locator("#lozfs-visible")).not_to_be_checked()
        expect(page.locator("#lozfs-legend")).to_be_hidden()
        page.locator("#list-tab").click()
        expect(page.locator(".entry textarea")).to_have_value("Saved before optional loading")
        self.click_point(page=page)
        self.choose(self.plan, page)
        self.assertEqual(len(pending), 2)
        for route in pending:
            name = urlsplit(route.request.url).path.rsplit("/", 1)[1]
            route.fulfill(status=200, content_type="application/geo+json",
                          body=(ROOT / "web/layers" / name).read_bytes())
        page.locator("#layers-tab").click()
        expect(page.locator("#lozfs-visible")).to_be_enabled()
        expect(page.locator("#lozfs-visible")).not_to_be_checked()
        expect(page.locator(".leaflet-lozfs-pane path")).to_have_count(0)
        page.locator("#lozfs-visible").check()
        self.assert_lozfs_rendered(page)

    def test_lozfs_optional_404_and_malformed_json_preserve_core_and_saved_list(self):
        """LOZFS-01: failure of either optional source is isolated from core loading."""
        for name in ("proposed", "established"):
            for failure in ("404", "malformed", "invalid-shape"):
                with self.subTest(source=name, failure=failure):
                    page = self.open_page()
                    self.load_entries([self.plan], ["Saved despite LoZFS failure"], page)
                    saved = self.saved(page)
                    page.route(f"**/layers/lozfs-{name}.geojson", lambda route, failure=failure:
                               route.fulfill(status=404 if failure == "404" else 200,
                                             content_type="application/json",
                                             body='{"type":"FeatureCollection","features":null}'
                                             if failure == "invalid-shape" else "not JSON"))
                    page.reload()
                    self.wait_ready(page)
                    expect(page.locator("#warnings")).to_contain_text("LoZFS")
                    expect(page.locator("#lozfs-visible")).to_be_disabled()
                    expect(page.locator("#lozfs-visible")).not_to_be_checked()
                    expect(page.locator("#lozfs-legend")).to_be_hidden()
                    expect(page.locator(".leaflet-lozfs-pane path")).to_have_count(0)
                    page.locator("#list-tab").click()
                    expect(page.locator(".entry textarea")).to_have_value("Saved despite LoZFS failure")
                    self.assertEqual(self.saved(page), saved)
                    self.click_point(page=page)
                    self.choose(self.plan, page)
                    page.locator(".detail-view textarea").fill("Core still edits notes")
                    self.wait_saved([{"note": "Core still edits notes"}], page)
                    page.locator("#layers-tab").click()
                    page.locator("#no-types").click()
                    expect(page.locator("#visible-count")).to_have_text("0 / 826")
                    self.assert_boundary_visible(page)
                    page.context.close()
                    self.contexts.remove(page.context)

    def wait_map_idle(self, page=None):
        (page or self.page).wait_for_function("""() =>
          !__testMap._animatingZoom && !__testMap._panAnim?._inProgress &&
          [...document.querySelectorAll('.leaflet-popup')].every(
            el => getComputedStyle(el).opacity === '1')
        """)

    def assert_boundary_visible(self, page):
        """MAP-06: identify the boundary itself, never the similarly colored BDL."""
        expect(page.locator("#boundary-visible")).to_have_count(0)
        self.assertTrue(page.evaluate("""() => {
          const boundaries = Object.values(__testMap._layers).filter(layer =>
            layer instanceof L.GeoJSON && layer.options.style?.dashArray === '8 6');
          return boundaries.length === 1 && boundaries[0].getLayers().length === 1 &&
            boundaries[0].getLayers().every(child => __testMap.hasLayer(child) &&
              child._renderer?._container?.isConnected &&
              child._renderer._layers[L.stamp(child)] === child && child.options.fill === false &&
              child.options.dashArray === '8 6' && child.options.weight > 0 &&
              getComputedStyle(child._renderer._container).display !== 'none');
        }"""), "Inspectorate boundary must remain rendered on the map")

    def track_note_navigation(self, page):
        page.evaluate("""() => {
          window.__testNoteFits = [];
          window.__testNotePopups = [];
          const fit = __testMap.fitBounds, popup = L.popup;
          __testMap.fitBounds = function(bounds, ...args) {
            __testNoteFits.push(bounds.toBBoxString());
            return fit.call(this, bounds, ...args);
          };
          L.popup = function(...args) {
            const result = popup.apply(this, args);
            __testNotePopups.push(result);
            return result;
          };
        }""")

    def assert_note_navigation(self, feature, count, page):
        self.wait_map_idle(page)
        expect(page.locator(".leaflet-popup")).to_have_count(1)
        expect(page.locator(".result-button")).to_have_count(1)
        expect(page.locator('.result-button[aria-pressed="true"]')).to_have_count(1)
        self.assert_active_note(feature, page)
        self.assert_active_object(feature if feature["properties"]["source"] == "rozplan" else None, page)
        self.assertEqual(page.evaluate("[__testNoteFits.length, __testNotePopups.length]"), [count, count])
        self.assertTrue(page.evaluate("""feature => {
          const bounds = L.geoJSON(feature).getBounds();
          const focus = Object.values(__testMap._layers).find(layer =>
            layer instanceof L.GeoJSON && layer.options.style?.color === '#102bff');
          return __testNoteFits.at(-1) === bounds.toBBoxString() &&
            __testMap.getBounds().intersects(bounds) &&
            __testMap._popup === __testNotePopups.at(-1) &&
            focus.getLayers().length === 1 &&
            JSON.stringify(focus.getLayers()[0].feature) === JSON.stringify(feature);
        }""", feature))
        expect(page.locator("#list-tab")).to_have_attribute("aria-pressed", "true")
        self.assert_boundary_visible(page)

    def hits(self, point, source):
        return [
            self.features[source][int(i)]
            for i in self.trees[source].query(point, predicate="intersects")
        ]

    def click_point(self, point=None, page=None):
        page = page or self.page
        point = point if point is not None else self.overlap
        page.evaluate("""([lat, lng]) => {
          __testMap.closePopup();
          __testMap.setView([lat, lng], 18, {animate: false});
        }""", [point.y, point.x])
        # Leaflet retains the old popup briefly while its fade-out completes.
        expect(page.locator(".leaflet-popup")).to_have_count(0)
        pixel = page.evaluate("""([lat, lng]) => {
          const p = __testMap.latLngToContainerPoint([lat, lng]);
          const r = document.querySelector('#map').getBoundingClientRect();
          return {x: r.x + p.x, y: r.y + p.y};
        }""", [point.y, point.x])
        page.mouse.click(pixel["x"], pixel["y"])
        self.wait_map_idle(page)

    def choose(self, feature, page=None):
        page = page or self.page
        p = feature["properties"]
        label = pretty(p["address"])
        if p["source"] == "rozplan":
            label += f" \u00b7 {p['type']} \u00b7 dzia\u0142ka {p['compartment']}"
        button = page.locator(".result-button").get_by_text(label, exact=True)
        button.click()
        self.wait_map_idle(page)
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".detail-view h3")).to_have_text(pretty(p["address"]))
        focused = page.evaluate("""() => {
          let result = [];
          __testMap.eachLayer(layer => {
            if (layer instanceof L.GeoJSON && layer.options.style &&
                layer.options.style.color === '#102bff')
              result = layer.getLayers().map(child => child.feature.properties.id);
          });
          return result;
        }""")
        self.assertEqual(focused, [p["id"]])

    def add(self, feature=None, page=None):
        page = page or self.page
        feature = feature or self.plan
        self.click_point(page=page)
        self.choose(feature, page)
        page.locator(".detail-view .primary").click()
        self.wait_saved([{"address": feature["properties"]["address"]}], page)

    def saved(self, page=None):
        return (page or self.page).evaluate("key => localStorage.getItem(key)", STORAGE_KEY)

    def wait_saved(self, expected, page=None, exact_count=False):
        """Wait for Web Lock writes, checking content rather than elapsed time."""
        page = page or self.page
        page.wait_for_function("""({key, expected, exactCount}) => {
          let saved;
          try { saved = JSON.parse(localStorage.getItem(key)); } catch (_) { return false; }
          return saved && Array.isArray(saved.entries) &&
            (!exactCount || saved.entries.length === expected.length) &&
            expected.every(fields => saved.entries.some(entry =>
              Object.entries(fields).every(([key, value]) => entry[key] === value)));
        }""", arg={"key": STORAGE_KEY, "expected": expected, "exactCount": exact_count})
        return self.saved(page)

    def confirm_clear(self, accept, page=None):
        page = page or self.page
        page.locator("#list-tab").click()
        page.once("dialog", lambda dialog: dialog.accept() if accept else dialog.dismiss())
        page.locator("#clear-list").click()
        if accept:
            self.wait_saved([], page, exact_count=True)

    def assert_no_overflow(self, page):
        self.assertTrue(page.evaluate("""() =>
          document.documentElement.scrollWidth <= innerWidth &&
          document.body.scrollWidth <= innerWidth &&
          [...document.querySelectorAll('#sidebar, .leaflet-popup-content')].every(
            el => el.scrollWidth <= el.clientWidth + 1)
        """), "Page, sidebar, or popup overflows horizontally")
        box = page.locator("#map").bounding_box()
        self.assertGreater(box["width"], 200)
        self.assertGreater(box["height"], 200)

    def test_desktop_and_mobile_layout_and_workflow(self):
        """MAP-02/05, SEL-01/03/04, LIST-01/04; desktop and touch layout."""
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                expect(page.locator("#types [data-type]:checked")).to_have_count(13)
                expect(page.locator("#visible-count")).to_have_text("826 / 826")
                self.assert_no_overflow(page)
                self.click_point(page=page)
                self.choose(self.plan, page)
                self.assert_no_overflow(page)
                page.screenshot(path=str(SCREENSHOTS / f"pul-{'mobile' if mobile else 'desktop'}-popup.png"), full_page=True)
                page.locator(".detail-view .primary").click()
                expect(page.locator("#count")).to_have_text("1")
                expect(page.locator(".entry h3")).to_have_text(pretty(self.plan["properties"]["address"]))
                page.locator(".entry textarea").fill("Browser layout note")
                self.assert_no_overflow(page)
                page.screenshot(path=str(SCREENSHOTS / f"pul-{'mobile' if mobile else 'desktop'}-list.png"), full_page=True)

    def test_popup_stays_inside_map_after_result_choice(self):
        """SEL-04, LIST-04: replacing result details must update popup layout."""
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                self.click_point(page=page)
                self.choose(self.plan, page)
                page.screenshot(path=str(SCREENSHOTS / f"pul-{'mobile' if mobile else 'desktop'}-popup-bounds.png"), full_page=True)
                bounds = page.evaluate("""() => {
                  const map = document.querySelector('#map').getBoundingClientRect();
                  const popup = document.querySelector('.leaflet-popup').getBoundingClientRect();
                  return {map: {left: map.left, right: map.right, top: map.top, bottom: map.bottom},
                          popup: {left: popup.left, right: popup.right, top: popup.top, bottom: popup.bottom}};
                }""")
                for edge in ("left", "top"):
                    self.assertGreaterEqual(bounds["popup"][edge], bounds["map"][edge] - 1, bounds)
                for edge in ("right", "bottom"):
                    self.assertLessEqual(bounds["popup"][edge], bounds["map"][edge] + 1, bounds)

    def assert_objects(self, features, page):
        expect(page.locator("#objects-count")).to_have_text(f"Obiekty BULIG: {len(features)}")
        rows = page.locator("#objects-list .object-button").evaluate_all("""els => els.map(el => ({
          id: el.dataset.objectId, key: el.dataset.addressKey,
          address: el.querySelector('strong').textContent,
          meta: el.querySelector('.object-meta').textContent
        }))""")
        expected = {f["properties"]["id"]: f["properties"] for f in features}
        self.assertCountEqual([row["id"] for row in rows], expected)
        keys = [row["key"] for row in rows]
        self.assertEqual(keys, sorted(keys, key=lambda key: [
            int(part) if part.isdigit() else part for part in re.split(r"(\d+)", key)
        ]))
        for row in rows:
            p = expected[row["id"]]
            self.assertEqual(row, {
                "id": p["id"], "key": address_key(p["address"]),
                "address": pretty(p["address"]),
                "meta": f"{p['type']} \u00b7 {p['percent']:g}% \u00b7 dzia\u0142ka {p['compartment']}",
            })
        if features:
            expect(page.locator("#objects-empty")).to_be_hidden()
        else:
            expect(page.locator("#objects-empty")).to_be_visible()

    def assert_active_object(self, feature, page):
        active = page.locator(".object-button.is-active")
        pressed = page.locator('.object-button[aria-pressed="true"]')
        if feature is None:
            expect(active).to_have_count(0)
            expect(pressed).to_have_count(0)
            return
        expect(active).to_have_count(1)
        expect(active).to_have_attribute("data-object-id", feature["properties"]["id"])
        expect(pressed).to_have_count(1)
        expect(active).to_have_attribute("aria-pressed", "true")
        focused = page.evaluate("""() => {
          const features = [];
          __testMap.eachLayer(layer => {
            if (layer instanceof L.GeoJSON && layer.options.style?.color === '#102bff')
              features.push(...layer.getLayers().map(child => child.feature));
          });
          return features;
        }""")
        self.assertEqual([f["properties"]["id"] for f in focused], [feature["properties"]["id"]])
        self.assertEqual(focused[0]["geometry"], feature["geometry"])

    def test_objects_filters_counts_rows_groups_visibility_and_extent(self):
        """OBJ-01: all BULIG features, numeric addresses, filters but not extent/BDL."""
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                self.assertEqual(page.locator(".tabs button").evaluate_all(
                    "els => els.map(el => el.id)"), ["layers-tab", "objects-tab", "list-tab"])
                expect(page.locator("#layers-tab")).to_have_text("Warstwy")
                expect(page.locator("#objects-tab")).to_have_text("Obiekty")
                expect(page.locator("#list-tab")).to_contain_text("Uwagi")
                page.locator("#objects-tab").click()
                expect(page.locator("#objects-panel")).to_be_visible()
                self.assert_objects(self.features["rozplan"], page)
                self.assert_no_overflow(page)
                page.evaluate("__testMap.setView([0, 0], 18, {animate: false})")
                self.wait_map_idle(page)
                self.assert_objects(self.features["rozplan"], page)
                page.locator("#layers-tab").click()
                page.locator("#bdl-visible").uncheck()
                page.locator("#objects-tab").click()
                self.assert_objects(self.features["rozplan"], page)
                for group in ("clearcut", "complex"):
                    page.locator("#layers-tab").click()
                    page.locator("#no-types").click()
                    page.locator(f'[data-group="{group}"]').check()
                    symbols = ("IA", "IB") if group == "clearcut" else tuple(
                        t for t in self.manifest["types"] if t not in ("IA", "IB"))
                    page.locator("#objects-tab").click()
                    self.assert_objects([f for f in self.features["rozplan"]
                                         if f["properties"]["type"] in symbols], page)
                page.locator("#layers-tab").click()
                page.locator("#no-types").click()
                for symbol in ("IB", "IIAU"):
                    page.locator(f'[data-type="{symbol}"]').check()
                page.locator("#objects-tab").click()
                self.assert_objects([f for f in self.features["rozplan"]
                                     if f["properties"]["type"] in ("IB", "IIAU")], page)
                page.locator("#layers-tab").click()
                page.locator("#no-types").click()
                page.locator("#objects-tab").click()
                self.assert_objects([], page)
                page.locator("#layers-tab").click()
                page.locator("#all-types").click()
                page.locator("#plan-visible").uncheck()
                page.locator("#objects-tab").click()
                self.assert_objects([], page)
                page.locator("#layers-tab").click()
                page.locator("#plan-visible").check()
                page.locator("#objects-tab").click()
                self.assert_objects(self.features["rozplan"], page)
                expect(page.locator("#count")).to_have_text("0")

    def test_objects_click_zoom_single_popup_repeated_address_and_map_choice(self):
        """OBJ-02: explicit per-feature activation, zoom, popup choice and close."""
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                before = self.saved(page)
                page.locator("#objects-tab").click()
                page.evaluate("""() => {
                  __testMap.setView([0, 0], 4, {animate: false});
                  const original = __testMap.fitBounds;
                  window.__testFits = [];
                  __testMap.fitBounds = function(bounds, options) {
                    __testFits.push(bounds.toBBoxString());
                    return original.call(this, bounds, options);
                  };
                }""")
                for feature in self.repeated_plan[:2]:
                    p = feature["properties"]
                    page.locator(f'.object-button[data-object-id="{p["id"]}"]').click()
                    self.wait_map_idle(page)
                    expect(page.locator(".leaflet-popup")).to_have_count(1)
                    expect(page.locator(".popup section")).to_have_count(1)
                    expect(page.locator(".result-button")).to_have_count(1)
                    expect(page.locator('.result-button[aria-pressed="true"]')).to_have_count(1)
                    expect(page.locator(".detail-view .source-label")).to_have_text("BULIG \u00b7 plan r\u0119bni")
                    expect(page.locator(".detail-view h3")).to_have_text(pretty(p["address"]))
                    expect(page.locator("#objects-tab")).to_have_attribute("aria-pressed", "true")
                    expect(page.locator("#objects-panel")).to_be_visible()
                    self.assert_active_object(feature, page)
                    self.assertTrue(page.evaluate("""feature => {
                      const bounds = L.geoJSON(feature).getBounds();
                      return __testFits.at(-1) === bounds.toBBoxString() &&
                        __testMap.getBounds().intersects(bounds) && __testMap.getZoom() > 4;
                    }""", feature))
                    expect(page.locator("#count")).to_have_text("0")
                    self.assertEqual(self.saved(page), before)
                    self.assert_no_overflow(page)
                page.locator(".leaflet-popup-close-button").click()
                self.assert_active_object(None, page)
                self.click_point(page=page)
                self.choose(self.plan, page)
                self.assert_active_object(self.plan, page)
                self.choose(self.bdl, page)
                self.assert_active_object(None, page)
                expect(page.locator("#objects-tab")).to_have_attribute("aria-pressed", "true")
                self.assertEqual(self.saved(page), before)

    def test_objects_saved_addresses_blank_notes_bdl_removal_reset_and_reload(self):
        """OBJ-03: saved state depends only on address membership, not note/source."""
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                repeated = self.repeated_plan[0]
                for feature in (repeated, self.matched_bdl):
                    self.click_point(shape(feature["geometry"]).representative_point(), page)
                    self.choose(feature, page)
                    page.locator(".detail-view .primary").click()
                    self.wait_saved([{"id": feature["properties"]["id"], "note": ""}], page)
                keys = {address_key(f["properties"]["address"]) for f in (repeated, self.matched_bdl)}
                expected_ids = {f["properties"]["id"] for f in self.features["rozplan"]
                                if address_key(f["properties"]["address"]) in keys}
                saved_rows = page.locator(".object-button.is-saved")
                for note in ("", "   \n  ", "A note", ""):
                    for editor in page.locator(".entry textarea").all():
                        editor.fill(note)
                    self.wait_saved([{"id": f["properties"]["id"], "note": note}
                                     for f in (repeated, self.matched_bdl)], page, exact_count=True)
                    page.locator("#objects-tab").click()
                    self.assertCountEqual(saved_rows.evaluate_all("els => els.map(el => el.dataset.objectId)"), expected_ids)
                    expect(page.locator(".object-saved:visible")).to_have_text(["W uwagach"] * len(expected_ids))
                    expect(page.locator(".object-button:not(.is-saved) .object-saved:visible")).to_have_count(0)
                    page.locator("#list-tab").click()
                page.reload()
                self.wait_ready(page)
                page.locator("#objects-tab").click()
                self.assertCountEqual(saved_rows.evaluate_all("els => els.map(el => el.dataset.objectId)"), expected_ids)
                self.assert_active_object(None, page)
                page.locator(f'.object-button[data-object-id="{repeated["properties"]["id"]}"]').click()
                self.wait_map_idle(page)
                self.assert_active_object(repeated, page)
                for mode in ("type", "percent"):
                    page.locator("#layers-tab").click()
                    page.locator("#color-mode").select_option(mode)
                    page.locator("#objects-tab").click()
                    self.assert_active_object(repeated, page)
                    self.assertCountEqual(saved_rows.evaluate_all("els => els.map(el => el.dataset.objectId)"), expected_ids)
                page.locator(".object-button.is-active").scroll_into_view_if_needed()
                self.assert_no_overflow(page)
                page.screenshot(path=str(SCREENSHOTS / f"pul-{'mobile' if mobile else 'desktop'}-objects.png"), full_page=True)
                page.locator("#layers-tab").click()
                page.locator("#no-types").click()
                page.locator(f'[data-type="{repeated["properties"]["type"]}"]').check()
                page.locator("#objects-tab").click()
                self.assert_active_object(None, page)
                filtered_ids = expected_ids & {f["properties"]["id"] for f in self.features["rozplan"]
                                               if f["properties"]["type"] == repeated["properties"]["type"]}
                self.assertCountEqual(saved_rows.evaluate_all("els => els.map(el => el.dataset.objectId)"), filtered_ids)
                page.locator(f'.object-button[data-object-id="{repeated["properties"]["id"]}"]').click()
                self.assert_active_object(repeated, page)
                page.locator("#layers-tab").click()
                page.locator("#all-types").click()
                page.locator("#list-tab").click()
                card = page.locator(".entry").filter(has=page.get_by_text(pretty(repeated["properties"]["address"]), exact=True))
                card.get_by_text("Usu\u0144", exact=True).click()
                self.wait_saved([{"id": self.matched_bdl["properties"]["id"]}], page, exact_count=True)
                page.locator("#objects-tab").click()
                for feature in self.repeated_plan:
                    expect(page.locator(f'.object-button[data-object-id="{feature["properties"]["id"]}"]')).not_to_have_class(re.compile(r"\bis-saved\b"))
                self.assert_active_object(None, page)
                before = self.saved(page)
                self.confirm_clear(False, page)
                self.assertEqual(self.saved(page), before)
                page.locator("#objects-tab").click()
                remaining = {f["properties"]["id"] for f in self.features["rozplan"]
                             if address_key(f["properties"]["address"]) == address_key(self.matched_bdl["properties"]["address"])}
                self.assertCountEqual(saved_rows.evaluate_all("els => els.map(el => el.dataset.objectId)"), remaining)
                self.confirm_clear(True, page)
                page.locator("#objects-tab").click()
                expect(saved_rows).to_have_count(0)
                expect(page.locator(".object-saved:visible")).to_have_count(0)
                page.reload()
                self.wait_ready(page)
                page.locator("#objects-tab").click()
                expect(saved_rows).to_have_count(0)

    def test_actual_overlap_separate_sources_and_explicit_choice(self):
        """SEL-01/03/04, INFO-01, PLAN-01/03, LIST-04: no inferred join."""
        self.click_point()
        popup = self.page.locator(".popup")
        bdl_hits, plan_hits = self.hits(self.overlap, "bdl"), self.hits(self.overlap, "rozplan")
        expect(popup.locator("section")).to_have_count(2)
        expect(popup.locator("section").nth(0).locator(".result-button")).to_have_count(len(bdl_hits))
        expect(popup.locator("section").nth(1).locator(".result-button")).to_have_count(len(plan_hits))
        expect(popup).to_contain_text("Wydzielenie BDL")
        expect(popup).to_contain_text("Plan r\u0119bni")
        expect(popup).to_contain_text("Obiekty nakładają się przestrzennie, ale mają różne adresy.")
        expect(popup).not_to_contain_text("Nie oznacza to potwierdzonego powi\u0105zania")
        expect(popup.locator(".detail-view .primary")).to_have_count(0)
        expect(self.page.locator("#count")).to_have_text("0")
        self.choose(self.bdl)
        detail = popup.locator(".detail-view")
        expect(detail.locator(".source-label")).to_have_text("BDL \u00b7 stan na 2026 rok")
        for label, value in self.bdl["properties"]["details"].items():
            expect(detail.locator("dt").get_by_text(label, exact=True)).to_have_count(1)
            self.assertIn(value, detail.locator("dd").all_text_contents())
        self.choose(self.plan)
        expect(detail.locator(".source-label")).to_have_text("BULIG \u00b7 plan r\u0119bni")
        expect(detail).not_to_contain_text("Brak jednoznacznego powi\u0105zania")
        self.assertEqual(detail.locator("dd").all_text_contents(), [
            f"{self.plan['properties']['areaHa']:.2f}".replace(".", ",") + " ha",
            self.plan["properties"]["type"], f"{self.plan['properties']['percent']:g}%",
        ])
        expect(detail).not_to_contain_text("Oznaczenie \u017ar\u00f3d\u0142owe")
        self.assertEqual(self.page.evaluate("""() => {
          let original;
          __testMap.eachLayer(layer => {
            if (layer instanceof L.GeoJSON && layer.options.style?.color === '#102bff')
              original = layer.getLayers()[0].feature.properties.original;
          });
          return original;
        }"""), self.plan["properties"]["original"])
        detail.locator(".primary").click()
        self.wait_saved([{"source": "rozplan"}], exact_count=True)
        stored = json.loads(self.saved())["entries"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["source"], "rozplan")
        self.assertEqual(stored[0]["address"], self.plan["properties"]["address"])
        self.click_point()
        self.choose(self.bdl)
        self.page.locator(".detail-view .primary").click()
        self.wait_saved([{"source": "bdl"}, {"source": "rozplan"}], exact_count=True)
        self.assertEqual({e["address"] for e in json.loads(self.saved())["entries"]},
                         {self.bdl["properties"]["address"], self.plan["properties"]["address"]})

    def load_entries(self, features, notes, page):
        entries = [{"source": f["properties"]["source"], "id": f["properties"]["id"],
                    "address": f["properties"]["address"],
                    "key": address_key(f["properties"]["address"]), "note": note}
                   for f, note in zip(features, notes)]
        raw = json.dumps({"schema": 1, "version": self.manifest["version"], "entries": entries})
        page.evaluate("([key, raw]) => localStorage.setItem(key, raw)", [STORAGE_KEY, raw])
        page.reload()
        self.wait_ready(page)
        expect(page.locator(".entry")).to_have_count(len(entries))
        return entries

    def assert_active_note(self, feature, page):
        active = page.locator('.entry.is-active')
        current = page.locator('.entry[aria-current="true"]')
        expect(active).to_have_count(0 if feature is None else 1)
        expect(current).to_have_count(0 if feature is None else 1)
        if feature is not None:
            expect(active).to_have_attribute("data-address-key", address_key(feature["properties"]["address"]))
            expect(active).to_have_attribute("aria-current", "true")

    def test_note_card_activation_click_keyboard_edit_and_delete_desktop_mobile(self):
        """LIST-05: navigation happens once on activation, never during editing/deletion."""
        features = [self.repeated_plan[1], self.matched_bdl]
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                entries = self.load_entries(features, ["Original note"] * 2, page)
                before = self.saved(page)
                self.track_note_navigation(page)
                for count, method in enumerate(("click", "textarea-click"), 1):
                    feature = features[(count - 1) % 2]
                    p = feature["properties"]
                    page.locator("#layers-tab").click()
                    for source in ("bdl", "plan", "mbdl"):
                        page.locator(f"#{source}-visible").uncheck()
                    page.locator("#no-types").click()
                    page.evaluate("__testMap.setView([0, 0], 4, {animate: false})")
                    self.assert_boundary_visible(page)
                    page.locator("#list-tab").click()
                    card = page.locator(f'.entry[data-address-key="{address_key(p["address"])}"]')
                    editor = card.locator("textarea")
                    expect(card).to_have_attribute("tabindex", "0")
                    expect(card).to_have_attribute("aria-label", f'Uwaga do {pretty(p["address"])}')
                    expect(card.get_by_role("button", name="Poka\u017c na mapie")).to_have_count(0)
                    if method == "click":
                        card.locator("h3").click()
                    else:
                        editor.click()
                    expected_count = page.evaluate("__testNoteFits.length")
                    self.assertEqual(expected_count, count)
                    self.assert_note_navigation(feature, expected_count, page)
                    source = "bdl" if p["source"] == "bdl" else "plan"
                    self.assertTrue(page.locator(f"#{source}-visible").is_checked())
                    if p["source"] == "rozplan":
                        self.assertTrue(page.locator(f'[data-type="{p["type"]}"]').is_checked())
                    editor.fill("Cursor retained")
                    editor.press("Home")
                    editor.press("ArrowRight")
                    node = editor.element_handle()
                    popup = page.evaluate_handle("__testMap._popup")
                    editor.press("X")
                    expect(editor).to_have_value("CXursor retained")
                    self.assertTrue(node.evaluate("""el => el === document.activeElement &&
                      el.isConnected && el.selectionStart === 2 && el.selectionEnd === 2"""))
                    editor.click()
                    card.locator("h3").click()
                    editor.focus()
                    editor.press("End")
                    editor.press("!")
                    expect(editor).to_be_focused()
                    self.assertTrue(popup.evaluate("value => value === __testMap._popup"))
                    self.assert_note_navigation(feature, expected_count, page)
                    self.wait_saved([{"id": p["id"], "source": p["source"], "note": "CXursor retained!"}], page, exact_count=False)
                    expect(page.locator(".detail-view textarea")).to_have_value("CXursor retained!")
                    node.dispose()
                    popup.dispose()
                stored = json.loads(self.saved(page))
                self.assertEqual(stored["version"], json.loads(before)["version"])
                self.assertEqual([{k: e[k] for k in ("source", "id", "key", "address")} for e in stored["entries"]],
                                 [{k: e[k] for k in ("source", "id", "key", "address")} for e in entries])
                page.screenshot(path=str(SCREENSHOTS / f"pul-{'mobile' if mobile else 'desktop'}-note-card.png"), full_page=True)
                # Delete an inactive card, then delete the remaining inactive card
                # with the keyboard. Neither focus nor button bubbling navigates.
                fits = page.evaluate("__testNoteFits.length")
                first = page.locator(f'.entry[data-address-key="{entries[0]["key"]}"]')
                first.get_by_role("button", name="Usu\u0144", exact=True).click()
                self.wait_saved([{"id": entries[1]["id"]}], page, exact_count=True)
                page.locator(".entry button").focus()
                page.keyboard.press("Enter")
                self.wait_saved([], page, exact_count=True)
                self.assertEqual(page.evaluate("[__testNoteFits.length, __testNotePopups.length]"), [fits, fits])
                expect(page.locator(".leaflet-popup")).to_have_count(0)
                self.assert_boundary_visible(page)

    def test_note_keyboard_focus_activates_inactive_card_and_textarea(self):
        """LIST-05: actual Tab/Shift+Tab focus restores the exact stored feature."""
        for mobile in (False, True):
            for target in ("card", "textarea"):
                with self.subTest(mobile=mobile, target=target):
                    page = self.open_page(mobile=mobile)
                    self.load_entries([self.repeated_plan[1], self.matched_bdl], ["First", "Second"], page)
                    page.locator("#bdl-visible").uncheck()
                    page.locator("#plan-visible").uncheck()
                    page.locator("#no-types").click()
                    page.locator("#list-tab").click()
                    self.track_note_navigation(page)
                    feature = self.matched_bdl if target == "card" else self.repeated_plan[1]
                    card = page.locator(f'.entry[data-address-key="{address_key(feature["properties"]["address"])}"]')
                    button = page.locator(".entry").first.get_by_role("button", name="Usu\u0144", exact=True)
                    button.focus()
                    self.assertEqual(page.evaluate("[__testNoteFits.length, __testNotePopups.length]"), [0, 0])
                    page.keyboard.press("Tab" if target == "card" else "Shift+Tab")
                    expect(card if target == "card" else card.locator("textarea")).to_be_focused()
                    self.assert_note_navigation(feature, 1, page)
                    card.locator("textarea").press("End")
                    card.locator("textarea").press("!")
                    self.assert_note_navigation(feature, 1, page)

    def test_note_map_activation_preserves_bdl_provenance_while_editing_bulig(self):
        """LIST-05/SEL-06: address activation never navigates back to the saved source."""
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                entries = self.load_entries([self.matched_bdl], ["Stored BDL"], page)
                before = self.saved(page)
                page.locator("#list-tab").click()
                page.locator("#list-tab").focus()
                self.track_note_navigation(page)
                self.click_point(shape(self.matched_plan["geometry"]).representative_point(), page)
                self.choose(self.matched_plan, page)
                self.assertEqual(page.evaluate("__testNoteFits.length"), 0)
                self.assertEqual(page.evaluate("__testNotePopups.length"), 1)
                self.assertFalse(page.locator(".entry").evaluate("el => el.contains(document.activeElement)"))
                self.assert_active_note(self.matched_bdl, page)
                self.assertEqual(self.saved(page), before)
                popup = page.evaluate_handle("__testMap._popup")
                card = page.locator(".entry")
                card.locator("h3").click()
                card.focus()
                card.locator("textarea").fill("Edit while viewing BULIG")
                self.wait_saved([dict(entries[0], note="Edit while viewing BULIG")], page, exact_count=True)
                self.assertEqual(page.evaluate("[__testNoteFits.length, __testNotePopups.length]"), [0, 1])
                self.assertTrue(popup.evaluate("value => value === __testMap._popup"))
                self.assert_active_object(self.matched_plan, page)
                expect(page.locator(".detail-view .source-label")).to_have_text("BULIG \u00b7 plan r\u0119bni")
                expect(page.locator(".detail-view textarea")).to_have_value("Edit while viewing BULIG")
                expect(page.locator("#list-tab")).to_have_attribute("aria-pressed", "true")
                self.assert_boundary_visible(page)
                popup.dispose()

    def test_boundary_persists_with_all_overlays_off_filters_reset_and_reload(self):
        """MAP-06: the permanent boundary is independent of every user layer control."""
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                self.load_entries([self.repeated_plan[1]], ["Boundary regression"], page)
                self.assert_boundary_visible(page)
                for source in ("bdl", "plan", "mbdl"):
                    page.locator(f"#{source}-visible").uncheck()
                    self.assert_boundary_visible(page)
                for action in ("no-types", "all-types", "no-types"):
                    page.locator(f"#{action}").click()
                    self.assert_boundary_visible(page)
                for base in ("ortho", "osm"):
                    page.locator(f'input[name="base"][value="{base}"]').check()
                    for mode in ("type", "percent"):
                        page.locator("#color-mode").select_option(mode)
                        self.assert_boundary_visible(page)
                page.locator("#home").click()
                self.wait_map_idle(page)
                self.assert_boundary_visible(page)
                page.locator("#list-tab").click()
                page.locator(".entry h3").click()
                self.wait_map_idle(page)
                self.assert_boundary_visible(page)
                self.confirm_clear(False, page)
                self.assert_boundary_visible(page)
                self.confirm_clear(True, page)
                self.assert_boundary_visible(page)
                page.reload()
                self.wait_ready(page)
                self.assert_boundary_visible(page)

    def viewport_state(self, page):
        return page.evaluate("""() => {
          const r = document.querySelector('#map').getBoundingClientRect();
          return [scrollX, scrollY, r.x, r.y, r.width, r.height];
        }""")

    def assert_sidebar_reveals_active(self, selector, page):
        bounds = page.locator(selector).evaluate("""el => {
          const r = el.getBoundingClientRect();
          const sidebar = document.querySelector('#sidebar');
          const s = sidebar.getBoundingClientRect();
          const tabs = document.querySelector('.tabs').getBoundingClientRect();
          return {top: r.top, bottom: r.bottom, height: r.height,
                  visibleTop: tabs.bottom, visibleBottom: s.bottom,
                  scroll: sidebar.scrollTop};
        }""")
        self.assertGreater(bounds["scroll"], 0, bounds)
        self.assertGreaterEqual(bounds["top"], bounds["visibleTop"], bounds)
        self.assertLess(bounds["top"], bounds["visibleBottom"] - 30, bounds)
        if bounds["height"] <= bounds["visibleBottom"] - bounds["visibleTop"] - 16:
            self.assertLessEqual(bounds["bottom"], bounds["visibleBottom"], bounds)

    def test_map_selection_syncs_notes_objects_and_sidebar_without_switching_tabs(self):
        """SEL-06: real map choices, BDL provenance, sticky tabs and local scrolling."""
        bdl_by_key = {address_key(f["properties"]["address"]): f for f in self.features["bdl"]}
        # A late address forces the object list to scroll instead of passing at its top.
        plan = sorted((f for f in self.features["rozplan"]
                       if address_key(f["properties"]["address"]) in bdl_by_key),
                      key=lambda f: [int(p) if p.isdigit() else p for p in
                                     re.split(r"(\d+)", address_key(f["properties"]["address"]))])[-1]
        bdl = bdl_by_key[address_key(plan["properties"]["address"])]
        fillers = [f for f in self.features["bdl"]
                   if address_key(f["properties"]["address"]) != address_key(bdl["properties"]["address"])][:24]
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                self.load_entries(fillers + [bdl], ["Filler"] * len(fillers) + ["BDL saved note"], page)
                before = self.saved(page)
                for tab in ("layers", "objects", "list"):
                    page.locator(f"#{tab}-tab").click()
                    page.locator("#sidebar").evaluate("el => el.scrollTop = 0")
                    self.click_point(shape(plan["geometry"]).representative_point(), page)
                    baseline = self.viewport_state(page)
                    self.choose(plan, page)
                    expect(page.locator(f"#{tab}-tab")).to_have_attribute("aria-pressed", "true")
                    self.assert_active_object(plan, page)
                    self.assert_active_note(bdl, page)
                    expect(page.locator(".detail-view textarea")).to_have_value("BDL saved note")
                    self.assertEqual(self.viewport_state(page), baseline)
                    if tab != "layers":
                        self.assert_sidebar_reveals_active(
                            ".object-button.is-active" if tab == "objects" else ".entry.is-active", page)
                    # Switching tabs must reveal the already active card, not require another click.
                    for reveal in ("objects", "list"):
                        page.locator(f"#{reveal}-tab").click()
                        self.assert_sidebar_reveals_active(
                            ".object-button.is-active" if reveal == "objects" else ".entry.is-active", page)
                        self.assertEqual(self.viewport_state(page), baseline)
                    page.screenshot(path=str(SCREENSHOTS / f"pul-{'mobile' if mobile else 'desktop'}-active-notes.png"), full_page=True)
                    self.click_point(shape(bdl["geometry"]).representative_point(), page)
                    self.choose(bdl, page)
                    self.assert_active_object(None, page)
                    self.assert_active_note(bdl, page)
                    expect(page.locator("#list-tab")).to_have_attribute("aria-pressed", "true")
                    # Exercise Leaflet's close event independently of close-button hit testing.
                    page.evaluate("__testMap.closePopup()")
                    self.assert_active_object(None, page)
                    self.assert_active_note(None, page)
                    self.assertEqual(self.saved(page), before)
                # A fresh multi-hit popup must not inherit or arbitrarily choose an active result.
                self.click_point(page=page)
                self.assertGreater(page.locator(".result-button").count(), 1)
                self.assert_active_object(None, page)
                self.assert_active_note(None, page)
                self.choose(self.plan, page)
                self.assert_active_object(self.plan, page)
                self.assert_active_note(None, page)
                # Unsaved address activation must not create a note implicitly.
                self.assertEqual(self.saved(page), before)
                self.click_point(shape({"type": "Point", "coordinates": [0, 0]}), page)
                expect(page.locator(".leaflet-popup")).to_have_count(0)
                self.assert_active_object(None, page)
                self.assert_active_note(None, page)
                self.assertEqual(self.saved(page), before)
                self.confirm_clear(True, page)
                self.assert_active_object(None, page)
                self.assert_active_note(None, page)
                self.click_point(page=page)
                self.choose(self.plan, page)
                self.assert_active_note(None, page)
                expect(page.locator(".entry")).to_have_count(0)
                page.locator(".detail-view .primary").click()
                self.wait_saved([{"id": self.plan["properties"]["id"], "note": ""}], page, exact_count=True)
                self.assert_active_note(self.plan, page)
                self.assert_active_object(self.plan, page)
                page.locator(".leaflet-popup-close-button").click()
                self.assert_active_note(None, page)
                self.assert_active_object(None, page)

    def test_mobile_saved_bdl_popup_close_button_is_clickable(self):
        """SEL-06: a saved BDL popup must close through its real visible close control."""
        page = self.open_page(mobile=True)
        bdl_by_key = {address_key(f["properties"]["address"]): f for f in self.features["bdl"]}
        plan = sorted((f for f in self.features["rozplan"]
                       if address_key(f["properties"]["address"]) in bdl_by_key),
                      key=lambda f: [int(p) if p.isdigit() else p for p in
                                     re.split(r"(\d+)", address_key(f["properties"]["address"]))])[-1]
        bdl = bdl_by_key[address_key(plan["properties"]["address"])]
        self.load_entries([bdl], ["BDL saved note"], page)
        page.locator("#list-tab").click()
        self.click_point(shape(bdl["geometry"]).representative_point(), page)
        self.choose(bdl, page)
        self.assert_active_note(bdl, page)
        page.screenshot(path=str(SCREENSHOTS / "pul-mobile-bdl-close-overlap.png"), full_page=True)
        page.locator(".leaflet-popup-close-button").click(timeout=2000)
        expect(page.locator(".leaflet-popup")).to_have_count(0)
        self.assert_active_note(None, page)
        self.assert_active_object(None, page)

    def assert_text_download(self, entries, page):
        expected = "Uwagi do PUL Dojlidy 2027-2037\n\n" + "".join(
            f"Adres leśny: {pretty(e['address'])}\n\n{e['note'] if e['note'].strip() else 'Brak notatki.'}\n\n"
            for e in entries)
        before = self.saved(page)
        self.assertEqual(page.evaluate("entries => PulCore.toPlainText(entries)", entries), expected)
        self.assertEqual(page.evaluate("entries => { const before = JSON.stringify(entries); PulCore.toPlainText(entries); return JSON.stringify(entries) === before; }", entries), True)
        page.locator("#list-tab").click()
        expect(page.locator("#export-text")).to_be_enabled()
        page.wait_for_load_state("networkidle")
        requests = []
        listener = lambda request: requests.append(request.url)
        page.on("request", listener)
        page.evaluate("""() => {
          const original = URL.createObjectURL;
          URL.createObjectURL = function(blob) {
            window.__testExportType = blob.type;
            return original.call(this, blob);
          };
        }""")
        try:
            with page.expect_download() as pending:
                page.locator("#export-text").click()
            download = pending.value
            self.assertEqual(download.suggested_filename, "uwagi-pul-dojlidy.txt")
            self.assertTrue(download.url.startswith("blob:"), download.url)
            self.assertIsNone(download.failure())
            self.assertEqual(Path(download.path()).read_bytes(), expected.encode("utf-8"))
            self.assertEqual(page.evaluate("window.__testExportType"), "text/plain;charset=utf-8")
            page.wait_for_load_state("networkidle")
            self.assertEqual(requests, [], "Export must not make local or outbound HTTP requests")
            self.assertEqual(self.saved(page), before)
            self.assertEqual(page.locator(".entry textarea").evaluate_all("els => els.map(el => el.value)"),
                             [e["note"] for e in entries])
        finally:
            page.remove_listener("request", listener)

    def test_text_download_all_entries_raw_notes_empty_reload_and_removal(self):
        """Plain text export: bytes, insertion order, hidden data, blank and Unicode notes."""
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                page.locator("#list-tab").click()
                expect(page.locator("#export-text")).to_be_disabled()
                features = [self.plan, self.bdl, self.matched_bdl]
                self.assertEqual(len({address_key(f["properties"]["address"]) for f in features}), 3)
                notes = ["  # Za\u017c\u00f3\u0142\u0107 **g\u0119\u015bl\u0105**\n\n- [link](https://example.invalid)\n`kod` <tag> & | \\\nKoniec  \n", " \t\n  ", ""]
                entries = self.load_entries(features, notes, page)
                page.locator("#no-types").click()
                page.locator("#bdl-visible").uncheck()
                self.assert_text_download(entries, page)
                page.reload()
                self.wait_ready(page)
                self.assert_text_download(entries, page)
                for remaining in (2, 1, 0):
                    page.locator(".entry").first.get_by_role("button", name="Usu\u0144", exact=True).click()
                    self.wait_saved(entries[3 - remaining:], page, exact_count=True)
                    if remaining:
                        expect(page.locator("#export-text")).to_be_enabled()
                    else:
                        expect(page.locator("#export-text")).to_be_disabled()
                page.reload()
                self.wait_ready(page)
                page.locator("#list-tab").click()
                expect(page.locator("#export-text")).to_be_disabled()

    def test_text_export_recovers_in_memory_notes_when_storage_conflicts(self):
        """Export stays available during SAVE-01 conflict without touching newer storage."""
        entries = self.load_entries([self.plan], ["Local recovery\n**keep me**"], self.page)
        newer = self.page.context.new_page()
        newer.on("pageerror", lambda error: self.errors.append(str(error)))
        newer.goto(self.url)
        self.wait_ready(newer)
        newer.locator("#list-tab").click()
        newer.locator(".entry textarea").fill("Newer persisted note")
        latest = self.wait_saved([{"note": "Newer persisted note"}], newer)
        expect(self.page.locator("#warnings")).to_contain_text("Lista zmieni\u0142a si\u0119 w innej karcie")
        expect(self.page.locator(".entry textarea")).to_be_disabled()
        self.assert_text_download(entries, self.page)
        self.assertEqual(self.saved(), latest)

    def test_duplicate_address_note_edit_clear_and_reload(self):
        """LIST-01/04, NOTE-01, SAVE-01: exact address and persisted notes."""
        self.add()
        self.add()
        expect(self.page.locator(".entry")).to_have_count(1)
        note = self.page.locator(".entry textarea")
        note.fill("First note")
        note.fill("Changed note\nSecond line <not HTML>")
        self.wait_saved([{"note": "Changed note\nSecond line <not HTML>"}])
        self.page.reload()
        self.wait_ready()
        self.page.locator("#list-tab").click()
        expect(self.page.locator(".entry textarea")).to_have_value("Changed note\nSecond line <not HTML>")
        self.click_point()
        self.choose(self.plan)
        self.page.locator(".detail-view textarea").fill("Edited from popup")
        expect(self.page.locator(".entry textarea")).to_have_value("Edited from popup")
        self.page.locator(".detail-view textarea").fill("")
        self.wait_saved([{"note": ""}])
        self.page.reload()
        self.wait_ready()
        self.page.locator("#list-tab").click()
        expect(self.page.locator(".entry textarea")).to_have_value("")
        expect(self.page.locator("#count")).to_have_text("1")

    def test_remove_and_clear_confirmation_persist(self):
        """LIST-01, SAVE-01/02: remove one, cancel clear, accept clear."""
        self.add()
        self.page.locator(".entry button").get_by_text("Usu\u0144", exact=True).click()
        self.wait_saved([], exact_count=True)
        expect(self.page.locator("#count")).to_have_text("0")
        self.assertEqual(json.loads(self.saved())["entries"], [])
        self.page.reload()
        self.wait_ready()
        expect(self.page.locator("#count")).to_have_text("0")
        self.add()
        self.page.locator(".entry textarea").fill("Keep on cancel")
        before = self.wait_saved([{"note": "Keep on cancel"}])
        self.confirm_clear(False)
        self.assertEqual(self.saved(), before)
        expect(self.page.locator(".entry textarea")).to_have_value("Keep on cancel")
        self.confirm_clear(True)
        expect(self.page.locator("#count")).to_have_text("0")
        self.assertEqual(json.loads(self.saved())["entries"], [])
        self.page.reload()
        self.wait_ready()
        expect(self.page.locator("#count")).to_have_text("0")

    def test_visibility_and_multiselect_filters_respect_hits(self):
        """MAP-02, SEL-05, PLAN-01/02: hidden features never produce hits."""
        self.add()
        self.page.locator(".entry textarea").fill("Retain through filters")
        before = self.wait_saved([{"note": "Retain through filters"}])
        self.page.locator("#layers-tab").click()
        self.page.locator("#no-types").click()
        expect(self.page.locator("#visible-count")).to_have_text("0 / 826")
        expect(self.page.locator("#types [data-type]:checked")).to_have_count(0)
        self.click_point()
        expect(self.page.locator(".popup section")).to_have_count(1)
        expect(self.page.locator(".popup")).to_contain_text("Wydzielenie BDL")
        expect(self.page.locator(".popup")).not_to_contain_text("Plan r\u0119bni \u00b7")
        self.page.locator("#bdl-visible").uncheck()
        self.click_point()
        expect(self.page.locator(".popup")).to_have_count(0)
        expect(self.page.locator("#status")).to_contain_text("Brak obiekt\u00f3w")
        types = [self.plan["properties"]["type"]]
        types.append(next(t for t in self.manifest["types"] if t not in types))
        for symbol in types:
            self.page.locator(f'[data-type="{symbol}"]').check()
        count = sum(f["properties"]["type"] in types for f in self.features["rozplan"])
        expect(self.page.locator("#visible-count")).to_have_text(f"{count} / 826")
        self.click_point()
        expect(self.page.locator(".popup section")).to_have_count(1)
        expect(self.page.locator(".popup")).not_to_contain_text("Wydzielenie BDL")
        expect(self.page.locator(".result-button")).to_have_count(sum(
            f["properties"]["type"] in types for f in self.hits(self.overlap, "rozplan")))
        self.page.locator("#plan-visible").uncheck()
        self.click_point()
        expect(self.page.locator(".popup")).to_have_count(0)
        self.page.locator("#plan-visible").check()
        self.page.locator("#all-types").click()
        expect(self.page.locator("#visible-count")).to_have_text("826 / 826")
        self.assertEqual(self.saved(), before)

    def test_base_and_independent_overlays_preserve_list_and_notes(self):
        """MAP-02/05, LIST-01, NOTE-01: basemap is not selection state."""
        self.add()
        self.page.locator(".entry textarea").fill("Do not lose on base change")
        before = self.wait_saved([{"note": "Do not lose on base change"}])
        self.page.locator("#layers-tab").click()
        self.assert_boundary_visible(self.page)
        self.page.locator("#bdl-visible").uncheck()
        self.page.locator("#mbdl-visible").check()
        for base in ("ortho", "osm"):
            self.page.locator(f'input[name="base"][value="{base}"]').check()
            expect(self.page.locator("#bdl-visible")).not_to_be_checked()
            self.assert_boundary_visible(self.page)
            expect(self.page.locator("#plan-visible")).to_be_checked()
            expect(self.page.locator("#mbdl-visible")).to_be_checked()
            self.assertEqual(self.saved(), before)
            self.click_point()
            self.choose(self.plan)
            expect(self.page.locator(".popup section")).to_have_count(1)
            layer_state = self.page.evaluate("""() => {
              const tiles = [], vectors = [];
              __testMap.eachLayer(layer => {
                if (layer instanceof L.TileLayer) tiles.push(layer);
                if (layer instanceof L.GeoJSON) vectors.push(layer);
              });
              return {tiles: tiles.map(l => l._url), above: vectors.every(v =>
                +getComputedStyle(__testMap.getPane(v.options.pane || 'overlayPane')).zIndex >
                +getComputedStyle(__testMap.getPane('tilePane')).zIndex)};
            }""")
            self.assertEqual(len(layer_state["tiles"]), 2)
            self.assertTrue(any(("geoportal" if base == "ortho" else "openstreetmap") in url
                                for url in layer_state["tiles"]))
            self.assertTrue(layer_state["above"])
        self.page.locator("#mbdl-visible").uncheck()
        self.page.locator("#list-tab").click()
        expect(self.page.locator(".entry textarea")).to_have_value("Do not lose on base change")

    def test_hierarchical_groups_all_none_partial_and_global_sync(self):
        """PLAN-04: native parents reflect children and global filter actions."""
        page = self.page
        expect(page.locator('#types input[data-group][type="checkbox"]')).to_have_count(2)
        expected_groups = {
            "clearcut": ["IA", "IB"],
            "complex": [t for t in self.manifest["types"] if t not in ("IA", "IB")],
        }
        for group, symbols in expected_groups.items():
            with self.subTest(group=group):
                parent = page.locator(f'[data-group="{group}"]')
                children = page.locator(f'#types-{group} [data-type]')
                self.assertEqual(children.evaluate_all("els => els.map(el => el.dataset.type)"), symbols)
                expect(parent).to_be_checked()
                self.assertFalse(parent.evaluate("el => el.indeterminate"))
                parent.uncheck()
                expect(page.locator(f'#types-{group} [data-type]:checked')).to_have_count(0)
                self.assertFalse(parent.evaluate("el => el.indeterminate"))
                visible = sum(f["properties"]["type"] not in symbols for f in self.features["rozplan"])
                expect(page.locator("#visible-count")).to_have_text(f"{visible} / 826")
                children.first.check()
                expect(parent).not_to_be_checked()
                self.assertTrue(parent.evaluate("el => el.indeterminate"))
                visible += sum(f["properties"]["type"] == symbols[0] for f in self.features["rozplan"])
                expect(page.locator("#visible-count")).to_have_text(f"{visible} / 826")
                # Native activation of a mixed parent selects every child.
                parent.check()
                expect(page.locator(f'#types-{group} [data-type]:checked')).to_have_count(len(symbols))
                self.assertFalse(parent.evaluate("el => el.indeterminate"))
                expect(page.locator("#visible-count")).to_have_text("826 / 826")
                children.last.uncheck()
                self.assertTrue(parent.evaluate("el => el.indeterminate"))
                page.locator("#no-types").click()
                expect(page.locator("#types [data-type]:checked")).to_have_count(0)
                expect(page.locator("#types [data-group]:checked")).to_have_count(0)
                self.assertEqual(page.locator("[data-group]").evaluate_all(
                    "els => els.map(el => el.indeterminate)"), [False, False])
                page.locator("#all-types").click()
                expect(page.locator("#types [data-type]:checked")).to_have_count(13)
                expect(page.locator("#types [data-group]:checked")).to_have_count(2)
                self.assertEqual(page.locator("[data-group]").evaluate_all(
                    "els => els.map(el => el.indeterminate)"), [False, False])

    def test_collapsing_groups_preserves_filters_hits_and_saved_list(self):
        """PLAN-04, SEL-05: disclosure is independent of filter selection."""
        page = self.page
        self.add()
        page.locator(".entry textarea").fill("Keep while collapsing groups")
        saved = self.wait_saved([{"note": "Keep while collapsing groups"}])
        page.locator("#layers-tab").click()
        page.locator("#no-types").click()
        page.locator(f'[data-type="{self.plan["properties"]["type"]}"]').check()
        self.click_point()
        selected = page.locator("[data-type]:checked").evaluate_all("els => els.map(el => el.dataset.type)")
        hits = page.locator(".result-button").all_text_contents()
        count = page.locator("#visible-count").inner_text()
        for group in ("clearcut", "complex"):
            toggle = page.locator(f'.group-toggle[aria-controls="types-{group}"]')
            children = page.locator(f"#types-{group}")
            expect(toggle).to_have_attribute("aria-expanded", "true")
            for expanded in (False, True):
                toggle.click()
                expect(toggle).to_have_attribute("aria-expanded", str(expanded).lower())
                if expanded:
                    expect(children).to_be_visible()
                else:
                    expect(children).to_be_hidden()
                self.assertEqual(page.locator("[data-type]:checked").evaluate_all(
                    "els => els.map(el => el.dataset.type)"), selected)
                expect(page.locator("#visible-count")).to_have_text(count)
                self.assertEqual(page.locator(".result-button").all_text_contents(), hits)
                self.click_point()
                self.assertEqual(page.locator(".result-button").all_text_contents(), hits)
                self.assertEqual(self.saved(), saved)

    def test_plan_transparency_slider(self):
        """PLAN-06: live fill transparency, both modes and filter rebuilds."""
        page = self.page
        slider = page.locator("#plan-transparency")
        expect(slider).to_be_enabled()
        expect(slider).to_have_value("30")
        def opacity():
            return page.evaluate("""() => {
              let values = [];
              __testMap.eachLayer(layer => {
                if (layer instanceof L.GeoJSON && typeof layer.options.style === 'function')
                  values = layer.getLayers().map(child => child.options.fillOpacity);
              });
              return [...new Set(values)];
            }""")
        self.assertEqual(opacity(), [0.7])
        self.click_point()
        self.choose(self.plan)
        page.locator("#layers-tab").click()
        page.evaluate("window.__sliderPopup = __testMap._popup")
        for mode in ("percent", "type"):
            page.locator("#color-mode").select_option(mode)
            slider.focus()
            slider.press("Home")
            expect(page.locator("#plan-transparency-value")).to_have_text("0%")
            self.assertEqual(opacity(), [1])
            slider.press("End")
            expect(page.locator("#plan-transparency-value")).to_have_text("100%")
            self.assertEqual(opacity(), [0])
            self.assertTrue(page.evaluate("__testMap._popup === window.__sliderPopup"))
            expect(page.locator(".detail-view h3")).to_have_text(pretty(self.plan["properties"]["address"]))
        page.locator("#no-types").click()
        page.locator("#all-types").click()
        self.assertEqual(opacity(), [0])
        page.reload()
        self.wait_ready(page)
        expect(slider).to_have_value("30")
        self.assertEqual(opacity(), [0.7])

    def plan_styles(self, page=None):
        """Inspect the thematic GeoJSON, not saved/focus copies of its features."""
        return (page or self.page).evaluate("""() => {
          const layers = [];
          __testMap.eachLayer(layer => {
            if (layer instanceof L.GeoJSON && typeof layer.options.style === 'function')
              layers.push(...layer.getLayers().map(child => ({
                id: child.feature.properties.id, leafletId: L.stamp(child),
                type: child.feature.properties.type, percent: child.feature.properties.percent,
                color: child.options.color, fillColor: child.options.fillColor || child.options.color
              })));
          });
          return layers;
        }""")

    def test_plan_colors_use_fixed_percent_range_and_type_families(self):
        """PLAN-05: real Leaflet styles, fixed gradient and I-V categorical legend."""
        page = self.page
        families = {"IA": "I", "IB": "I", "IIA": "II", "IIAU": "II", "IIB": "II",
                    "IIBU": "II", "IIIA": "III", "IIIAU": "III", "IIIB": "III",
                    "IIIBU": "III", "IVD": "IV", "IVDU": "IV", "V": "V"}
        self.assertEqual(page.evaluate("types => Object.fromEntries(types.map(t => [t, PulCore.typeFamily(t)]))",
                                       self.manifest["types"]), families)
        exported = page.evaluate("PulCore.typeFamilies")
        self.assertEqual([f["code"] for f in exported], ["I", "II", "III", "IV", "V"])
        palette = {f["code"]: f["color"] for f in exported}
        self.assertEqual(palette, {
            "I": "#c6323e", "II": "#c7a4dc", "III": "#a878c3",
            "IV": "#8651a6", "V": "#5c2e7e",
        })
        self.assertEqual(len(set(palette.values())), 5)
        expect(page.locator("#color-mode")).to_have_value("percent")
        expect(page.locator("#plan-legend .legend-ticks span")).to_have_text(["0%", "25%", "50%", "75%", "100%"])
        expect(page.locator("#plan-legend .legend-row")).to_have_count(0)
        expect(page.locator("[data-type-color]:visible")).to_have_count(0)
        ramp = page.locator("#plan-legend .color-ramp").evaluate("el => getComputedStyle(el).backgroundImage")
        self.assertIn("linear-gradient(to right", ramp)
        self.assertIn("rgb(255, 240, 207)", ramp)
        self.assertIn("rgb(138, 50, 31)", ramp)
        percent_styles = self.plan_styles()
        self.assertEqual(len(percent_styles), 826)
        by_percent = defaultdict(set)
        by_family = defaultdict(set)
        for style in percent_styles:
            by_percent[style["percent"]].add(style["color"])
            by_family[families[style["type"]]].add(style["color"])
            expected = "#" + "".join(f"{int(start + (end - start) * style['percent'] / 100 + .5):02x}"
                                     for start, end in zip((255, 240, 207), (138, 50, 31)))
            self.assertEqual(style["color"], expected)
            self.assertEqual(style["fillColor"], expected)
        self.assertTrue(all(len(colors) == 1 for colors in by_percent.values()))
        self.assertEqual(len({next(iter(colors)) for colors in by_percent.values()}), len(by_percent))
        self.assertTrue(any(len(colors) > 1 for colors in by_family.values()))
        synthetic = page.evaluate("""() => [0, 1, 20, 21, 50, 99, 100].map(percent =>
          PulCore.planColor({type: 'IIAU', percent}, 'percent'))""")
        self.assertEqual(synthetic[0], "#fff0cf")
        self.assertEqual(synthetic[-1], "#8a321f")
        self.assertEqual(len(set(synthetic)), 7, "Percent mode must not use categorical bins")
        page.locator("#color-mode").select_option("type")
        expect(page.locator("#plan-legend .color-ramp")).to_have_count(0)
        expect(page.locator("#plan-legend .legend-row")).to_have_text([
            "I · Zupełne", "II · Częściowe", "III · Gniazdowe", "IV · Stopniowe", "V · Przerębowe",
        ])
        expect(page.locator("[data-type-color]:visible")).to_have_count(13)
        self.assertEqual(page.locator("#plan-legend .swatch").evaluate_all(
            "els => els.map(el => el.style.backgroundColor)"), page.evaluate(
                "PulCore.typeFamilies.map(f => { const el = document.createElement('span'); el.style.backgroundColor = f.color; return el.style.backgroundColor; })"))
        type_styles = self.plan_styles()
        self.assertEqual([s["leafletId"] for s in type_styles], [s["leafletId"] for s in percent_styles])
        for style in type_styles:
            self.assertEqual(style["color"], palette[families[style["type"]]])
            self.assertEqual(style["fillColor"], style["color"])
        self.assertEqual({s["type"] for s in type_styles}, set(self.manifest["types"]))
        page.locator("#no-types").click()
        page.locator('[data-type="IIAU"]').check()
        page.locator("#color-mode").select_option("percent")
        # Filtering must not rescale the gradient or the surviving feature colors.
        self.assertEqual(page.locator("#plan-legend .color-ramp").evaluate(
            "el => getComputedStyle(el).backgroundImage"), ramp)
        original = {s["id"]: s["color"] for s in percent_styles}
        for style in self.plan_styles():
            self.assertEqual(style["color"], original[style["id"]])
        expect(page.locator("[data-type-color]:visible")).to_have_count(0)

    def test_color_switch_preserves_popup_focus_filters_notes_and_visibility(self):
        """PLAN-05: setStyle only, with live popup and saved state on both layouts."""
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                page.locator("#no-types").click()
                page.locator(f'[data-type="{self.plan["properties"]["type"]}"]').check()
                page.locator("#bdl-visible").uncheck()
                self.assert_boundary_visible(page)
                page.locator("#mbdl-visible").check()
                self.add(page=page)
                page.locator(".entry textarea").fill("Keep note through color changes")
                saved = self.wait_saved([{"note": "Keep note through color changes"}], page)
                page.locator("#layers-tab").click()
                self.click_point(page=page)
                self.choose(self.plan, page)
                before = self.plan_styles(page)
                popup = page.locator(".leaflet-popup").element_handle()
                page.evaluate("""() => {
                  window.__testLayerChanges = {style: 0, add: 0, remove: 0};
                  __testMap.eachLayer(layer => {
                    if (layer instanceof L.GeoJSON && typeof layer.options.style === 'function') {
                      const original = layer.setStyle;
                      layer.setStyle = function(...args) {
                        __testLayerChanges.style++;
                        return original.apply(this, args);
                      };
                    }
                  });
                  __testMap.on('layeradd', () => __testLayerChanges.add++);
                  __testMap.on('layerremove', () => __testLayerChanges.remove++);
                  window.__testFocus = [...Object.values(__testMap._layers)].filter(layer =>
                    layer instanceof L.GeoJSON && layer.options.style?.color === '#102bff')[0];
                  window.__testFocusChildren = __testFocus.getLayers();
                }""")
                visible = page.locator("#visible-count").inner_text()
                for number, mode in enumerate(("type", "percent"), 1):
                    page.locator("#color-mode").select_option(mode)
                    self.wait_map_idle(page)
                    self.assertTrue(popup.evaluate("el => el === document.querySelector('.leaflet-popup') && el.isConnected"))
                    self.assertEqual(page.evaluate("__testLayerChanges"), {"style": number, "add": 0, "remove": 0})
                    self.assertTrue(page.evaluate("""() => __testMap.hasLayer(__testFocus) &&
                      __testFocus.getLayers().length === 1 &&
                      __testFocus.getLayers()[0] === __testFocusChildren[0]"""))
                    self.assertEqual([s["leafletId"] for s in self.plan_styles(page)], [s["leafletId"] for s in before])
                    expect(page.locator('.result-button[aria-pressed="true"]')).to_have_count(1)
                    expect(page.locator(".detail-view h3")).to_have_text(pretty(self.plan["properties"]["address"]))
                    expect(page.locator(".detail-view textarea")).to_have_value("Keep note through color changes")
                    expect(page.locator("#visible-count")).to_have_text(visible)
                    self.assertEqual(page.locator("[data-type]:checked").evaluate_all(
                        "els => els.map(el => el.dataset.type)"), [self.plan["properties"]["type"]])
                    self.assert_boundary_visible(page)
                    for selector, checked in (("bdl", False), ("mbdl", True), ("plan", True)):
                        self.assertEqual(page.locator(f"#{selector}-visible").is_checked(), checked)
                    self.assertEqual(self.saved(page), saved)
                    self.assert_no_overflow(page)
                    if mode == "type":
                        page.screenshot(path=str(SCREENSHOTS / f"pul-{'mobile' if mobile else 'desktop'}-color-type.png"), full_page=True)
                popup.dispose()
                page.locator(".detail-view textarea").fill("Popup still edits after recoloring")
                self.wait_saved([{"note": "Popup still edits after recoloring"}], page)
                page.locator("#list-tab").click()
                expect(page.locator(".entry textarea")).to_have_value("Popup still edits after recoloring")
                page.locator("#layers-tab").click()
                page.locator("#plan-visible").uncheck()
                page.locator("#color-mode").select_option("type")
                expect(page.locator("#plan-visible")).not_to_be_checked()
                expect(page.locator("#visible-count")).to_have_text("0 / 826")
                self.assertEqual(self.plan_styles(page), [])
                page.locator("#plan-visible").check()
                for style in self.plan_styles(page):
                    self.assertEqual(style["color"], page.evaluate(
                        "p => PulCore.planColor(p, 'type')", {"type": style["type"]}))

    def assert_inventory(self, inventory, page):
        """Compare every displayed source row, including repeated species/ages."""
        def text(value):
            if value is None or value == "":
                return "Brak danych"
            if isinstance(value, (int, float)):
                return f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", "\u00a0").replace(".", ",")
            return value

        def fields(locator, pairs):
            expect(locator.locator(":scope > dt")).to_have_text([label for label, _ in pairs])
            expect(locator.locator(":scope > dd")).to_have_text([text(value) for _, value in pairs])

        content = page.locator(".inventory-content")
        expect(content).to_have_count(1)
        fields(content.locator(":scope > dl"), [
            ("Struktura drzewostanu (kod BDL)", inventory["structure"]),
            ("Wiek r\u0119bno\u015bci [lata]", inventory["rotationAge"]),
        ])
        layers = content.locator(".inventory-layer")
        expect(layers).to_have_count(len(inventory["layers"]))
        expect(content.locator(".inventory-species li")).to_have_count(
            sum(len(layer["species"]) for layer in inventory["layers"]))
        for index, layer in enumerate(inventory["layers"]):
            section = layers.nth(index)
            expect(section.locator("h4")).to_have_text(layer["name"])
            fields(section.locator(":scope > dl"), [
                ("Zwarcie", layer["density"]), ("Zmieszanie", layer["mixture"]),
                ("Wska\u017anik zadrzewienia", layer["stocking"]),
            ])
            rows = section.locator(".inventory-species li")
            expect(rows).to_have_count(len(layer["species"]))
            for i, item in enumerate(layer["species"]):
                expect(rows.nth(i).locator("h5")).to_have_text(item["name"])
                fields(rows.nth(i).locator("dl"), [
                    ("Udzia\u0142", item["share"]), ("Wiek [lata]", item["age"]),
                    ("Bonitacja", item["siteClass"]), ("Wysoko\u015b\u0107 [m]", item["height"]),
                    ("Pier\u015bnica [cm]", item["diameter"]),
                ])
        expect(content).not_to_contain_text(re.compile(r"XLSX|m[\u00b33]|mi\u0105\u017cszo\u015b\u0107|pozyskanie", re.I))

    def assert_inventory_bounds(self, page):
        self.wait_map_idle(page)
        self.assert_no_overflow(page)
        bounds = page.evaluate("""() => {
          const rect = selector => {
            const r = document.querySelector(selector).getBoundingClientRect();
            return {left: r.left, right: r.right, top: r.top, bottom: r.bottom};
          };
          return {map: rect('#map'), popup: rect('.leaflet-popup')};
        }""")
        for edge in ("left", "top"):
            self.assertGreaterEqual(bounds["popup"][edge], bounds["map"][edge] - 1, bounds)
        for edge in ("right", "bottom"):
            self.assertLessEqual(bounds["popup"][edge], bounds["map"][edge] + 1, bounds)

    def test_inventory_real_source_complete_lazy_toggle_desktop_mobile(self):
        """INFO-03: complete decoded source inventory, lazy once, stable popup/focus."""
        feature = self.inventory_bdl
        self.assertEqual(feature["properties"]["inventory"], self.source_inventory)
        self.assertRegex(self.manifest["inventoryVersion"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(self.manifest["inventoryVersion"], self.manifest["version"])
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                self.click_point(shape(feature["geometry"]).representative_point(), page)
                self.choose(feature, page)
                details = page.locator("details.inventory")
                expect(details).to_have_count(1)
                expect(details.locator("summary")).to_have_text("Opis taksacyjny \u00b7 BDL 2026")
                self.assertFalse(details.evaluate("el => el.open"))
                expect(page.locator(".inventory-content")).to_have_count(0)
                self.assertTrue(details.evaluate("""el => {
                  const detail = el.parentElement;
                  return ['.source-label', 'h3', 'dl', 'small', '.primary'].every(selector => {
                    const node = detail.querySelector(selector);
                    return node && !el.contains(node) && !!(node.compareDocumentPosition(el) & 4);
                  });
                }"""))
                before = self.saved(page)
                page.evaluate("""() => {
                  window.__testInventoryPopup = __testMap._popup;
                  window.__testInventoryFocus = Object.values(__testMap._layers).find(layer =>
                    layer instanceof L.GeoJSON && layer.options.style?.color === '#102bff').getLayers()[0];
                  window.__testInventoryUpdates = 0;
                  const original = __testInventoryPopup.update;
                  __testInventoryPopup.update = function(...args) {
                    __testInventoryUpdates++;
                    return original.apply(this, args);
                  };
                }""")
                content = None
                for count, opened in enumerate((True, False, True), 1):
                    details.locator("summary").click()
                    page.wait_for_function("expected => document.querySelector('.inventory').open === expected",
                                           arg=opened)
                    page.wait_for_function("count => __testInventoryUpdates >= count", arg=count)
                    self.assert_inventory(self.source_inventory, page)
                    if content is None:
                        content = page.locator(".inventory-content").element_handle()
                    self.assertTrue(content.evaluate("el => el === document.querySelector('.inventory-content')"))
                    self.assertTrue(page.evaluate("""() => __testMap._popup === __testInventoryPopup &&
                      __testMap.hasLayer(__testInventoryPopup) && __testMap.hasLayer(__testInventoryFocus)"""))
                    expect(page.locator('.result-button[aria-pressed="true"]')).to_have_count(1)
                    expect(page.locator(".detail-view h3")).to_have_text(pretty(feature["properties"]["address"]))
                    self.assert_active_object(None, page)
                    self.assert_inventory_bounds(page)
                    self.assertEqual(self.saved(page), before)
                page.locator(".inventory-species h5").first.scroll_into_view_if_needed()
                self.assert_inventory_bounds(page)
                page.screenshot(path=str(SCREENSHOTS / f"pul-{'mobile' if mobile else 'desktop'}-inventory.png"), full_page=True)
                content.dispose()
                page.locator(".leaflet-popup-close-button").click(timeout=2000)
                expect(page.locator(".leaflet-popup")).to_have_count(0)
                self.assert_active_note(None, page)

    def test_inventory_toggle_preserves_add_active_notes_edit_and_reload(self):
        """INFO-03, NOTE-01, SEL-06, SAVE-01: disclosure never replaces selection/notes."""
        feature = self.inventory_bdl
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.open_page(mobile=True) if mobile else self.page
                self.click_point(shape(feature["geometry"]).representative_point(), page)
                self.choose(feature, page)
                page.locator(".inventory summary").click()
                expect(page.locator(".inventory-content")).to_have_count(1)
                page.locator(".detail-view .primary").click()
                self.wait_saved([{"id": feature["properties"]["id"], "source": "bdl", "note": ""}], page, exact_count=True)
                self.assert_active_note(feature, page)
                self.choose(feature, page)
                expect(page.locator(".inventory-content")).to_have_count(0)
                for opened in (True, False, True):
                    page.locator(".inventory summary").click()
                    page.wait_for_function("value => document.querySelector('.inventory').open === value", arg=opened)
                    self.assert_active_note(feature, page)
                    self.assert_active_object(None, page)
                    popup_note = page.locator(".detail-view textarea")
                    sidebar_note = page.locator(".entry.is-active textarea")
                    popup_note.fill(f"Popup edit after toggle {opened}")
                    expect(sidebar_note).to_have_value(f"Popup edit after toggle {opened}")
                    sidebar_note.fill(f"List edit after toggle {opened}")
                    expect(popup_note).to_have_value(f"List edit after toggle {opened}")
                    self.wait_saved([{"id": feature["properties"]["id"], "source": "bdl",
                                      "note": f"List edit after toggle {opened}"}], page, exact_count=True)
                    self.assert_inventory_bounds(page)
                page.locator(".leaflet-popup-close-button").click(timeout=2000)
                expect(page.locator(".leaflet-popup")).to_have_count(0)
                self.assert_active_note(None, page)
                page.reload()
                self.wait_ready(page)
                page.locator("#list-tab").click()
                expect(page.locator(".entry textarea")).to_have_value("List edit after toggle True")
                self.assertEqual(json.loads(self.saved(page))["version"], self.manifest["version"])

    def test_inventory_switch_result_collapses_and_never_joins_plan(self):
        """INFO-03, SEL-04: neither exact-address nor spatial BULIG hits inherit BDL."""
        for plan, bdl, point in ((self.plan, self.bdl, self.overlap),
                                 (self.matched_plan, self.matched_bdl, None)):
            with self.subTest(plan=plan["properties"]["id"]):
                page = self.open_page()
                if point is None:
                    self.click_point(shape(bdl["geometry"]).representative_point(), page)
                    self.choose(bdl, page)
                    page.locator(".inventory summary").click()
                    self.click_point(shape(plan["geometry"]).representative_point(), page)
                else:
                    self.click_point(point, page)
                    self.choose(bdl, page)
                    page.locator(".inventory summary").click()
                    expect(page.locator(".inventory-content")).to_have_count(1)
                    self.choose(plan, page)
                    expect(page.locator(".inventory, .inventory-content, .inventory-layer")).to_have_count(0)
                    self.choose(bdl, page)
                    self.assertFalse(page.locator(".inventory").evaluate("el => el.open"))
                    expect(page.locator(".inventory-content")).to_have_count(0)
                self.choose(plan, page)
                expect(page.locator(".inventory, .inventory-content, .inventory-layer")).to_have_count(0)
                expect(page.locator(".detail-view")).not_to_contain_text("Wiek r\u0119bno\u015bci")
                self.assert_active_object(plan, page)
                expect(page.locator("#count")).to_have_text("0")
                self.assertIsNone(self.saved(page))

    def test_inventory_missing_empty_zero_and_decimal_fixture_in_memory_only(self):
        """INFO-03: browser-only fixture keeps zeros, labels missing values, rounds pl-PL."""
        feature = self.inventory_bdl
        inventory = {"structure": "", "rotationAge": 0, "layers": [{
            "code": "DRZEW", "name": "Test layer", "density": None, "mixture": "", "stocking": 0,
            "species": [{"code": "SO", "name": "Test species", "age": 0, "share": "",
                         "siteClass": None, "height": 12.346, "diameter": 12345.678},
                        {"code": "BRZ", "name": "Missing species", "age": None, "share": None,
                         "siteClass": "", "height": 0, "diameter": None}],
        }]}
        # Only the selected feature in the HTTP response is replaced. No disk writes.
        payload = {"type": "FeatureCollection", "features": [
            dict(f, properties=dict(f["properties"], inventory=inventory))
            if f["properties"]["id"] == feature["properties"]["id"] else f
            for f in self.features["bdl"]]}
        self.page.route("**/layers/bdl.geojson", lambda route: route.fulfill(json=payload))
        self.page.reload()
        self.wait_ready()
        self.click_point(shape(feature["geometry"]).representative_point())
        self.choose(feature)
        expect(self.page.locator(".inventory-content")).to_have_count(0)
        self.page.locator(".inventory summary").click()
        self.assert_inventory(inventory, self.page)
        expect(self.page.locator(".inventory-content > dl > dd")).to_have_text(["Brak danych", "0"])
        expect(self.page.locator(".inventory-species li").first.locator("dd")).to_have_text([
            "Brak danych", "0", "Brak danych", "12,35", "12\u00a0345,68",
        ])

    def test_geometry_area_in_bdl_and_plan_popup_and_format_boundaries(self):
        """INFO-02: the chosen element's numeric area, not a joined or harvested area."""
        self.click_point()
        areas = []
        for feature in (self.bdl, self.plan):
            with self.subTest(source=feature["properties"]["source"]):
                self.choose(feature)
                area = feature["properties"]["areaHa"]
                self.assertIsInstance(area, (int, float))
                self.assertGreater(area, 0)
                expected = "< 0,01 ha" if area < .01 else f"{area:.2f}".replace(".", ",") + " ha"
                expect(self.page.locator(".detail-view dt").get_by_text(
                    "Powierzchnia elementu", exact=True).locator("+ dd")).to_have_text(expected)
                areas.append(area)
        self.assertNotEqual(*areas, "Fixture must distinguish the two source geometries")
        cases = [(0, "0,00 ha"), (.000001, "< 0,01 ha"), (.009999, "< 0,01 ha"),
                 (.01, "0,01 ha"), (.014, "0,01 ha"), (.015, "0,02 ha"),
                 (1, "1,00 ha"), (12.345, "12,35 ha"), (1234.5, "1234,50 ha"),
                 (12345.5, "12\u00a0345,50 ha"),
                 (None, "Brak danych"), (-1, "Brak danych"), ("1.5", "Brak danych")]
        for value, expected in cases:
            with self.subTest(area=value):
                self.assertEqual(self.page.evaluate("value => PulCore.formatArea(value)", value), expected)
        self.assertEqual(self.page.evaluate("""() => [undefined, NaN, Infinity, -Infinity].map(
          value => PulCore.formatArea(value))"""), ["Brak danych"] * 4)

    def test_pre_additions_saved_entries_keep_same_source_fingerprint(self):
        """SAVE-01, INFO-02/UI-05: derived fields/UI must not invalidate old entries."""
        fingerprint = "sha256:2e99b0b7e79dad47b7b2dcbe5ff69ab7c456ddd10cbcda534e0bf387ecd0b6c0"
        self.assertEqual(self.manifest["version"], fingerprint)
        entries = [{"source": f["properties"]["source"], "id": f["properties"]["id"],
                    "address": f["properties"]["address"], "key": address_key(f["properties"]["address"]),
                    "note": f"Legacy {f['properties']['source']} note\nSecond line"}
                   for f in (self.bdl, self.plan)]
        raw = json.dumps({"schema": 1, "version": fingerprint, "entries": entries})
        self.page.evaluate("([key, raw]) => localStorage.setItem(key, raw)", [STORAGE_KEY, raw])
        self.page.reload()
        self.wait_ready()
        self.page.locator("#list-tab").click()
        expect(self.page.locator(".entry")).to_have_count(2)
        expect(self.page.locator("#warnings")).not_to_contain_text("Nie można odtworzyć listy")
        for entry in entries:
            card = self.page.locator(".entry").filter(has=self.page.get_by_text(pretty(entry["address"]), exact=True))
            expect(card.locator("textarea")).to_have_value(entry["note"])
        self.assertEqual(self.saved(), raw, "Loading a valid legacy snapshot must not rewrite it")
        self.click_point()
        self.choose(self.plan)
        self.page.locator(".detail-view textarea").fill("Legacy entry remains editable")
        updated = json.loads(self.wait_saved([
            {"note": "Legacy entry remains editable"}, {"note": entries[0]["note"]},
        ], exact_count=True))
        self.assertEqual(updated["version"], fingerprint)
        self.assertEqual({(e["source"], e["id"], e["key"]) for e in updated["entries"]},
                         {(e["source"], e["id"], e["key"]) for e in entries})

    def test_repeated_plan_address_keeps_each_object_and_percent(self):
        """PLAN-03: repeated address records remain distinct, never summed."""
        self.page.locator("#bdl-visible").uncheck()
        for feature in self.repeated_plan:
            with self.subTest(id=feature["properties"]["id"]):
                self.click_point(shape(feature["geometry"]).representative_point())
                self.choose(feature)
                expect(self.page.locator(".detail-view dt").get_by_text("Planowane pozyskanie", exact=True).locator("+ dd")).to_have_text(
                    f"{feature['properties']['percent']:g}%")
                self.page.locator(".detail-view .primary").click()
                expect(self.page.locator("#count")).to_have_text("1")

    def test_stale_and_corrupt_storage_is_protected_until_confirmed_clear(self):
        """SAVE-01/02: invalid persisted state is warned about, not overwritten."""
        p = self.bdl["properties"]
        valid_entry = {"source": "bdl", "id": p["id"], "address": p["address"],
                       "key": address_key(p["address"]), "note": "Keep"}
        payloads = [
            "{not valid JSON",
            json.dumps({"schema": 1, "version": "old-dataset", "entries": []}),
            json.dumps({"schema": 1, "version": self.manifest["version"], "entries": [
                {"source": "bdl", "id": "missing", "address": "bad", "key": "bad", "note": "Keep"}
            ]}),
        ]
        for fields in ({"source": ["bdl"]}, {"id": int(p["id"])}, {"note": None}):
            payloads.append(json.dumps({"schema": 1, "version": self.manifest["version"],
                                        "entries": [dict(valid_entry, **fields)]}))
        for raw in payloads:
            with self.subTest(raw=raw):
                self.page.evaluate("([key, raw]) => localStorage.setItem(key, raw)", [STORAGE_KEY, raw])
                self.page.reload()
                self.wait_ready()
                expect(self.page.locator("#warnings")).to_contain_text("Oryginalny zapis pozostaje nietkni\u0119ty")
                self.click_point()
                self.choose(self.plan)
                expect(self.page.locator(".detail-view .primary")).to_be_disabled()
                self.assertEqual(self.saved(), raw)
                self.confirm_clear(False)
                self.assertEqual(self.saved(), raw)
                self.confirm_clear(True)
                self.assertEqual(json.loads(self.saved())["entries"], [])
                self.add()
                expect(self.page.locator("#count")).to_have_text("1")

    def test_sidebar_and_popup_note_editors_stay_synchronized(self):
        """NOTE-01, SAVE-01: both live editors share edits without reopening."""
        self.add()
        self.click_point()
        self.choose(self.plan)
        sidebar = self.page.locator(".entry textarea")
        popup = self.page.locator(".detail-view textarea")
        expect(sidebar).to_have_value("")
        expect(popup).to_have_value("")
        for text in ("Sidebar edit\nSecond line", "", "Sidebar replacement"):
            sidebar.fill(text)
            expect(popup).to_have_value(text)
            self.wait_saved([{"note": text}], exact_count=True)
        for text in ("Popup edit\nSecond line", "", "Final popup replacement"):
            popup.fill(text)
            expect(sidebar).to_have_value(text)
            self.wait_saved([{"note": text}], exact_count=True)
        self.page.reload()
        self.wait_ready()
        self.page.locator("#list-tab").click()
        expect(sidebar).to_have_value("Final popup replacement")
        self.click_point()
        self.choose(self.plan)
        expect(popup).to_have_value("Final popup replacement")

    def test_removing_entry_closes_popup_and_invalidates_detached_editor(self):
        """LIST-01/04, NOTE-01, SAVE-01: removed notes cannot be edited back in."""
        self.add()
        self.click_point()
        self.choose(self.plan)
        popup_note = self.page.locator(".detail-view textarea")
        popup_note.fill("Remove this note")
        self.wait_saved([{"note": "Remove this note"}])
        # Retain the old DOM node to simulate a late input from a stale editor.
        old_editor = popup_note.element_handle()
        self.page.locator(".entry button").get_by_text("Usu\u0144", exact=True).click()
        expect(self.page.locator(".leaflet-popup")).to_have_count(0)
        expect(self.page.locator(".entry")).to_have_count(0)
        empty = self.wait_saved([], exact_count=True)
        old_editor.evaluate("""textarea => {
          textarea.value = 'Late stale edit';
          textarea.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        old_editor.dispose()
        self.assertEqual(self.saved(), empty)
        expect(self.page.locator("#count")).to_have_text("0")
        self.page.reload()
        self.wait_ready()
        expect(self.page.locator("#count")).to_have_text("0")

    def test_newer_tab_storage_event_blocks_stale_editors(self):
        """SAVE-01, NOTE-01: real shared-origin tabs protect newer notes."""
        self.add()
        self.page.locator(".entry textarea").fill("Initial shared note")
        self.wait_saved([{"note": "Initial shared note"}])
        newer = self.page.context.new_page()
        newer.on("pageerror", lambda error: self.errors.append(str(error)))
        newer.goto(self.url)
        self.wait_ready(newer)
        self.click_point()
        self.choose(self.plan)
        newer.locator("#list-tab").click()
        newer.locator(".entry textarea").fill("Newer tab note")
        latest = self.wait_saved([{"note": "Newer tab note"}], newer)
        expect(self.page.locator("#warnings")).to_contain_text("Lista zmieni\u0142a si\u0119 w innej karcie")
        expect(self.page.locator(".entry textarea")).to_be_disabled()
        expect(self.page.locator(".leaflet-popup")).to_have_count(0)
        self.page.locator(".entry textarea").evaluate("""textarea => {
          textarea.value = 'Stale event after blocking';
          textarea.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        self.assertEqual(self.saved(), latest)
        self.page.reload()
        self.wait_ready()
        self.page.locator("#list-tab").click()
        expect(self.page.locator(".entry textarea")).to_have_value("Newer tab note")

    def test_stale_input_before_storage_event_preserves_newer_snapshot(self):
        """SAVE-01, NOTE-01: snapshot guard works before event-driven blocking."""
        self.add()
        self.page.locator(".entry textarea").fill("Original snapshot")
        self.wait_saved([{"note": "Original snapshot"}])
        self.assertTrue(self.page.evaluate("!!navigator.locks"), "Web Locks unavailable on localhost")
        # Defer only this tab's real event handler. Both tabs keep real storage,
        # real Web Locks, and unmodified app input/persistence handlers.
        self.page.add_init_script("""(() => {
          window.__testDeferredStorage = [];
          window.__testDeferStorage = event => {
            if (event.key === 'pul-dojlidy-list-v1') {
              window.__testDeferredStorage.push(event);
              event.stopImmediatePropagation();
            }
          };
          addEventListener('storage', window.__testDeferStorage, true);
        })();""")
        self.page.reload()
        self.wait_ready()
        self.page.locator("#list-tab").click()
        newer = self.page.context.new_page()
        newer.on("pageerror", lambda error: self.errors.append(str(error)))
        newer.goto(self.url)
        self.wait_ready(newer)
        newer.locator("#list-tab").click()
        newer.locator(".entry textarea").fill("Newer committed snapshot")
        latest = self.wait_saved([{"note": "Newer committed snapshot"}], newer)
        self.page.wait_for_function("window.__testDeferredStorage.length === 1")
        expect(self.page.locator(".entry textarea")).to_be_enabled()
        expect(self.page.locator(".entry textarea")).to_have_value("Original snapshot")
        self.page.locator(".entry textarea").evaluate("""textarea => {
          textarea.value = 'Unsaved stale input';
          textarea.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        expect(self.page.locator("#warnings")).to_contain_text("Wykryto nowszy zapis z innej karty")
        expect(self.page.locator(".entry textarea")).to_be_disabled()
        expect(self.page.locator(".entry textarea")).to_have_value("Unsaved stale input")
        self.assertEqual(self.saved(), latest)
        # Deliver the deferred event only after verifying the snapshot-check path.
        self.page.evaluate("""() => {
          removeEventListener('storage', window.__testDeferStorage, true);
          for (const event of window.__testDeferredStorage) dispatchEvent(event);
        }""")
        self.assertEqual(self.saved(), latest)
        self.page.reload()
        self.wait_ready()
        self.page.locator("#list-tab").click()
        expect(self.page.locator(".entry textarea")).to_have_value("Newer committed snapshot")

    def test_same_address_cross_source_discloses_original_saved_source(self):
        """LIST-04, SEL-04: deduplication must not silently change provenance."""
        for first, second in ((self.matched_bdl, self.matched_plan),
                              (self.matched_plan, self.matched_bdl)):
            with self.subTest(saved_source=first["properties"]["source"]):
                page = self.open_page()
                self.click_point(shape(first["geometry"]).representative_point(), page)
                self.choose(first, page)
                expect(page.locator(".detail-view")).not_to_contain_text("Zgodny adres w BDL 2026")
                page.locator(".detail-view .primary").click()
                page.locator(".entry textarea").fill("Retain original source and geometry")
                original = self.wait_saved([{
                    "source": first["properties"]["source"], "id": first["properties"]["id"],
                    "note": "Retain original source and geometry",
                }], page, exact_count=True)
                self.click_point(shape(second["geometry"]).representative_point(), page)
                self.choose(second, page)
                expect(page.locator(".detail-view")).not_to_contain_text("Zgodny adres w BDL 2026")
                source = "BDL 2026" if first["properties"]["source"] == "bdl" else "BULIG"
                expect(page.locator(".detail-view small")).to_have_text(
                    f"Adres zapisany ze \u017ar\u00f3d\u0142a: {source}. Akcja otwiera istniej\u0105cy wpis i nie zmienia jego \u017ar\u00f3d\u0142a ani geometrii.")
                expect(page.locator(".detail-view .primary")).to_have_text("Adres ju\u017c na li\u015bcie \u00b7 edytuj notatk\u0119")
                expect(page.locator(".detail-view textarea")).to_have_value("Retain original source and geometry")
                page.locator(".detail-view .primary").click()
                expect(page.locator(".entry")).to_have_count(1)
                expect(page.locator(".entry small")).to_have_text(f"\u0179r\u00f3d\u0142o adresu: {source}")
                self.wait_saved([{"source": first["properties"]["source"],
                                  "id": first["properties"]["id"]}], page, exact_count=True)
                self.assertEqual(self.saved(page), original)

    def test_contains_polygon_hole_and_multipolygon(self):
        """SEL-01/05: synthetic geometry cases exercise the shipped browser core."""
        outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        hole = [[3, 3], [7, 3], [7, 7], [3, 7], [3, 3]]
        island = [[20, 0], [24, 0], [24, 4], [20, 4], [20, 0]]
        polygon = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [outer, hole]}}
        multi = {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": [[outer, hole], [island]]}}
        cases = [(1, 1, True), (5, 5, False), (12, 2, False), (0, 5, True),
                 (3, 5, False), (22, 2, False), (-1, 5, False)]
        for feature in (polygon, multi):
            for with_bbox in (False, True):
                geometry = dict(feature)
                if with_bbox:
                    geometry["bbox"] = [0, 0, 24 if feature is multi else 10, 10]
                for x, y, expected in cases:
                    if feature is multi and (x, y) == (22, 2):
                        expected = True
                    with self.subTest(type=feature["geometry"]["type"], bbox=with_bbox, point=(x, y)):
                        self.assertIs(self.page.evaluate("""({feature, x, y}) =>
                          PulCore.contains(feature, x, y)
                        """, {"feature": geometry, "x": x, "y": y}), expected)

    def test_external_tile_failure_does_not_block_local_work(self):
        """INFO-01, SEL-01, LIST-01, SAVE-01 under deterministic tile failure."""
        expect(self.page.locator("#warnings")).to_contain_text("OSM: nie uda\u0142o si\u0119 pobra\u0107")
        self.assertTrue(any("tile.openstreetmap.org" in url for url in self.blocked_requests))
        self.add(self.bdl)
        self.page.locator(".entry textarea").fill("Local functionality despite tile failure")
        self.wait_saved([{"note": "Local functionality despite tile failure"}])
        self.page.reload()
        self.wait_ready()
        self.page.locator("#list-tab").click()
        expect(self.page.locator(".entry textarea")).to_have_value("Local functionality despite tile failure")
        self.click_point()
        self.choose(self.bdl)
        expect(self.page.locator(".detail-view")).to_contain_text("BDL \u00b7 stan na 2026 rok")


if __name__ == "__main__":
    unittest.main()
