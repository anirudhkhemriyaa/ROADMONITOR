"""City road network + POIs with stable segment ids (roadnet.py).

Primary path: OSMnx (drive network + amenity POIs), cached to GeoJSON so
the network loads offline afterwards. OSMnx/geopandas import lazily - on
hosts without the geo stack, a small clearly-labelled *sample* grid centred
on Gwalior keeps the pipeline demo- and test-ready (never sent to prod maps
as real data; source='sample').

User-uploaded road networks: a GeoJSON FeatureCollection of LineStrings
(+ optional Point POIs) is accepted, both via file path and via the
POST /api/v1/roads/network endpoint.

Stable segment_id: 'seg_' + sha1(source | origin id | endpoints)[:12], so
re-loads with unchanged OSM data keep ids stable.
"""
import hashlib
import json
import os
import urllib.request
from pathlib import Path

DEFAULT_PLACE = "Gwalior, Madhya Pradesh, India"
GWALIOR_CENTER = (26.2183, 78.1828)      # used only by the sample grid

# POI types the plan asked for -> OSM amenity/practice tags
POI_OSM_TAGS = {
    "school": ("amenity", "school"),
    "hospital": ("amenity", "hospital"),
    "college": ("amenity", "college"),
    "university": ("amenity", "university"),
    "dormitory": ("amenity", "dormitory"),
    "marketplace": ("amenity", "marketplace"),
    "bus_station": ("amenity", "bus_station"),
}
POI_TYPES = list(POI_OSM_TAGS)


def _seg_id(source: str, origin: str, u, v) -> str:
    key = f"{source}|{origin}|{round(u[0], 7)},{round(u[1], 7)}|" \
          f"{round(v[0], 7)},{round(v[1], 7)}"
    return "seg_" + hashlib.sha1(key.encode()).hexdigest()[:12]


def polyline_length_m(coords: list) -> float:
    from ..geo import haversine_m
    return sum(haversine_m(coords[i][0], coords[i][1],
                           coords[i + 1][0], coords[i + 1][1])
               for i in range(len(coords) - 1))


def _classify_highway(tag) -> str:
    """Coarse road class for weight lookup (config priority.road_class)."""
    if isinstance(tag, list):
        tag = tag[0]
    return tag if tag else "unclassified"


# ---- OSMnx path (lazy) --------------------------------------------------------

def load_osmnx(place: str = DEFAULT_PLACE, cache_dir: str = None,
               use_cache: bool = True) -> dict:
    """Downloads (or loads from cache) the drive network + POIs of `place`
    via OSMnx. Raises ImportError with guidance when osmnx is not installed.
    Output: {'segments': [...], 'pois': [...], 'source': 'osm'} with segment
    dicts {segment_id, name, road_class, length_m, coords, source}."""
    try:
        import osmnx as ox
    except ImportError as e:                     # pragma: no cover
        raise ImportError(
            "osmnx is not installed. pip install osmnx geopandas networkx "
            "(Python 3.11 hosts) - or use the sample/upload network path "
            "until then.") from e
    cache = Path(cache_dir or os.environ.get("ROADMONITOR_CACHE", "./data/cache"))
    cache.mkdir(parents=True, exist_ok=True)
    edges_f = cache / f"{_slug(place)}_edges.geojson"
    pois_f = cache / f"{_slug(place)}_pois.geojson"
    if use_cache and edges_f.exists():
        return load_geojson_file(edges_f, source="osm")

    graph = ox.graph_from_place(place, network_type="drive")
    segments = []
    for u, v, key, data in graph.edges(keys=True, data=True):
        seg_coords = _node_coords(graph, u) + _mid_coords(data) + _node_coords(graph, v)
        origin = f"way{data.get('osmid')}:{u}/{v}/{key}"
        segments.append({
            "segment_id": _seg_id("osm", origin, _node_coords(graph, u)[0],
                                  _node_coords(graph, v)[0]),
            "name": data.get("name") if isinstance(data.get("name"), str)
                    else (data.get("name") or [""])[0],
            "road_class": _classify_highway(data.get("highway")),
            "length_m": polyline_length_m(seg_coords),
            "coords": seg_coords,
            "source": "osm",
        })
    pois = []
    for ptype, (k, v) in POI_OSM_TAGS.items():
        try:
            gdf = ox.features_from_place(place, {k: v})
        except Exception:
            continue
        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None:
                continue
            pt = (geom.centroid if geom.geom_type != "Point" else geom)
            pois.append({"poi_id": f"poi_{abs(hash((ptype, round(pt.y, 6), round(pt.x, 6)))) & 0xffff:08x}",
                         "poi_type": ptype, "lat": pt.y, "lon": pt.x,
                         "name": row.get("name"), "source": "osm"})
    out = {"segments": segments, "pois": pois, "source": "osm"}
    if use_cache:
        _write_geojson(edges_f, segments)
        _write_geojson(pois_f, pois)
    return out


