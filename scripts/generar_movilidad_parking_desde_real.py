# Script: generar_movilidad_parking_desde_real.py
# Genera el sintético de movilidad_parking anclado en datos reales de dos
# fuentes distintas:
#   1. Catálogo de aparcamientos municipales de Málaga (github/catalogo.csv,
#      idéntico a portal/catalogo.csv): id, nombre, dirección y coordenadas
#      reales de 10 aparcamientos.
#   2. Capacidad real (plazas) publicada por SMASSA, la empresa municipal de
#      aparcamientos, buscada manualmente para los 10 aparcamientos del
#      catálogo más "Salitre" (id "SA"), que aparece en los ficheros de
#      ocupación pero no tiene ficha en el catálogo abierto.
#
# Lo que NO es real y se documenta explícitamente como supuesto:
#   - Coordenadas de "Salitre": el catálogo no lo incluye. Se aproxima con un
#     punto real de la calle Salitre tomado de otro dataset abierto (semáforos
#     acústicos), no es la entrada exacta del aparcamiento.
#   - Patrón horario de ocupación: solo hay 2 instantáneas reales de "libres"
#     por aparcamiento (github/latest.csv y portal/ocupappublicosmun.csv), sin
#     hora del día. No hay ninguna fuente real de la que derivar cómo varía la
#     ocupación a lo largo del día, así que se asume una curva horaria típica
#     de aparcamiento urbano (mínimo de madrugada, subida por la mañana, pico
#     mediodía-tarde) y se escala por aparcamiento según su nivel medio de
#     ocupación real observado en las 2 instantáneas. Es una hipótesis de
#     negocio razonada, no un dato medido.

import argparse
import csv
import random
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

FIELDNAMES = ["id", "nombre", "latitud", "longitud", "direccion", "fecha", "hora", "ocupacion_total", "ocupacion_libres"]
SMALL_WORDS = {"de", "del", "la", "las", "los", "y", "en", "a", "el"}

# Capacidad real (SMASSA) por id de catálogo. "SA" (Salitre) no tiene ficha en
# el catálogo abierto pero sí aparece en los ficheros de ocupación real.
CAPACIDAD_REAL = {
    "CE": 866, "AN": 929, "CA": 526, "AL": 543, "SJ": 704, "MA": 450,
    "SA": 931, "PB": 437, "PA": 291, "TE": 253, "CY": 400,
}

# Aparcamiento real sin ficha en el catálogo abierto (ver cabecera).
SALITRE_EXTRA = {
    "id": "SA", "nombre": "Salitre",
    "direccion": "Calle Salitre",
    # Punto real aproximado de la calle Salitre (semáforos acústicos reales de esa calle),
    # no la entrada exacta del aparcamiento: no hay coordenada oficial en el catálogo abierto.
    "latitud": "36.7153", "longitud": "-4.4286",
}

# Curva horaria típica de un aparcamiento urbano (supuesto documentado, no dato
# real): fracción de ocupación relativa a lo largo del día.
HOURLY_CURVE = {
    0: 0.15, 1: 0.10, 2: 0.08, 3: 0.07, 4: 0.07, 5: 0.10,
    6: 0.20, 7: 0.40, 8: 0.60, 9: 0.75, 10: 0.85, 11: 0.90,
    12: 0.92, 13: 0.90, 14: 0.85, 15: 0.80, 16: 0.78, 17: 0.80,
    18: 0.85, 19: 0.88, 20: 0.80, 21: 0.65, 22: 0.45, 23: 0.25,
}


def parse_args():
    parser = argparse.ArgumentParser(description="Genera ocupación sintética de parkings anclada en catálogo+capacidad reales, con patrón horario asumido.")
    parser.add_argument("--catalogo", type=Path, default=Path(r"C:\Users\albas\Desktop\TFGPROYECTOEDINT\DATOS_SINTETICOS_Y_PREGUNTAS\raw_data\movilidad_parking\portal\catalogo.csv"))
    parser.add_argument("--ocupacion-1", type=Path, default=Path(r"C:\Users\albas\Desktop\TFGPROYECTOEDINT\DATOS_SINTETICOS_Y_PREGUNTAS\raw_data\movilidad_parking\github\latest.csv"))
    parser.add_argument("--ocupacion-2", type=Path, default=Path(r"C:\Users\albas\Desktop\TFGPROYECTOEDINT\DATOS_SINTETICOS_Y_PREGUNTAS\raw_data\movilidad_parking\portal\ocupappublicosmun.csv"))
    parser.add_argument("--output", type=Path, default=Path("data/sinteticos/movilidad_parking.csv"))
    parser.add_argument("--target-year", type=int, default=2026)
    parser.add_argument("--observations-per-parking", type=int, default=30, help="Filas generadas por aparcamiento.")
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace('"', "'").strip())


