"""Browser acceptance tests with local data and deliberately unavailable tiles.

Run: /tmp/opencode/pul-venv/bin/python -m unittest discover -s tests -p test_browser.py -v
No availability, imagery, licensing, or accuracy claim about external services is made.
"""

from collections import defaultdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
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
        expect(forest.locator("#bdl-visible, #boundary-visible, #mbdl-visible, #plan-visible")).to_have_count(4)
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

    def wait_map_idle(self, page=None):
        (page or self.page).wait_for_function("""() =>
          !__testMap._animatingZoom && !__testMap._panAnim?._inProgress &&
          [...document.querySelectorAll('.leaflet-popup')].every(
            el => getComputedStyle(el).opacity === '1')
        """)

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
            self.plan["properties"]["original"],
        ])
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
        self.page.locator("#boundary-visible").uncheck()
        self.page.locator("#bdl-visible").uncheck()
        self.page.locator("#mbdl-visible").check()
        for base in ("ortho", "osm"):
            self.page.locator(f'input[name="base"][value="{base}"]').check()
            expect(self.page.locator("#bdl-visible")).not_to_be_checked()
            expect(self.page.locator("#boundary-visible")).not_to_be_checked()
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
                page.locator("#boundary-visible").uncheck()
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
                    for selector, checked in (("bdl", False), ("boundary", False), ("mbdl", True), ("plan", True)):
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
