import argparse
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

COLUMNS = [
    "id", "nombre", "descripcion", "direccion", "latitud", "longitud",
    "fecha_inicio", "hora_inicio", "fecha_fin", "hora_fin",
    "tipo_intervencion", "tipo_afectacion", "titularidad", "impacto_pmr", "notas",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Genera afectaciones sintéticas mediante remuestreo empírico.")
    parser.add_argument("--rows", type=int, default=250, help="Número de filas a generar.")
    parser.add_argument("--seed", type=int, default=42, help="Semilla reproducible.")
    parser.add_argument("--source", type=Path, default=Path("../ayuntamiento/gestion_afectaciones_urbanas.csv"), help="CSV de origen observado.")
    parser.add_argument("--output", type=Path, default=Path("frontend/afectaciones_generales_sinteticas.csv"), help="CSV de salida.")
    parser.add_argument("--start-date", default="2026-01-01", help="Fecha mínima de inicio, YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=90, help="Ventana temporal de generación.")
    return parser.parse_args()


def read_source_rows(source_path):
    with source_path.open(newline="", encoding="utf-8-sig") as csv_file:
        rows = list(csv.DictReader(csv_file))
    if not rows:
        raise ValueError("El CSV de origen está vacío.")
    missing = [column for column in COLUMNS if column not in rows[0]]
    if missing:
        raise ValueError(f"Faltan columnas en el CSV de origen: {', '.join(missing)}")

    valid_rows = []
    for row in rows:
        try:
            begin = datetime.strptime(f"{row['fecha_inicio']} {row['hora_inicio']}", "%Y-%m-%d %H:%M")
            end = datetime.strptime(f"{row['fecha_fin']} {row['hora_fin']}", "%Y-%m-%d %H:%M")
            float(row["latitud"])
            float(row["longitud"])
            if end <= begin:
                continue
        except (TypeError, ValueError):
            continue
        valid_rows.append(row)
    if not valid_rows:
        raise ValueError("El CSV de origen no contiene filas válidas con fechas y coordenadas.")
    return valid_rows


def generate_rows(total_rows, seed, start_date, days, source_rows):
    if total_rows < 1 or days < 1:
        raise ValueError("--rows y --days deben ser mayores que cero.")

    generator = random.Random(seed)
    minimum_date = datetime.strptime(start_date, "%Y-%m-%d")
    generated = []
    for index in range(1, total_rows + 1):
        template = generator.choice(source_rows)
        original_begin = datetime.strptime(f"{template['fecha_inicio']} {template['hora_inicio']}", "%Y-%m-%d %H:%M")
        original_end = datetime.strptime(f"{template['fecha_fin']} {template['hora_fin']}", "%Y-%m-%d %H:%M")
        duration = original_end - original_begin
        begin = minimum_date + timedelta(days=generator.randrange(days))
        begin = begin.replace(hour=original_begin.hour, minute=original_begin.minute)
        end = begin + duration

        record = {column: template[column] for column in COLUMNS}
        record.update({
            "id": f"AFEC-{index:04d}",
            "latitud": f"{float(template['latitud']) + generator.uniform(-0.0008, 0.0008):.6f}",
            "longitud": f"{float(template['longitud']) + generator.uniform(-0.0008, 0.0008):.6f}",
            "fecha_inicio": begin.strftime("%Y-%m-%d"),
            "hora_inicio": begin.strftime("%H:%M"),
            "fecha_fin": end.strftime("%Y-%m-%d"),
            "hora_fin": end.strftime("%H:%M"),
        })
        generated.append(record)
    return generated


def main():
    args = parse_args()
    source_rows = read_source_rows(args.source)
    rows = generate_rows(args.rows, args.seed, args.start_date, args.days, source_rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Generadas {len(rows)} filas desde {args.source} en {args.output} con seed={args.seed}")


if __name__ == "__main__":
    main()
