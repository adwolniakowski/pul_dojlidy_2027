#!/usr/bin/env python3
"""Prepare local Dojlidy layers. Run from any directory; sources are read-only.

INFO-01, INFO-02, PLAN-01 to PLAN-03, SEL-03 to SEL-05: data, not UI acceptance.
No spatial/address joins, simplification, coordinate rounding, or record merging.
"""

import csv
import hashlib
import json
import math
from pathlib import Path

import pyproj
import shapefile
import shapely
from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.geometry.polygon import orient
from shapely.ops import transform
from shapely.validation import explain_validity


ROOT = Path(__file__).resolve().parents[1]
BDL = ROOT / "data/BDL_01_08_DOJLIDY_2026"
OUTPUT = ROOT / "web/layers"
SOURCES = {
    "bdl": BDL / "G_SUBAREA",
    "rozplan": ROOT / "data/ROZPLAN zip/ROZPLAN/ROZPLAN",
    "boundary": BDL / "G_INSPECTORATE",
}
COUNTS = {"bdl": 6105, "rozplan": 826, "boundary": 1}
TYPES = ["IA", "IB", "IIA", "IIAU", "IIB", "IIBU", "IIIA", "IIIAU",
         "IIIB", "IIIBU", "IVD", "IVDU", "V"]
# Labels describe only verified dictionary-backed attributes, not stand composition.
DETAILS = {
    "area_type": ("Rodzaj powierzchni", "f_area_type_dic.txt", "area_type_cd", "area_type_name"),
    "site_type": ("Typ siedliskowy lasu", "f_site_type_dic.txt", "site_type_cd", "site_type_name"),
    "forest_fun": ("Funkcja lasu", "f_forest_func_dic.txt", "forest_func_cd", "forest_func_name"),
    "prot_categ": ("Kategoria ochronnosci", "f_prot_categ_dic.txt", "prot_category_cd", "prot_category_name"),
}
TO_WGS84 = Transformer.from_crs(2180, 4326, always_xy=True)


def source_files():
    return sorted({p.with_suffix(ext) for p in SOURCES.values()
                   for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg")} |
                  {BDL / spec[1] for spec in DETAILS.values()})


def fingerprint():
    """Hash consumed source bytes and relative names, never mtimes or absolute paths."""
    digest = hashlib.sha256()
    for path in source_files():
        name = path.relative_to(ROOT).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def dictionaries():
    result = {}
    for field, (_, filename, code, name) in DETAILS.items():
        values = {}
        with (BDL / filename).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                key, value = row[code].strip(), row[name].strip()
                if key in values and values[key] != value:
                    raise ValueError(f"Ambiguous dictionary {filename}: {key}")
                values[key] = value
        result[field] = values
    return result


def polygon_stats(geometry):
    polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    return {
        "polygons": len(polygons),
        "holes": sum(len(p.interiors) for p in polygons),
        "vertices": sum(len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors)
                        for p in polygons),
    }


def prepare_geometry(source_shape, feature_id, repairs):
    if source_shape.shapeType != shapefile.POLYGON:
        raise ValueError(f"{feature_id}: expected 2D polygon, got {source_shape.shapeType}")
    geometry = shape(source_shape.__geo_interface__)
    if geometry.is_empty or geometry.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"{feature_id}: empty or nonpolygon geometry")
    before = polygon_stats(geometry)
    # Every Shapefile part must survive as either a shell or a hole.
    if before["polygons"] + before["holes"] != len(source_shape.parts) or before["vertices"] != len(source_shape.points):
        raise ValueError(f"{feature_id}: Shapefile ring/vertex loss")
    if not geometry.is_valid:
        repaired = make_valid(geometry)
        if (repaired.is_empty or repaired.geom_type not in ("Polygon", "MultiPolygon")
                or not repaired.is_valid):
            raise ValueError(f"{feature_id}: repair would discard nonpolygon components")
        delta = repaired.area - geometry.area
        if not math.isclose(repaired.area, geometry.area, rel_tol=1e-12, abs_tol=1e-6):
            raise ValueError(f"{feature_id}: repair changes polygon area: {delta}")
        repairs.append({"id": feature_id, "reason": explain_validity(geometry),
                        "method": "shapely.make_valid", "areaDeltaM2": delta,
                        "before": before, "after": polygon_stats(repaired)})
        geometry = repaired
    # EPSG:2180 uses metres; Shapely area includes all parts and subtracts holes.
    area_ha = geometry.area / 10000
    projected = transform(TO_WGS84.transform, geometry)
    if not projected.is_valid or projected.is_empty:
        raise ValueError(f"{feature_id}: invalid projected geometry: {explain_validity(projected)}")
    xmin, ymin, xmax, ymax = projected.bounds
    if not (22 < xmin <= xmax < 24 and 52 < ymin <= ymax < 54):
        raise ValueError(f"{feature_id}: outside expected Dojlidy WGS84 bounds")
    if projected.geom_type == "Polygon":
        projected = orient(projected, sign=1.0)
    else:
        projected = shapely.geometry.MultiPolygon([orient(p, sign=1.0) for p in projected.geoms])
    return projected, area_ha


