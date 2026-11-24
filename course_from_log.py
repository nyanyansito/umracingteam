"""Convert one recorded course walk into a WGS84 course outline."""
import argparse
import json
from pathlib import Path

from hardware_bench import checked_nmea, rmc_fix
from pedal_sync import elapsed


def extract(lines):
    coords = []
    last_tick = None
    for line in lines:
        raw = line.rstrip()
        if raw.startswith(b"UMR_RMC,"):
            raw = raw.split(b",", 3)[-1]
        fields = checked_nmea(raw)
        if not fields or fields[0][2:] != "RMC":
            continue
        fix = rmc_fix(fields)
        if not fix:
            if coords:
                raise ValueError("GPS fix lost during the walk. Use one uninterrupted recording.")
            continue
        tick = fix["timestamp_ms"]
        if last_tick is not None:
            dt = elapsed(tick, last_tick)
            if dt == 0:
                continue
            if not 0 < dt <= 3000:
                raise ValueError("GPS time gap during the walk. Use one uninterrupted recording.")
        last_tick = tick
        coord = [fix["longitude"], fix["latitude"]]
        if not coords or coords[-1] != coord:
            coords.append(coord)
    if len(coords) < 2:
        raise ValueError("Record at least two distinct valid GPS positions.")
    return {"type": "LineString", "coordinates": coords}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        with args.recording.open("rb") as source:
            course = extract(source)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(course, output, separators=(",", ":"))
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
