"""Distance-aligned lap plots and small Plotly append patches."""
from dash import Patch
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from telemetry_validation import FAST_FIELDS, is_number

CHANNELS = (("Throttle (%)", "#22c55e"), ("Brake (bar)", "#ef4444"),
            ("Steering (°)", "#fbbf24"), ("Speed (km/h)", "#38bdf8"))


def lap_input_figure(state, inspection=None, live_revision=0):
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=.04)
    distances = state.get("current_distances", [])
    for row, (field, (label, colour)) in enumerate(zip(FAST_FIELDS, CHANNELS), 1):
        reference = state.get("reference_channels", [])
        current = state.get("current_channels", [])
        fig.add_trace(go.Scatter(x=[p[3] for p in state["reference"]],
                                y=[p.get(field) for p in reference], mode="lines", connectgaps=False,
                                name=f"{label} · reference", line={"color": "#94a3b8", "dash": "dot", "width": 2}), row=row, col=1)
        fig.add_trace(go.Scatter(x=distances, y=[p.get(field) for p in current], mode="lines", connectgaps=False,
                                name=f"{label} · live", line={"color": colour, "width": 2}), row=row, col=1)
        fig.update_yaxes(title_text=label, row=row, col=1)
        if is_number(inspection):
            fig.add_vline(x=inspection, line_color="#f8fafc", line_dash="dot", row=row, col=1)
    fig.update_xaxes(title_text="Distance along reference lap (m)" if state["reference"] else "Distance travelled this lap (m)", row=4, col=1)
    fig.update_layout(template="plotly_dark", paper_bgcolor="#0b1120", plot_bgcolor="#172033",
                      title="Live lap inputs · aligned to GPS fixes", height=620,
                      hovermode="x unified", uirevision=f"lap-inputs:{live_revision}", showlegend=False,
                      margin={"t": 55, "b": 45, "l": 65, "r": 20})
    return fig


def stream_figure(figure, previous, identity):
    """Append unchanged trace prefixes; replace short moving marker arrays.

    The browser stores counts and endpoint values, never sends whole figures back.
    A changed lap/reference identity forces a coherent replacement.
    """
    data = figure.to_plotly_json()
    traces = data["data"]
    metadata = {"identity": identity, "traces": []}
    for trace in traces:
        arrays = {key: value for key, value in trace.items() if key in ("x", "y", "customdata") and isinstance(value, (list, tuple))}
        metadata["traces"].append({key: {"size": len(value), "first": value[0] if value else None,
                                         "last": value[-1] if value else None} for key, value in arrays.items()})
    if not previous or previous.get("identity") != identity or len(previous.get("traces", [])) != len(traces):
        return figure, metadata
    patch = Patch()
    for i, trace in enumerate(traces):
        for key, value in trace.items():
            if key in metadata["traces"][i]:
                old = previous["traces"][i].get(key)
                if (old and old["size"] <= len(value) and (not old["size"] or
                        (value[0] == old["first"] and value[old["size"] - 1] == old["last"]))):
                    if len(value) > old["size"]:
                        patch["data"][i][key].extend(value[old["size"]:])
                else:
                    patch["data"][i][key] = value
            else:
                patch["data"][i][key] = value
    # Preserve user zoom; only update layout fields which actually change live.
    patch["layout"]["shapes"] = data["layout"].get("shapes", [])
    patch["layout"]["title"] = data["layout"].get("title", {})
    return patch, metadata
