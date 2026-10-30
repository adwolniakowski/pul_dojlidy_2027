"""Data acceptance tests; no app or remote-service tests are implied."""

import importlib.util
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
import unittest
from unittest.mock import patch

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
        inventory_before = pipeline.fingerprint(pipeline.inventory_source_files())
        layers, manifest = pipeline.build_layers()
        self.assertEqual(before, pipeline.fingerprint())
        self.assertEqual(inventory_before, pipeline.fingerprint(pipeline.inventory_source_files()))
        self.assertEqual(self.manifest["inventoryVersion"], inventory_before)
        self.assertEqual(self.manifest["version"], before)
        self.assertRegex(before, r"^sha256:[0-9a-f]{64}$")
        for name, value in {**layers, "manifest": manifest}.items():
            suffix = ".json" if name == "manifest" else ".geojson"
            expected = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
            self.assertEqual((pipeline.OUTPUT / (name + suffix)).read_bytes(), expected.encode("utf-8"))

    def test_inventory_exact_source_mapping_all_records(self):
        def read(filename):
            with (pipeline.BDL / filename).open(encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle, delimiter="\t"))

        dictionaries = {
            kind: {r[code].strip(): r[name].strip() for r in read(filename)}
            for kind, (filename, code, name) in pipeline.INVENTORY_DICTIONARIES.items()}
        subareas = {r["arodes_int_num"]: r for r in read("f_subarea.txt") if r["a_year"] == "2026"}
        metadata, species = {}, {}
        for r in read("f_arod_storey.txt"):
            self.assertEqual(r["a_year"], "2026")
            key = (r["arodes_int_num"], r["storey_cd"].strip())
            self.assertNotIn(key, metadata)
            metadata[key] = r
        for r in read("f_storey_species.txt"):
            self.assertEqual(r["a_year"], "2026")
            species.setdefault((r["arodes_int_num"], r["storey_cd"].strip()), []).append(r)
        numeric_coverage, shares = Counter(), Counter()
        layer_count = species_count = 0
        for feature in self.layers["bdl"]["features"]:
            props = feature["properties"]
            identifier, actual = props["id"], props["inventory"]
            source = subareas[identifier]
            self.assertEqual(set(actual), {"structure", "rotationAge", "layers"})
            self.assertEqual(actual["structure"], source["stand_struct_cd"].strip() or None)
            self.assertEqual(actual["rotationAge"], float(source["rotation_age"]) if source["rotation_age"].strip() else None)
            keys = {k for k in metadata.keys() | species.keys() if k[0] == identifier}
            ordered = sorted(keys, key=lambda k: (float(metadata[k]["st_rank_order_act"]) if k in metadata else math.inf, k[1]))
            self.assertEqual([l["code"] for l in actual["layers"]], [k[1] for k in ordered])
            for key, layer in zip(ordered, actual["layers"]):
                layer_count += 1
                self.assertEqual(set(layer), {"code", "name", "density", "mixture", "stocking", "species"})
                self.assertEqual(layer["name"], dictionaries["storey"][key[1]])
                meta = metadata.get(key, {})
                for target, column, kind in (("density", "density_cd", "density"), ("mixture", "mixture_cd", "mixture")):
                    self.assertEqual(layer[target], dictionaries[kind].get(meta.get(column, "").strip()))
                raw = meta.get("standdensity_index", "").strip()
                self.assertEqual(layer["stocking"], float(raw) if raw else None)
                numeric_coverage["stocking", "blank" if not raw else "zero" if float(raw) == 0 else "value"] += 1
                rows = sorted(species.get(key, []), key=lambda r: int(r["sp_rank_order_act"]))
                self.assertEqual(len(layer["species"]), len(rows))
                for row, item in zip(rows, layer["species"]):
                    species_count += 1
                    self.assertEqual(set(item), {"code", "name", "age", "share", "siteClass", "height", "diameter"})
                    self.assertEqual(item["code"], row["species_cd"].strip())
                    self.assertEqual(item["name"], dictionaries["species"][item["code"]])
                    share = row["part_cd_act"].strip()
                    shares[share] += 1
                    self.assertEqual(item["share"], dictionaries["part"].get(share))
                    self.assertEqual(item["siteClass"], row["site_class_cd"].strip() or None)
                    for target, column in (("age", "species_age"), ("height", "height"), ("diameter", "bhd")):
                        raw = row[column].strip()
                        self.assertEqual(item[target], float(raw) if raw else None)
                        numeric_coverage[target, "blank" if not raw else "zero" if float(raw) == 0 else "value"] += 1
        ids = {f["properties"]["id"] for f in self.layers["bdl"]["features"]}
        self.assertEqual(layer_count, sum(k[0] in ids for k in metadata.keys() | species.keys()))
        self.assertEqual(species_count, sum(len(v) for k, v in species.items() if k[0] in ids))
        self.assertEqual(layer_count, self.manifest["validation"]["inventory"]["publishedLayers"])
        self.assertEqual(species_count, self.manifest["validation"]["inventory"]["publishedSpecies"])
        self.assertGreater(shares["MJS"], 0)
        self.assertGreater(shares["PJD"], 0)
        self.assertGreater(shares[""], 0)
        for field in ("stocking", "age", "height", "diameter"):
            self.assertGreater(numeric_coverage[field, "blank"], 0)
        # This source edition has no literal numeric zeros; exercise those below.
        self.assertEqual(sum(numeric_coverage[field, "zero"] for field in ("stocking", "age", "height", "diameter")), 0)

    def test_inventory_preserves_selection_and_existing_payload(self):
        self.assertEqual(self.manifest["version"], "sha256:2e99b0b7e79dad47b7b2dcbe5ff69ab7c456ddd10cbcda534e0bf387ecd0b6c0")
        original = json.loads(json.dumps(self.layers["bdl"]))
        for feature in original["features"]:
            del feature["properties"]["inventory"]
        payload = json.dumps(original, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.assertEqual(hashlib.sha256(payload.encode()).hexdigest(), "68becf73894699bec8dab2b6b55f52e67e5ddae8c6b52cc09913d94153aeeede")
        for name, expected in (("rozplan", "eb65fb9ee3355c174e4e753c66a6f42e42810ee49074f06786ae77a15b9a1e19"),
                               ("boundary", "14bdca4356ca46ace250bef9ccd057162bca944564c38ac648c4bd2e97add9c1")):
            self.assertEqual(hashlib.sha256((pipeline.OUTPUT / f"{name}.geojson").read_bytes()).hexdigest(), expected)
            self.assertTrue(all("inventory" not in f["properties"] for f in self.layers[name]["features"]))

    def test_inventory_provenance_includes_all_consumed_sources(self):
        expected = {pipeline.BDL / n for n in (
            "f_subarea.txt", "f_arod_storey.txt", "f_storey_species.txt", "f_storey_dic.txt",
            "f_tree_species_dic.txt", "f_part_dic.txt", "f_density_dic.txt", "f_mixture_dic.txt")}
        expected.update(pipeline.SOURCES["bdl"].with_suffix(e) for e in (".shp", ".shx", ".dbf", ".prj", ".cpg"))
        self.assertEqual(set(pipeline.inventory_source_files()), expected)
        self.assertEqual(self.manifest["inventorySources"], [p.relative_to(ROOT).as_posix() for p in sorted(expected)])
        self.assertNotEqual(self.manifest["version"], self.manifest["inventoryVersion"])

    def test_inventory_missing_metadata_and_ambiguous_sources(self):
        original = pipeline.table_rows

        def missing(filename):
            rows = list(original(filename))
            return rows[1:] if filename == "f_arod_storey.txt" else rows

        with patch.object(pipeline, "table_rows", side_effect=missing):
            inventory, validation = pipeline.build_inventory()
        layer = next(l for l in inventory["108024116"]["layers"] if l["code"] == "DRZEW")
        self.assertTrue(layer["species"])
        self.assertEqual((layer["density"], layer["mixture"], layer["stocking"]), (None, None, None))
        self.assertEqual(inventory["108024116"]["layers"][-1]["code"], "DRZEW")
        self.assertEqual(validation["layersWithoutMetadata"], [{"id": "108024116", "code": "DRZEW"}])
        for filename, message in (("f_subarea.txt", "Ambiguous inventory subarea"),
                                  ("f_arod_storey.txt", "Ambiguous inventory storey"),
                                  ("f_storey_dic.txt", "Ambiguous/empty inventory dictionary")):
            def duplicate(name):
                rows = list(original(name))
                if name == filename:
                    extra = dict(rows[0])
                    if name == "f_storey_dic.txt":
                        extra["storey_name"] = "conflicting label"
                    rows.append(extra)
                return rows
            with patch.object(pipeline, "table_rows", side_effect=duplicate):
                with self.assertRaisesRegex(ValueError, message):
                    pipeline.build_inventory()

    def test_inventory_blank_zero_invalid_numbers_and_unknown_codes(self):
        self.assertIsNone(pipeline.inventory_number("  "))
        self.assertEqual(pipeline.inventory_number("0"), 0)
        for value in ("nan", "inf", "unknown"):
            with self.assertRaises(ValueError):
                pipeline.inventory_number(value)
        original = pipeline.table_rows

        def unknown(filename):
            rows = list(original(filename))
            if filename == "f_arod_storey.txt":
                rows[0]["density_cd"] = "UNKNOWN"
            return rows

        with patch.object(pipeline, "table_rows", side_effect=unknown):
            with self.assertRaisesRegex(ValueError, "Unknown inventory density code"):
                pipeline.build_inventory()

        def zero_numbers(filename):
            rows = list(original(filename))
            fields = {"f_subarea.txt": ("rotation_age",),
                      "f_arod_storey.txt": ("standdensity_index",),
                      "f_storey_species.txt": ("species_age", "height", "bhd")}.get(filename, ())
            for row in rows:
                for field in fields:
                    if row[field].strip():
                        row[field] = "0"
            return rows

        with patch.object(pipeline, "table_rows", side_effect=zero_numbers):
            inventory, _ = pipeline.build_inventory()
        items = list(inventory.values())
        layers = [l for i in items for l in i["layers"]]
        species = [s for l in layers for s in l["species"]]
        for records, fields in ((items, ("rotationAge",)), (layers, ("stocking",)),
                                (species, ("age", "height", "diameter"))):
            for field in fields:
                self.assertEqual({r[field] for r in records}, {0, None})


if __name__ == "__main__":
    unittest.main()
