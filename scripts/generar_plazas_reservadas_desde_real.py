# Script: generar_plazas_reservadas_desde_real.py
# Combina 7 fuentes reales de datos abiertos de Málaga en el contrato de
# movilidad_plazas_reservadas. Sin oversampling: las ~2379 filas son reales 1:1.
#
#   bicicleta        <- da_aparcamientosBici     (NROPLAZAS real)
#   moto             <- da_aparcamientosMoto     (LONGITUD real, sin conteo de plazas)
#   taxi             <- da_transporte_paradasTaxi (CAPACIDADVEHICULOS real)
#   pmr              <- da_aparcamientosMovilidadReducida (NROPLAZAS real)
#   eléctrico        <- descarga.json  ("PRVE", sin conteo de plazas en el propio dato)
#   electrico_recarga<- descarga_2.json ("CVE", plazas de recarga reales en INFOESP)
#   carga_descarga   <- descarga_3.json ("CYD", LONGITUD real, sin conteo de plazas)
#
# "pmr" es una categoría nueva añadida al catálogo del contrato: no es una
# variante de otra (a diferencia de accesibilidad_pmr en ITS), es un tipo de
# plaza distinto con soporte real propio (1366 registros, la base real más
# grande de las 7).
#
# Cuatro de las fuentes solo están en EPSG:25830 (ED89/UTM huso 30N): se
# convierten a EPSG:4326 con la fórmula estándar de UTM inverso (sin pyproj,
# no disponible en el entorno), validada contra un par de coordenadas
# conocidas en ambos sistemas con un error de ~4 mm.

import argparse
import csv
import json
import math
import re
from collections import Counter
from pathlib import Path

FIELDNAMES = ["id", "nombre", "latitud", "longitud", "direccion", "tipo_plaza", "plaza_numero", "plaza_longitud"]
SMALL_WORDS = {"de", "del", "la", "las", "los", "y", "en", "a", "el"}
POINT_RE = re.compile(r"POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)")
TRAILING_NUMBER_RE = re.compile(r",\s*(s/n|s\.n\.?|\d+\w*)\s*$", re.IGNORECASE)


def utm_to_latlon(easting: float, northing: float, zone: int = 30) -> tuple[float, float]:
    """UTM inverso (hemisferio norte, elipsoide WGS84/GRS80 -- ETRS89 es
    indistinguible de WGS84 a esta precisión). Validado contra coordenadas
    reales conocidas en ambos sistemas (error ~4 mm)."""
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    e2p = e2 / (1 - e2)
    k0 = 0.9996
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))

    x = easting - 500000.0
    m = northing / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))

    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu))

    n1 = a / math.sqrt(1 - e2 * math.sin(phi1) ** 2)
    t1 = math.tan(phi1) ** 2
    c1 = e2p * math.cos(phi1) ** 2
    r1 = a * (1 - e2) / (1 - e2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * k0)

    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d ** 2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * e2p) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * e2p - 3 * c1 ** 2) * d ** 6 / 720
    )
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * e2p + 24 * t1 ** 2) * d ** 5 / 120) / math.cos(phi1)

    lon_origin = (zone - 1) * 6 - 180 + 3
    return math.degrees(lat), lon_origin + math.degrees(lon)


def clean_text(value) -> str:
    return re.sub(r"\s+", " ", (value or "").replace('"', "'").strip())


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


def parse_point_4326(raw: str):
    match = POINT_RE.search(raw or "")
    if not match:
        return None, None
    lon, lat = match.group(1), match.group(2)
    return lat, lon


def read_csv_4326(path: Path, id_prefix: str, tipo_plaza: str, numero_field: str | None, longitud_field: str | None):
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            lat, lon = parse_point_4326(row.get("SDOGEOMETRIA", ""))
            if lat is None:
                continue
            plaza_numero = row.get(numero_field, "").strip() if numero_field else ""
            plaza_longitud = row.get(longitud_field, "").strip().replace(",", ".") if longitud_field else ""
            rows.append({
                "id": f"PLAZ-{id_prefix}-{row['ID']}",
                "nombre": clean_text(row.get("NOMBRE", "")),
                "latitud": lat,
                "longitud": lon,
                "direccion": parse_direccion(row.get("DIRECCION", "")),
                "tipo_plaza": tipo_plaza,
                "plaza_numero": plaza_numero,
                "plaza_longitud": plaza_longitud,
            })
    return rows


