"""DBSCAN over (lat, lon) points with a metre-scale epsilon (Phase C).

Pure Python - no sklearn - so it runs (and is unit-tested) everywhere.
Returns labels: -1 = noise, otherwise the cluster index.
"""
from ..geo import haversine_m

NOISE = -1


def cluster(points: list, eps_m: float, min_pts: int) -> list:
    """Density-based clustering; eps_m is the neighbourhood radius in metres."""
    n = len(points)
    labels = [NOISE] * n
    visited = [False] * n
    clusters = 0

    def neighbours(i: int) -> list:
        out = []
        for j in range(n):
            if j != i and haversine_m(points[i][0], points[i][1],
                                      points[j][0], points[j][1]) <= eps_m:
                out.append(j)
        return out

    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        nb = neighbours(i)
        if len(nb) + 1 < min_pts:
            continue                      # may still be claimed as a border point by a core cluster
        labels[i] = clusters
        queue = list(nb)
        while queue:
            j = queue.pop(0)
            if labels[j] == NOISE:
                labels[j] = clusters      # border point (not expanded)
            if visited[j]:
                continue
            visited[j] = True
            labels[j] = clusters
            nb2 = neighbours(j)
            if len(nb2) + 1 >= min_pts:
                queue.extend(k for k in nb2 if not visited[k])
        clusters += 1
    return labels
