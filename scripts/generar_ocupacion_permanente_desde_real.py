# Script: generar_ocupacion_permanente_desde_real.py
# Transforma el plano de terrazas de hostelería de Málaga (da_ovpMesasPlanos) al
# contrato de ocupacion_permanente_espacio_publico. No hay oversampling: las
# 1646 filas son reales 1:1 (id, ubicación y superficie proceden directamente
# del dato abierto).
#
# Decisión de alcance (mismo criterio que control_gestion_its_desde_real.py):
# esta fuente abierta solo cubre terrazas de hostelería, no quioscos, puestos
# ni concesiones. Por eso tipo_ocupacion se fija a "terraza" para todas las
# filas en vez de repartir un catálogo completo sin base real. titularidad
# ("privada": son negocios privados con autorización sobre espacio público) y
# estado_autorizacion ("activa": el plano publicado son las autorizaciones
# vigentes) también son constantes justificadas por la naturaleza de la
# fuente, no inventadas al azar. ocupacion_longitud y las fechas de
# autorización no son campos obligatorios del contrato y no tienen ningún
# equivalente en el dato real, así que se dejan vacíos en vez de fabricarlos.

import argparse
import csv
import re
from collections import Counter
from pathlib import Path

FIELDNAMES = [
    "id", "nombre_establecimiento", "tipo_ocupacion", "direccion", "latitud", "longitud",
    "ocupacion_longitud", "ocupacion_superficie", "titularidad", "estado_autorizacion",
    "fecha_inicio_autorizacion", "fecha_fin_autorizacion", "notas",
]
SMALL_WORDS = {"de", "del", "la", "las", "los", "y", "en", "a", "el"}
POINT_RE = re.compile(r"POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)")
VIA_TYPE_MAP = {
    "CL": "Calle", "AV": "Avenida", "PL": "Plaza", "CMNO": "Camino", "PJE": "Pasaje",
    "ALMD": "Alameda", "PSO": "Paseo", "BV": "Bulevar", "CRIL": "Carril",
    "CTRA": "Carretera", "PLLO": "Pasillo", "PZLA": "Plazuela",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Transforma el plano real de terrazas de Málaga al contrato de ocupacion_permanente_espacio_publico.")
    parser.add_argument("--source", type=Path, default=Path(r"C:\Users\albas\Desktop\TFGPROYECTOEDINT\DATOS_SINTETICOS_Y_PREGUNTAS\raw_data\ocupacion_permanente_espacio_publico\da_ovpMesasPlanos-4326.csv"), help="CSV real de origen (variante EPSG:4326).")
    parser.add_argument("--output", type=Path, default=Path("data/sinteticos/ocupacion_permanente_espacio_publico.csv"), help="CSV de salida.")
    return parser.parse_args()


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


def parse_direccion(via_code: str, direccion_actividad: str) -> str:
    tipo_via = VIA_TYPE_MAP.get(clean_text(via_code).upper(), title_case(via_code))
    nombre_via = title_case(clean_text(direccion_actividad))
    return f"{tipo_via} {nombre_via}".strip()


def parse_point(raw: str):
    match = POINT_RE.search(raw or "")
    if not match:
        return None, None
    lon, lat = match.group(1), match.group(2)
    return lat, lon


def parse_metros(raw: str):
    raw = (raw or "").strip().replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def build_rows(source: Path):
    rows = []
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for index, source_row in enumerate(csv.DictReader(handle), start=1):
            lat, lon = parse_point(source_row.get("wkb_geometry", ""))
            superficie = parse_metros(source_row.get("metros", ""))
            if lat is None or superficie is None:
                continue
            rows.append({
                "id": f"OCUP-{index:04d}",
                "nombre_establecimiento": clean_text(source_row.get("nombre_lugar", "")),
                "tipo_ocupacion": "terraza",
                "direccion": parse_direccion(source_row.get("via", ""), source_row.get("direccion_actividad", "")),
                "latitud": lat,
                "longitud": lon,
                "ocupacion_longitud": "",
                "ocupacion_superficie": superficie,
                "titularidad": "privada",
                "estado_autorizacion": "activa",
                "fecha_inicio_autorizacion": "",
                "fecha_fin_autorizacion": "",
                "notas": "",
            })
    return rows


def print_mini_eda(rows):
    total = len(rows)
    by_direccion = Counter(r["direccion"] for r in rows)
    superficies = [r["ocupacion_superficie"] for r in rows]
    superficies_sorted = sorted(superficies)
    mediana = superficies_sorted[total // 2]
    singles = sum(1 for count in by_direccion.values() if count == 1)

    print(f"\n--- Mini-EDA ocupacion_permanente_espacio_publico (n={total}, sin oversampling: 1:1 con el dato real) ---")
    print("tipo_ocupacion: {'terraza': %d} (100%% -- unico tipo con dato abierto disponible)" % total)
    print("titularidad: {'privada': %d} | estado_autorizacion: {'activa': %d}" % (total, total))
    print(f"ocupacion_superficie (m2): min={min(superficies):.1f} max={max(superficies):.1f} media={sum(superficies)/total:.1f} mediana={mediana:.1f}")
    print(f"Vias distintas: {len(by_direccion)} | densidad media: {total / len(by_direccion):.2f} terrazas/via")
    print(f"Vias con una unica terraza: {singles} ({singles / len(by_direccion) * 100:.1f}%)")
    print("Top 5 vias con mas terrazas:")
    for via, count in by_direccion.most_common(5):
        print(f"  {via}: {count}")


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