def _node_coords(graph, n) -> list:
    d = graph.nodes[n]
    return [(d["y"], d["x"])]


def _mid_coords(data) -> list:
    geom = data.get("geometry")
    if geom is None:
        return []
    cs = list(geom.coords)
    return [(c[1], c[0]) for c in cs[1:-1]]


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s.lower())[:60]


# ---- GeoJSON cache / upload path ----------------------------------------------

def load_geojson_file(path, source: str = None) -> dict:
    """GeoJSON FeatureCollection -> {'segments': [...], 'pois': [...]}.
    LineString/MultiLineString features are segments; Point features with a
    recognised poi_type property are POIs. Works for OSMnx-cached files and
    user uploads alike."""
    doc = json.loads(Path(path).read_text(encoding="utf-8")
                     if not hasattr(path, "read") else path.read())
    feats = doc.get("features", [])
    segments, pois = [], []
    for f in feats:
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        gtype = geom.get("type")
        if gtype in ("LineString", "MultiLineString"):
            lines = ([geom["coordinates"]] if gtype == "LineString"
                     else geom["coordinates"])
            for line in lines:
                coords = [(c[1], c[0]) for c in line]   # GeoJSON lon,lat -> lat,lon
                u, v = coords[0], coords[-1]
                segments.append({
                    "segment_id": (props.get("segment_id")
                                   or _seg_id(source or props.get("source", "upload"),
                                              props.get("id", ""), u, v)),
                    "name": props.get("name"), "road_class":
                        _classify_highway(props.get("highway")
                                          or props.get("road_class")),
                    "length_m": polyline_length_m(coords),
                    "coords": coords,
                    "source": source or props.get("source", "upload"),
                })
        elif gtype == "Point":
            ptype = props.get("poi_type") or props.get("amenity")
            if ptype in POI_TYPES:
                lon, lat = geom["coordinates"][:2]
                pois.append({"poi_id": props.get("poi_id")
                             or f"poi_up_{len(pois)}", "poi_type": ptype,
                             "lat": lat, "lon": lon, "name": props.get("name"),
                             "source": source or props.get("source", "upload")})
    return {"segments": segments, "pois": pois, "source":
            source or "upload"}


def _write_geojson(path: Path, features: list) -> None:
    feats = []
    for it in features:
        if "coords" in it:                       # segment
            feats.append({"type": "Feature",
                          "geometry": {"type": "LineString",
                                       "coordinates": [[c[1], c[0]]
                                                       for c in it["coords"]]},
                          "properties": {k: v for k, v in it.items()
                                         if k != "coords"}})
        else:                                    # poi
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point",
                                       "coordinates": [it["lon"], it["lat"]]},
                          "properties": it})
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}),
                    encoding="utf-8")


# ---- sample grid (offline demo/testing fallback) -------------------------------

