"""OSRM map matching adapter (Phase C).

Wraps the osrm-routed `/match` service and turns each input point into an
OSM node-pair "link key" (u, v) taken from the leg starting at that point.
Bins on the same stretch of the same way get the same key, which is the
road-link grouping used by aggregate.build_links when map matching is on.

The HTTP transport is injectable: tests pass a fake fetch(url)->dict, so the
whole thing is unit-tested without OSRM running (no Docker on the dev box).
"""
import json as _json
import urllib.request

# OSRM wants lon,lat order and returns 200 even on match failures,
# distinguishes results via the "code" field.


def osrm_match_url(base_url: str, coords: list, profile: str = "driving") -> str:
    """coords is a list of (lat, lon); OSRM matches along the given order."""
    param = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
    return (f"{base_url.rstrip('/')}/match/v1/{profile}/{param}"
            "?annotations=true&geometries=geojson&overview=false")


def http_fetch(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def match_points(fetch, url: str) -> list:
    """Runs the match request and returns per-input-point records in the
    same order as the coordinates were sent: {snap_lat, snap_lon, key}
    where key is the OSM node-pair "osm:{u}:{v}", or None if that point
    is unmatched (null tracepoint) or the whole request failed.
    """
    try:
        resp = fetch(url)
    except Exception:
        return []
    if resp.get("code") != "Ok" or not resp.get("matchings"):
        return []
    # per-matching: flat annotation nodes are pairs (segment_u, segment_v)
    def first_pair(matching, leg_idx):
        nodes = matching["legs"][leg_idx]["annotation"]["nodes"]
        return nodes[0], nodes[1]

    def last_pair(matching, leg_idx):
        nodes = matching["legs"][leg_idx]["annotation"]["nodes"]
        return nodes[-2], nodes[-1]

    def usable(matching):
        legs = matching.get("legs") or []
        return (legs and all("annotation" in leg and "nodes" in leg["annotation"]
                             and leg["annotation"]["nodes"] for leg in legs))

    out = []
    for tp in resp.get("tracepoints", []):
        if tp is None or "location" not in tp:
            out.append(None)
            continue
        m = resp["matchings"][tp["matchings_index"]]
        if not usable(m):
            out.append(None)
            continue
        n_legs = len(m["legs"])
        w = tp["waypoint_index"]
        if w < n_legs:
            u, v = first_pair(m, w)          # leg w starts at this waypoint
        else:
            u, v = last_pair(m, n_legs - 1)  # final waypoint: ends leg n-1
        out.append({
            "snap_lat": tp["location"][1],
            "snap_lon": tp["location"][0],
            "key": f"osm:{u}:{v}",
        })
    return out
