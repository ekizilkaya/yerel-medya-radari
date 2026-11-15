"""İl sınırlarını GeoJSON'dan hafif SVG yollarına çevirir → site/assets/tr-map.json

Bir kez çalıştırılır; çıktı depoda tutulur (pano haritayı çizmek için dış kütüphane/CDN kullanmaz).
Kaynak: github.com/alpers/Turkey-Maps-GeoJSON (Apache-2.0) — tr-cities.json
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from radar.cities import NAME_OF  # noqa: E402
from radar.paths import SITE  # noqa: E402

URL = "https://raw.githubusercontent.com/alpers/Turkey-Maps-GeoJSON/master/tr-cities.json"
WIDTH = 1000.0
TOLERANCE = 0.9          # sadeleştirme eşiği (çıktı pikseli)
MIN_RING_AREA = 6.0      # bundan küçük adacıklar atılır (piksel²)


def project(lon: float, lat: float) -> tuple[float, float]:
    """Mercator; Türkiye ölçeğinde şekilleri bozmadan düzleme taşır."""
    return math.radians(lon), math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def simplify(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Ramer–Douglas–Peucker (yinelemeli)."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = points[a], points[b]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy)
        best, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            # Kapalı halkada ilk ve son nokta aynıdır; o durumda noktaya olan uzaklık kullanılır
            d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm if norm > 1e-9 else math.hypot(px - x1, py - y1)
            if d > best:
                best, idx = d, i
        if best > tol and idx > 0:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def area(ring: list[tuple[float, float]]) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2


def main() -> None:
    geo = httpx.get(URL, timeout=60, follow_redirects=True).json()
    feats = geo["features"]
    proj: dict[int, list[list[tuple[float, float]]]] = {}
    xs, ys = [], []
    for f in feats:
        plate = int(f["properties"]["number"])
        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        rings = []
        for poly in polys:
            ring = [project(lon, lat) for lon, lat in poly[0]]     # yalnız dış halka
            rings.append(ring)
            xs += [p[0] for p in ring]
            ys += [p[1] for p in ring]
        proj[plate] = rings
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    scale = WIDTH / (x1 - x0)
    height = round((y1 - y0) * scale, 1)

    provinces = []
    for plate in sorted(proj):
        parts, biggest, centroid = [], 0.0, (0.0, 0.0)
        for ring in proj[plate]:
            pts = [((x - x0) * scale, (y1 - y) * scale) for x, y in ring]
            a = area(pts)
            if a < MIN_RING_AREA and len(proj[plate]) > 1:
                continue
            pts = simplify(pts, TOLERANCE)
            if len(pts) < 4:
                continue
            if a > biggest:
                biggest = a
                centroid = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
            parts.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z")
        provinces.append({"plate": plate, "name": NAME_OF[plate], "d": "".join(parts),
                          "cx": round(centroid[0], 1), "cy": round(centroid[1], 1)})

    out = SITE / "assets" / "tr-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"width": WIDTH, "height": height, "provinces": provinces,
                               "source": "github.com/alpers/Turkey-Maps-GeoJSON (Apache-2.0)"},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(provinces)} il · {out.stat().st_size / 1024:.0f} KB · {WIDTH:.0f}×{height}")


if __name__ == "__main__":
    main()
