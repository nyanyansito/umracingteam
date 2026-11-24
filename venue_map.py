"""Competition maps. Every background uses the same measured fixes."""
import json
import math
from pathlib import Path

import plotly.graph_objects as go
from dash import Patch, no_update

ROOT = Path(__file__).resolve().parent
VENUE = (101.57407530038864, 3.370175403636311)
with (ROOT / "assets/venue-roads.geojson").open() as roads_file:
    ROADS = json.load(roads_file)
COLORS = {"brake": "#ef4444", "throttle": "#22c55e", "coast": "#b5c0cc", "unknown": "#6485a5"}
LABELS = {"brake": "Braking", "throttle": "Throttle", "coast": "Coasting", "unknown": "Inputs unavailable"}


def load_course(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if data.get("type") == "FeatureCollection" and len(data.get("features", [])) == 1:
        data = data["features"][0]
    if data.get("type") == "Feature":
        data = data["geometry"]
    if data.get("type") != "LineString":
        raise ValueError("Course must contain one GeoJSON LineString.")
    points = data["coordinates"]
    if len(points) < 2 or any(len(p) < 2 or not -180 <= p[0] <= 180 or not -85 <= p[1] <= 85 for p in points):
        raise ValueError("Invalid course coordinates.")
    return [(lon, lat) for lon, lat, *_ in points]


def bounds(points):
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def activity(t, b):
    if t is None or b is None:
        return "unknown"
    if b > 5:
        return "brake"
    return "throttle" if t > 5 else "coast"


def live_traces(points, fresh):
    lines = {k: ([], [], []) for k in COLORS}
    for previous, current in zip(points, points[1:]):
        if current[4]:
            continue
        mode = activity(previous[2], previous[3])
        lon, lat, times = lines[mode]
        times.extend((previous[6], current[6], None))
        lon.extend((previous[0], current[0], None))
        lat.extend((previous[1], current[1], None))
    traces = [go.Scattermap(lon=xs, lat=ys, mode="lines", line=dict(color=COLORS[k], width=4),
                            customdata=times, hovertemplate="%{customdata:.3f} s<extra>%{fullData.name}</extra>",
                            name=LABELS[k]) for k, (xs, ys, times) in lines.items()]
    last = points[-1] if points else None
    detail = []
    if last:
        for key, value in zip(("Throttle", "Brake"), last[2:4]):
            detail.append(f"{key} {value:.1f}%" if value is not None else f"{key} unavailable")
    traces.append(go.Scattermap(lon=[last[0]] if last else [], lat=[last[1]] if last else [],
                                mode="markers", marker=dict(size=15, color="#34d399" if fresh else "#94a3b8"),
                                text=[" · ".join(detail)] if last else [], hoverinfo="text",
                                customdata=[last[6]] if last else [],
                                name="Car" if fresh else "Last position"))
    return traces


def build_map(mode, points, fresh, course, follow, previous, generation):
    # MapLibre draws the offline streets from a local GeoJSON source. No external
    # sprite, glyph or tile URLs are required by either offline style.
    lon, lat = points[-1][:2] if points else VENUE
    fixed_course = mode == "track" and bool(course)
    if fixed_course:
        west, south, east, north = bounds(course)
        centre = [(west + east) / 2, (north + south) / 2]
        span = max((east-west)*1.3, (north-south)*1.8, .0004)
        zoom = max(1, min(19, math.log2(270/span)))
    else:
        centre, zoom = [lon, lat], 16
    position = dict(lon=centre[0], lat=centre[1])
    version = [mode, generation, follow, bool(points)]
    latest = [points[-1][5], fresh, len(points)] if points else None
    first = points[0][5] if points else None
    meta = {"view": version, "latest": latest, "first": first}
    if previous == meta:
        return no_update, no_update
    if previous and previous.get("view") == version:
        patch = Patch()
        tail = points
        append = False
        if first == previous.get("first") and previous.get("latest"):
            last_tick = previous["latest"][0]
            for n in range(len(points) - 1, -1, -1):
                if points[n][5] == last_tick:
                    tail, append = points[n:], True
                    break
        traces = live_traces(tail, fresh)
        for i, trace in enumerate(traces):
            for field, value in trace.to_plotly_json().items():
                if field in ("lon", "lat", "customdata") and i < len(COLORS) and append:
                    if value:
                        patch["data"][i][field].extend(value)
                elif field in ("lon", "lat", "customdata", "name", "marker", "text"):
                    patch["data"][i][field] = value
        if follow and not fixed_course:
            patch["layout"]["map"]["center"] = position
            patch["layout"]["map"]["uirevision"] = str(latest)
        return patch, meta
    traces = live_traces(points, fresh)
    style = f"/assets/{mode}-map-style.json"
    if mode == "offline":
        named = [(f["geometry"]["coordinates"], f["properties"]["name"]) for f in ROADS["features"]
                 if f["properties"]["name"] != f["properties"]["name"].lower()]
        centres = [road[len(road)//2] for road, _ in named]
        traces.append(go.Scattermap(lon=[p[0] for p in centres], lat=[p[1] for p in centres],
                                    mode="markers", marker=dict(size=8, color="#8194aa", opacity=.3),
                                    text=[name for _, name in named], hoverinfo="text",
                                    showlegend=False, name="Street name"))
    if course:
        traces.append(go.Scattermap(lon=[p[0] for p in course], lat=[p[1] for p in course],
                                    mode="lines", line=dict(width=2, color="#e9b640"),
                                    name="Course outline", hoverinfo="skip"))
    fig = go.Figure(traces)
    fig.update_layout(map=dict(style=style, center=position, zoom=zoom),
                      uirevision=str(version), template="plotly_dark", height=480,
                      paper_bgcolor="#112239", margin=dict(t=4, l=0, r=0, b=0),
                      legend=dict(orientation="h"))
    return fig, meta


def area_status(mode, points, course):
    if mode == "track" and not course:
        return "Course alignment pending · showing measured GPS trail"
    if not points:
        return ""
    lon, lat = points[-1][:2]
    if mode == "offline":
        w, s, e, n = ROADS["bbox"]
        if not (w <= lon <= e and s <= lat <= n):
            return "Outside saved street coverage"
    if mode == "track" and course:
        w, s, e, n = bounds(course)
        if not (w-.0003 <= lon <= e+.0003 and s-.0003 <= lat <= n+.0003):
            return "Outside course area"
    return ""
