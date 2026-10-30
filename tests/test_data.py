"""Data acceptance tests; no app or remote-service tests are implied."""

import importlib.util
import json
import math
from collections import Counter
from pathlib import Path
import unittest

import shapefile
from pyproj import Transformer
from shapely import equals_exact, make_valid
from shapely.geometry import MultiPolygon, Point, shape
from shapely.ops import transform


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_data", ROOT / "scripts/prepare_data.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class DataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layers = {
            name: json.loads((pipeline.OUTPUT / f"{name}.geojson").read_text(encoding="utf-8"))
            for name in pipeline.SOURCES
        }
        cls.manifest = json.loads((pipeline.OUTPUT / "manifest.json").read_text(encoding="utf-8"))

    def test_counts_unique_ids_and_types(self):
        self.assertEqual(self.manifest["counts"], {"bdl": 6105, "rozplan": 826, "boundary": 1})
        for name, collection in self.layers.items():
            self.assertEqual(collection["type"], "FeatureCollection")
            features = collection["features"]
            self.assertEqual(len(features), pipeline.COUNTS[name])
            ids = [f["properties"]["id"] for f in features]
            self.assertTrue(all(isinstance(i, str) and i for i in ids))
            self.assertEqual(len(set(ids)), len(ids))
        plan = self.layers["rozplan"]["features"]
        self.assertEqual(sorted({f["properties"]["type"] for f in plan}), pipeline.TYPES)
        self.assertEqual(self.manifest["types"], pipeline.TYPES)
        # Repeated addresses represent separate interventions, never merged.
        addresses = Counter(f["properties"]["address"] for f in plan)
        self.assertEqual(len(addresses), 750)
        repeated = [count for count in addresses.values() if count > 1]
        self.assertEqual(len(repeated), 64)
        self.assertEqual((min(repeated), max(repeated)), (2, 4))

    def test_source_cutting_classification_and_counts(self):
        counts = Counter()
        with shapefile.Reader(str(pipeline.SOURCES["rozplan"]), encoding="utf-8") as reader:
            for record in reader.iterRecords():
                row = record.as_dict()
                expected = "zup" if row["rebnia_1"] in ("IA", "IB") else "zloz"
                self.assertEqual(row["ciecia"], expected)
                counts[row["rebnia_1"], row["ciecia"]] += 1
        self.assertEqual(counts, {
            ("IA", "zup"): 2, ("IB", "zup"): 266,
            ("IIA", "zloz"): 60, ("IIAU", "zloz"): 3,
            ("IIB", "zloz"): 20, ("IIBU", "zloz"): 42,
            ("IIIA", "zloz"): 47, ("IIIAU", "zloz"): 41,
            ("IIIB", "zloz"): 100, ("IIIBU", "zloz"): 35,
            ("IVD", "zloz"): 189, ("IVDU", "zloz"): 1,
            ("V", "zloz"): 20,
        })
        self.assertEqual(Counter(f["properties"]["type"] for f in self.layers["rozplan"]["features"]),
                         Counter({kind: count for (kind, _), count in counts.items()}))

    def test_source_attributes_preserved_and_translated(self):
        lookup = pipeline.dictionaries()
        for name, path in pipeline.SOURCES.items():
            with shapefile.Reader(str(path), encoding="utf-8") as reader:
                for record, feature in zip(reader.iterRecords(), self.layers[name]["features"]):
                    row, props = record.as_dict(), feature["properties"]
                    is_plan = name == "rozplan"
                    self.assertEqual(props["id"], str(row["OBJECTID_1" if is_plan else "a_i_num"]))
                    self.assertEqual(props["address"], row["ADR_FOR" if is_plan else "adr_for"])
                    self.assertEqual(props["source"], "rozplan" if is_plan else "bdl")
                    if is_plan:
                        self.assertEqual(props["type"], row["rebnia_1"])
                        self.assertIsInstance(props["percent"], (int, float))
                        self.assertEqual(props["percent"], float(row["proc"]))
                        self.assertTrue(20 <= props["percent"] <= 100)
                        for target, source in (("original", "N_REBNIA"), ("compartment", "NR_DZIAL"),
                                               ("strip", "NR_PASA"), ("planYear", "ROK")):
                            self.assertIsInstance(props[target], str)
                            self.assertEqual(props[target], str(row[source]))
                    else:
                        self.assertEqual(props["year"], 2026)
                        self.assertTrue(all(isinstance(v, str) and v for v in props["details"].values()))
                        if name == "bdl":
                            self.assertEqual(set(props["details"]), {s[0] for s in pipeline.DETAILS.values()})
                            for field, (label, *_) in pipeline.DETAILS.items():
                                code = row[field].strip()
                                expected = lookup[field][code] if code else "Brak danych"
                                self.assertEqual(props["details"][label], expected)
        example = self.layers["bdl"]["features"][0]["properties"]["details"]
        self.assertEqual(example["Rodzaj powierzchni"], "drzewostan")
        self.assertEqual(example["Funkcja lasu"], "lasy gospodarcze")
        self.assertEqual(example["Kategoria ochronnosci"], "Brak danych")

    def test_valid_geometry_projection_bbox_and_no_loss(self):
        projection = Transformer.from_crs(2180, 4326, always_xy=True)
        for name, path in pipeline.SOURCES.items():
            with shapefile.Reader(str(path), encoding="utf-8") as reader:
                for source, feature in zip(reader.iterShapes(), self.layers[name]["features"]):
                    actual = shape(feature["geometry"])
                    self.assertIn(actual.geom_type, ("Polygon", "MultiPolygon"))
                    self.assertTrue(actual.is_valid)
                    self.assertFalse(actual.is_empty)
                    self.assertEqual(feature["bbox"], list(actual.bounds))
                    xmin, ymin, xmax, ymax = actual.bounds
                    self.assertTrue(22 < xmin <= xmax < 24)
                    self.assertTrue(52 < ymin <= ymax < 54)
                    original = shape(source.__geo_interface__)
                    stats = pipeline.polygon_stats(original)
                    self.assertEqual(stats["polygons"] + stats["holes"], len(source.parts))
                    self.assertEqual(stats["vertices"], len(source.points))
                    expected = make_valid(original) if not original.is_valid else original
                    expected = transform(projection.transform, expected)
                    self.assertTrue(equals_exact(actual, expected, tolerance=0, normalize=True))
                    polygons = actual.geoms if actual.geom_type == "MultiPolygon" else [actual]
                    for polygon in polygons:
                        self.assertTrue(polygon.exterior.is_ccw)
                        self.assertTrue(all(not ring.is_ccw for ring in polygon.interiors))

    def test_repairs_explicit_and_area_preserving(self):
        validation = self.manifest["validation"]["layers"]
        repairs = validation["bdl"]["repairs"]
        self.assertEqual({r["id"] for r in repairs},
                         {"108015803", "108031428", "108028385", "108026376", "108032687"})
        self.assertEqual(validation["bdl"]["sourceInvalidGeometries"], 5)
        for repair in repairs:
            self.assertIn("Ring Self-intersection", repair["reason"])
            self.assertEqual(repair["method"], "shapely.make_valid")
            self.assertLess(abs(repair["areaDeltaM2"]), 1e-6)
            self.assertEqual(repair["after"]["holes"], repair["before"]["holes"] + 1)
        for name in ("rozplan", "boundary"):
            self.assertEqual(validation[name]["repairs"], [])
            self.assertEqual(validation[name]["sourceInvalidGeometries"], 0)
        for name in pipeline.SOURCES:
            self.assertEqual(validation[name]["unknownDictionaryCodes"], {})
            self.assertEqual(validation[name]["validOutputGeometries"], pipeline.COUNTS[name])

    def test_area_ha_from_repaired_source_without_rounding(self):
        for name, path in pipeline.SOURCES.items():
            with shapefile.Reader(str(path), encoding="utf-8") as reader:
                for source, feature in zip(reader.iterShapes(), self.layers[name]["features"]):
                    original = shape(source.__geo_interface__)
                    repaired = make_valid(original) if not original.is_valid else original
                    expected = repaired.area / 10000
                    actual = feature["properties"]["areaHa"]
                    self.assertIsInstance(actual, (int, float))
                    self.assertTrue(math.isfinite(actual) and actual > 0)
                    self.assertTrue(math.isclose(actual, expected, rel_tol=1e-14, abs_tol=1e-14))
                    # JSON retains the source calculation's full float precision.
                    self.assertEqual(actual, expected)

    def test_holes_multipart_and_click_topology(self):
        exterior = [(770000, 600000), (770000, 600100), (770100, 600100),
                    (770100, 600000), (770000, 600000)]
        hole = [(770020, 600020), (770080, 600020), (770080, 600080),
                (770020, 600080), (770020, 600020)]
        other = [(770200, 600000), (770200, 600100), (770300, 600100),
                 (770300, 600000), (770200, 600000)]
        source = shapefile.Shape(shapefile.POLYGON)
        source.points, source.parts = exterior + hole + other, [0, 5, 10]
        repairs = []
        output, area_ha = pipeline.prepare_geometry(source, "synthetic", repairs)
        self.assertEqual(area_ha, 1.64)
        self.assertEqual(repairs, [])
        self.assertIsInstance(output, MultiPolygon)
        self.assertEqual(pipeline.polygon_stats(output), {"polygons": 2, "holes": 1, "vertices": 15})
        for xy, hit in (((770010, 600010), True), ((770050, 600050), False),
                        ((770250, 600050), True), ((770150, 600050), False)):
            point = Point(*pipeline.TO_WGS84.transform(*xy))
            self.assertEqual(output.covers(point), hit)

    def test_refuses_lossy_repairs_and_nonpolygons(self):
        source = shapefile.Shape(shapefile.POLYGON)
        source.parts = [0]
        # A spike becomes a LineString in make_valid; it must not be dropped.
        source.points = [(770000, 600000), (770000, 600100), (770050, 600100),
                         (770050, 600150), (770050, 600100), (770100, 600100),
                         (770100, 600000), (770000, 600000)]
        with self.assertRaisesRegex(ValueError, "nonpolygon components"):
            pipeline.prepare_geometry(source, "spike", [])
        with self.assertRaisesRegex(ValueError, "expected 2D polygon"):
            pipeline.prepare_geometry(shapefile.Shape(shapefile.NULL), "null", [])

    def test_reproducible_bytes_and_read_only_sources(self):
        before = pipeline.fingerprint()
        layers, manifest = pipeline.build_layers()
        self.assertEqual(before, pipeline.fingerprint())
        self.assertEqual(self.manifest["version"], before)
        self.assertRegex(before, r"^sha256:[0-9a-f]{64}$")
        for name, value in {**layers, "manifest": manifest}.items():
            suffix = ".json" if name == "manifest" else ".geojson"
            expected = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
            self.assertEqual((pipeline.OUTPUT / (name + suffix)).read_bytes(), expected.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
