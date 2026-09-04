# Script: generar_carriles_bici_desde_real.py
# Transforma la red real de carriles bici de Málaga (da_carrilesBici) al contrato
# de movilidad_carriles_bici. Sin oversampling: los 75 tramos son reales 1:1.
#
# LONGITUDTOTAL del dato abierto es un agregado global de toda la red (mismo
# valor, 49670, en las 75 filas) y no sirve como longitud por tramo. En su
# lugar, carril_bici_longitud se calcula tramo a tramo sumando la distancia
# haversine entre puntos consecutivos de la geometría LINESTRING real. La
# ubicación (latitud/longitud) del tramo es el punto medio de su LINESTRING.

import argparse
import csv
import re
from collections import Counter
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

FIELDNAMES = ["id", "nombre", "latitud", "longitud", "direccion", "carril_bici_longitud"]
SMALL_WORDS = {"de", "del", "la", "las", "los", "y", "en", "a", "el"}
LINESTRING_RE = re.compile(r"LINESTRING\s*\(([^)]+)\)")
TRAILING_NUMBER_RE = re.compile(r",\s*(s/n|s\.n\.?|\d+\w*)\s*$", re.IGNORECASE)
EARTH_RADIUS_M = 6371000


def clean_text(value: str) -> str:
    value = (value or "").replace('"', "'")
    return re.sub(r"\s+", " ", value.strip())


def title_case(text: str) -> str:
    words = text.lower().split(" ")
    out = []
    for index, word in enumerate(words):
        if not word:
            continue
        out.append(word if word in SMALL_WORDS and index != 0 else word[:1].upper() + word[1:])
    return " ".join(out)


def parse_direccion(raw: str) -> str:
    raw = TRAILING_NUMBER_RE.sub("", clean_text(raw)).strip().rstrip(",").strip()
    return title_case(raw)


def haversine_m(lon1, lat1, lon2, lat2) -> float:
    lon1, lat1, lon2, lat2 = map(radians, (lon1, lat1, lon2, lat2))
    d_lon, d_lat = lon2 - lon1, lat2 - lat1
    a = sin(d_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(a))


def parse_linestring(raw: str):
    match = LINESTRING_RE.search(raw or "")
    if not match:
        return []
    points = []
    for pair in match.group(1).split(","):
        parts = pair.strip().split()
        if len(parts) != 2:
            continue
        points.append((float(parts[0]), float(parts[1])))  # (lon, lat)
    return points


def segment_length_and_midpoint(points):
    if len(points) < 2:
        return None, None
    total_length = sum(haversine_m(*points[i], *points[i + 1]) for i in range(len(points) - 1))
    mid_index = len(points) // 2
    mid_lon, mid_lat = points[mid_index]
    return total_length, (mid_lat, mid_lon)


def build_rows(source: Path):
    rows = []
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for source_row in csv.DictReader(handle):
            points = parse_linestring(source_row.get("SDOGEOMETRIA", ""))
            length_m, midpoint = segment_length_and_midpoint(points)
            if length_m is None:
                continue
            lat, lon = midpoint
            rows.append({
                "id": f"BICI-{source_row['ID']}",
                "nombre": clean_text(source_row.get("NOMBRE", "")),
                "latitud": lat,
                "longitud": lon,
                "direccion": parse_direccion(source_row.get("DIRECCION", "")),
                "carril_bici_longitud": round(length_m, 1),
            })
    return rows


def print_mini_eda(rows):
    total = len(rows)
    longitudes = [r["carril_bici_longitud"] for r in rows]
    by_direccion = Counter(r["direccion"] for r in rows)
    print(f"\n--- Mini-EDA movilidad_carriles_bici (n={total}, sin oversampling: 1:1 con el dato real) ---")
    print(f"carril_bici_longitud (m): min={min(longitudes):.1f} max={max(longitudes):.1f} media={sum(longitudes)/total:.1f} suma_red={sum(longitudes):.0f}")
    print(f"(LONGITUDTOTAL del dato abierto: 49670 m para toda la red -- la suma por tramo aqui calculada es la cifra real desagregada)")
    print(f"Vias distintas: {len(by_direccion)}")
    print("Top 5 vias con mas tramos:")
    for via, count in by_direccion.most_common(5):
        print(f"  {via}: {count}")


def parse_args():
    parser = argparse.ArgumentParser(description="Transforma la red real de carriles bici de Málaga al contrato de movilidad_carriles_bici.")
    parser.add_argument("--source", type=Path, default=Path(r"C:\Users\albas\Desktop\TFGPROYECTOEDINT\DATOS_SINTETICOS_Y_PREGUNTAS\raw_data\movilidad_carriles_bici\da_carrilesBici-4326.csv"))
    parser.add_argument("--output", type=Path, default=Path("data/sinteticos/movilidad_carriles_bici.csv"))
    return parser.parse_args()


def main():
    args = parse_args()
    rows = build_rows(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Escritas {len(rows)} filas en {args.output}")
    print_mini_eda(rows)


if __name__ == "__main__":
    main()