def load_sample_network(cx: float = None, cy: float = None) -> dict:
    """A small regular street grid around Gwalior centre with named streets
    and every POI type anchored nearby. Clearly source='sample': for demos
    and tests only, never for geographic claims."""
    cx, cy = GWALIOR_CENTER[1], GWALIOR_CENTER[0]      # (lon, lat)
    step = 0.0015                                # ~167 m cells
    n = 5                                        # 5x5 nodes
    segments = []
    streets = ["Deendayal Road", "Jayendra Ganj Road", "Sarafa Bazaar Line",
               "Tansen Road", "Hospital Street"]
    for row in range(n):
        for col in range(n):
            lat = cy + row * step
            lon = cx + col * step
            for (la1, lo1, la2, lo2) in ((lat, lon, lat, lon + step),   # east
                                         (lat, lon, lat + step, lon)):  # north
                if la2 > cy + (n - 1) * step + 1e-12:
                    continue
                if lo2 > cx + (n - 1) * step + 1e-12:
                    continue
                cl = _classify_highway("secondary" if row % 2 == 0 else "residential")
                segments.append({
                    "segment_id": _seg_id("sample", f"e{row}-{col}-{la1}-{lo1}",
                                          (la1, lo1), (la2, lo2)),
                    "name": streets[(row + col) % len(streets)],
                    "road_class": cl,
                    "length_m": polyline_length_m([(la1, lo1), (la2, lo2)]),
                    "coords": [(la1, lo1), (la2, lo2)],
                    "source": "sample",
                })
    pois = []
    anchors = {"school": (n // 2, n // 2), "hospital": (1, 2),
               "college": (3, 1), "university": (4, 4), "dormitory": (2, 3),
               "marketplace": (3, 3), "bus_station": (0, 4)}
    for ptype, (r, c) in anchors.items():
        pois.append({"poi_id": f"poi_s_{ptype}", "poi_type": ptype,
                     "lat": cy + r * step + step / 2,
                     "lon": cx + c * step + step / 2,
                     "name": ptype.title() + " (sample)", "source": "sample"})
    return {"segments": segments, "pois": pois, "source": "sample"}


# ---- persistence ---------------------------------------------------------------

def save_to_store(store, net: dict, source: str = None) -> dict:
    """Upserts segments + POIs. Existing rows with the same ids are replaced
    (resetting segment_stats ties to the network version in aggregate)."""
    from .schema import ensure_tables
    ensure_tables(store)
    with store.lock:
        for s in net["segments"]:
            store.conn.execute(
                "INSERT OR REPLACE INTO net_segments (segment_id, geom, name,"
                " road_class, length_m, source) VALUES (?,?,?,?,?,?)",
                (s["segment_id"], _wkt(s["coords"]), s.get("name"),
                 s.get("road_class"), s.get("length_m"),
                 source or s.get("source", "osm")))
        for p in net["pois"]:
            store.conn.execute(
                "INSERT OR REPLACE INTO net_pois (poi_id, poi_type, lat, lon,"
                " name, source) VALUES (?,?,?,?,?,?)",
                (p["poi_id"], p["poi_type"], p["lat"], p["lon"], p.get("name"),
                 source or p.get("source", "osm")))
        store.conn.commit()
    return {"segments": len(net["segments"]), "pois": len(net["pois"])}


def _wkt(coords: list) -> str:
    pts = ", ".join(f"{lon:.7f} {lat:.7f}" for lat, lon in coords)
    return f"LINESTRING({pts})"


def load_from_store(store) -> dict:
    segs = []
    for r in store.conn.execute("SELECT * FROM net_segments").fetchall():
        d = dict(r)
        d["coords"] = _wkt_points(d.pop("geom"))
        segs.append(d)
    pois = [dict(r) for r in
            store.conn.execute("SELECT * FROM net_pois").fetchall()]
    return {"segments": segs, "pois": pois,
            "source": segs[0]["source"] if segs else "none"}


def _wkt_points(wkt: str) -> list:
    body = wkt[wkt.index("(") + 1:wkt.rindex(")")]
    pts = [p.split() for p in body.split(", ")]
    return [(float(la), float(lo)) for lo, la in pts]


def load_network(store, mode: str = "auto", place: str = DEFAULT_PLACE) -> dict:
    """mode: 'sample' | 'upload:<path>' | 'osmnx' | 'auto'.
    auto: store cache -> sample upload file if present -> OSMnx -> sample."""
    if mode == "sample":
        net = load_sample_network()
    elif mode.startswith("upload:"):
        net = load_geojson_file(mode.split(":", 1)[1])
    elif mode == "osmnx":
        net = load_osmnx(place)
    else:                                          # auto
        cached = Path(os.environ.get("ROADMONITOR_CACHE", "./data/cache"))
        edge_files = sorted(cached.glob("*_edges.geojson")) if cached.exists() else []
        if store.conn.execute(
                "SELECT COUNT(*) FROM net_segments").fetchone()[0] > 0:
            return load_from_store(store)
        if edge_files:
            net = load_geojson_file(edge_files[0], source="osm")
        else:
            try:
                net = load_osmnx(place)
            except ImportError:
                net = load_sample_network()
    save_to_store(store, net)
    return net
