"""Exporta a disco local los CSV de Malaga en cada capa del lake (raw ->
normalizado -> tabla Iceberg curated), organizados por dataset, para poder
enseñar la evolución de un CSV sin depender de mc/Trino en vivo.

Uso: .venv/Scripts/python.exe scripts/exportar_evolucion_malaga.py
"""
import csv
import io
import os

import boto3
from trino.dbapi import connect

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "evolucion_malaga")

s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:9100",
    aws_access_key_id="seaweed_access_key",
    aws_secret_access_key="seaweed_secret_key",
    region_name="us-east-1",
)

# (nombre_dataset, ruta_raw, [rutas_traceability], [rutas_normalized])
DATASETS = {
    "afectaciones_urbanas": {
        "raw": ["afectaciones_urbanas/malaga/gestion_afectaciones_urbanas/gestion_afectaciones_urbanas.csv"],
        "traceability": ["_traceability/afectaciones_urbanas/malaga/gestion_afectaciones_urbanas/gestion_afectaciones_urbanas.csv.rows.csv"],
        "normalized": ["_normalized/afectaciones_urbanas/malaga/gestion_afectaciones_urbanas/60/normalized.csv"],
        "iceberg_table": "lake.curated.afectaciones_urbanas",
    },
    "control_gestion_its": {
        "raw": [
            "control_gestion_its/malaga/control_gestion_its/control_gestion_its.csv",
            "control_gestion_its/malaga/control_gestion_its/control_gestion_its__from_MALAGA.csv",
        ],
        "traceability": ["_traceability/control_gestion_its/malaga/control_gestion_its/control_gestion_its.csv.rows.csv"],
        "normalized": ["_normalized/control_gestion_its/malaga/control_gestion_its/58/normalized.csv"],
        "iceberg_table": "lake.curated.control_gestion_its",
    },
    "movilidad_carriles_bici": {
        "raw": ["movilidad/malaga/movilidad_carriles_bici/movilidad_carriles_bici.csv"],
        "traceability": ["_traceability/movilidad/malaga/movilidad_carriles_bici/movilidad_carriles_bici.csv.rows.csv"],
        "normalized": ["_normalized/movilidad/malaga/movilidad_carriles_bici/62/normalized.csv"],
        "iceberg_table": "lake.curated.movilidad_carriles_bici",
    },
    "movilidad_parking": {
        "raw": [
            "movilidad/malaga/movilidad_parking/movilidad_parking.csv",
            "movilidad/malaga/movilidad_parking/movilidad_parking__from_MALAGA.csv",
        ],
        "traceability": [
            "_traceability/movilidad/malaga/movilidad_parking/movilidad_parking.csv.rows.csv",
            "_traceability/movilidad/malaga/movilidad_parking/movilidad_parking.csv.rows__from_MALAGA.csv",
        ],
        "normalized": ["_normalized/movilidad/malaga/movilidad_parking/63/normalized.csv"],
        "iceberg_table": "lake.curated.movilidad_parking",
    },
    "movilidad_plazas_reservadas": {
        "raw": ["movilidad/malaga/movilidad_plazas_reservadas/movilidad_plazas_reservadas.csv"],
        "traceability": [
            "_traceability/movilidad/malaga/movilidad_plazas_reservadas/movilidad_plazas_reservadas.csv.rows.csv",
            "_traceability/movilidad/malaga/movilidad_plazas_reservadas/movilidad_plazas_reservadas.csv.rows__from_MALAGA.csv",
        ],
        "normalized": [
            "_normalized/movilidad/malaga/movilidad_plazas_reservadas/65/normalized.csv",
            "_normalized/movilidad/malaga/movilidad_plazas_reservadas/73/normalized.csv",
        ],
        "iceberg_table": "lake.curated.movilidad_plazas_reservadas",
    },
    "movilidad_trafico": {
        "raw": ["movilidad/malaga/movilidad_trafico/movilidad_trafico.csv"],
        "traceability": ["_traceability/movilidad/malaga/movilidad_trafico/movilidad_trafico.csv.rows.csv"],
        "normalized": ["_normalized/movilidad/malaga/movilidad_trafico/66/normalized.csv"],
        "iceberg_table": "lake.curated.movilidad_trafico",
    },
    "ocupacion_permanente_espacio_publico": {
        "raw": ["ocupacion_permanente_espacio_publico/malaga/ocupacion_permanente_espacio_publico/ocupacion_permanente_espacio_publico.csv"],
        "traceability": [
            "_traceability/ocupacion_permanente_espacio_publico/malaga/ocupacion_permanente_espacio_publico/ocupacion_permanente_espacio_publico.csv.rows.csv",
            "_traceability/ocupacion_permanente_espacio_publico/malaga/ocupacion_permanente_espacio_publico/ocupacion_permanente_espacio_publico.csv.rows__from_MALAGA.csv",
        ],
        "normalized": [
            "_normalized/ocupacion_permanente_espacio_publico/malaga/ocupacion_permanente_espacio_publico/61/normalized.csv",
            "_normalized/ocupacion_permanente_espacio_publico/malaga/ocupacion_permanente_espacio_publico/72/normalized.csv",
        ],
        "iceberg_table": "lake.curated.ocupacion_permanente_espacio_publico",
    },
}


def download(bucket, key, dest_path):
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    with open(dest_path, "wb") as f:
        f.write(body)
    return len(body)


def export_iceberg(table, dest_path):
    conn = connect(host="localhost", port=9110, user="admin", catalog="lake", schema="curated")
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} WHERE lower(split_part(dataset_id, '|', 1)) = 'malaga'")
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]
    cur.close()
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)
    return len(rows)


def main():
    for dataset, cfg in DATASETS.items():
        base = os.path.join(OUT_DIR, dataset)
        print(f"\n=== {dataset} ===")
        for key in cfg["raw"]:
            name = os.path.basename(key)
            size = download("raw", key, os.path.join(base, "1_raw", name))
            print(f"  1_raw/{name}  ({size} bytes)")
        for key in cfg["traceability"]:
            name = os.path.basename(key)
            size = download("raw", key, os.path.join(base, "2_raw_traceability", name))
            print(f"  2_raw_traceability/{name}  ({size} bytes)")
        for key in cfg["normalized"]:
            delivery_id = key.split("/")[-2]
            dest_name = f"normalized_delivery_{delivery_id}.csv"
            size = download("curated", key, os.path.join(base, "3_curated_normalized", dest_name))
            print(f"  3_curated_normalized/{dest_name}  ({size} bytes)")
        try:
            rows = export_iceberg(cfg["iceberg_table"], os.path.join(base, "4_curated_iceberg_trino.csv"))
            print(f"  4_curated_iceberg_trino.csv  ({rows} filas, vía SQL en {cfg['iceberg_table']})")
        except Exception as exc:
            print(f"  4_curated_iceberg_trino.csv  -- ERROR: {exc}")

    print(f"\nExportado en: {os.path.abspath(OUT_DIR)}")


if __name__ == "__main__":
    main()