def text(value):
    return "" if value is None else str(value)


def build_layers():
    lookup = dictionaries()
    layers, validation = {}, {}
    for layer, path in SOURCES.items():
        # Legacy ESRI WKT lacks EPSG authority/axis metadata (PROJ confidence 60).
        if CRS.from_wkt(path.with_suffix(".prj").read_text()).to_epsg(min_confidence=60) != 2180:
            raise ValueError(f"Unexpected CRS: {path}")
        encoding = path.with_suffix(".cpg").read_text().strip()
        if encoding.lower().replace("-", "") not in ("utf8", "65001"):
            raise ValueError(f"Unexpected encoding: {encoding}")
        repairs, features, ids, unknown = [], [], set(), {}
        with shapefile.Reader(str(path), encoding="utf-8") as reader:
            for sr in reader.iterShapeRecords():
                row = sr.record.as_dict()
                identifier = text(row["OBJECTID_1" if layer == "rozplan" else "a_i_num"])
                address = row["ADR_FOR" if layer == "rozplan" else "adr_for"]
                if not identifier or identifier in ids or not address:
                    raise ValueError(f"{layer}: duplicate/missing ID or address {identifier}")
                ids.add(identifier)
                properties = {"id": identifier, "address": address,
                              "source": "rozplan" if layer == "rozplan" else "bdl"}
                if layer == "rozplan":
                    percent = float(row["proc"])
                    if not math.isfinite(percent) or not 0 <= percent <= 100:
                        raise ValueError(f"{identifier}: invalid percentage")
                    properties.update(type=row["rebnia_1"], percent=percent,
                                      original=row["N_REBNIA"], compartment=text(row["NR_DZIAL"]),
                                      strip=text(row["NR_PASA"]), planYear=text(row["ROK"]))
                else:
                    if row["a_year"] != 2026:
                        raise ValueError(f"{identifier}: unexpected BDL year")
                    properties["year"] = 2026
                    properties["details"] = {}
                    if layer == "bdl":
                        for field, (label, *_) in DETAILS.items():
                            code = text(row[field]).strip()
                            value = "Brak danych"
                            if code:
                                value = lookup[field].get(code, f"Nieznany kod: {code}")
                                if code not in lookup[field]:
                                    unknown.setdefault(field, set()).add(code)
                            properties["details"][label] = value
                    else:
                        properties["details"]["Nazwa nadlesnictwa"] = row["i_name"]
                geometry, properties["areaHa"] = prepare_geometry(sr.shape, identifier, repairs)
                features.append({"type": "Feature", "bbox": list(geometry.bounds),
                                 "properties": properties, "geometry": mapping(geometry)})
            if len(features) != reader.numRecords or len(features) != COUNTS[layer]:
                raise ValueError(f"{layer}: record loss or unexpected count: {len(features)}")
        layers[layer] = {"type": "FeatureCollection", "features": features}
        validation[layer] = {"sourceRecords": len(features), "outputRecords": len(features),
                             "uniqueIds": len(ids), "validOutputGeometries": len(features),
                             "sourceInvalidGeometries": len(repairs), "repairs": repairs,
                             "unknownDictionaryCodes": {k: sorted(v) for k, v in sorted(unknown.items())}}
    types = sorted({f["properties"]["type"] for f in layers["rozplan"]["features"]})
    if types != TYPES:
        raise ValueError(f"Unexpected cutting symbols: {types}")
    manifest = {"version": fingerprint(), "counts": COUNTS, "types": types,
                "validation": {"sourceCRS": "EPSG:2180", "outputCRS": "EPSG:4326",
                               "axisOrder": "longitude,latitude", "layers": validation,
                               "dependencies": {"pyproj": pyproj.__version__, "shapely": shapely.__version__,
                                                "pyshp": shapefile.__version__, "PROJ": pyproj.proj_version_str},
                               "notes": ["No joins, simplification, rounding, or record merging.",
                                         "planYear is raw ROK; interpretation unconfirmed, not a calendar year.",
                                         "BDL 2026 is not confirmed equivalent to PUL 2027-2037."]}}
    return layers, manifest


def main():
    layers, manifest = build_layers()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, content in {**layers, "manifest": manifest}.items():
        path = OUTPUT / (name + (".json" if name == "manifest" else ".geojson"))
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(content, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            handle.write("\n")
        print(f"{path.relative_to(ROOT)}: {path.stat().st_size:,} bytes")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