def read_csv_25830(path: Path, id_prefix: str, tipo_plaza: str, numero_field: str | None):
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            match = POINT_RE.search(row.get("SDOGEOMETRIA", "") or "")
            if not match:
                continue
            easting, northing = float(match.group(1)), float(match.group(2))
            lat, lon = utm_to_latlon(easting, northing)
            plaza_numero = row.get(numero_field, "").strip() if numero_field else ""
            rows.append({
                "id": f"PLAZ-{id_prefix}-{row['ID']}",
                "nombre": clean_text(row.get("NOMBRE", "")),
                "latitud": round(lat, 7),
                "longitud": round(lon, 7),
                "direccion": parse_direccion(row.get("DIRECCION", "")),
                "tipo_plaza": tipo_plaza,
                "plaza_numero": plaza_numero,
                "plaza_longitud": "",
            })
    return rows


def read_geojson_25830(path: Path, id_prefix: str, tipo_plaza: str, numero_extractor=None, longitud_field: str | None = None):
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    rows = []
    for feature in data["features"]:
        props = feature["properties"]
        easting, northing = feature["geometry"]["coordinates"][:2]
        lat, lon = utm_to_latlon(easting, northing)
        plaza_numero = numero_extractor(props) if numero_extractor else ""
        plaza_longitud = (props.get(longitud_field) or "").strip().replace(",", ".") if longitud_field else ""
        rows.append({
            "id": f"PLAZ-{id_prefix}-{props['ID']}",
            "nombre": clean_text(props.get("NOMBRE", "")),
            "latitud": round(lat, 7),
            "longitud": round(lon, 7),
            "direccion": parse_direccion(props.get("DIRECCION", "")),
            "tipo_plaza": tipo_plaza,
            "plaza_numero": plaza_numero,
            "plaza_longitud": plaza_longitud,
        })
    return rows


def extract_cve_plazas(props) -> str:
    infoesp = props.get("INFOESP") or []
    if infoesp and isinstance(infoesp, list):
        return str(infoesp[0].get("Plazas_de_vehiculo_disponibles_para_recarga", "")).strip()
    return ""


def parse_args():
    parser = argparse.ArgumentParser(description="Combina 7 fuentes reales de plazas reservadas de Málaga en el contrato de movilidad_plazas_reservadas.")
    parser.add_argument("--downloads-dir", type=Path, default=Path(r"C:\Users\albas\Downloads"))
    parser.add_argument("--output", type=Path, default=Path("data/sinteticos/movilidad_plazas_reservadas.csv"))
    return parser.parse_args()


def main():
    args = parse_args()
    downloads = args.downloads_dir
    rows = []
    rows += read_csv_4326(downloads / "da_aparcamientosBici-4326.csv", "BICI", "bicicleta", "NROPLAZAS", None)
    rows += read_csv_4326(downloads / "da_aparcamientosMoto-4326.csv", "MOTO", "moto", None, "LONGITUD")
    rows += read_csv_4326(downloads / "da_transporte_paradasTaxi-4326.csv", "TAXI", "taxi", "CAPACIDADVEHICULOS", None)
    rows += read_csv_25830(downloads / "da_aparcamientosMovilidadReducida-25830.csv", "PMR", "pmr", "NROPLAZAS")
    rows += read_geojson_25830(downloads / "descarga.json", "ELEC", "eléctrico")
    rows += read_geojson_25830(downloads / "descarga_2.json", "RECARGA", "electrico_recarga", extract_cve_plazas)
    rows += read_geojson_25830(downloads / "descarga_3.json", "CYD", "carga_descarga", None, "LONGITUD")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    by_tipo = Counter(r["tipo_plaza"] for r in rows)
    print(f"\n--- Mini-EDA movilidad_plazas_reservadas (n={len(rows)}, sin oversampling: 1:1 con el dato real) ---")
    for tipo, count in by_tipo.most_common():
        print(f"  {tipo}: {count} ({count/len(rows)*100:.1f}%)")
    print(f"\nEscritas {len(rows)} filas en {args.output}")


if __name__ == "__main__":
    main()
