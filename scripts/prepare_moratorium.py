"""Prepare MOR-01 GeoJSON without modifying the source Shapefile."""

import json
from pathlib import Path

from pyproj import CRS
import shapefile
from shapely.geometry import shape
from shapely.validation import explain_validity


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/m_zwtl_wstrz_suba_01-08/m_zwtl_wstrz_suba_01-08/m_zwtl_wstrz_suba_01-08.shp"
OUTPUT = ROOT / "web/layers/moratorium.geojson"


def prepare():
    if not CRS.from_wkt(SOURCE.with_suffix(".prj").read_text()).equals(CRS.from_epsg(4326)):
        raise ValueError("Expected WGS84 source coordinates")
    features = []
    with shapefile.Reader(str(SOURCE), encoding="utf-8") as reader:
        if len(reader) != 398:
            raise ValueError("Expected 398 moratorium features")
        for index, record in enumerate(reader.iterShapeRecords()):
            geometry = record.shape.__geo_interface__
            polygon = shape(geometry)
            if (polygon.is_empty or polygon.geom_type not in ("Polygon", "MultiPolygon")
                    or not polygon.is_valid):
                raise ValueError(f"Feature {index}: invalid polygon: {explain_validity(polygon)}")
            west, south, east, north = polygon.bounds
            if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
                raise ValueError(f"Feature {index}: coordinates outside WGS84 domain")
            features.append({"type": "Feature", "properties": record.record.as_dict(),
                             "geometry": geometry})
    raw = json.dumps({"type": "FeatureCollection", "features": features},
                     ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    OUTPUT.write_text(raw, encoding="utf-8")
    print(f"{OUTPUT.name}: {len(features)} features, {len(raw.encode('utf-8'))} bytes")


if __name__ == "__main__":
    prepare()
