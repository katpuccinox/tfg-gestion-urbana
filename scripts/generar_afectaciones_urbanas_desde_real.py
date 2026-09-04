# Script: generar_afectaciones_urbanas_desde_real.py
# Genera el sintético de gestion_afectaciones_urbanas anclado en los cortes de
# tráfico reales de Málaga (da_cortesTrafico): ubicación, tipo y texto vienen de
# un registro real; solo la fecha de inicio y, por tanto, la de fin (real
# inicio + duración muestreada de la distribución real de duraciones) son
# generadas. Con solo ~97 intervenciones reales la densidad temporal es baja
# para preguntas como AQ3 (acumulación por franja horaria), de ahí el
# oversampling: se repite cada intervención real varias veces con fechas
# distintas dentro del año objetivo, sin inventar tipos ni ubicaciones nuevas.
#
# impacto_pmr se deja siempre en "no": ninguna de las intervenciones reales de
# origen tiene impacto PMR marcado, así que no hay base real de la que partir
# (mismo criterio que accesibilidad_pmr en control_gestion_its_desde_real.py).

import argparse
import csv
import random
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median

FIELDNAMES = [
    "id", "nombre", "descripcion", "direccion", "latitud", "longitud",
    "fecha_inicio", "hora_inicio", "fecha_fin", "hora_fin",
    "tipo_intervencion", "tipo_afectacion", "titularidad", "impacto_pmr", "notas",
]
SMALL_WORDS = {"de", "del", "la", "las", "los", "y", "en", "a", "el"}
POINT_RE = re.compile(r"POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)")
TRAILING_NUMBER_RE = re.compile(r",\s*(s/n|s\.n\.?|\d+\w*)\s*$", re.IGNORECASE)

