# Script: generar_control_gestion_its_desde_real.py
# Transforma los CSV de datos abiertos de Málaga (cámaras de tráfico y semáforos
# acústicos) al contrato de control_gestion_its. No hay oversampling: los 543
# dispositivos son reales y cada fila queda anclada 1:1 a un dispositivo real.
#
# Decisión de alcance (ver memoria): el dato abierto solo publica semáforos con
# accesibilidad acústica, no el universo completo de semáforos. Por eso
# "categoria" se queda como tipo de dispositivo (semaforo/camara_trafico) y la
# accesibilidad es un campo booleano aparte (accesibilidad_pmr), en vez de una
# categoría "semaforo_acustico" separada: no son mutuamente excluyentes, y así
# se puede cruzar directamente con impacto_pmr de afectaciones (AQ6/EQ3). No se
# sintetizan semáforos "ordinarios" (sin accesibilidad): no hay dataset abierto
# de referencia para ellos, y cualquier ratio inventado no sería defendible.
# Tampoco se cubren panel_mensaje_variable/radar_trafico/foto_rojo por la misma
# razón: se añaden cuando haya un CSV real de origen para ellos.

import argparse
import csv
import re
from collections import Counter
from pathlib import Path

FIELDNAMES = ["id", "categoria", "nombre", "descripcion", "direccion", "latitud", "longitud", "titularidad", "accesibilidad_pmr"]
SMALL_WORDS = {"de", "del", "la", "las", "los", "y", "en", "a", "el"}
POINT_RE = re.compile(r"POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)")
TRAILING_NUMBER_RE = re.compile(r",\s*(s/n|s\.n\.?|\d+\w*)\s*$", re.IGNORECASE)


def parse_args():
    parser = argparse.ArgumentParser(description="Transforma cámaras de tráfico y semáforos acústicos reales al contrato de control_gestion_its.")
    parser.add_argument("--raw-dir", type=Path, default=Path(r"C:\Users\albas\Desktop\TFGPROYECTOEDINT\DATOS_SINTETICOS_Y_PREGUNTAS\raw_data\control_gestion_its"), help="Carpeta con los CSV de datos abiertos (variante EPSG:4326).")
    parser.add_argument("--output", type=Path, default=Path("data/sinteticos/control_gestion_its.csv"), help="CSV de salida.")
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


def read_source(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_rows(raw_dir: Path):
    rows = []
    seen_ids = set()

    sources = [
        ("da_camarasTrafico-4326.csv", "ITS-CAM", "camara_trafico", "no"),
        ("da_semaforosAcusticos-4326.csv", "ITS-SEM", "semaforo", "si"),
    ]
    for filename, id_prefix, categoria, accesibilidad_pmr in sources:
        for source_row in read_source(raw_dir / filename):
            lat, lon = parse_point(source_row.get("SDOGEOMETRIA", ""))
            if lat is None:
                continue
            row_id = f"{id_prefix}-{source_row['ID']}"
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            rows.append({
                "id": row_id,
                "categoria": categoria,
                "nombre": clean_text(source_row.get("NOMBRE", "")),
                "descripcion": clean_text(source_row.get("DESCRIPCION", "")),
                "direccion": parse_direccion(source_row.get("DIRECCION", "")),
                "latitud": lat,
                "longitud": lon,
                "titularidad": (source_row.get("TITULARIDAD") or "").strip().lower(),
                "accesibilidad_pmr": accesibilidad_pmr,
            })
    return rows


def print_mini_eda(rows):
    """Deja constancia de la distribución real (no hay oversampling: esto ES la
    estadística de generación, no una comprobación posterior)."""
    total = len(rows)
    by_categoria = Counter(r["categoria"] for r in rows)
    by_titularidad = Counter(r["titularidad"] for r in rows)
    by_accesibilidad = Counter(r["accesibilidad_pmr"] for r in rows)
    by_direccion = Counter(r["direccion"] for r in rows)
    lats = [float(r["latitud"]) for r in rows]
    lons = [float(r["longitud"]) for r in rows]
    singles = sum(1 for count in by_direccion.values() if count == 1)

    print(f"\n--- Mini-EDA control_gestion_its (n={total}, sin oversampling: 1:1 con el dato real) ---")
    print("Por categoria:")
    for categoria, count in by_categoria.most_common():
        print(f"  {categoria}: {count} ({count / total * 100:.1f}%)")
    print("Por titularidad:", dict(by_titularidad))
    print("Por accesibilidad_pmr:", dict(by_accesibilidad))
    print(f"Vias distintas: {len(by_direccion)} | densidad media: {total / len(by_direccion):.2f} dispositivos/via")
    print(f"Vias con un unico dispositivo: {singles} ({singles / len(by_direccion) * 100:.1f}%)")
    print("Top 5 vias con mas dispositivos:")
    for via, count in by_direccion.most_common(5):
        print(f"  {via}: {count}")
    print(f"Bounding box real: lat [{min(lats):.4f}, {max(lats):.4f}]  lon [{min(lons):.4f}, {max(lons):.4f}]")


def main():
    args = parse_args()
    rows = build_rows(args.raw_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    cameras = sum(1 for r in rows if r["categoria"] == "camara_trafico")
    semaforos = sum(1 for r in rows if r["categoria"] == "semaforo")
    print(f"Escritas {len(rows)} filas en {args.output} (camara_trafico={cameras}, semaforo={semaforos})")
    print_mini_eda(rows)


if __name__ == "__main__":
    main()
