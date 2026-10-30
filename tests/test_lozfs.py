"""Independent exact-copy acceptance checks for LOZFS-01 only."""

import copy
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from shapely.geometry import shape
from shapely.validation import explain_validity


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    (ROOT / "glosowanie_maj_2026-main/propozycje.geojson", "lozfs-proposed.geojson", 34),
    (ROOT / "glosowanie_maj_2026-main/ustalone.geojson", "lozfs-established.geojson", 8),
)
spec = importlib.util.spec_from_file_location("prepare_lozfs", ROOT / "scripts/prepare_lozfs.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class LozfsTests(unittest.TestCase):
    def test_published_bytes_match_entire_sources(self):
        for source, filename, _ in SOURCES:
            with self.subTest(layer=filename):
                self.assertEqual((ROOT / "web/layers" / filename).read_bytes(), source.read_bytes())

    def test_names_properties_and_geometry_are_preserved_in_order(self):
        for source, filename, count in SOURCES:
            original = json.loads(source.read_bytes())
            published = json.loads((ROOT / "web/layers" / filename).read_bytes())
            self.assertEqual(published["type"], "FeatureCollection")
            self.assertEqual(len(published["features"]), count)
            self.assertEqual(published, original)
            for expected, actual in zip(original["features"], published["features"]):
                self.assertEqual(actual["type"], "Feature")
                self.assertEqual(actual["properties"]["name"], expected["properties"]["name"])
                self.assertEqual(actual["properties"], expected["properties"])
                self.assertEqual(actual["geometry"], expected["geometry"])

    def test_crs_coordinate_domain_and_polygon_shape(self):
        for _, filename, _ in SOURCES:
            collection = json.loads((ROOT / "web/layers" / filename).read_bytes())
            self.assertEqual(collection["crs"], {
                "type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
            })
            for feature in collection["features"]:
                geometry = feature["geometry"]
                self.assertIn(geometry["type"], ("Polygon", "MultiPolygon"))
                self.assertFalse(shape(geometry).is_empty)
                polygons = ([geometry["coordinates"]] if geometry["type"] == "Polygon"
                            else geometry["coordinates"])
                self.assertTrue(polygons)
                for polygon in polygons:
                    self.assertTrue(polygon)
                    for ring in polygon:
                        self.assertGreaterEqual(len(ring), 4)
                        self.assertEqual(ring[0], ring[-1])
                        for position in ring:
                            self.assertIn(len(position), (2, 3))
                            self.assertTrue(all(type(n) in (int, float) and math.isfinite(n)
                                                for n in position))
                            self.assertTrue(-180 <= position[0] <= 180)
                            self.assertTrue(-90 <= position[1] <= 90)

    def test_known_source_invalidity_is_preserved_not_repaired(self):
        for source, filename, _ in SOURCES:
            invalid = []
            for path in (source, ROOT / "web/layers" / filename):
                invalid.append([(i, explain_validity(shape(f["geometry"])))
                                for i, f in enumerate(json.loads(path.read_bytes())["features"])
                                if not shape(f["geometry"]).is_valid])
            self.assertEqual(invalid[0], invalid[1])
            self.assertEqual([i for i, _ in invalid[0]], [3] if "proposed" in filename else [])
            if invalid[0]:
                self.assertTrue(invalid[0][0][1].startswith("Self-intersection["))

    def test_copy_is_reproducible_and_originals_are_read_only(self):
        before = {p: (p.read_bytes(), p.stat()) for p, _, _ in SOURCES}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            stderr = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                pipeline.prepare(output)
                first = {name: (output / name).read_bytes() for _, name, _ in SOURCES}
                pipeline.prepare(output)
            self.assertIn("Self-intersection[", stderr.getvalue())
            self.assertIn("retained unchanged", stderr.getvalue())
            self.assertEqual(set(p.name for p in output.iterdir()), set(first))
            for source, filename, _ in SOURCES:
                self.assertEqual(first[filename], before[source][0])
                self.assertEqual((output / filename).read_bytes(), first[filename])
        for source, (raw, stat) in before.items():
            self.assertEqual(source.read_bytes(), raw)
            after = source.stat()
            for field in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_ino"):
                self.assertEqual(getattr(after, field), getattr(stat, field))

    def test_validation_rejects_bad_structure_crs_and_coordinates(self):
        original = json.loads(SOURCES[0][0].read_bytes())
        mutations = (
            lambda d: d.update(type="GeometryCollection"),
            lambda d: d.update(features=d["features"][:-1]),
            lambda d: d.pop("crs"),
            lambda d: d["crs"]["properties"].update(name="EPSG:2180"),
            lambda d: d["features"][0].update(type="NotFeature"),
            lambda d: d["features"][0].update(properties=None),
            lambda d: d["features"][0].update(geometry=None),
            lambda d: d["features"][0]["geometry"].update(type="LineString"),
            lambda d: d["features"][0]["geometry"].update(coordinates=[]),
        )
        for mutate in mutations:
            data = copy.deepcopy(original)
            mutate(data)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                pipeline.validate(json.dumps(data).encode(), 34)
        for position in ([181, 53], [23, 91], [float("nan"), 53],
                         [23, float("inf")], [True, 53], [23], [23, 53, float("inf")]):
            data = copy.deepcopy(original)
            ring = data["features"][0]["geometry"]["coordinates"][0][0]
            ring[0] = ring[-1] = position
            with self.subTest(position=position), self.assertRaises(ValueError):
                pipeline.validate(json.dumps(data).encode(), 34)
        with self.assertRaises(ValueError):
            pipeline.validate(b"not JSON", 34)

    def test_both_sources_validated_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(pipeline, "validate", side_effect=[[], ValueError("bad source")]):
                with self.assertRaisesRegex(ValueError, "bad source"):
                    pipeline.prepare(output)
            self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
