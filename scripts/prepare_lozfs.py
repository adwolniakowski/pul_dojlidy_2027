"""Validate and copy the complete LOZFS-01 reference layers without rewriting JSON."""

import hashlib
import json
import math
from pathlib import Path
import sys

from shapely.geometry import shape
from shapely.validation import explain_validity


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "web/layers"
LAYERS = (
    (ROOT / "glosowanie_maj_2026-main/propozycje.geojson", "lozfs-proposed.geojson", 34),
    (ROOT / "glosowanie_maj_2026-main/ustalone.geojson", "lozfs-established.geojson", 8),
)
CRS = {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}


def reject_constant(value):
    raise ValueError(f"Non-JSON numeric constant: {value}")


def validate(raw, count):
    collection = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise ValueError("Expected a FeatureCollection")
    if collection.get("crs") != CRS:
        raise ValueError("Expected WGS84 longitude/latitude CRS84")
    features = collection.get("features")
    if not isinstance(features, list) or len(features) != count:
        raise ValueError(f"Expected exactly {count} features")
    invalid = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError(f"Feature {index}: expected a Feature")
        properties = feature.get("properties")
        if not isinstance(properties, dict) or not isinstance(properties.get("name"), str):
            raise ValueError(f"Feature {index}: expected named properties")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError(f"Feature {index}: expected Polygon or MultiPolygon")
        coordinates = geometry.get("coordinates")
        polygons = [coordinates] if geometry["type"] == "Polygon" else coordinates
        if not isinstance(polygons, list) or not polygons:
            raise ValueError(f"Feature {index}: expected nonempty polygons")
        for polygon in polygons:
            if not isinstance(polygon, list) or not polygon:
                raise ValueError(f"Feature {index}: expected polygon rings")
            for ring in polygon:
                if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
                    raise ValueError(f"Feature {index}: expected closed rings of at least four positions")
                for position in ring:
                    if (not isinstance(position, list) or len(position) not in (2, 3)
                            or any(type(n) not in (int, float) or not math.isfinite(n) for n in position)
                            or not -180 <= position[0] <= 180 or not -90 <= position[1] <= 90):
                        raise ValueError(f"Feature {index}: expected finite WGS84 coordinates in domain")
        geometry_shape = shape(geometry)
        if not geometry_shape.is_valid:
            invalid.append(f"feature {index} ({properties['name']!r}): {explain_validity(geometry_shape)}")
    return invalid


def prepare(output=OUTPUT):
    # Validate both complete sources before writing either output; never repair topology.
    copies = []
    for source, filename, count in LAYERS:
        raw = source.read_bytes()
        invalid = validate(raw, count)
        for issue in invalid:
            print(f"WARNING {source.name}: {issue}; retained unchanged", file=sys.stderr)
        copies.append((filename, count, raw, len(invalid)))
    for filename, count, raw, invalid_count in copies:
        (output / filename).write_bytes(raw)
        print(f"{filename}: {count} features, {len(raw)} bytes, "
              f"{count - invalid_count} valid, {invalid_count} invalid, "
              f"sha256={hashlib.sha256(raw).hexdigest()}")


if __name__ == "__main__":
    prepare()