# Mapeos del catálogo real de Málaga (TIPOCORTE/TIPOAFECTACION) al catálogo del
# contrato del proyecto. Lo que no tiene equivalente claro cae en "otro".
TIPO_INTERVENCION_MAP = {"obras": "obra", "mudanza": "otro", "izado": "otro", "": "otro"}
TIPO_AFECTACION_MAP = {
    "ocupación de calzada": "ocupacion_calzada", "ocupacion de calzada": "ocupacion_calzada",
    "corte": "corte_trafico",
    "ocupación de estacionamiento": "otro", "ocupacion de estacionamiento": "otro",
    "": "otro",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Genera afectaciones sintéticas ancladas en cortes de tráfico reales de Málaga.")
    parser.add_argument("--source", type=Path, default=Path(r"C:\Users\albas\Desktop\TFGPROYECTOEDINT\DATOS_SINTETICOS_Y_PREGUNTAS\raw_data\gestion_afectaciones_urbanas\da_cortesTrafico-4326.csv"), help="CSV real de origen (variante EPSG:4326).")
    parser.add_argument("--output", type=Path, default=Path("data/sinteticos/gestion_afectaciones_urbanas.csv"), help="CSV de salida.")
    parser.add_argument("--target-year", type=int, default=2026, help="Año objetivo para las fechas generadas.")
    parser.add_argument("--oversample-factor", type=int, default=3, help="Multiplicador de filas sobre el número de registros reales válidos.")
    parser.add_argument("--seed", type=int, default=42, help="Semilla reproducible.")
    return parser.parse_args()


def clean_text(value: str) -> str:
    value = (value or "").replace('"', "'")
    return re.sub(r"\s+", " ", value.strip())


def title_case_direccion(street: str) -> str:
    words = street.lower().split(" ")
    out = []
    for index, word in enumerate(words):
        if not word:
            continue
        out.append(word if word in SMALL_WORDS and index != 0 else word[:1].upper() + word[1:])
    return " ".join(out)


def parse_direccion(raw: str) -> str:
    raw = TRAILING_NUMBER_RE.sub("", clean_text(raw)).strip().rstrip(",").strip()
    return title_case_direccion(raw)


def parse_point(raw: str):
    match = POINT_RE.search(raw or "")
    if not match:
        return None, None
    lon, lat = match.group(1), match.group(2)
    return lat, lon


def parse_datetime(raw: str):
    try:
        return datetime.strptime(clean_text(raw), "%d/%m/%Y %H:%M")
    except ValueError:
        return None


def read_real_records(source: Path):
    records = []
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            desde = parse_datetime(row.get("DESDE", ""))
            hasta = parse_datetime(row.get("HASTA", ""))
            lat, lon = parse_point(row.get("SDOGEOMETRIA", ""))
            if desde is None or hasta is None or hasta <= desde or lat is None:
                continue
            records.append({
                "direccion": parse_direccion(row.get("DIRECCION", "")),
                "latitud": lat,
                "longitud": lon,
                "tipo_intervencion": TIPO_INTERVENCION_MAP.get(clean_text(row.get("TIPOCORTE", "")).lower(), "otro"),
                "tipo_afectacion": TIPO_AFECTACION_MAP.get(clean_text(row.get("TIPOAFECTACION", "")).lower(), "otro"),
                "titularidad": clean_text(row.get("TITULARIDAD", "")).lower() or "municipal",
                "descripcion": clean_text(row.get("DESCRIPCION", "")),
                "notas": clean_text(row.get("NOTAS", "")),
                "duracion_dias": (hasta - desde).total_seconds() / 86400,
                "hora_inicio": desde.time(),
                "hora_fin": hasta.time(),
            })
    return records


def build_rows(records, target_year, oversample_factor, rng):
    durations = [record["duracion_dias"] for record in records]
    target_total = len(records) * oversample_factor
    rows = []
    row_id = 1
    while len(rows) < target_total:
        for record in records:
            if len(rows) >= target_total:
                break
            duracion = rng.choice(durations)
            fecha_inicio_dt = datetime(target_year, 1, 1) + timedelta(days=rng.randint(0, 364))
            fecha_inicio_dt = fecha_inicio_dt.replace(hour=record["hora_inicio"].hour, minute=record["hora_inicio"].minute)
            fecha_fin_dt = (fecha_inicio_dt + timedelta(days=duracion)).replace(hour=record["hora_fin"].hour, minute=record["hora_fin"].minute)
            if fecha_fin_dt <= fecha_inicio_dt:
                fecha_fin_dt = fecha_inicio_dt + timedelta(hours=2)
            rows.append({
                "id": f"AFEC-{row_id:04d}",
                "nombre": f"Intervención {record['direccion']}",
                "descripcion": record["descripcion"],
                "direccion": record["direccion"],
                "latitud": record["latitud"],
                "longitud": record["longitud"],
                "fecha_inicio": fecha_inicio_dt.strftime("%Y-%m-%d"),
                "hora_inicio": fecha_inicio_dt.strftime("%H:%M"),
                "fecha_fin": fecha_fin_dt.strftime("%Y-%m-%d"),
                "hora_fin": fecha_fin_dt.strftime("%H:%M"),
                "tipo_intervencion": record["tipo_intervencion"],
                "tipo_afectacion": record["tipo_afectacion"],
                "titularidad": record["titularidad"],
                "impacto_pmr": "no",
                "notas": record["notas"] or "Basado en dato abierto de cortes de tráfico de Málaga (oversampling de fechas).",
            })
            row_id += 1
    return rows


def print_real_stats(records):
    total = len(records)
    durations = [record["duracion_dias"] for record in records]
    print(f"\n--- Mini-EDA afectaciones REALES validas (n={total}) ---")
    print("tipo_intervencion:", dict(Counter(r["tipo_intervencion"] for r in records)))
    print("tipo_afectacion:", dict(Counter(r["tipo_afectacion"] for r in records)))
    print("titularidad:", dict(Counter(r["titularidad"] for r in records)))
    print(f"duracion_dias: min={min(durations):.0f} max={max(durations):.0f} media={mean(durations):.1f} mediana={median(durations):.1f}")
    print("(impacto_pmr real: 0 casos con 'si' en el origen -> se deja siempre 'no' en el sintetico)")


def print_synthetic_stats(rows, target_year, oversample_factor):
    total = len(rows)
    durations, months = [], Counter()
    for row in rows:
        start = datetime.strptime(row["fecha_inicio"], "%Y-%m-%d")
        end = datetime.strptime(row["fecha_fin"], "%Y-%m-%d")
        durations.append((end - start).days)
        months[start.month] += 1

    print(f"\n--- Mini-EDA afectaciones SINTETICAS generadas (n={total}, oversampling x{oversample_factor}) ---")
    print("tipo_intervencion:", dict(Counter(r["tipo_intervencion"] for r in rows)))
    print("tipo_afectacion:", dict(Counter(r["tipo_afectacion"] for r in rows)))
    print(f"duracion_dias: min={min(durations)} max={max(durations)} media={mean(durations):.1f} mediana={median(durations):.1f}")
    print(f"Intervenciones por mes en {target_year} (comprueba que el oversampling reparte bien, no agrupa en pocos meses):")
    for month in range(1, 13):
        print(f"  {month:02d}: {months.get(month, 0)}")


def main():
    args = parse_args()
    rng = random.Random(args.seed)
    records = read_real_records(args.source)
    if not records:
        raise ValueError("No se encontraron registros reales válidos en el CSV de origen.")
    print_real_stats(records)

    rows = build_rows(records, args.target_year, args.oversample_factor, rng)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nRegistros reales válidos: {len(records)} -> {len(rows)} filas escritas en {args.output} (seed={args.seed})")
    print_synthetic_stats(rows, args.target_year, args.oversample_factor)


if __name__ == "__main__":
    main()