def title_case(text: str) -> str:
    words = text.lower().split(" ")
    out = []
    for index, word in enumerate(words):
        if not word:
            continue
        out.append(word if word in SMALL_WORDS and index != 0 else word[:1].upper() + word[1:])
    return " ".join(out)


def clean_direccion(raw: str) -> str:
    # El catálogo concatena a veces la dirección con "Málaga" sin separador (p.ej. "Calle CervantesMálaga").
    raw = re.sub(r"(?<=[a-záéíóúñ])(Málaga)", "", raw or "", flags=re.IGNORECASE)
    raw = re.sub(r"\s*-\s*$", "", raw).strip().rstrip(",").strip()
    return title_case(clean_text(raw))


def read_catalogo(path: Path):
    parkings = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            parkings[row["id"]] = {
                "id": row["id"],
                "nombre": clean_text(row["nombre"]),
                "direccion": clean_direccion(row["direccion"]),
                "latitud": row["latitude"],
                "longitud": row["longitude"],
            }
    parkings[SALITRE_EXTRA["id"]] = SALITRE_EXTRA
    return parkings


def read_ocupacion(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["id"]: int(row["libres"]) for row in csv.DictReader(handle)}


def build_real_rates(parkings, ocupacion_1, ocupacion_2):
    """Tasa de ocupación real (0-1) por aparcamiento, a partir de las 2 instantáneas reales."""
    rates = {}
    for parking_id in parkings:
        total = CAPACIDAD_REAL.get(parking_id)
        if not total:
            continue
        libres_obs = [libres[parking_id] for libres in (ocupacion_1, ocupacion_2) if parking_id in libres]
        if not libres_obs:
            continue
        rates[parking_id] = [max(0.0, min(1.0, 1 - libres / total)) for libres in libres_obs]
    return rates


def build_rows(parkings, real_rates, target_year, observations_per_parking, rng):
    overall_avg_rate = mean(value for rates in real_rates.values() for value in rates)
    rows = []
    row_id = 1
    for parking_id, info in parkings.items():
        total = CAPACIDAD_REAL.get(parking_id)
        if not total or parking_id not in real_rates:
            continue
        popularidad = mean(real_rates[parking_id]) / overall_avg_rate
        for _ in range(observations_per_parking):
            day = datetime(target_year, 1, 1) + timedelta(days=rng.randint(0, 364))
            hour = rng.randint(0, 23)
            base_rate = HOURLY_CURVE[hour] * popularidad
            rate = max(0.02, min(0.98, rng.gauss(base_rate, 0.05)))
            ocupados = round(total * rate)
            libres = max(0, total - ocupados)
            rows.append({
                "id": f"PARK-{row_id:04d}",
                "nombre": f"Parking Publico {info['nombre']}",
                "latitud": info["latitud"],
                "longitud": info["longitud"],
                "direccion": info["direccion"],
                "fecha": day.strftime("%Y-%m-%d"),
                "hora": f"{hour:02d}:00",
                "ocupacion_total": total,
                "ocupacion_libres": libres,
            })
            row_id += 1
    return rows


def print_mini_eda(real_rates, rows):
    print("\n--- Mini-EDA movilidad_parking: tasas REALES por aparcamiento (2 instantaneas, libres/capacidad SMASSA) ---")
    for parking_id, rates in real_rates.items():
        print(f"  {parking_id}: {[f'{r*100:.0f}%' for r in rates]} (capacidad={CAPACIDAD_REAL[parking_id]})")

    total = len(rows)
    rates = [1 - row["ocupacion_libres"] / row["ocupacion_total"] for row in rows]
    by_parking = Counter(row["nombre"] for row in rows)
    print(f"\n--- Mini-EDA movilidad_parking SINTETICO (n={total}, patron horario asumido) ---")
    print(f"tasa_ocupacion sintetica: min={min(rates)*100:.0f}% max={max(rates)*100:.0f}% media={mean(rates)*100:.0f}%")
    print(f"Aparcamientos: {len(by_parking)} x {total // len(by_parking)} observaciones cada uno")
    print("Curva horaria aplicada (supuesto, no dato real):", {h: f"{v*100:.0f}%" for h, v in HOURLY_CURVE.items() if h % 3 == 0})


def main():
    args = parse_args()
    rng = random.Random(args.seed)
    parkings = read_catalogo(args.catalogo)
    ocupacion_1 = read_ocupacion(args.ocupacion_1)
    ocupacion_2 = read_ocupacion(args.ocupacion_2)
    real_rates = build_real_rates(parkings, ocupacion_1, ocupacion_2)

    rows = build_rows(parkings, real_rates, args.target_year, args.observations_per_parking, rng)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Escritas {len(rows)} filas en {args.output} ({len(real_rates)} aparcamientos con capacidad real, seed={args.seed})")
    print_mini_eda(real_rates, rows)


if __name__ == "__main__":
    main()
