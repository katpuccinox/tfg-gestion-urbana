# Archivo: services.py
# Resumen: Contiene la validación estructural de archivos CSV y el parseo inicial para ingesta de afectaciones urbanas.
# Autor: Alba
# Fecha: 2026-08-01

import calendar
import csv
import hashlib
import io
import json
import os
import re
import shutil
import unicodedata
import uuid
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import psycopg2

from app.contracts import (
    AFFECTACIONES_DATA_CONTRACT,
    DATASET_CONTRACTS,
    ITS_DATA_CONTRACT,
    MOVILIDAD_DATA_CONTRACTS,
    OCUPACION_PERMANENTE_DATA_CONTRACT,
)


def get_db_connection():
    """Devuelve una conexión a PostgreSQL usando las variables de entorno del proyecto."""
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "datalake")
    user = os.getenv("POSTGRES_USER", "datalake")
    password = os.getenv("POSTGRES_PASSWORD", "datalake_local")

    if os.getenv("USE_FAKE_DB", "false").lower() == "true":
        raise RuntimeError("Base de datos desactivada para pruebas locales")

    return psycopg2.connect(host=host, port=port, dbname=db, user=user, password=password)


def get_lake_root() -> Path:
    """Devuelve la ruta base del lago local para staging/raw."""
    configured = os.getenv("LAKE_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "data" / "lake"


def get_lake_dataset_paths(dataset_name: str = "afectaciones_urbanas") -> Dict[str, Path]:
    """Devuelve las rutas concretas de staging/raw para un dataset."""
    root = get_lake_root()
    return {
        "root": root,
        "staging_dir": root / "staging" / dataset_name,
        "raw_dir": root / "raw" / dataset_name,
    }


def resolve_lake_file_path(filename: str, dataset_name: str = "afectaciones_urbanas") -> Path:
    """Resuelve la ruta del archivo en raw, con compatibilidad con el layout anterior."""
    paths = get_lake_dataset_paths(dataset_name)
    candidate = paths["raw_dir"] / filename
    if candidate.exists():
        return candidate
    legacy = get_lake_root() / "raw" / filename
    if legacy.exists():
        return legacy
    return candidate


def publish_upload_to_s3(
    filename: str,
    content_bytes: bytes,
    dataset_name: str = "afectaciones_urbanas",
    dimension: str = "afectaciones_urbanas",
    entity: str = "municipio_demo",
) -> Dict[str, Any]:
    """Publica un original de Capa 1 con un prefijo aislado por dimensión, municipio y dataset."""
    import boto3

    endpoint = os.getenv("S3_ENDPOINT")
    bucket = os.getenv("S3_BUCKET_RAW", "raw")
    access_key = os.getenv("S3_ACCESS_KEY")
    secret_key = os.getenv("S3_SECRET_KEY")
    key = f"{dimension}/{entity}/{dataset_name}/{filename}"

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.getenv("S3_REGION", "us-east-1"),
    )
    client.put_object(Bucket=bucket, Key=key, Body=content_bytes, ContentType="text/csv")
    return {
        "provider": "seaweedfs",
        "bucket": bucket,
        "key": key,
        "uri": f"s3://{bucket}/{key}",
        "object_exists": True,
    }


def inspect_s3_lake_object(filename: str, dataset_name: str = "afectaciones_urbanas") -> Dict[str, Any]:
    """Comprueba la presencia del archivo en los buckets staging y raw de S3."""
    import boto3
    from botocore.exceptions import ClientError

    endpoint = os.getenv("S3_ENDPOINT")
    if not endpoint:
        return {"enabled": False, "errors": ["S3_ENDPOINT no está configurado."]}

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.getenv("S3_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY"),
        region_name=os.getenv("S3_REGION", "us-east-1"),
    )
    key = f"{dataset_name}/{filename}"
    buckets = {
        "staging": os.getenv("S3_BUCKET_STAGING", "staging"),
        "raw": os.getenv("S3_BUCKET_RAW", "raw"),
    }
    result: Dict[str, Any] = {"enabled": True, "key": key, "buckets": {}}

    try:
        for stage, bucket in buckets.items():
            try:
                metadata = client.head_object(Bucket=bucket, Key=key)
                result["buckets"][stage] = {
                    "bucket": bucket,
                    "exists": True,
                    "content_length": metadata.get("ContentLength"),
                }
            except ClientError as exc:
                status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if status_code == 404:
                    result["buckets"][stage] = {"bucket": bucket, "exists": False}
                else:
                    raise
        result["status"] = "raw" if result["buckets"]["raw"]["exists"] else "staging" if result["buckets"]["staging"]["exists"] else "not_found"
        return result
    except Exception as exc:
        return {"enabled": True, "status": "error", "key": key, "errors": [str(exc)]}


def run_trino_query(sql: str) -> List[tuple]:
    """Ejecuta una consulta SQL contra Trino y devuelve las filas obtenidas."""
    from trino.dbapi import connect

    conn = connect(
        host=os.getenv("TRINO_HOST", "trino"),
        port=int(os.getenv("TRINO_PORT", "8080")),
        user=os.getenv("TRINO_USER", "admin"),
        catalog="hive",
        schema="raw",
    )
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()
    return rows


def run_trino_query_with_columns(sql: str) -> Dict[str, Any]:
    """Ejecuta una consulta SQL contra Trino y devuelve columnas + filas (para lecturas fila a fila)."""
    from trino.dbapi import connect

    conn = connect(
        host=os.getenv("TRINO_HOST", "trino"),
        port=int(os.getenv("TRINO_PORT", "8080")),
        user=os.getenv("TRINO_USER", "admin"),
        catalog="hive",
        schema="raw",
    )
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    columns = [desc[0] for desc in cur.description] if cur.description else []
    cur.close()
    return {"columns": columns, "rows": rows}


# Pesos de impacto 
IMPACTO_BASE_POR_TIPO_AFECTACION = {"corte_trafico": 45, "ocupacion_calzada": 32, "restriccion_paso": 22, "ocupacion_acera": 12}
IMPACTO_MULT_POR_TIPO_INTERVENCION = {"obra": 1.3, "evento": 1.2, "actuacion_municipal": 1.1, "actuación_municipal": 1.1, "mantenimiento": 1.0}
IMPACTO_MULT_POR_FRANJA_HORARIA = {
    "00:00-06:00": 0.10, "06:00-09:00": 0.78, "09:00-13:00": 0.38,
    "13:00-16:00": 0.25, "16:00-20:00": 0.85, "20:00-24:00": 0.30,
}
IMPACTO_CLIP_MIN = 2
IMPACTO_CLIP_MAX = 58
# Umbrales de nivel
IMPACTO_UMBRAL_CRITICO = 35
IMPACTO_UMBRAL_ALTO = 25
IMPACTO_UMBRAL_MEDIO = 15


def _franja_horaria_desde_hora(hora: Any) -> str:
    """Deriva la franja horaria (misma tabla que _franja_key en ayuntamiento) a partir de hora_inicio."""
    try:
        h = int(str(hora).split(":")[0])
        if 0 <= h < 6:
            return "00:00-06:00"
        if 6 <= h < 9:
            return "06:00-09:00"
        if 9 <= h < 13:
            return "09:00-13:00"
        if 13 <= h < 16:
            return "13:00-16:00"
        if 16 <= h < 20:
            return "16:00-20:00"
        return "20:00-24:00"
    except (ValueError, TypeError):
        return "09:00-13:00"


def _nivel_impacto(pct_impacto: float) -> str:
    """Categoriza un pct_impacto en Crítico/Alto/Medio/Bajo (mismos umbrales que ayuntamiento)."""
    if pct_impacto >= IMPACTO_UMBRAL_CRITICO:
        return "Crítico"
    if pct_impacto >= IMPACTO_UMBRAL_ALTO:
        return "Alto"
    if pct_impacto >= IMPACTO_UMBRAL_MEDIO:
        return "Medio"
    return "Bajo"


def calcular_variacion_impacto(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calcula pct_impacto ponderado por fila y agrega KPIs, replicando get_obras_impacto de ayuntamiento."""
    detalle: List[Dict[str, Any]] = []
    for row in rows:
        tipo_afectacion = str(row.get("tipo_afectacion") or "").strip().lower()
        tipo_intervencion = str(row.get("tipo_intervencion") or "").strip().lower()
        franja = _franja_horaria_desde_hora(row.get("hora_inicio")) if row.get("hora_inicio") else "09:00-13:00"

        base = IMPACTO_BASE_POR_TIPO_AFECTACION.get(tipo_afectacion, 20)
        mult_intervencion = IMPACTO_MULT_POR_TIPO_INTERVENCION.get(tipo_intervencion, 1.0)
        mult_franja = IMPACTO_MULT_POR_FRANJA_HORARIA.get(franja, 0.30)

        pct_impacto = round(min(max(base * mult_intervencion * mult_franja, IMPACTO_CLIP_MIN), IMPACTO_CLIP_MAX), 1)
        nivel = _nivel_impacto(pct_impacto)

        detalle.append({
            "nombre": row.get("nombre"),
            "via": row.get("direccion") or row.get("nombre"),
            "tipo_intervencion": row.get("tipo_intervencion"),
            "tipo_afectacion": row.get("tipo_afectacion"),
            "pct_impacto": pct_impacto,
            "nivel": nivel,
        })

    total = len(detalle)
    if total == 0:
        return {
            "variacion_media_pct": 0.0,
            "count_alto_critico": 0,
            "pct_alto_critico": 0.0,
            "detalle_variacion": [],
            "ranking_criticidad_vias": [],
        }

    count_alto_critico = sum(1 for d in detalle if d["nivel"] in ("Alto", "Crítico"))
    variacion_media_pct = round(sum(d["pct_impacto"] for d in detalle) / total, 1)
    pct_alto_critico = round(count_alto_critico / total * 100, 1)

    return {
        "variacion_media_pct": variacion_media_pct,
        "count_alto_critico": count_alto_critico,
        "pct_alto_critico": pct_alto_critico,
        "detalle_variacion": detalle,
        "ranking_criticidad_vias": ranking_criticidad_por_via(detalle),
    }


def ranking_criticidad_por_via(detalle: List[Dict[str, Any]], top_n: int = 10) -> List[Dict[str, Any]]:
    """Responde a Q3 (\"criticidad por vía y tipo\"):"""
    peor_por_via: Dict[str, Dict[str, Any]] = {}
    conteo_por_via: Dict[str, int] = {}
    for d in detalle:
        via = d.get("via") or "sin_dato"
        conteo_por_via[via] = conteo_por_via.get(via, 0) + 1
        actual = peor_por_via.get(via)
        if actual is None or d["pct_impacto"] > actual["pct_impacto"]:
            peor_por_via[via] = d

    ranking = sorted(peor_por_via.values(), key=lambda d: d["pct_impacto"], reverse=True)[:top_n]
    return [
        {
            "via": d.get("via"),
            "nombre": d.get("nombre"),
            "tipo_intervencion": d.get("tipo_intervencion"),
            "tipo_afectacion": d.get("tipo_afectacion"),
            "pct_impacto": d["pct_impacto"],
            "nivel": d["nivel"],
            "count": conteo_por_via.get(d.get("via") or "sin_dato", 1),
        }
        for d in ranking
    ]


def build_afectaciones_layer5(table: str = "afectaciones_urbanas") -> Dict[str, Any]:
    """Construye reglas descriptivas y rankings reproducibles para la capa 5.

    Lee de lake.curated (capa 4), no de hive.raw (capa 2): así las reglas se calculan
    sobre datos ya tipados y depurados (fechas coherentes, catálogos normalizados),
    no sobre el CSV crudo.
    """
    qualified_table = f"lake.curated.{table}"
    try:
        result = run_trino_query_with_columns(f"SELECT * FROM {qualified_table}")
        rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
        if not rows:
            return {"is_valid": True, "source": qualified_table, "model": "reglas_descriptivas", "total_registros": 0, "reglas": [], "ranking_vias": []}

        impact = calcular_variacion_impacto(rows)
        by_intervention: Dict[str, List[float]] = {}
        for detail in impact["detalle_variacion"]:
            key = str(detail.get("tipo_intervencion") or "sin_dato")
            by_intervention.setdefault(key, []).append(float(detail["pct_impacto"]))

        rules = []
        for intervention, values in sorted(by_intervention.items(), key=lambda item: (-sum(item[1]) / len(item[1]), item[0])):
            average = round(sum(values) / len(values), 1)
            rules.append({
                "tipo_intervencion": intervention,
                "registros": len(values),
                "impacto_medio_pct": average,
                "nivel": _nivel_impacto(average),
            })

        return {
            "is_valid": True,
            "source": qualified_table,
            "model": "reglas_descriptivas",
            "total_registros": len(rows),
            "metricas": {
                "variacion_media_pct": impact["variacion_media_pct"],
                "count_alto_critico": impact["count_alto_critico"],
                "pct_alto_critico": impact["pct_alto_critico"],
            },
            "reglas": rules,
            "ranking_vias": impact["ranking_criticidad_vias"],
        }
    except Exception as exc:
        return {"is_valid": False, "source": qualified_table, "errors": [str(exc)]}


# Recomendación de acción por nivel de congestión y si hay una obra activa en la vía en ese
# momento — es lo que convierte "esta vía está en nivel Alto" en algo que alguien puede hacer.
RECOMENDACION_CONGESTION = {
    ("Crítico", True): "Coordinar desvíos con la obra activa y activar paneles de mensaje variable en los accesos.",
    ("Crítico", False): "Revisar semaforización y valorar restricciones temporales de acceso.",
    ("Alto", True): "Reforzar señalización de la obra y vigilar la evolución del flujo en la franja crítica.",
    ("Alto", False): "Vigilancia reforzada en la franja horaria de mayor flujo.",
    ("Medio", True): "Sin acción inmediata; valorar reprogramar la obra fuera de la franja de mayor flujo.",
    ("Medio", False): "Sin acción prioritaria; monitorizar evolución.",
    ("Bajo", True): "Sin acción prioritaria.",
    ("Bajo", False): "Sin acción prioritaria.",
}


def build_movilidad_trafico_layer5(table: str = "movilidad_trafico") -> Dict[str, Any]:
    """Congestión relativa por vía, con recomendación de acción — pensada para decidir dónde
    intervenir primero, no solo para describir el tráfico.

    El nivel de congestión no compara contra un umbral fijo para toda la ciudad (300 veh/h
    es crítico en una calle residencial y normal en una avenida): se calcula contra los
    percentiles p50/p75/p90 de flujo de esa misma vía. Cada medición se cruza además con
    afectaciones_urbanas para saber si hay una obra activa en la misma vía y momento.
    """
    curated_table = f"lake.curated.{table}"
    try:
        result = run_trino_query_with_columns(f"SELECT * FROM {curated_table}")
        rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
        if not rows:
            return {
                "is_valid": True, "source": curated_table, "model": "congestion_relativa",
                "total_registros": 0, "metricas": {}, "prioridades": [],
            }

        # Ventanas de obra activa por vía (enriquecimiento: si afectaciones no está disponible,
        # la capa 5 de tráfico sigue funcionando, solo sin la señal de obra activa).
        obras_por_via: Dict[str, List[tuple]] = {}
        try:
            afect = run_trino_query_with_columns(
                "SELECT direccion, fecha_hora_inicio, fecha_hora_fin FROM lake.curated.afectaciones_urbanas"
            )
            for direccion, inicio, fin in afect["rows"]:
                if direccion and inicio and fin:
                    obras_por_via.setdefault(direccion, []).append((inicio, fin))
        except Exception:
            pass

        def hay_obra_activa(direccion: str, momento: Any) -> bool:
            if momento is None:
                return False
            for inicio, fin in obras_por_via.get(direccion or "", []):
                try:
                    if inicio <= momento <= fin:
                        return True
                except TypeError:
                    continue
            return False

        flujos_por_via: Dict[str, List[float]] = {}
        for row in rows:
            flujo = row.get("trafico_flujo")
            if flujo is not None:
                flujos_por_via.setdefault(row.get("direccion") or "sin_dato", []).append(float(flujo))

        def percentiles(valores: List[float]) -> Dict[str, float]:
            ordenados = sorted(valores)
            def pct(p: float) -> float:
                return ordenados[min(len(ordenados) - 1, int(len(ordenados) * p))]
            return {"p50": pct(0.50), "p75": pct(0.75), "p90": pct(0.90)}

        percentiles_por_via = {via: percentiles(valores) for via, valores in flujos_por_via.items()}

        def nivel_congestion(direccion: str, flujo: float) -> str:
            p = percentiles_por_via.get(direccion or "sin_dato", {"p50": 0.0, "p75": 0.0, "p90": 0.0})
            if flujo >= p["p90"]:
                return "Crítico"
            if flujo >= p["p75"]:
                return "Alto"
            if flujo >= p["p50"]:
                return "Medio"
            return "Bajo"

        resumen_por_via: Dict[str, Dict[str, Any]] = {}
        count_alto_critico = 0
        for row in rows:
            direccion = row.get("direccion") or "sin_dato"
            flujo = float(row.get("trafico_flujo") or 0.0)
            nivel = nivel_congestion(direccion, flujo)
            obra_activa = hay_obra_activa(direccion, row.get("fecha_hora_inicio"))
            recomendacion = RECOMENDACION_CONGESTION[(nivel, obra_activa)]
            es_critico = nivel in ("Alto", "Crítico")
            count_alto_critico += 1 if es_critico else 0

            resumen = resumen_por_via.setdefault(direccion, {
                "direccion": direccion, "mediciones": 0, "alto_critico": 0,
                "con_obra_activa": 0, "recomendaciones": set(), "umbral_p90": round(percentiles_por_via[direccion]["p90"], 1),
            })
            resumen["mediciones"] += 1
            if es_critico:
                resumen["alto_critico"] += 1
                resumen["recomendaciones"].add(recomendacion)
            if obra_activa:
                resumen["con_obra_activa"] += 1

        total = len(rows)
        prioridades = []
        for via, resumen in resumen_por_via.items():
            pct_alto_critico = round(resumen["alto_critico"] / resumen["mediciones"] * 100, 1)
            prioridades.append({
                "direccion": via,
                "mediciones": resumen["mediciones"],
                "pct_tiempo_alto_critico": pct_alto_critico,
                "umbral_critico_veh_h": resumen["umbral_p90"],
                "mediciones_con_obra_activa": resumen["con_obra_activa"],
                "recomendaciones": sorted(resumen["recomendaciones"]) or ["Sin acción prioritaria."],
            })
        # Ordenado para responder directamente "¿dónde actúo primero?", no solo describir.
        prioridades.sort(key=lambda item: (item["pct_tiempo_alto_critico"], item["mediciones_con_obra_activa"]), reverse=True)

        return {
            "is_valid": True,
            "source": curated_table,
            "model": "congestion_relativa",
            "total_registros": total,
            "metricas": {
                "count_alto_critico": count_alto_critico,
                "pct_alto_critico": round(count_alto_critico / total * 100, 1) if total else 0.0,
            },
            "prioridades": prioridades[:10],
        }
    except Exception as exc:
        return {"is_valid": False, "source": curated_table, "errors": [str(exc)]}


def predecir_congestion_via(
    direccion: str,
    franja_horaria: str | None = None,
    dia_semana: str | None = None,
) -> Dict[str, Any]:
    """Estima la congestión esperada en una vía a partir de su propio histórico — no es un
    modelo entrenado (con ~150 filas sintéticas no merece la pena todavía, ver memoria),
    es una consulta a los mismos percentiles y reglas que ya calcula la capa 5 de tráfico."""
    # direccion en curated solo se pasa a minúsculas (no se le quitan tildes, a diferencia
    # de las columnas de catálogo) — normalizar igual aquí, no con normalize_catalog_value.
    direccion_normalizada = _sql_escape(direccion.strip().lower())
    curated_table = "lake.curated.movilidad_trafico"
    try:
        result = run_trino_query_with_columns(
            f"SELECT trafico_flujo, fecha_hora_inicio FROM {curated_table} WHERE direccion = '{direccion_normalizada}'"
        )
        historico = [row[0] for row in result["rows"] if row[0] is not None]
        if not historico:
            return {"is_valid": False, "errors": [f"No hay histórico de tráfico para '{direccion}'."]}

        filtro_sql = f"direccion = '{direccion_normalizada}'"
        if franja_horaria:
            filtro_sql += f" AND franja_horaria = '{_sql_escape(franja_horaria.strip())}'"
        if dia_semana:
            filtro_sql += f" AND dia_semana = '{_sql_escape(normalize_catalog_value(dia_semana))}'"
        filtrado = run_trino_query_with_columns(f"SELECT trafico_flujo FROM {curated_table} WHERE {filtro_sql}")
        muestra = [row[0] for row in filtrado["rows"] if row[0] is not None] or historico
        flujo_esperado = sum(muestra) / len(muestra)

        ordenados = sorted(historico)
        def pct(p: float) -> float:
            return ordenados[min(len(ordenados) - 1, int(len(ordenados) * p))]
        p50, p75, p90 = pct(0.50), pct(0.75), pct(0.90)
        nivel = "Crítico" if flujo_esperado >= p90 else "Alto" if flujo_esperado >= p75 else "Medio" if flujo_esperado >= p50 else "Bajo"

        obra_activa = False
        try:
            obras = run_trino_query(
                f"SELECT count(*) FROM lake.curated.afectaciones_urbanas WHERE direccion = '{direccion_normalizada}' "
                "AND fecha_hora_inicio <= current_timestamp AND fecha_hora_fin >= current_timestamp"
            )
            obra_activa = bool(obras[0][0]) if obras else False
        except Exception:
            pass

        return {
            "is_valid": True,
            "direccion": direccion,
            "franja_horaria": franja_horaria,
            "dia_semana": dia_semana,
            "muestra_utilizada": len(muestra),
            "confiabilidad": "baja" if len(muestra) < 5 else "media" if len(muestra) < 20 else "alta",
            "flujo_esperado_veh_h": round(flujo_esperado, 1),
            "umbral_critico_veh_h": round(p90, 1),
            "nivel_congestion": nivel,
            "obra_activa": obra_activa,
            "recomendacion": RECOMENDACION_CONGESTION[(nivel, obra_activa)],
        }
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)]}


def build_dimension_layer5(dataset: str) -> Dict[str, Any]:
    """Construye la capa 5 para cualquier dimensión, generalizando build_afectaciones_layer5.

    afectaciones_urbanas conserva su motor de impacto simulado (impacto de tráfico por
    tipo de intervención): no tiene equivalente genérico porque las tablas de reglas son
    de negocio, no derivables del contrato. Para el resto, calcula reglas de frecuencia:
    distribución por una columna categórica y, si hay una columna de ubicación, un ranking
    de las más frecuentes. Ambas se infieren solas del contrato (layer5_group_by usa por
    defecto la primera allowed_values; layer5_location_column, "direccion" si existe) —
    los campos del contrato solo hacen falta para forzar una elección distinta.
    """
    if dataset in ("afectaciones_urbanas", "gestion_afectaciones_urbanas"):
        return build_afectaciones_layer5(dataset)
    if dataset == "movilidad_trafico":
        return build_movilidad_trafico_layer5(dataset)

    contract = DATASET_CONTRACTS.get(dataset)
    if contract is None:
        return {"is_valid": False, "errors": [f"Dataset no permitido: {dataset}"]}

    group_column = contract.get("layer5_group_by") or next(iter(contract.get("allowed_values", {})), None)
    if not group_column:
        return {
            "is_valid": False,
            "errors": [
                f"La dimensión '{dataset}' no tiene una columna categórica para capa 5 "
                "(defina allowed_values o layer5_group_by en su contrato)."
            ],
        }

    curated_table = f"lake.curated.{dataset}"
    try:
        total_rows = run_trino_query(f"SELECT count(*) FROM {curated_table}")
        total = int(total_rows[0][0]) if total_rows else 0
        if total == 0:
            return {
                "is_valid": True,
                "source": curated_table,
                "model": "reglas_frecuencia",
                "total_registros": 0,
                "reglas": [],
                "ranking_ubicaciones": [],
            }

        grupo_rows = run_trino_query(
            f"SELECT {group_column}, count(*) FROM {curated_table} GROUP BY {group_column} ORDER BY 2 DESC"
        )
        reglas = [
            {
                "categoria": row[0],
                "registros": row[1],
                "porcentaje": round(row[1] / total * 100, 1),
            }
            for row in grupo_rows
        ]

        ranking_ubicaciones: List[Dict[str, Any]] = []
        location_column = contract.get("layer5_location_column")
        if location_column is None and "direccion" in contract.get("columns", contract["required_columns"]):
            location_column = "direccion"
        if location_column:
            ubicacion_rows = run_trino_query(
                f"SELECT {location_column}, count(*) FROM {curated_table} "
                f"WHERE {location_column} IS NOT NULL GROUP BY {location_column} ORDER BY 2 DESC LIMIT 10"
            )
            ranking_ubicaciones = [{"ubicacion": row[0], "registros": row[1]} for row in ubicacion_rows]

        return {
            "is_valid": True,
            "source": curated_table,
            "model": "reglas_frecuencia",
            "group_by": group_column,
            "total_registros": total,
            "reglas": reglas,
            "ranking_ubicaciones": ranking_ubicaciones,
        }
    except Exception as exc:
        return {"is_valid": False, "source": curated_table, "errors": [str(exc)]}


def get_afectaciones_layer4_summary(table: str = "afectaciones_urbanas") -> Dict[str, Any]:
    """Responde con un resumen operativo de la capa 4 para el frontend y la capa analítica."""
    source = f"lake.analytics.{table}_resumen"
    try:
        kpi_rows = run_trino_query(
            f"""
            SELECT
                coalesce(sum(total_afectaciones), 0),
                coalesce(sum(duracion_media_horas * total_afectaciones) / nullif(sum(total_afectaciones), 0), 0),
                coalesce(sum(afectaciones_pmr), 0)
            FROM {source}
            """
        )
        total_afectaciones = int(kpi_rows[0][0]) if kpi_rows else 0
        duracion_media_horas = float(kpi_rows[0][1]) if kpi_rows else 0.0
        afectaciones_pmr = int(kpi_rows[0][2]) if kpi_rows else 0
        porcentaje_pmr = round((afectaciones_pmr / total_afectaciones * 100), 2) if total_afectaciones else 0.0

        top_vias_rows = run_trino_query(
            f"SELECT direccion, sum(total_afectaciones) AS total FROM {source} GROUP BY direccion ORDER BY total DESC LIMIT 10"
        )
        por_tipo_afectacion_rows = run_trino_query(
            f"SELECT tipo_afectacion, sum(total_afectaciones) AS total FROM {source} GROUP BY tipo_afectacion ORDER BY total DESC"
        )
        por_hora_rows = run_trino_query(
            f"SELECT hora_inicio, sum(total_afectaciones) AS total FROM {source} GROUP BY hora_inicio ORDER BY total DESC LIMIT 12"
        )

        return {
            "is_valid": True,
            "source": source,
            "kpis": {
                "total_afectaciones": total_afectaciones,
                "duracion_media_horas": round(duracion_media_horas, 2),
                "afectaciones_pmr": afectaciones_pmr,
                "porcentaje_pmr": porcentaje_pmr,
            },
            "summary": {
                "top_vias": [{"via": row[0], "count": int(row[1])} for row in top_vias_rows],
                "por_tipo_afectacion": [{"tipo_afectacion": row[0], "count": int(row[1])} for row in por_tipo_afectacion_rows],
                "por_hora": [{"hora": row[0], "count": int(row[1])} for row in por_hora_rows],
            },
        }
    except Exception as exc:
        return {
            "is_valid": False,
            "source": source,
            "errors": [str(exc)],
            "kpis": {"total_afectaciones": 0, "duracion_media_horas": 0.0, "afectaciones_pmr": 0, "porcentaje_pmr": 0.0},
            "summary": {"top_vias": [], "por_tipo_afectacion": [], "por_hora": []},
        }


def get_dimension_analytics_summary(dataset: str, limit: int = 50) -> Dict[str, Any]:
    """Lee el resumen de capa 4 (lake.analytics.{dataset}_resumen) de cualquier dimensión como
    filas genéricas, generalizando get_afectaciones_layer4_summary (que interpreta columnas
    concretas) para dimensiones cuyo agregado no necesita esa lectura a medida."""
    analytics_table = f"lake.analytics.{dataset}_resumen"
    try:
        result = run_trino_query_with_columns(f"SELECT * FROM {analytics_table} LIMIT {int(limit)}")
        rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
        return {"is_valid": True, "source": analytics_table, "rows": rows}
    except Exception as exc:
        return {"is_valid": False, "source": analytics_table, "errors": [str(exc)], "rows": []}


def get_afectaciones_kpis(table: str = "afectaciones_urbanas") -> Dict[str, Any]:
    """Calcula KPIs de afectaciones urbanas consultando la tabla Hive/Trino sobre SeaweedFS."""
    qualified_table = f"hive.raw.{table}"
    try:
        total_rows = run_trino_query(f"SELECT count(*) FROM {qualified_table}")
        total = total_rows[0][0] if total_rows else 0

        if total == 0:
            return {
                "is_valid": True,
                "source": "trino",
                "table": qualified_table,
                "kpis": {
                    "total_afectaciones": 0,
                    "variacion_media_pct": 0.0,
                    "count_alto_critico": 0,
                    "pct_alto_critico": 0.0,
                },
                "por_tipo_intervencion": [],
                "por_tipo_afectacion": [],
                "ranking_vias": [],
                "detalle_variacion": [],
            }

        tipo_intervencion_rows = run_trino_query(
            f"SELECT tipo_intervencion, count(*) FROM {qualified_table} GROUP BY tipo_intervencion ORDER BY 2 DESC"
        )
        tipo_afectacion_rows = run_trino_query(
            f"SELECT tipo_afectacion, count(*) FROM {qualified_table} GROUP BY tipo_afectacion ORDER BY 2 DESC"
        )
        ranking_rows = run_trino_query(
            f"SELECT nombre, count(*) FROM {qualified_table} GROUP BY nombre ORDER BY 2 DESC LIMIT 10"
        )

        # pct_impacto ponderado: requiere leer filas completas; columnas opcionales ausentes se degradan sin romper el resto.
        variacion_impacto = {
            "variacion_media_pct": 0.0,
            "count_alto_critico": 0,
            "pct_alto_critico": 0.0,
            "detalle_variacion": [],
            "ranking_criticidad_vias": [],
        }
        try:
            table_data = run_trino_query_with_columns(f"SELECT * FROM {qualified_table}")
            columns = table_data["columns"]
            if "tipo_afectacion" in columns and "tipo_intervencion" in columns:
                dict_rows = [dict(zip(columns, row)) for row in table_data["rows"]]
                variacion_impacto = calcular_variacion_impacto(dict_rows)
        except Exception:
            pass

        return {
            "is_valid": True,
            "source": "trino",
            "table": qualified_table,
            "kpis": {
                "total_afectaciones": total,
                "variacion_media_pct": variacion_impacto["variacion_media_pct"],
                "count_alto_critico": variacion_impacto["count_alto_critico"],
                "pct_alto_critico": variacion_impacto["pct_alto_critico"],
            },
            "por_tipo_intervencion": [{"tipo_intervencion": r[0], "count": r[1]} for r in tipo_intervencion_rows],
            "por_tipo_afectacion": [{"tipo_afectacion": r[0], "count": r[1]} for r in tipo_afectacion_rows],
            "ranking_vias": [{"nombre": r[0], "count": r[1]} for r in ranking_rows],
            "detalle_variacion": variacion_impacto["detalle_variacion"],
            "ranking_criticidad_vias": variacion_impacto["ranking_criticidad_vias"],
        }
    except Exception as exc:
        return {"is_valid": False, "source": "trino", "table": qualified_table, "errors": [str(exc)]}


def run_trino_statement(sql: str) -> None:
    """Ejecuta una sentencia DDL/DML en Trino (sin resultado tabular relevante)."""
    from trino.dbapi import connect

    conn = connect(
        host=os.getenv("TRINO_HOST", "trino"),
        port=int(os.getenv("TRINO_PORT", "8080")),
        user=os.getenv("TRINO_USER", "admin"),
        catalog="hive",
        schema="raw",
    )
    cur = conn.cursor()
    cur.execute(sql)
    try:
        cur.fetchall()
    except Exception:
        pass
    cur.close()


def _afectaciones_curated_select_sql(source_table: str) -> str:
    """Cuerpo SELECT que tipa y depura afectaciones desde hive.raw (usado solo por el
    reconstructor manual build_afectaciones_layers; el pipeline automático usa el motor
    genérico _dimension_curated_select_sql)."""
    return f"""
        SELECT
            to_hex(sha256(to_utf8(concat_ws('|', coalesce(id, ''), coalesce(fecha_inicio, ''), coalesce(hora_inicio, ''), coalesce(fecha_fin, ''), coalesce(hora_fin, ''), coalesce(direccion, ''))))) AS row_id_tecnico,
            nullif(trim(id), '') AS id,
            nullif(trim(nombre), '') AS nombre,
            nullif(trim(descripcion), '') AS descripcion,
            nullif(lower(trim(direccion)), '') AS direccion,
            try_cast(replace(nullif(trim(latitud), ''), ',', '.') AS double) AS latitud,
            try_cast(replace(nullif(trim(longitud), ''), ',', '.') AS double) AS longitud,
            try_cast(nullif(trim(fecha_inicio), '') AS date) AS fecha_inicio,
            try_cast(nullif(trim(hora_inicio), '') AS time) AS hora_inicio,
            try_cast(nullif(trim(fecha_fin), '') AS date) AS fecha_fin,
            try_cast(nullif(trim(hora_fin), '') AS time) AS hora_fin,
            try_cast(concat(trim(fecha_inicio), ' ', trim(hora_inicio)) AS timestamp) AS fecha_hora_inicio,
            try_cast(concat(trim(fecha_fin), ' ', trim(hora_fin)) AS timestamp) AS fecha_hora_fin,
            CASE lower(trim(tipo_intervencion))
                WHEN 'actuación_municipal' THEN 'actuacion_municipal'
                ELSE nullif(lower(trim(tipo_intervencion)), '')
            END AS tipo_intervencion,
            nullif(lower(trim(tipo_afectacion)), '') AS tipo_afectacion,
            lower(nullif(trim(titularidad), '')) AS titularidad,
            CASE lower(trim(impacto_pmr)) WHEN 'si' THEN true WHEN 'sí' THEN true WHEN 'no' THEN false ELSE NULL END AS impacto_pmr,
            nullif(trim(notas), '') AS notas,
            date_diff(
                'minute',
                try_cast(concat(trim(fecha_inicio), ' ', trim(hora_inicio)) AS timestamp),
                try_cast(concat(trim(fecha_fin), ' ', trim(hora_fin)) AS timestamp)
            ) AS duracion_minutos
        FROM {source_table}
        WHERE nullif(trim(id), '') IS NOT NULL
          AND try_cast(concat(trim(fecha_inicio), ' ', trim(hora_inicio)) AS timestamp) IS NOT NULL
          AND try_cast(concat(trim(fecha_fin), ' ', trim(hora_fin)) AS timestamp) IS NOT NULL
          AND try_cast(concat(trim(fecha_fin), ' ', trim(hora_fin)) AS timestamp) > try_cast(concat(trim(fecha_inicio), ' ', trim(hora_inicio)) AS timestamp)
    """


def build_afectaciones_curated_sql(raw_table: str = "hive.raw.afectaciones_urbanas") -> str:
    """Construye la tabla Iceberg Silver completa a partir del CSV Bronze/raw validado (reemplazo total)."""
    return f"""
        CREATE TABLE lake.curated.afectaciones_urbanas
        WITH (format = 'PARQUET', location = 's3://curated/iceberg/afectaciones_urbanas/') AS
        {_afectaciones_curated_select_sql(raw_table)}
    """


def _afectaciones_exploitation_select_sql(curated_table: str = "lake.curated.afectaciones_urbanas") -> str:
    """Cuerpo SELECT del resumen de afectaciones (agregados de negocio que no se derivan del contrato)."""
    return f"""
        SELECT
            tipo_intervencion,
            tipo_afectacion,
            direccion,
            hour(fecha_hora_inicio) AS hora_inicio,
            count(*) AS total_afectaciones,
            avg(date_diff('minute', fecha_hora_inicio, fecha_hora_fin) / 60.0) AS duracion_media_horas,
            count_if(impacto_pmr = true) AS afectaciones_pmr
        FROM {curated_table}
        GROUP BY tipo_intervencion, tipo_afectacion, direccion, hour(fecha_hora_inicio)
    """


def build_afectaciones_exploitation_sql(curated_table: str = "lake.curated.afectaciones_urbanas") -> str:
    """Construye el mart individual de afectaciones, sin mezclar medidas de tráfico."""
    return f"""
        CREATE TABLE lake.analytics.afectaciones_urbanas_resumen
        WITH (format = 'PARQUET', location = 's3://analytics/iceberg/afectaciones_urbanas_resumen/') AS
        {_afectaciones_exploitation_select_sql(curated_table)}
    """


def build_afectaciones_trafico_mart_sql(
    afectaciones_table: str = "lake.curated.afectaciones_urbanas",
    trafico_table: str = "lake.curated.movilidad_trafico",
) -> str:
    """Construye el mart multidimensional con variación observada de tráfico por afectación."""
    return f"""
        CREATE TABLE lake.analytics.afectaciones_trafico
        WITH (format = 'PARQUET', location = 's3://analytics/iceberg/afectaciones_trafico/') AS
        WITH ventanas AS (
            SELECT
                a.row_id_tecnico AS id_afectacion,
                a.nombre,
                a.direccion,
                a.fecha_hora_inicio,
                a.fecha_hora_fin,
                a.tipo_intervencion,
                a.tipo_afectacion,
                a.impacto_pmr,
                date_diff('minute', a.fecha_hora_inicio, a.fecha_hora_fin) / 60.0 AS duracion_horas,
                greatest(3, CAST(ceil(date_diff('minute', a.fecha_hora_inicio, a.fecha_hora_fin) / 60.0) AS integer)) AS ventana_horas
            FROM {afectaciones_table} a
        ), flujos AS (
            SELECT
                v.*,
                avg(CASE WHEN try_cast(concat(CAST(t.fecha_inicio AS varchar), ' ', CAST(t.hora_inicio AS varchar)) AS timestamp) >= date_add('hour', -v.ventana_horas, v.fecha_hora_inicio)
                          AND try_cast(concat(CAST(t.fecha_inicio AS varchar), ' ', CAST(t.hora_inicio AS varchar)) AS timestamp) < v.fecha_hora_inicio THEN t.trafico_flujo END) AS flujo_antes,
                avg(CASE WHEN try_cast(concat(CAST(t.fecha_inicio AS varchar), ' ', CAST(t.hora_inicio AS varchar)) AS timestamp) >= v.fecha_hora_inicio
                          AND try_cast(concat(CAST(t.fecha_inicio AS varchar), ' ', CAST(t.hora_inicio AS varchar)) AS timestamp) <= v.fecha_hora_fin THEN t.trafico_flujo END) AS flujo_durante,
                avg(CASE WHEN try_cast(concat(CAST(t.fecha_inicio AS varchar), ' ', CAST(t.hora_inicio AS varchar)) AS timestamp) > v.fecha_hora_fin
                          AND try_cast(concat(CAST(t.fecha_inicio AS varchar), ' ', CAST(t.hora_inicio AS varchar)) AS timestamp) <= date_add('hour', v.ventana_horas, v.fecha_hora_fin) THEN t.trafico_flujo END) AS flujo_despues
            FROM ventanas v
                        LEFT JOIN {trafico_table} t ON t.direccion = v.direccion
                            AND try_cast(concat(CAST(t.fecha_inicio AS varchar), ' ', CAST(t.hora_inicio AS varchar)) AS timestamp) >= date_add('hour', -v.ventana_horas, v.fecha_hora_inicio)
                            AND try_cast(concat(CAST(t.fecha_inicio AS varchar), ' ', CAST(t.hora_inicio AS varchar)) AS timestamp) <= date_add('hour', v.ventana_horas, v.fecha_hora_fin)
            GROUP BY v.id_afectacion, v.nombre, v.direccion, v.fecha_hora_inicio, v.fecha_hora_fin,
                     v.tipo_intervencion, v.tipo_afectacion, v.impacto_pmr, v.duracion_horas, v.ventana_horas
        )
        SELECT *,
            CASE WHEN flujo_antes > 0 THEN round((flujo_durante - flujo_antes) * 100.0 / flujo_antes, 2) ELSE 0.0 END AS variacion_trafico_pct,
            CASE
                WHEN flujo_antes > 0 AND (flujo_durante - flujo_antes) * 100.0 / flujo_antes >= 50 THEN 'Crítico'
                WHEN flujo_antes > 0 AND (flujo_durante - flujo_antes) * 100.0 / flujo_antes >= 25 THEN 'Alto'
                WHEN flujo_antes > 0 AND (flujo_durante - flujo_antes) * 100.0 / flujo_antes >= 10 THEN 'Medio'
                ELSE 'Bajo'
            END AS impacto_estimado
        FROM flujos
    """


def build_afectaciones_trafico_mart() -> Dict[str, Any]:
    """Materializa el mart cruzado solo cuando ambas fuentes Curated están disponibles."""
    try:
        run_trino_statement("DROP TABLE IF EXISTS lake.analytics.afectaciones_trafico")
        run_trino_statement(build_afectaciones_trafico_mart_sql())
        return {"is_valid": True, "analytics_table": "lake.analytics.afectaciones_trafico"}
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)]}


def build_afectaciones_layers() -> Dict[str, Any]:
    """Crea las capas Iceberg tipada y descriptiva a partir de raw."""
    try:
        run_trino_statement("CREATE SCHEMA IF NOT EXISTS lake.curated")
        run_trino_statement("CREATE SCHEMA IF NOT EXISTS lake.analytics")
        run_trino_statement("DROP TABLE IF EXISTS lake.curated.afectaciones_urbanas")
        run_trino_statement("DROP TABLE IF EXISTS lake.analytics.afectaciones_urbanas_resumen")
        run_trino_statement(build_afectaciones_curated_sql())
        run_trino_statement(build_afectaciones_exploitation_sql())
        cross_mart = build_afectaciones_trafico_mart()
        return {
            "is_valid": True,
            "curated_table": "lake.curated.afectaciones_urbanas",
            "analytics_table": "lake.analytics.afectaciones_urbanas_resumen",
            "cross_mart": cross_mart,
        }
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)]}


def _curated_table_supports_incremental(qualified_table: str) -> bool:
    """True si la tabla ya existe con columna dataset_id (permite insertar sin reemplazar periodos previos).

    Si la tabla no existe, o existe pero es de un esquema anterior sin dataset_id
    (curated tables creadas antes de este cambio), se hace un único DROP + CREATE
    de migración y a partir de ahí ya queda en modo incremental.
    """
    try:
        run_trino_query(f"SELECT dataset_id FROM {qualified_table} LIMIT 1")
        return True
    except Exception:
        return False


def _movilidad_parking_exploitation_select_sql(curated_table: str = "lake.curated.movilidad_parking") -> str:
    """Resumen por parking: el genérico no aplica (no hay columnas de catálogo que agrupar),
    y lo que responde AQ_PARK es justo esto: % de tiempo saturado, por vía y franja."""
    return f"""
        SELECT
            direccion,
            franja_horaria,
            count(*) AS mediciones,
            count_if(saturado) AS mediciones_saturadas,
            round(count_if(saturado) * 100.0 / count(*), 1) AS pct_saturacion,
            round(avg(ocupacion_rate) * 100, 1) AS ocupacion_media_pct
        FROM {curated_table}
        GROUP BY direccion, franja_horaria
    """


# Resúmenes de negocio que no se pueden derivar automáticamente del contrato (agregados a
# medida, con lógica propia). Cualquier dataset que no aparezca aquí recibe un agregado
# genérico calculado a partir de sus propios metadatos (ver _auto_dimension_aggregation_sql).
DIMENSION_ANALYTICS_OVERRIDES: Dict[str, Any] = {
    "afectaciones_urbanas": _afectaciones_exploitation_select_sql,
    "gestion_afectaciones_urbanas": _afectaciones_exploitation_select_sql,
    "movilidad_parking": _movilidad_parking_exploitation_select_sql,
}


def _auto_dimension_aggregation_sql(contract: Dict[str, Any], curated_table: str) -> str:
    """Agregado genérico: cuenta y agrupa por cada columna de catálogo del contrato,
    y promedia las magnitudes numéricas declaradas (coordenadas excluidas)."""
    group_columns = list(contract.get("allowed_values", {}).keys())
    measure_columns = list(contract.get("numeric_columns", [])) + list(contract.get("non_negative_integer_columns", []))

    if not group_columns:
        return f"SELECT count(*) AS total_registros FROM {curated_table}"

    aggregates = ["count(*) AS total_registros"]
    aggregates.extend(f"avg({column}) AS {column}_medio" for column in measure_columns)

    return (
        f"SELECT {', '.join(group_columns)}, {', '.join(aggregates)} "
        f"FROM {curated_table} GROUP BY {', '.join(group_columns)}"
    )


def _dimension_curated_select_sql(contract: Dict[str, Any], source_table: str) -> str:
    """Construye el SELECT tipado de Capa 4 para cualquier dimensión a partir de su contrato.

    Además del casteo por tipo (coordenadas/numéricos, fechas, horas, enteros, booleanos),
    resuelve de forma declarativa las combinaciones fecha+hora en timestamp (timestamp_pairs)
    y la duración entre dos de esos timestamps (duration_minutes) — lo único que antes
    obligaba a afectaciones_urbanas a tener su propio SQL a mano.
    """
    coordinate_columns = set(contract.get("coordinate_columns", []))
    numeric_columns = set(contract.get("numeric_columns", []))
    date_columns = set(contract.get("date_columns", []))
    time_columns = set(contract.get("time_columns", []))
    integer_columns = set(contract.get("non_negative_integer_columns", []))
    boolean_columns = contract.get("boolean_columns", {})

    selected_columns = []
    for column in contract.get("columns", contract["required_columns"]):
        if column in coordinate_columns or column in numeric_columns:
            expression = f"try_cast(replace(nullif(trim({column}), ''), ',', '.') AS double) AS {column}"
        elif column in date_columns:
            expression = f"try_cast(nullif(trim({column}), '') AS date) AS {column}"
        elif column in time_columns:
            expression = f"try_cast(nullif(trim({column}), '') AS time) AS {column}"
        elif column in integer_columns:
            expression = f"try_cast(nullif(trim({column}), '') AS bigint) AS {column}"
        elif column in boolean_columns:
            true_values = ", ".join(f"'{value.lower()}'" for value in boolean_columns[column].get("true", []))
            false_values = ", ".join(f"'{value.lower()}'" for value in boolean_columns[column].get("false", []))
            expression = (
                f"CASE WHEN lower(trim({column})) IN ({true_values}) THEN true "
                f"WHEN lower(trim({column})) IN ({false_values}) THEN false ELSE NULL END AS {column}"
            )
        elif column == "direccion":
            # Convención compartida por todas las dimensiones con vía: minúsculas para que
            # los cruces entre dimensiones (p. ej. afectaciones x tráfico) casen por dirección.
            expression = f"nullif(lower(trim({column})), '') AS {column}"
        else:
            expression = f"nullif(trim({column}), '') AS {column}"
        selected_columns.append(expression)

    for date_column, time_column, output_name in contract.get("timestamp_pairs", []):
        selected_columns.append(
            f"try_cast(concat(trim({date_column}), ' ', trim({time_column})) AS timestamp) AS {output_name}"
        )

    duration = contract.get("duration_minutes")
    if duration:
        start_expr = f"concat(trim({duration['start_date']}), ' ', trim({duration['start_time']}))"
        end_expr = f"concat(trim({duration['end_date']}), ' ', trim({duration['end_time']}))"
        selected_columns.append(
            f"date_diff('minute', try_cast({start_expr} AS timestamp), try_cast({end_expr} AS timestamp)) "
            f"AS {duration['output']}"
        )

    time_features = contract.get("time_features")
    if time_features:
        # Genérico para cualquier serie temporal: día de la semana, fin de semana, franja
        # horaria (mismos tramos que _franja_horaria_desde_hora) y hora punta. Se usan para
        # agrupar en el cuadro de mando y como features de los modelos de capa 5.
        date_col, time_col = time_features
        date_expr = f"try_cast(nullif(trim({date_col}), '') AS date)"
        time_expr = f"try_cast(nullif(trim({time_col}), '') AS time)"
        selected_columns.append(
            f"CASE day_of_week({date_expr}) "
            "WHEN 1 THEN 'lunes' WHEN 2 THEN 'martes' WHEN 3 THEN 'miercoles' WHEN 4 THEN 'jueves' "
            "WHEN 5 THEN 'viernes' WHEN 6 THEN 'sabado' WHEN 7 THEN 'domingo' END AS dia_semana"
        )
        selected_columns.append(f"day_of_week({date_expr}) IN (6, 7) AS es_fin_de_semana")
        selected_columns.append(
            f"CASE WHEN hour({time_expr}) < 6 THEN '00:00-06:00' WHEN hour({time_expr}) < 9 THEN '06:00-09:00' "
            f"WHEN hour({time_expr}) < 13 THEN '09:00-13:00' WHEN hour({time_expr}) < 16 THEN '13:00-16:00' "
            f"WHEN hour({time_expr}) < 20 THEN '16:00-20:00' ELSE '20:00-24:00' END AS franja_horaria"
        )
        selected_columns.append(
            f"(hour({time_expr}) BETWEEN 6 AND 8 OR hour({time_expr}) BETWEEN 16 AND 19) AS hora_punta"
        )

    for output_name, expression in contract.get("computed_columns", {}).items():
        # Escape hatch genérico: cualquier fórmula que combine varias columnas (ratios,
        # indicadores) y no encaje en los casos ya declarativos de arriba.
        selected_columns.append(f"{expression} AS {output_name}")

    selected_columns.extend(["dataset_id", "row_id_tecnico"])
    return f"SELECT {', '.join(selected_columns)} FROM {source_table}"


def build_dimension_layer4(dataset: str, contract: Dict[str, Any], normalized_table: str) -> Dict[str, Any]:
    """Materializa Capa 4 en Iceberg desde la representación normalizada de Capa 3.

    Un único motor genérico sirve a cualquier dimensión del contrato, afectaciones_urbanas
    incluida: lo que antes era una rama de código aparte ahora es más metadatos en su
    contrato (timestamp_pairs, duration_minutes, boolean_columns). Lo único que sigue
    siendo específico es el agregado analítico de negocio de afectaciones (ver
    DIMENSION_ANALYTICS_OVERRIDES) y el cruce con tráfico, que no tienen equivalente genérico.

    La tabla curated es acumulativa entre entregas: cada periodo (dataset_id) se inserta
    sin tocar los periodos ya cargados. Si se vuelve a subir la misma entrega (mismo
    dataset_id, p. ej. una corrección), sus filas anteriores se sustituyen para no
    duplicar, pero el resto de periodos no se ve afectado.
    """
    curated_table = f"lake.curated.{dataset}" if dataset != "gestion_afectaciones_urbanas" else "lake.curated.afectaciones_urbanas"
    location_name = "afectaciones_urbanas" if dataset in ("afectaciones_urbanas", "gestion_afectaciones_urbanas") else dataset
    analytics_table = f"lake.analytics.{location_name}_resumen"

    curated_select_sql = _dimension_curated_select_sql(contract, normalized_table)

    analytics_override = DIMENSION_ANALYTICS_OVERRIDES.get(dataset)
    aggregation_select = (
        analytics_override(curated_table) if analytics_override else _auto_dimension_aggregation_sql(contract, curated_table)
    )
    analytics_sql = f"""
        CREATE TABLE {analytics_table}
        WITH (format = 'PARQUET', location = 's3://analytics/iceberg/{location_name}_resumen/') AS
        {aggregation_select}
    """

    def _rebuild_curated_from_scratch() -> None:
        run_trino_statement(f"DROP TABLE IF EXISTS {curated_table}")
        run_trino_statement(
            f"CREATE TABLE {curated_table} "
            f"WITH (format = 'PARQUET', location = 's3://curated/iceberg/{location_name}/') AS "
            f"{curated_select_sql}"
        )

    try:
        run_trino_statement("CREATE SCHEMA IF NOT EXISTS lake.curated")
        run_trino_statement("CREATE SCHEMA IF NOT EXISTS lake.analytics")

        if _curated_table_supports_incremental(curated_table):
            try:
                # Sustituye solo las filas de esta misma entrega (por si es un reintento o una corrección).
                run_trino_statement(
                    f"DELETE FROM {curated_table} WHERE dataset_id IN "
                    f"(SELECT DISTINCT dataset_id FROM {normalized_table})"
                )
                run_trino_statement(f"INSERT INTO {curated_table} {curated_select_sql}")
            except Exception:
                # El contrato cambió de forma incompatible con la tabla ya creada (p. ej. una
                # columna que antes se guardaba como texto ahora se declara numérica). Se
                # reconstruye una vez con el esquema nuevo; a partir de ahí vuelve a ser incremental.
                _rebuild_curated_from_scratch()
        else:
            # Primera vez para este dataset, o migración desde el esquema anterior sin dataset_id.
            _rebuild_curated_from_scratch()

        # El resumen analítico siempre se recalcula entero a partir de la curated acumulada.
        run_trino_statement(f"DROP TABLE IF EXISTS {analytics_table}")
        run_trino_statement(analytics_sql)

        cross_mart = None
        if dataset in ("afectaciones_urbanas", "gestion_afectaciones_urbanas", "movilidad_trafico"):
            # Específico de negocio: cruce nombrado entre estas dos dimensiones concretas,
            # no hay una noción genérica de "cruzar dimensión X con Y".
            cross_mart = build_afectaciones_trafico_mart()
        return {"is_valid": True, "curated_table": curated_table, "analytics_table": analytics_table}
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)]}


def register_hive_table_from_csv(
    filename: str,
    content_bytes: bytes,
    dataset_name: str = "afectaciones_urbanas",
    dimension: str = "afectaciones_urbanas",
    entity: str = "municipio_demo",
) -> Dict[str, Any]:
    """Crea/actualiza la tabla externa Hive leyendo la cabecera real del CSV, igual que hace ayuntamiento."""
    try:
        text = content_bytes.decode("utf-8", errors="replace")
        header_line = text.split("\n", 1)[0]
        dialect = None
        for sample in [header_line, text[:4096]]:
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                break
            except csv.Error:
                continue
        if dialect is None:
            return {"is_valid": False, "errors": ["No se detectó un delimitador válido para registrar la tabla Hive."]}

        raw_header = next(csv.reader(io.StringIO(header_line), dialect))
        columns = [re.sub(r"[^a-z0-9_]", "_", col.strip().lower()) or f"col_{i}" for i, col in enumerate(raw_header)]

        bucket_raw = os.getenv("S3_BUCKET_RAW", "raw")
        location = f"s3a://{bucket_raw}/{dimension}/{entity}/{dataset_name}/"
        col_defs = ", ".join(f"{name} varchar" for name in columns)

        run_trino_statement("CREATE SCHEMA IF NOT EXISTS hive.raw")
        run_trino_statement(f"DROP TABLE IF EXISTS hive.raw.{dataset_name}")
        run_trino_statement(
            f"CREATE TABLE hive.raw.{dataset_name} ({col_defs}) "
            f"WITH (external_location = '{location}', format = 'CSV', skip_header_line_count = 1)"
        )
        return {"is_valid": True, "table": f"hive.raw.{dataset_name}", "columns": columns}
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)]}


def publish_upload_to_lake(
    filename: str,
    content_bytes: bytes | None,
    dataset_name: str = "afectaciones_urbanas",
    dimension: str | None = None,
    entity: str = "municipio_demo",
) -> Dict[str, Any]:
    """Publica el archivo en S3 y replica localmente solo si se solicita."""
    if not filename:
        return {
            "is_valid": False,
            "errors": ["Falta el nombre del archivo."],
            "lake": {"root": str(get_lake_root())},
        }

    paths = get_lake_dataset_paths(dataset_name)
    staging_dir = paths["staging_dir"]
    raw_dir = paths["raw_dir"]
    payload = content_bytes if content_bytes is not None else b""
    dimension = dimension or DATASET_CONTRACTS.get(dataset_name, {}).get("dimension", dataset_name)
    bronze_key = f"{dimension}/{entity}/{dataset_name}/{Path(filename).stem}_csv"
    s3_result: Dict[str, Any] = {"enabled": False}
    if os.getenv("S3_ENDPOINT"):
        try:
            s3_result = {
                "enabled": True,
                "is_valid": True,
                **publish_upload_to_s3(filename, payload, dataset_name, dimension, entity),
            }
        except Exception as exc:
            s3_result = {"enabled": True, "is_valid": False, "errors": [str(exc)]}

    hive_result: Dict[str, Any] = {"enabled": False}
    if os.getenv("TRINO_HOST") and s3_result.get("is_valid"):
        hive_result = {
            "enabled": True,
            **register_hive_table_from_csv(filename, payload, dataset_name, dimension, entity),
        }

    local_mirror = os.getenv("LAKE_LOCAL_MIRROR", "false").lower() == "true"
    staging_path = staging_dir / filename
    if local_mirror:
        staging_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        staging_path.write_bytes(payload)

    return {
        "is_valid": True,
        "errors": [],
        "lake": {
            "root": str(paths["root"]),
            "dataset": dataset_name,
            "stage": "bronze",
            "staging_dir": str(staging_dir),
            "raw_dir": str(raw_dir),
            "filename": filename,
            "staging_path": str(staging_path),
            "staging_file_exists": staging_path.exists(),
            "local_mirror_enabled": local_mirror,
            "bronze_key": bronze_key,
            "s3": s3_result,
            "hive": hive_result,
        },
    }


def move_staged_file_to_raw(filename: str, dataset_name: str = "afectaciones_urbanas") -> Dict[str, Any]:
    """Mueve un archivo desde staging hacia raw para simular una transición de ingestión."""
    if not filename:
        return {
            "is_valid": False,
            "errors": ["Falta el nombre del archivo."],
            "lake": {"root": str(get_lake_root())},
        }

    paths = get_lake_dataset_paths(dataset_name)
    staging_dir = paths["staging_dir"]
    raw_dir = paths["raw_dir"]
    staging_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    staging_path = staging_dir / filename
    raw_path = raw_dir / filename

    if not staging_path.exists():
        return {
            "is_valid": False,
            "errors": [f"No existe el archivo en staging: {staging_path}"],
            "lake": {
                "root": str(paths["root"]),
                "dataset": dataset_name,
                "stage": "staging",
                "staging_dir": str(staging_dir),
                "raw_dir": str(raw_dir),
                "filename": filename,
            },
        }

    if raw_path.exists():
        raw_path.unlink()

    shutil.move(str(staging_path), str(raw_path))

    return {
        "is_valid": True,
        "errors": [],
        "lake": {
            "root": str(paths["root"]),
            "dataset": dataset_name,
            "stage": "raw",
            "staging_dir": str(staging_dir),
            "raw_dir": str(raw_dir),
            "filename": filename,
            "raw_path": str(raw_path),
            "raw_file_exists": raw_path.exists(),
        },
    }


RULE_DESCRIPTIONS: Dict[str, str] = {
    "CSV_EMPTY_FILE": "El fichero recibido no contiene contenido.",
    "CSV_ENCODING": "El fichero no se pudo decodificar como texto.",
    "CSV_DELIMITER": "No se pudo determinar el delimitador del CSV.",
    "CSV_REQUIRED_COLUMNS": "Faltan columnas obligatorias definidas en el contrato del dataset.",
    "CSV_COLUMN_COUNT": "Alguna fila tiene un número de columnas distinto al de la cabecera.",
    "CSV_EXTRA_COLUMNS": "El fichero incluye columnas no declaradas en el contrato; se conservan sin bloquear la ingesta.",
    "CSV_REQUIRED_VALUE": "Un campo obligatorio del contrato está vacío.",
    "CSV_DATE_FORMAT": "El valor de un campo de fecha no cumple el formato YYYY-MM-DD.",
    "CSV_TIME_FORMAT": "El valor de un campo de hora no cumple el formato HH:MM.",
    "CSV_TEMPORAL_COHERENCE": "La fecha/hora de fin no es posterior a la de inicio.",
    "CSV_CATALOG_VALUE": "El valor de un campo categórico no pertenece al catálogo permitido.",
    "CSV_COORDINATE_FORMAT": "Una coordenada geográfica está fuera de rango o no es numérica.",
    "CSV_NON_NEGATIVE_INTEGER": "Un campo numérico de negocio no es un entero no negativo.",
    "CSV_FIELD_RELATION": "Dos campos relacionados incumplen la relación de negocio esperada (p. ej. libres > total).",
    "CSV_STRUCTURE": "Incidencia estructural no clasificada en una regla específica.",
    "CSV_DECLARED_PERIOD_MISMATCH": "Una fecha del fichero no pertenece al año/periodo declarado en la entrega.",
}


def compute_consolidation_window(anio: int | None, period: str) -> tuple[str | None, str | None]:
    """Deriva la ventana de consolidación (fechas de inicio/fin) a partir del año y periodo
    declarados en la entrega. Solo sabe resolver Anual y Trimestre N; cualquier otro periodo
    (o la ausencia de año) no permite determinar una ventana y se deja sin resolver."""
    if anio is None:
        return None, None
    normalized = (period or "").strip().lower()
    if normalized == "anual":
        return f"{anio}-01-01", f"{anio}-12-31"
    quarter_match = re.fullmatch(r"trimestre\s+([1-4])", normalized)
    if quarter_match:
        quarter = int(quarter_match.group(1))
        start_month = (quarter - 1) * 3 + 1
        end_month = start_month + 2
        last_day = calendar.monthrange(anio, end_month)[1]
        return f"{anio}-{start_month:02d}-01", f"{anio}-{end_month:02d}-{last_day:02d}"
    return None, None


def validate_csv_text(
    csv_text: str,
    required_columns: List[str],
    business_rules: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Valida un CSV en texto plano y devuelve errores si falla."""
    errors: List[str] = []
    incidencias: List[Dict[str, Any]] = []
    row_count = 0
    business_rules = business_rules or AFFECTACIONES_DATA_CONTRACT
    required_value_columns = business_rules.get("required_value_columns", required_columns)
    validation_id = uuid.uuid4().hex

    def build_incident(
        code: str,
        tipo_regla: str,
        message: str,
        field: str | None = None,
        source_row: int | None = None,
    ) -> Dict[str, Any]:
        technical_row_id = None
        if source_row and source_row > 1:
            payload = csv_text.encode("utf-8") if isinstance(csv_text, str) else csv_text
            content_hash = hashlib.sha256(payload).hexdigest()
            technical_row_id = hashlib.sha256(f"{content_hash}:{source_row - 1}".encode("utf-8")).hexdigest()
        return {
            "codigo_regla": code,
            "descripcion_regla": RULE_DESCRIPTIONS.get(code, message),
            "tipo_regla": tipo_regla,
            "campo_afectado": field,
            "row_number_origen": source_row,
            "row_id_tecnico": technical_row_id,
            "descripcion_incidencia": message,
            "severidad": "critica" if tipo_regla == "bloqueante" else "advertencia",
            "accion_requerida": "corregir_y_reenviar" if tipo_regla == "bloqueante" else "revisar",
        }

    def build_validation_metadata(evaluated_rows: int, incidents: List[Dict[str, Any]]) -> Dict[str, Any]:
        hay_bloqueantes = any(incident["tipo_regla"] == "bloqueante" for incident in incidents)
        filas_con_error = {
            incident["row_number_origen"] for incident in incidents
            if incident["tipo_regla"] == "bloqueante" and incident.get("row_number_origen")
        }
        filas_con_incidencia = {
            incident["row_number_origen"] for incident in incidents
            if incident["tipo_regla"] == "no_bloqueante" and incident.get("row_number_origen")
        }
        if hay_bloqueantes:
            resultado = "rechazado"
        elif incidents:
            resultado = "aceptado_con_incidencias"
        else:
            resultado = "apto_para_ingesta"
        return {
            "validacion_id": validation_id,
            "fecha_hora_validacion": datetime.now(timezone.utc).isoformat(),
            "regla_validacion_version": business_rules.get("validation_rule_version", "capa2-v1"),
            "resultado_validacion": resultado,
            "numero_registros_evaluados": evaluated_rows,
            "numero_registros_con_error": len(filas_con_error),
            "numero_registros_con_incidencia": len(filas_con_incidencia),
            "numero_registros_validos": evaluated_rows if not hay_bloqueantes else 0,
            "fecha_inicio_ventana_consolidacion": None,
            "fecha_fin_ventana_consolidacion": None,
            "estado_ciclo_vida_dataset": "en_consolidacion",
        }

    # Revisa si el contenido recibido está vacío antes de intentar procesarlo.
    if not csv_text or not csv_text.strip():
        message = "El archivo está vacío."
        incident = build_incident("CSV_EMPTY_FILE", "bloqueante", message)
        return {"is_valid": False, "errors": [message], "incidencias": [incident], "row_count": 0, "validation_status": "rejected", "validation_metadata": build_validation_metadata(0, [incident])}

    try:
        # Intenta convertir el contenido a texto UTF-8, con un fallback a Latin-1.
        text = csv_text.decode("utf-8") if isinstance(csv_text, bytes) else csv_text
    except UnicodeDecodeError:
        try:
            text = csv_text.decode("latin-1") if isinstance(csv_text, bytes) else csv_text
        except UnicodeDecodeError:
            message = "No se pudo leer el archivo. Asegúrate de que esté en UTF-8 o Latin-1."
            incident = build_incident("CSV_ENCODING", "bloqueante", message)
            return {"is_valid": False, "errors": [message], "incidencias": [incident], "row_count": 0, "validation_status": "rejected", "validation_metadata": build_validation_metadata(0, [incident])}

    # Detecta el delimitador más probable para leer el CSV de forma robusta.
    header_line = text.split("\n", 1)[0]
    dialect = None
    for sample in [header_line, text[:4096]]:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            break
        except csv.Error:
            continue

    if dialect is None:
        message = "No se detectó un delimitador válido (coma, punto y coma, tabulador o pipe)."
        incident = build_incident("CSV_DELIMITER", "bloqueante", message)
        return {"is_valid": False, "errors": [message], "incidencias": [incident], "row_count": 0, "validation_status": "rejected", "validation_metadata": build_validation_metadata(0, [incident])}

    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)

    if not rows:
        message = "El archivo no contiene ninguna fila."
        incident = build_incident("CSV_EMPTY_FILE", "bloqueante", message)
        return {"is_valid": False, "errors": [message], "incidencias": [incident], "row_count": 0, "validation_status": "rejected", "validation_metadata": build_validation_metadata(0, [incident])}

    header = rows[0]
    if not header or all(h.strip() == "" for h in header):
        errors.append("La primera fila (cabecera) está vacía.")

    if len(rows) < 2:
        errors.append("El archivo no contiene filas de datos (solo cabecera).")

    # Comprueba si alguna fila tiene un número de columnas distinto al de la cabecera.
    if len(header) > 1:
        col_lengths = [len(r) for r in rows[1:] if r]
        if col_lengths and max(col_lengths) != len(header):
            errors.append(
                f"Número de columnas inconsistente: cabecera={len(header)}, algunas filas tienen {max(col_lengths)}."
            )

    expected_columns = [column for column in required_columns if column not in header]
    if expected_columns:
        errors.append(f"Faltan columnas requeridas: {', '.join(expected_columns)}")

    declared_columns = business_rules.get("columns", required_columns)
    extra_columns = [column for column in header if column not in declared_columns]
    if extra_columns:
        incidencias.append(build_incident(
            "CSV_EXTRA_COLUMNS",
            "no_bloqueante",
            f"Columnas adicionales conservadas: {', '.join(extra_columns)}",
        ))

    # Revisa cada fila de datos para detectar campos obligatorios vacíos.
    for index, row in enumerate(rows[1:], start=2):
        row_count += 1
        for column in required_value_columns:
            if column not in header:
                continue
            value = (row[header.index(column)] if header.index(column) < len(row) else "").strip()
            if not value:
                errors.append(f"Fila {index}: el campo '{column}' está vacío")
                break

        if not errors or not any(error.startswith(f"Fila {index}") for error in errors):
            for column in business_rules.get("date_columns", []):
                if column not in header:
                    continue
                value = (row[header.index(column)] if header.index(column) < len(row) else "").strip()
                if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    errors.append(f"Fila {index}: Formato de fecha inválido para '{column}'")
                    break

            for column in business_rules.get("time_columns", []):
                if column not in header:
                    continue
                value = (row[header.index(column)] if header.index(column) < len(row) else "").strip()
                if value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                    errors.append(f"Fila {index}: Formato de hora inválido para '{column}'")
                    break

            if business_rules.get("temporal_coherence"):
                temporal_columns = {column: header.index(column) for column in ["fecha_inicio", "hora_inicio", "fecha_fin", "hora_fin"] if column in header}
                if len(temporal_columns) == 4:
                    try:
                        start = datetime.strptime(
                            f"{row[temporal_columns['fecha_inicio']].strip()} {row[temporal_columns['hora_inicio']].strip()}",
                            "%Y-%m-%d %H:%M",
                        )
                        end = datetime.strptime(
                            f"{row[temporal_columns['fecha_fin']].strip()} {row[temporal_columns['hora_fin']].strip()}",
                            "%Y-%m-%d %H:%M",
                        )
                        if end <= start:
                            errors.append(f"Fila {index}: La fecha y hora de fin debe ser posterior al inicio")
                    except ValueError:
                        pass

            for column, allowed_values in business_rules.get("allowed_values", {}).items():
                if column not in header:
                    continue
                value = (row[header.index(column)] if header.index(column) < len(row) else "").strip()
                if value and normalize_catalog_value(value) not in [normalize_catalog_value(item) for item in allowed_values]:
                    errors.append(f"Fila {index}: el valor '{value}' para '{column}' no está permitido")
                    break

            for column in business_rules.get("coordinate_columns", []):
                if column not in header:
                    continue
                value = (row[header.index(column)] if header.index(column) < len(row) else "").strip()
                if not value:
                    continue
                try:
                    coordinate = float(value.replace(",", "."))
                    limit = 90 if column == "latitud" else 180
                    if not -limit <= coordinate <= limit:
                        raise ValueError
                except ValueError:
                    errors.append(f"Fila {index}: coordenada inválida para '{column}'")
                    break

            for column in business_rules.get("non_negative_integer_columns", []):
                if column not in header:
                    continue
                value = (row[header.index(column)] if header.index(column) < len(row) else "").strip()
                if value and (not value.isdigit() or int(value) < 0):
                    errors.append(f"Fila {index}: el campo '{column}' debe ser un entero no negativo")
                    break

            for lower_column, upper_column in business_rules.get("less_or_equal_rules", []):
                if lower_column not in header or upper_column not in header:
                    continue
                lower_value = (row[header.index(lower_column)] if header.index(lower_column) < len(row) else "").strip()
                upper_value = (row[header.index(upper_column)] if header.index(upper_column) < len(row) else "").strip()
                if lower_value and upper_value:
                    try:
                        if int(lower_value) > int(upper_value):
                            errors.append(f"Fila {index}: '{lower_column}' no puede ser mayor que '{upper_column}'")
                            break
                    except ValueError:
                        pass

            for start_column, end_column in business_rules.get("optional_date_order_rules", []):
                if start_column not in header or end_column not in header:
                    continue
                start_value = (row[header.index(start_column)] if header.index(start_column) < len(row) else "").strip()
                end_value = (row[header.index(end_column)] if header.index(end_column) < len(row) else "").strip()
                if start_value and end_value:
                    try:
                        if datetime.strptime(end_value, "%Y-%m-%d") < datetime.strptime(start_value, "%Y-%m-%d"):
                            errors.append(f"Fila {index}: '{end_column}' debe ser posterior o igual a '{start_column}'")
                            break
                    except ValueError:
                        pass

    for error in errors:
        row_match = re.search(r"Fila (\d+)", error)
        source_row = int(row_match.group(1)) if row_match else None
        field_match = re.search(r"'([^']+)'", error)
        field = field_match.group(1) if field_match else None
        if "Faltan columnas" in error:
            code = "CSV_REQUIRED_COLUMNS"
        elif "Número de columnas" in error:
            code = "CSV_COLUMN_COUNT"
        elif "Formato de fecha" in error:
            code = "CSV_DATE_FORMAT"
        elif "Formato de hora" in error:
            code = "CSV_TIME_FORMAT"
        elif "posterior al inicio" in error:
            code = "CSV_TEMPORAL_COHERENCE"
        elif "no está permitido" in error:
            code = "CSV_CATALOG_VALUE"
        elif "coordenada inválida" in error:
            code = "CSV_COORDINATE_FORMAT"
        elif "entero no negativo" in error:
            code = "CSV_NON_NEGATIVE_INTEGER"
        elif "no puede ser mayor" in error:
            code = "CSV_FIELD_RELATION"
        elif "está vacío" in error:
            code = "CSV_REQUIRED_VALUE"
        else:
            code = "CSV_STRUCTURE"
        incidencias.append(build_incident(code, "bloqueante", error, field, source_row))

    validation_status = "rejected" if errors else "accepted_with_warnings" if incidencias else "ready_for_ingestion"
    return {
        "is_valid": not errors,
        "errors": errors,
        "incidencias": incidencias,
        "row_count": row_count,
        "validation_status": validation_status,
        "validation_metadata": build_validation_metadata(row_count, incidencias),
    }


def validate_delivery_period(
    content_bytes: bytes,
    contract: Dict[str, Any],
    anio: int | None,
    period: str,
) -> Dict[str, Any]:
    """Comprueba que las fechas funcionales pertenecen al año y trimestre declarados."""
    date_columns = contract.get("date_columns", [])
    if anio is None or not date_columns:
        return {"is_valid": True, "errors": [], "incidencias": []}

    parsed = parse_csv_rows(content_bytes)
    if not parsed["is_valid"]:
        return {"is_valid": False, "errors": parsed["errors"], "incidencias": []}

    quarter_match = re.fullmatch(r"Trimestre\s+([1-4])", (period or "").strip(), flags=re.IGNORECASE)
    expected_quarter = int(quarter_match.group(1)) if quarter_match else None
    for row_number, row in enumerate(parsed["rows"], start=2):
        for column in date_columns:
            value = (row.get(column) or "").strip()
            if not value:
                continue
            try:
                date_value = datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                continue
            if date_value.year != anio:
                message = f"Fila {row_number}: la fecha '{value}' de '{column}' no pertenece al año declarado {anio}."
            elif expected_quarter and ((date_value.month - 1) // 3 + 1) != expected_quarter:
                message = f"Fila {row_number}: la fecha '{value}' de '{column}' no pertenece a {period}."
            else:
                continue
            return {
                "is_valid": False,
                "errors": [message],
                "incidencias": [{
                    "codigo_regla": "CSV_DECLARED_PERIOD_MISMATCH",
                    "descripcion_regla": RULE_DESCRIPTIONS["CSV_DECLARED_PERIOD_MISMATCH"],
                    "tipo_regla": "bloqueante",
                    "campo_afectado": column,
                    "row_number_origen": row_number,
                    "row_id_tecnico": None,
                    "descripcion_incidencia": message,
                    "severidad": "critica",
                    "accion_requerida": "corregir_periodo_o_reenviar",
                }],
            }
    return {"is_valid": True, "errors": [], "incidencias": []}


def normalize_dimension_dataset(
    content_bytes: bytes,
    dataset: str,
    contract: Dict[str, Any],
    entity: str = "municipio_demo",
    period: str = "",
    schema_version: str = "v1",
) -> Dict[str, Any]:
    """Construye la representación canónica de Capa 3 para una entrega aceptada por Capa 2."""
    validation = validate_csv_text(
        content_bytes,
        required_columns=contract["required_columns"],
        business_rules=contract,
    )
    if not validation["is_valid"]:
        return {
            "is_valid": False,
            "status": "rejected",
            "errors": validation["errors"],
            "incidencias": validation["incidencias"],
            "validation_metadata": validation["validation_metadata"],
            "control": None,
            "business_rows": [],
        }

    parsed = parse_csv_rows(content_bytes)
    if not parsed["is_valid"]:
        return {
            "is_valid": False,
            "status": "rejected",
            "errors": parsed["errors"],
            "incidencias": [],
            "validation_metadata": validation["validation_metadata"],
            "control": None,
            "business_rows": [],
        }

    dataset_id = build_delivery_key(entity, contract["dimension"], dataset, period, schema_version)
    content_hash = hashlib.sha256(content_bytes).hexdigest()
    business_rows = []
    for row_number, row in enumerate(parsed["rows"], start=1):
        normalized_row = {
            normalize_contract_header(column): re.sub(r"[\r\n]+", " ", (value or "")).strip()
            for column, value in row.items()
        }
        for column in contract.get("allowed_values", {}):
            if normalized_row.get(column):
                normalized_row[column] = normalize_catalog_value(normalized_row[column])
        business_row = {
            column: normalized_row[column]
            for column in contract.get("columns", contract["required_columns"])
            if column in normalized_row
        }
        business_row["dataset_id"] = dataset_id
        business_row["row_id_tecnico"] = hashlib.sha256(
            f"{content_hash}:{row_number}".encode("utf-8")
        ).hexdigest()
        business_rows.append(business_row)

    return {
        "is_valid": True,
        "status": "normalized",
        "errors": [],
        "incidencias": validation["incidencias"],
        "validation_metadata": validation["validation_metadata"],
        "control": {
            "dataset_id": dataset_id,
            "dimension": contract["dimension"],
            "dataset": dataset,
            "entity": entity,
            "period": period,
            "schema_version": schema_version,
            "validacion_id": validation["validation_metadata"]["validacion_id"],
        },
        "business_rows": business_rows,
    }


def record_pipeline_stage(delivery_id: int | None, layer: int, stage: str, status: str, details: Dict[str, Any] | None = None) -> None:
    """Guarda el último resultado verificable de cada capa para una entrega."""
    if delivery_id is None:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ingesta_progreso_capas (
            id SERIAL PRIMARY KEY,
            delivery_id INTEGER NOT NULL,
            layer INTEGER NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (delivery_id, layer)
        )
        """
    )
    cur.execute(
        """
        INSERT INTO ingesta_progreso_capas (delivery_id, layer, stage, status, details)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (delivery_id, layer) DO UPDATE SET
            stage = EXCLUDED.stage,
            status = EXCLUDED.status,
            details = EXCLUDED.details,
            updated_at = NOW()
        """,
        (delivery_id, layer, stage, status, json.dumps(details or {}, default=str)),
    )
    conn.commit()
    cur.close()
    conn.close()


def publish_normalized_dataset(
    dataset: str,
    contract: Dict[str, Any],
    business_rows: List[Dict[str, Any]],
    entity: str,
    delivery_id: int,
) -> Dict[str, Any]:
    """Publica la salida canónica de Capa 3 y la registra como tabla externa Hive."""
    try:
        import boto3

        fieldnames = list(contract.get("columns", contract["required_columns"])) + ["dataset_id", "row_id_tecnico"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(business_rows)
        payload = output.getvalue().encode("utf-8")
        dimension = contract["dimension"]
        bucket = os.getenv("S3_BUCKET_CURATED", "curated")
        prefix = f"_normalized/{dimension}/{entity}/{dataset}/{delivery_id}"
        key = f"{prefix}/normalized.csv"
        client = boto3.client(
            "s3",
            endpoint_url=os.getenv("S3_ENDPOINT"),
            aws_access_key_id=os.getenv("S3_ACCESS_KEY"),
            aws_secret_access_key=os.getenv("S3_SECRET_KEY"),
            region_name=os.getenv("S3_REGION", "us-east-1"),
        )
        client.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="text/csv")

        table = f"hive.normalized.{dataset}"
        columns_sql = ", ".join(f"{column} varchar" for column in fieldnames)
        run_trino_statement("CREATE SCHEMA IF NOT EXISTS hive.normalized")
        run_trino_statement(f"DROP TABLE IF EXISTS {table}")
        run_trino_statement(
            f"CREATE TABLE {table} ({columns_sql}) WITH ("
            f"external_location = 's3a://{bucket}/{prefix}/', format = 'CSV', skip_header_line_count = 1)"
        )
        return {
            "is_valid": True,
            "status": "published",
            "table": table,
            "row_count": len(business_rows),
            "uri": f"s3://{bucket}/{key}",
        }
    except Exception as exc:
        return {"is_valid": False, "status": "error", "errors": [str(exc)]}


def run_automatic_dimension_pipeline(
    filename: str,
    content_bytes: bytes,
    dataset: str,
    entity: str = "municipio_demo",
    period: str = "",
    schema_version: str = "v1",
    replace: bool = False,
    entry_channel: str = "web_manual",
    sender: str = "usuario_local",
    municipio_id: str | None = None,
    anio: int | None = None,
    fecha_hora_inicio_remision: datetime | None = None,
    numero_intento_carga: int = 1,
) -> Dict[str, Any]:
    """Ejecuta secuencialmente las capas 0 a 4 y detiene el flujo ante bloqueos."""
    contract = DATASET_CONTRACTS.get(dataset)
    if contract is None:
        return {"is_valid": False, "status": "rejected", "errors": ["Dataset no permitido"], "pipeline": []}
    effective_entity = municipio_id or entity
    preservation = preserve_dimension_delivery(
        filename,
        content_bytes,
        dataset,
        entity=effective_entity,
        period=period,
        schema_version=schema_version,
        replace=replace,
        entry_channel=entry_channel,
        sender=sender,
        municipio_id=municipio_id,
        anio=anio,
        fecha_hora_inicio_remision=fecha_hora_inicio_remision,
        numero_intento_carga=numero_intento_carga,
    )
    if not preservation.get("is_valid") or preservation.get("decision") == "duplicate":
        return {**preservation, "pipeline": []}

    delivery_id = preservation.get("delivery_id")
    pipeline = [
        {"layer": 0, "stage": "reception", "status": "accepted"},
        {"layer": 1, "stage": "preservation", "status": "preserved"},
    ]
    record_pipeline_stage(delivery_id, 0, "reception", "accepted")
    record_pipeline_stage(delivery_id, 1, "preservation", "preserved")

    validation = validate_csv_text(content_bytes, required_columns=contract["required_columns"], business_rules=contract)
    if validation["is_valid"]:
        period_validation = validate_delivery_period(content_bytes, contract, anio, period)
        if not period_validation["is_valid"]:
            validation["is_valid"] = False
            validation["validation_status"] = "rejected"
            validation["errors"].extend(period_validation["errors"])
            validation["incidencias"].extend(period_validation["incidencias"])
    ventana_inicio, ventana_fin = compute_consolidation_window(anio, period)
    validation["validation_metadata"]["fecha_inicio_ventana_consolidacion"] = ventana_inicio
    validation["validation_metadata"]["fecha_fin_ventana_consolidacion"] = ventana_fin
    validacion_id = validation["validation_metadata"].get("validacion_id")
    persist_validation_incidents(delivery_id, validation.get("incidencias", []), validacion_id)
    persist_validation_run(delivery_id, dataset, contract["dimension"], validation["validation_metadata"])
    validation_status = validation["validation_status"]
    pipeline.append({"layer": 2, "stage": "validation", "status": validation_status})
    record_pipeline_stage(delivery_id, 2, "validation", validation_status, {
        "row_count": validation.get("row_count", 0),
        "incidencias": validation.get("incidencias", []),
    })
    if not validation["is_valid"]:
        update_delivery_status(delivery_id, "rejected")
        return {
            **preservation,
            "is_valid": False,
            "status": "rejected",
            "stopped_at": "layer2",
            "errors": validation["errors"],
            "incidencias": validation.get("incidencias", []),
            "row_count": validation.get("row_count", 0),
            "pipeline": pipeline,
        }

    normalized = normalize_dimension_dataset(content_bytes, dataset, contract, effective_entity, period, schema_version)
    if not normalized["is_valid"]:
        pipeline.append({"layer": 3, "stage": "normalization", "status": "error"})
        record_pipeline_stage(delivery_id, 3, "normalization", "error", {"errors": normalized.get("errors", [])})
        update_delivery_status(delivery_id, "layer3_error")
        return {**preservation, "is_valid": False, "status": "partial", "stopped_at": "layer3", "errors": normalized.get("errors", []), "pipeline": pipeline}
    if normalized.get("control"):
        persist_dataset_control(normalized["control"], delivery_id, len(normalized["business_rows"]))

    normalized_publication = publish_normalized_dataset(dataset, contract, normalized["business_rows"], effective_entity, delivery_id)
    layer3_status = "normalized" if normalized_publication["is_valid"] else "error"
    pipeline.append({"layer": 3, "stage": "normalization", "status": layer3_status})
    record_pipeline_stage(delivery_id, 3, "normalization", layer3_status, normalized_publication)
    if not normalized_publication["is_valid"]:
        update_delivery_status(delivery_id, "layer3_error")
        return {**preservation, "is_valid": False, "status": "partial", "stopped_at": "layer3", "errors": normalized_publication.get("errors", []), "pipeline": pipeline}

    operational_store = {"is_valid": True, "inserted": 0, "errors": []}
    row_ids = [row["row_id_tecnico"] for row in normalized["business_rows"]]
    if dataset in ("afectaciones_urbanas", "gestion_afectaciones_urbanas"):
        operational_store = persist_affectaciones(normalized["business_rows"], preservation.get("logical_key"), row_ids)
    elif dataset == "control_gestion_its":
        operational_store = persist_its(normalized["business_rows"], preservation.get("logical_key"), row_ids)

    layer4 = build_dimension_layer4(dataset, contract, normalized_publication["table"])
    layer4_status = "published" if layer4["is_valid"] else "error"
    pipeline.append({"layer": 4, "stage": "curated_analytics", "status": layer4_status})
    record_pipeline_stage(delivery_id, 4, "curated_analytics", layer4_status, layer4)
    if not layer4["is_valid"]:
        update_delivery_status(delivery_id, "layer4_error")
        return {**preservation, "is_valid": False, "status": "partial", "stopped_at": "layer4", "errors": layer4.get("errors", []), "pipeline": pipeline}
    mark_dataset_ingested(validacion_id)

    final_status = "completed_with_warnings" if validation_status == "accepted_with_warnings" else "completed"
    update_delivery_status(delivery_id, final_status)
    return {
        **preservation,
        "is_valid": True,
        "status": final_status,
        "errors": operational_store.get("errors", []),
        "incidencias": validation.get("incidencias", []),
        "row_count": validation.get("row_count", 0),
        "inserted": operational_store.get("inserted", 0),
        "summary": {
            "rows_received": validation.get("row_count", 0),
            "rows_inserted": operational_store.get("inserted", 0),
            "errors_count": len(operational_store.get("errors", [])),
        },
        "pipeline": pipeline,
        "normalization": {"row_count": len(normalized["business_rows"]), "publication": normalized_publication},
        "layer4": layer4,
    }


def persist_validation_incidents(delivery_id: int | None, incidents: List[Dict[str, Any]], validacion_id: str | None = None) -> None:
    """Guarda el informe de incidencias de validación vinculado a la entrega de Capa 0."""
    if delivery_id is None or not incidents:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ingesta_incidencias'")
    existing_columns = {row[0] for row in cur.fetchall()}
    if existing_columns and "codigo_regla" not in existing_columns:
        # Esquema anterior (rule_code/severity/message/...): la tabla es un log de
        # incidencias sin valor de negocio que reconstruir, se recrea con el esquema nuevo.
        cur.execute("DROP TABLE ingesta_incidencias")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ingesta_incidencias (
            id SERIAL PRIMARY KEY,
            delivery_id INTEGER NOT NULL,
            validacion_id TEXT,
            codigo_regla TEXT NOT NULL DEFAULT 'CSV_STRUCTURE',
            descripcion_regla TEXT,
            tipo_regla TEXT NOT NULL DEFAULT 'bloqueante',
            severidad TEXT NOT NULL DEFAULT 'critica',
            campo_afectado TEXT,
            row_number_origen INTEGER,
            row_id_tecnico TEXT,
            descripcion_incidencia TEXT NOT NULL,
            accion_requerida TEXT NOT NULL DEFAULT 'revisar',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    for incident in incidents:
        cur.execute(
            """
            INSERT INTO ingesta_incidencias (
                delivery_id, validacion_id, codigo_regla, descripcion_regla, tipo_regla, severidad,
                campo_afectado, row_number_origen, row_id_tecnico, descripcion_incidencia, accion_requerida
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                delivery_id,
                validacion_id,
                incident.get("codigo_regla", "CSV_STRUCTURE"),
                incident.get("descripcion_regla"),
                incident.get("tipo_regla", "bloqueante"),
                incident.get("severidad", "critica"),
                incident.get("campo_afectado"),
                incident.get("row_number_origen"),
                incident.get("row_id_tecnico"),
                incident.get("descripcion_incidencia", ""),
                incident.get("accion_requerida", "revisar"),
            ),
        )
    conn.commit()
    cur.close()
    conn.close()


def persist_validation_run(delivery_id: int | None, dataset: str, dimension: str, metadata: Dict[str, Any]) -> None:
    """Guarda un registro por cada ejecución de Capa 2 (una fila = una validación completa),
    separado del detalle por incidencia que guarda persist_validation_incidents."""
    if delivery_id is None:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS capa2_validaciones (
            id SERIAL PRIMARY KEY,
            validacion_id TEXT NOT NULL UNIQUE,
            delivery_id INTEGER NOT NULL,
            dataset TEXT NOT NULL,
            dimension TEXT NOT NULL,
            regla_validacion_version TEXT,
            fecha_hora_validacion TIMESTAMPTZ NOT NULL,
            resultado_validacion TEXT NOT NULL,
            numero_registros_evaluados INTEGER NOT NULL DEFAULT 0,
            numero_registros_con_error INTEGER NOT NULL DEFAULT 0,
            numero_registros_con_incidencia INTEGER NOT NULL DEFAULT 0,
            numero_registros_validos INTEGER NOT NULL DEFAULT 0,
            fecha_inicio_ventana_consolidacion DATE,
            fecha_fin_ventana_consolidacion DATE,
            estado_ciclo_vida_dataset TEXT NOT NULL DEFAULT 'en_consolidacion'
        )
        """
    )
    cur.execute(
        """
        INSERT INTO capa2_validaciones (
            validacion_id, delivery_id, dataset, dimension, regla_validacion_version,
            fecha_hora_validacion, resultado_validacion, numero_registros_evaluados,
            numero_registros_con_error, numero_registros_con_incidencia, numero_registros_validos,
            fecha_inicio_ventana_consolidacion, fecha_fin_ventana_consolidacion, estado_ciclo_vida_dataset
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (validacion_id) DO NOTHING
        """,
        (
            metadata.get("validacion_id"),
            delivery_id,
            dataset,
            dimension,
            metadata.get("regla_validacion_version"),
            metadata.get("fecha_hora_validacion"),
            metadata.get("resultado_validacion"),
            metadata.get("numero_registros_evaluados", 0),
            metadata.get("numero_registros_con_error", 0),
            metadata.get("numero_registros_con_incidencia", 0),
            metadata.get("numero_registros_validos", 0),
            metadata.get("fecha_inicio_ventana_consolidacion"),
            metadata.get("fecha_fin_ventana_consolidacion"),
            metadata.get("estado_ciclo_vida_dataset", "en_consolidacion"),
        ),
    )
    conn.commit()
    cur.close()
    conn.close()


def list_ingest_deliveries(
    municipio_id: str | None = None,
    dataset: str | None = None,
    status_filter: str | None = None,
    limit: int = 200,
) -> list[Dict[str, Any]]:
    """Supervisión de ingestas para el panel de administración: cruza `ingesta_entregas`
    (Capa 0) con `capa2_validaciones` (Capa 2) para dar, por entrega, su estado de
    recepción y el resultado de validación si ya se calculó."""
    conn = get_db_connection()
    cur = conn.cursor()
    filters = []
    params: list[Any] = []
    if municipio_id:
        filters.append("e.municipio_id = %s")
        params.append(municipio_id)
    if dataset:
        filters.append("e.dataset = %s")
        params.append(dataset)
    if status_filter:
        filters.append("e.status = %s")
        params.append(status_filter)
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.append(limit)
    cur.execute(
        f"""
        SELECT
            e.id, e.entity, e.dimension, e.dataset, e.period, e.municipio_id, e.sender,
            e.status, e.received_at, e.indicador_conflicto, e.decision_sobre_conflicto,
            v.resultado_validacion, v.numero_registros_con_error, v.numero_registros_con_incidencia,
            v.numero_registros_validos, v.estado_ciclo_vida_dataset
        FROM ingesta_entregas e
        LEFT JOIN capa2_validaciones v ON v.delivery_id = e.id
        {where_clause}
        ORDER BY e.received_at DESC
        LIMIT %s
        """,
        params,
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "id": row[0], "entity": row[1], "dimension": row[2], "dataset": row[3], "period": row[4],
            "municipio_id": row[5], "sender": row[6], "status": row[7],
            "received_at": row[8].isoformat() if row[8] else None,
            "indicador_conflicto": row[9], "decision_sobre_conflicto": row[10],
            "resultado_validacion": row[11], "numero_registros_con_error": row[12],
            "numero_registros_con_incidencia": row[13], "numero_registros_validos": row[14],
            "estado_ciclo_vida_dataset": row[15],
        }
        for row in rows
    ]


def mark_dataset_ingested(validacion_id: str | None) -> None:
    """Transiciona el ciclo de vida de la validación a 'ingestado' una vez la entrega
    completa la Capa 4 con éxito (hasta entonces se queda en 'en_consolidacion')."""
    if not validacion_id:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE capa2_validaciones SET estado_ciclo_vida_dataset = 'ingestado' WHERE validacion_id = %s",
        (validacion_id,),
    )
    conn.commit()
    cur.close()
    conn.close()


def persist_dataset_control(control: Dict[str, Any], delivery_id: int | None, row_count: int) -> None:
    """Persiste la estructura de control de Capa 3 (identidad de la entrega, contexto temporal/
    territorial y referencias de recepción/validación), separada de las filas de negocio
    normalizadas que se publican en hive.normalized."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS capa3_control_dataset (
            id SERIAL PRIMARY KEY,
            dataset_id TEXT NOT NULL UNIQUE,
            dimension TEXT NOT NULL,
            dataset TEXT NOT NULL,
            entity TEXT NOT NULL,
            period TEXT,
            schema_version TEXT NOT NULL,
            validacion_id TEXT,
            recepcion_id INTEGER,
            row_count INTEGER NOT NULL DEFAULT 0,
            fecha_normalizacion TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        INSERT INTO capa3_control_dataset (
            dataset_id, dimension, dataset, entity, period, schema_version, validacion_id, recepcion_id, row_count, fecha_normalizacion
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (dataset_id) DO UPDATE SET
            validacion_id = EXCLUDED.validacion_id,
            recepcion_id = EXCLUDED.recepcion_id,
            row_count = EXCLUDED.row_count,
            fecha_normalizacion = NOW()
        """,
        (
            control["dataset_id"],
            control["dimension"],
            control["dataset"],
            control["entity"],
            control.get("period"),
            control["schema_version"],
            control.get("validacion_id"),
            delivery_id,
            row_count,
        ),
    )
    conn.commit()
    cur.close()
    conn.close()


def parse_csv_rows(csv_text: str) -> Dict[str, Any]:
    """Convierte un CSV de texto en una lista de registros con nombres de columna."""
    if not csv_text or not csv_text.strip():
        return {"is_valid": False, "errors": ["El archivo está vacío."], "rows": []}

    try:
        text = csv_text.decode("utf-8") if isinstance(csv_text, bytes) else csv_text
    except UnicodeDecodeError:
        text = csv_text.decode("latin-1") if isinstance(csv_text, bytes) else csv_text

    header_line = text.split("\n", 1)[0]
    dialect = None
    for sample in [header_line, text[:4096]]:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            break
        except csv.Error:
            continue

    if dialect is None:
        return {"is_valid": False, "errors": ["No se detectó un delimitador válido."], "rows": []}

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = list(reader)
    if not reader.fieldnames:
        return {"is_valid": False, "errors": ["El archivo no tiene cabeceras válidas."], "rows": []}

    return {"is_valid": True, "errors": [], "rows": rows}


def normalize_contract_header(value: str) -> str:
    """Normaliza una cabecera para identificar contratos sin alterar el fichero original."""
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", normalized.strip().lower()).strip("_")
    return {"conlongitud": "longitud", "longitud_geografica": "longitud"}.get(normalized, normalized)


def _sql_escape(value: str) -> str:
    """Escapa comillas simples para interpolar texto de usuario en un literal SQL."""
    return value.replace("'", "''")


def normalize_catalog_value(value: str) -> str:
    """Normaliza un valor de catálogo (sin tildes, minúsculas, sin espacios en los extremos).

    Genérico para cualquier dimensión: evita tener que declarar variantes acentuadas
    de un mismo valor en allowed_values, y que la capa 4 tenga que reconciliarlas a mano.
    """
    stripped = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(char for char in stripped if not unicodedata.combining(char))
    return stripped.strip().lower()


def build_delivery_key(
    entity: str,
    dimension: str,
    dataset: str,
    period: str,
    schema_version: str,
) -> str:
    """Construye la identidad lógica de una entrega de Capa 0."""
    parts = [entity, dimension, dataset, period, schema_version]
    return "|".join((str(part).strip() or "_" for part in parts))


def register_delivery(
    filename: str,
    content_bytes: bytes,
    entity: str = "municipio_demo",
    dimension: str = "afectaciones_urbanas",
    dataset: str = "gestion_afectaciones_urbanas",
    period: str = "",
    schema_version: str = "v1",
    replace: bool = False,
    entry_channel: str = "web_manual",
    sender: str = "usuario_local",
    municipio_id: str | None = None,
    anio: int | None = None,
    fecha_hora_inicio_remision: datetime | None = None,
    numero_intento_carga: int = 1,
) -> Dict[str, Any]:
    """Registra una entrega y decide si se acepta, duplica, entra en conflicto o reemplaza otra."""
    period_contains_year = bool(anio and re.search(rf"(?<!\d){anio}(?!\d)", period or ""))
    identity_period = period if not anio or period_contains_year else f"{anio}:{period or '_'}"
    logical_key = build_delivery_key(entity, dimension, dataset, identity_period, schema_version)
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    entry_channel = entry_channel.strip() or "web_manual"
    sender = sender.strip() or "usuario_local"
    remittance_started_at = fecha_hora_inicio_remision or datetime.now(timezone.utc)
    reception_timestamp = datetime.now(timezone.utc)

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ingesta_entregas (
                id SERIAL PRIMARY KEY,
                logical_key TEXT NOT NULL,
                entity TEXT NOT NULL,
                dimension TEXT NOT NULL,
                dataset TEXT NOT NULL,
                period TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                entry_channel TEXT NOT NULL DEFAULT 'web_manual',
                sender TEXT NOT NULL DEFAULT 'usuario_local',
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                replaced_at TIMESTAMPTZ,
                municipio_id TEXT,
                dimension_id TEXT,
                anio INTEGER,
                periodo TEXT,
                version_esquema TEXT,
                usuario_o_sistema_emisor TEXT,
                fecha_hora_inicio_remision TIMESTAMPTZ,
                recepcion_id INTEGER,
                fecha_hora_recepcion TIMESTAMPTZ,
                canal_entrada TEXT,
                nombre_fichero_original TEXT,
                tamano_fichero BIGINT,
                estado_recepcion TEXT,
                indicador_conflicto BOOLEAN NOT NULL DEFAULT FALSE,
                decision_sobre_conflicto TEXT
            )
            """
        )
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS entry_channel TEXT NOT NULL DEFAULT 'web_manual'")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS sender TEXT NOT NULL DEFAULT 'usuario_local'")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS municipio_id TEXT")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS dimension_id TEXT")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS anio INTEGER")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS version_esquema TEXT")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS usuario_o_sistema_emisor TEXT")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS fecha_hora_inicio_remision TIMESTAMPTZ")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS periodo TEXT")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS recepcion_id INTEGER")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS fecha_hora_recepcion TIMESTAMPTZ")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS canal_entrada TEXT")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS nombre_fichero_original TEXT")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS tamano_fichero BIGINT")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS estado_recepcion TEXT")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS indicador_conflicto BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE ingesta_entregas ADD COLUMN IF NOT EXISTS decision_sobre_conflicto TEXT")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ingesta_eventos_recepcion (
                id SERIAL PRIMARY KEY,
                delivery_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                entry_channel TEXT NOT NULL,
                observation TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                evento_recepcion_id BIGSERIAL,
                recepcion_id INTEGER,
                numero_intento_carga INTEGER NOT NULL DEFAULT 1,
                fecha_hora_evento TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                actor_evento TEXT,
                resultado_evento TEXT,
                observacion_evento TEXT
            )
            """
        )
        cur.execute("ALTER TABLE ingesta_eventos_recepcion ADD COLUMN IF NOT EXISTS evento_recepcion_id BIGSERIAL")
        cur.execute("ALTER TABLE ingesta_eventos_recepcion ADD COLUMN IF NOT EXISTS recepcion_id INTEGER")
        cur.execute("ALTER TABLE ingesta_eventos_recepcion ADD COLUMN IF NOT EXISTS numero_intento_carga INTEGER NOT NULL DEFAULT 1")
        cur.execute("ALTER TABLE ingesta_eventos_recepcion ADD COLUMN IF NOT EXISTS fecha_hora_evento TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute("ALTER TABLE ingesta_eventos_recepcion ADD COLUMN IF NOT EXISTS actor_evento TEXT")
        cur.execute("ALTER TABLE ingesta_eventos_recepcion ADD COLUMN IF NOT EXISTS resultado_evento TEXT")
        cur.execute("ALTER TABLE ingesta_eventos_recepcion ADD COLUMN IF NOT EXISTS observacion_evento TEXT")
        legacy_logical_key = build_delivery_key(entity, dimension, dataset, period, schema_version)
        cur.execute(
            """
            SELECT id, content_sha256, status
            FROM ingesta_entregas
            WHERE logical_key IN (%s, %s)
            ORDER BY CASE WHEN logical_key = %s THEN 0 ELSE 1 END, id DESC
            LIMIT 1
            """,
            (logical_key, legacy_logical_key, logical_key),
        )
        previous = cur.fetchone()
        active_delivery_statuses = ("accepted", "received", "preserved", "completed", "completed_with_warnings")
        if previous and previous[1] == content_sha256 and previous[2] in active_delivery_statuses:
            observation = "Contenido idéntico para la misma clave lógica."
            cur.execute(
                "INSERT INTO ingesta_eventos_recepcion (delivery_id, event_type, actor, entry_channel, observation, recepcion_id, numero_intento_carga, actor_evento, resultado_evento, observacion_evento) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (previous[0], "duplicate", sender, entry_channel, observation, previous[0], numero_intento_carga, sender, "success", observation),
            )
            conn.commit()
            cur.close()
            conn.close()
            return {
                "is_valid": True,
                "decision": "duplicate",
                "status": "duplicate",
                "logical_key": logical_key,
                "content_sha256": content_sha256,
                "delivery_id": previous[0],
                "recepcion_id": previous[0],
                "estado_recepcion": "accepted",
                "fecha_hora_recepcion": reception_timestamp.isoformat(),
                "canal_entrada": entry_channel,
                "nombre_fichero_original": filename,
                "tamano_fichero": len(content_bytes),
                "numero_intento_carga": numero_intento_carga,
                "indicador_conflicto": False,
                "decision_sobre_conflicto": "duplicate",
                "reception": {"entry_channel": entry_channel, "sender": sender},
            }

        if previous and previous[2] in active_delivery_statuses and not replace:
            observation = "Contenido distinto para la misma clave lógica."
            cur.execute(
                "INSERT INTO ingesta_eventos_recepcion (delivery_id, event_type, actor, entry_channel, observation, recepcion_id, numero_intento_carga, actor_evento, resultado_evento, observacion_evento) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (previous[0], "conflict", sender, entry_channel, observation, previous[0], numero_intento_carga, sender, "conflict", observation),
            )
            conn.commit()
            cur.close()
            conn.close()
            return {
                "is_valid": False,
                "decision": "conflict",
                "status": "conflict",
                "errors": ["Ya existe una entrega distinta para la misma clave lógica."],
                "logical_key": logical_key,
                "content_sha256": content_sha256,
                "previous_delivery_id": previous[0],
                "recepcion_id": previous[0],
                "estado_recepcion": "rejected",
                "fecha_hora_recepcion": reception_timestamp.isoformat(),
                "canal_entrada": entry_channel,
                "nombre_fichero_original": filename,
                "tamano_fichero": len(content_bytes),
                "numero_intento_carga": numero_intento_carga,
                "indicador_conflicto": True,
                "decision_sobre_conflicto": "rejected",
                "reception": {"entry_channel": entry_channel, "sender": sender},
            }

        if previous and replace:
            cur.execute("UPDATE ingesta_entregas SET status = 'replaced', replaced_at = NOW() WHERE id = %s", (previous[0],))
            observation = "Entrega reemplazada de forma controlada."
            cur.execute(
                "INSERT INTO ingesta_eventos_recepcion (delivery_id, event_type, actor, entry_channel, observation, recepcion_id, numero_intento_carga, actor_evento, resultado_evento, observacion_evento) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (previous[0], "replaced", sender, entry_channel, observation, previous[0], numero_intento_carga, sender, "success", observation),
            )

        cur.execute(
            """
            INSERT INTO ingesta_entregas (
                logical_key, entity, dimension, dataset, period, schema_version,
                filename, content_sha256, status, entry_channel, sender
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'received', %s, %s)
            RETURNING id
            """,
            (logical_key, entity, dimension, dataset, period, schema_version, filename, content_sha256, entry_channel, sender),
        )
        delivery_id = cur.fetchone()[0]
        cur.execute(
            """
            UPDATE ingesta_entregas SET
                municipio_id = %s, dimension_id = %s, anio = %s, periodo = %s,
                version_esquema = %s, usuario_o_sistema_emisor = %s,
                fecha_hora_inicio_remision = %s, recepcion_id = %s,
                fecha_hora_recepcion = %s, canal_entrada = %s,
                nombre_fichero_original = %s, tamano_fichero = %s,
                estado_recepcion = 'received', indicador_conflicto = %s,
                decision_sobre_conflicto = %s
            WHERE id = %s
            """,
            (municipio_id or entity, dimension, anio, period, schema_version, sender,
               remittance_started_at, delivery_id, reception_timestamp, entry_channel, filename, len(content_bytes),
             bool(previous), "replaced" if previous else "accepted", delivery_id),
        )
        cur.execute(
            """
            INSERT INTO ingesta_eventos_recepcion (
                delivery_id, event_type, actor, entry_channel, observation,
                recepcion_id, numero_intento_carga, actor_evento, resultado_evento, observacion_evento
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (delivery_id, "received", sender, entry_channel, "Entrega registrada y pendiente de validación.",
             delivery_id, numero_intento_carga, sender, "success", "Entrega registrada y pendiente de validación."),
        )
        conn.commit()
        cur.close()
        conn.close()
        return {
            "is_valid": True,
            "decision": "replaced" if previous else "accepted",
            "status": "received",
            "logical_key": logical_key,
            "content_sha256": content_sha256,
            "delivery_id": delivery_id,
            "recepcion_id": delivery_id,
            "municipio_id": municipio_id or entity,
            "dimension_id": dimension,
            "anio": anio,
            "periodo": period,
            "version_esquema": schema_version,
            "usuario_o_sistema_emisor": sender,
            "fecha_hora_inicio_remision": remittance_started_at.isoformat(),
            "fecha_hora_recepcion": reception_timestamp.isoformat(),
            "canal_entrada": entry_channel,
            "nombre_fichero_original": filename,
            "tamano_fichero": len(content_bytes),
            "estado_recepcion": "received",
            "numero_intento_carga": numero_intento_carga,
            "indicador_conflicto": bool(previous),
            "decision_sobre_conflicto": "replaced" if previous else "accepted",
            "reception": {"entry_channel": entry_channel, "sender": sender},
        }
    except Exception as exc:
        return {"is_valid": False, "decision": "error", "status": "error", "errors": [str(exc)], "logical_key": logical_key}


def register_dimension_delivery(
    filename: str,
    content_bytes: bytes,
    dataset: str,
    entity: str = "municipio_demo",
    period: str = "",
    schema_version: str = "v1",
    replace: bool = False,
    entry_channel: str = "web_manual",
    sender: str = "usuario_local",
    municipio_id: str | None = None,
    anio: int | None = None,
    fecha_hora_inicio_remision: datetime | None = None,
    numero_intento_carga: int = 1,
) -> Dict[str, Any]:
    """Registra una entrega de una dimensión en Capa 0 sin persistir sus filas."""
    contract = DATASET_CONTRACTS.get(dataset)
    if contract is None:
        return {"is_valid": False, "status": "rejected", "decision": "invalid_dataset", "errors": [f"Dataset no permitido: {dataset}"]}

    result = register_delivery(
        filename,
        content_bytes,
        entity=entity,
        dimension=contract["dimension"],
        dataset=dataset,
        period=period,
        schema_version=schema_version,
        replace=replace,
        entry_channel=entry_channel,
        sender=sender,
        municipio_id=municipio_id,
        anio=anio,
        fecha_hora_inicio_remision=fecha_hora_inicio_remision,
        numero_intento_carga=numero_intento_carga,
    )
    result["dataset"] = dataset
    result["dimension"] = contract["dimension"]
    result["grain"] = contract.get("grain", "inventario de dispositivos")
    result["nature"] = contract.get("nature", "inventario")
    result["functional_date"] = contract.get("functional_date", False)
    result["content_size_bytes"] = len(content_bytes)
    return result


def register_mobility_delivery(*args, **kwargs) -> Dict[str, Any]:
    """Compatibilidad para la recepción de los datasets de Movilidad."""
    return register_dimension_delivery(*args, **kwargs)


def preserve_dimension_delivery(
    filename: str,
    content_bytes: bytes,
    dataset: str,
    entity: str = "municipio_demo",
    period: str = "",
    schema_version: str = "v1",
    replace: bool = False,
    entry_channel: str = "web_manual",
    sender: str = "usuario_local",
    municipio_id: str | None = None,
    anio: int | None = None,
    fecha_hora_inicio_remision: datetime | None = None,
    numero_intento_carga: int = 1,
) -> Dict[str, Any]:
    """Ejecuta Capa 0 y preserva el CSV original en staging sin transformarlo."""
    effective_entity = municipio_id or entity
    contract = DATASET_CONTRACTS.get(dataset, {})
    effective_dimension = contract.get("dimension", dataset)
    delivery = register_dimension_delivery(
        filename,
        content_bytes,
        dataset,
        entity=effective_entity,
        period=period,
        schema_version=schema_version,
        replace=replace,
        entry_channel=entry_channel,
        sender=sender,
        municipio_id=municipio_id,
        anio=anio,
        fecha_hora_inicio_remision=fecha_hora_inicio_remision,
        numero_intento_carga=numero_intento_carga,
    )
    if not delivery.get("is_valid") or delivery.get("decision") == "duplicate":
        return delivery

    paths = get_lake_dataset_paths(dataset)
    staging_path = paths["root"] / "staging" / effective_dimension / effective_entity / dataset / filename
    payload_hash = hashlib.sha256(content_bytes).hexdigest()
    s3_result: Dict[str, Any] = {"enabled": False}
    if os.getenv("S3_ENDPOINT"):
        try:
            s3_result = {
                "enabled": True,
                "is_valid": True,
                **publish_upload_to_s3(
                    filename,
                    content_bytes,
                    dataset_name=dataset,
                    dimension=effective_dimension,
                    entity=effective_entity,
                ),
            }
        except Exception as exc:
            s3_result = {"enabled": True, "is_valid": False, "errors": [str(exc)]}

    local_mirror = os.getenv("LAKE_LOCAL_MIRROR", "false").lower() == "true"
    if local_mirror:
        paths["staging_dir"].mkdir(parents=True, exist_ok=True)
        staging_path.write_bytes(content_bytes)

    lake = {
        "root": str(paths["root"]),
        "dataset": dataset,
        "stage": "staging",
        "filename": filename,
        "staging_path": str(staging_path),
        "staging_file_exists": staging_path.exists(),
        "local_mirror_enabled": local_mirror,
        "s3": s3_result,
    }
    preservation = register_preserved_object(delivery.get("delivery_id"), filename, content_bytes, lake)
    traceability = {"enabled": False, "status": "not_published"}
    if delivery.get("logical_key") and (s3_result.get("is_valid") or staging_path.exists()):
        try:
            traceability = publish_row_traceability_manifest(
                filename,
                content_bytes,
                delivery["logical_key"],
                dataset,
                dimension=effective_dimension,
                entity=effective_entity,
            )
        except Exception as exc:
            traceability = {"enabled": True, "status": "error", "errors": [str(exc)]}

    if delivery.get("delivery_id"):
        update_delivery_status(delivery["delivery_id"], "preserved")
    return {
        **delivery,
        "status": "preserved",
        "estado_recepcion": "preserved",
        "content_sha256": payload_hash,
        "row_count": max(len(build_row_traceability_ids(content_bytes)), 0),
        "lake": {**lake, "preservation": preservation, "traceability": traceability},
    }


def build_row_traceability_ids(content_bytes: bytes) -> List[str]:
    """Devuelve identificadores técnicos deterministas según el contenido y posición de cada fila."""
    text = content_bytes.decode("utf-8", errors="replace")
    header_line = text.split("\n", 1)[0]
    dialect = None
    for sample in [header_line, text[:4096]]:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            break
        except csv.Error:
            continue
    if dialect is None:
        raise ValueError("No se detectó un delimitador válido para crear la trazabilidad")
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    reader = csv.reader(io.StringIO(text), dialect)
    next(reader, None)
    return [
        hashlib.sha256(f"{content_sha256}:{row_number}".encode("utf-8")).hexdigest()
        for row_number, _ in enumerate(reader, start=1)
    ]


def build_row_traceability_manifest(content_bytes: bytes, delivery_key: str) -> bytes:
    """Genera un manifiesto separado sin modificar el fichero raw original."""
    text = content_bytes.decode("utf-8", errors="replace")
    header_line = text.split("\n", 1)[0]
    dialect = None
    for sample in [header_line, text[:4096]]:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            break
        except csv.Error:
            continue
    if dialect is None:
        raise ValueError("No se detectó un delimitador válido para crear la trazabilidad")

    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    row_ids = build_row_traceability_ids(content_bytes)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["row_id_tecnico", "fila_origen", "delivery_key", "content_sha256"])
    reader = csv.reader(io.StringIO(text), dialect)
    next(reader, None)
    for row_number, row_id in enumerate(row_ids, start=1):
        writer.writerow([row_id, row_number, delivery_key, content_sha256])
    return output.getvalue().encode("utf-8")


def publish_row_traceability_manifest(
    filename: str,
    content_bytes: bytes,
    delivery_key: str,
    dataset_name: str = "afectaciones_urbanas",
    dimension: str | None = None,
    entity: str = "municipio_demo",
) -> Dict[str, Any]:
    """Publica la trazabilidad en un prefijo separado del CSV raw consultable."""
    import boto3

    endpoint = os.getenv("S3_ENDPOINT")
    if not endpoint:
        return {"enabled": False, "status": "disabled"}

    bucket = os.getenv("S3_BUCKET_RAW", "raw")
    effective_dimension = dimension or DATASET_CONTRACTS.get(dataset_name, {}).get("dimension", dataset_name)
    key = f"_traceability/{effective_dimension}/{entity}/{dataset_name}/{filename}.rows.csv"
    manifest = build_row_traceability_manifest(content_bytes, delivery_key)
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.getenv("S3_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY"),
        region_name=os.getenv("S3_REGION", "us-east-1"),
    )
    client.put_object(Bucket=bucket, Key=key, Body=manifest, ContentType="text/csv")
    return {"enabled": True, "status": "published", "bucket": bucket, "key": key, "row_count": max(manifest.count(b"\n") - 1, 0)}


def register_preserved_object(
    delivery_id: int | None,
    filename: str,
    content_bytes: bytes,
    lake: Dict[str, Any],
) -> Dict[str, Any]:
    """Registra la preservación del CSV original y sus metadatos de almacenamiento."""
    s3 = lake.get("s3", {})
    staging_path = lake.get("staging_path", "")
    if delivery_id is None or not (s3.get("is_valid") or lake.get("staging_file_exists")):
        return {"enabled": False, "status": "not_preserved"}

    storage_uri = s3.get("uri") or f"file://{staging_path}"
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ingesta_objetos_preservados (
                id SERIAL PRIMARY KEY,
                delivery_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                storage_uri TEXT NOT NULL,
                size_bytes BIGINT NOT NULL,
                content_sha256 TEXT NOT NULL,
                format_detected TEXT NOT NULL,
                status TEXT NOT NULL,
                stored_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            INSERT INTO ingesta_objetos_preservados (
                delivery_id, filename, storage_uri, size_bytes, content_sha256, format_detected, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'preserved')
            """,
            (delivery_id, filename, storage_uri, len(content_bytes), content_sha256, "csv"),
        )
        conn.commit()
        cur.close()
        conn.close()
        return {
            "enabled": True,
            "status": "preserved",
            "storage_uri": storage_uri,
            "size_bytes": len(content_bytes),
            "content_sha256": content_sha256,
            "format": "csv",
        }
    except Exception as exc:
        return {"enabled": True, "status": "error", "errors": [str(exc)]}


def update_delivery_status(delivery_id: int | None, status: str) -> None:
    """Actualiza el resultado final de una entrega registrada."""
    if delivery_id is None:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE ingesta_entregas SET status = %s WHERE id = %s", (status, delivery_id))
    observation = f"Estado actualizado a {status}."
    cur.execute(
        "INSERT INTO ingesta_eventos_recepcion (delivery_id, event_type, actor, entry_channel, observation, recepcion_id, actor_evento, resultado_evento, observacion_evento) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (delivery_id, status, "sistema", "interno", observation, delivery_id, "sistema", "success" if status in ("accepted", "received") else status, observation),
    )
    conn.commit()
    cur.close()
    conn.close()


def persist_affectaciones(rows: List[Dict[str, str]], delivery_key: str | None = None, row_ids: List[str] | None = None) -> Dict[str, Any]:
    """Guarda las filas validadas de afectaciones urbanas en PostgreSQL."""
    if not rows:
        return {"is_valid": False, "errors": ["No hay filas para insertar."], "inserted": 0, "status": "error"}

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS afectaciones_urbanas (
                id SERIAL PRIMARY KEY,
                external_id TEXT,
                nombre TEXT,
                descripcion TEXT,
                direccion TEXT,
                latitud TEXT,
                longitud TEXT,
                fecha_inicio TEXT,
                hora_inicio TEXT,
                fecha_fin TEXT,
                hora_fin TEXT,
                tipo_intervencion TEXT,
                tipo_afectacion TEXT,
                titularidad TEXT,
                impacto_pmr TEXT,
                notas TEXT,
                delivery_key TEXT,
                row_id_tecnico TEXT
            )
            """
        )
        cur.execute("ALTER TABLE afectaciones_urbanas ADD COLUMN IF NOT EXISTS delivery_key TEXT")
        cur.execute("ALTER TABLE afectaciones_urbanas ADD COLUMN IF NOT EXISTS row_id_tecnico TEXT")
        if delivery_key:
            cur.execute("DELETE FROM afectaciones_urbanas WHERE delivery_key = %s", (delivery_key,))

        for index, row in enumerate(rows):
            cur.execute(
                """
                INSERT INTO afectaciones_urbanas (
                    external_id, nombre, descripcion, direccion, latitud, longitud,
                    fecha_inicio, hora_inicio, fecha_fin, hora_fin,
                    tipo_intervencion, tipo_afectacion, titularidad, impacto_pmr, notas, delivery_key, row_id_tecnico
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row.get("id"),
                    row.get("nombre"),
                    row.get("descripcion"),
                    row.get("direccion"),
                    row.get("latitud"),
                    row.get("longitud"),
                    row.get("fecha_inicio"),
                    row.get("hora_inicio"),
                    row.get("fecha_fin"),
                    row.get("hora_fin"),
                    row.get("tipo_intervencion"),
                    row.get("tipo_afectacion"),
                    row.get("titularidad"),
                    row.get("impacto_pmr"),
                    row.get("notas"),
                    delivery_key,
                    row_ids[index] if row_ids and index < len(row_ids) else None,
                ),
            )

        conn.commit()
        cur.close()
        conn.close()
        return {"is_valid": True, "errors": [], "inserted": len(rows), "status": "success"}
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)], "inserted": 0, "status": "error"}


def persist_its(
    rows: List[Dict[str, str]],
    delivery_key: str | None = None,
    row_ids: List[str] | None = None,
) -> Dict[str, Any]:
    """Guarda las filas validadas de Control y Gestión ITS en PostgreSQL (inventario, sin fechas de negocio)."""
    if not rows:
        return {"is_valid": False, "errors": ["No hay filas para insertar."], "inserted": 0, "status": "error"}

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS control_gestion_its (
                id SERIAL PRIMARY KEY,
                external_id TEXT,
                categoria TEXT,
                nombre TEXT,
                descripcion TEXT,
                direccion TEXT,
                latitud TEXT,
                longitud TEXT,
                titularidad TEXT,
                delivery_key TEXT,
                row_id_tecnico TEXT
            )
            """
        )
        cur.execute("ALTER TABLE control_gestion_its ADD COLUMN IF NOT EXISTS delivery_key TEXT")
        cur.execute("ALTER TABLE control_gestion_its ADD COLUMN IF NOT EXISTS row_id_tecnico TEXT")

        for index, row in enumerate(rows):
            cur.execute(
                """
                INSERT INTO control_gestion_its (
                    external_id, categoria, nombre, descripcion, direccion, latitud, longitud, titularidad,
                    delivery_key, row_id_tecnico
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row.get("id"),
                    row.get("categoria"),
                    row.get("nombre"),
                    row.get("descripcion"),
                    row.get("direccion"),
                    row.get("latitud"),
                    row.get("longitud"),
                    row.get("titularidad"),
                    delivery_key,
                    row_ids[index] if row_ids and index < len(row_ids) else None,
                ),
            )

        conn.commit()
        cur.close()
        conn.close()
        return {"is_valid": True, "errors": [], "inserted": len(rows), "status": "success"}
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)], "inserted": 0, "status": "error"}


def _ingest_dataset(
    csv_text: str | bytes,
    contract: Dict[str, Any],
    dataset_name: str,
    persist_fn,
    source_filename: str | None = None,
    content_bytes: bytes | None = None,
    delivery_options: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Valida, parsea y persiste un CSV para cualquier dimensión, parametrizado por su contrato."""
    delivery = {}
    delivery_id = None
    if delivery_options:
        delivery = register_delivery(
            filename=source_filename or "upload.csv",
            content_bytes=content_bytes or (csv_text.encode("utf-8") if isinstance(csv_text, str) else csv_text),
            **delivery_options,
        )
        if not delivery["is_valid"]:
            return {"is_valid": False, "status": delivery["status"], "errors": delivery["errors"], "rows": [], "row_count": 0, "delivery": delivery}
        if delivery["decision"] == "duplicate":
            return {"is_valid": True, "status": "duplicate", "errors": [], "rows": [], "row_count": 0, "inserted": 0, "delivery": delivery}
        delivery_id = delivery.get("delivery_id")

    validation = validate_csv_text(csv_text, required_columns=contract["required_columns"], business_rules=contract)
    incidents = validation.get("incidencias", [])
    if delivery_id is not None:
        persist_validation_incidents(delivery_id, incidents)
    if not validation["is_valid"]:
        update_delivery_status(delivery_id, "rejected")
        return {
            "is_valid": False,
            "status": "error",
            "errors": validation["errors"],
            "row_count": validation["row_count"],
            "rows": [],
            "delivery": delivery,
            "incidencias": incidents,
        }

    parsed = parse_csv_rows(csv_text)
    if not parsed["is_valid"]:
        update_delivery_status(delivery_id, "rejected")
        return {
            "is_valid": False,
            "status": "error",
            "errors": parsed["errors"],
            "row_count": 0,
            "rows": [],
            "delivery": delivery,
            "incidencias": incidents,
        }

    row_ids = None
    if delivery_options and content_bytes is not None:
        row_ids = build_row_traceability_ids(content_bytes)
    if delivery_options:
        persistence = persist_fn(parsed["rows"], delivery_key=delivery["logical_key"], row_ids=row_ids)
    else:
        persistence = persist_fn(parsed["rows"])
    success = persistence["is_valid"]
    lake_result = {}
    if source_filename:
        lake_result = publish_upload_to_lake(source_filename, content_bytes, dataset_name=dataset_name)
        if delivery_options:
            lake_result["lake"]["preservation"] = register_preserved_object(
                delivery_id,
                source_filename,
                content_bytes or b"",
                lake_result["lake"],
            )
        if delivery_options and delivery.get("logical_key") and lake_result.get("lake", {}).get("s3", {}).get("is_valid"):
            try:
                lake_result["lake"]["traceability"] = publish_row_traceability_manifest(
                    source_filename,
                    content_bytes or b"",
                    delivery["logical_key"],
                    dataset_name,
                )
            except Exception as exc:
                lake_result["lake"]["traceability"] = {"enabled": True, "status": "error", "errors": [str(exc)]}

    if success:
        status = "success"
        is_valid = True
    elif lake_result.get("is_valid"):
        status = "partial"
        is_valid = False
    else:
        status = "error"
        is_valid = False
    final_delivery_status = "accepted" if success else "rejected"
    update_delivery_status(delivery_id, final_delivery_status)
    if delivery:
        delivery["status"] = final_delivery_status

    return {
        "is_valid": is_valid,
        "status": status,
        "errors": persistence["errors"],
        "row_count": len(parsed["rows"]),
        "rows": parsed["rows"],
        "inserted": persistence["inserted"],
        "summary": {
            "rows_received": len(parsed["rows"]),
            "rows_inserted": persistence["inserted"],
            "errors_count": len(persistence["errors"]),
        },
        "database": {
            "host": os.getenv("POSTGRES_HOST", "postgres"),
            "port": os.getenv("POSTGRES_PORT", "5432"),
            "database": os.getenv("POSTGRES_DB", "datalake"),
            "mode": "fake" if os.getenv("USE_FAKE_DB", "false").lower() == "true" else "postgres",
        },
        "lake": lake_result.get("lake", {}),
        "delivery": delivery,
        "incidencias": incidents,
    }


def ingest_affectaciones(
    csv_text: str | bytes,
    source_filename: str | None = None,
    content_bytes: bytes | None = None,
    entity: str = "municipio_demo",
    dataset: str = "gestion_afectaciones_urbanas",
    period: str = "",
    schema_version: str = "v1",
    replace: bool = False,
    entry_channel: str = "web_manual",
    sender: str = "usuario_local",
    municipio_id: str | None = None,
    anio: int | None = None,
    fecha_hora_inicio_remision: datetime | None = None,
    numero_intento_carga: int = 1,
) -> Dict[str, Any]:
    """Valida y prepara los registros de afectaciones urbanas para persistencia."""
    if source_filename and content_bytes is not None:
        return run_automatic_dimension_pipeline(
            source_filename,
            content_bytes,
            dataset=dataset,
            entity=entity,
            period=period,
            schema_version=schema_version,
            replace=replace,
            entry_channel=entry_channel,
            sender=sender,
            municipio_id=municipio_id,
            anio=anio,
            fecha_hora_inicio_remision=fecha_hora_inicio_remision,
            numero_intento_carga=numero_intento_carga,
        )
    delivery_options = None
    if source_filename:
        delivery_options = {
            "entity": entity,
            "dimension": "afectaciones_urbanas",
            "dataset": dataset,
            "period": period,
            "schema_version": schema_version,
            "replace": replace,
            "entry_channel": entry_channel,
            "sender": sender,
            "municipio_id": municipio_id,
            "anio": anio,
            "fecha_hora_inicio_remision": fecha_hora_inicio_remision,
            "numero_intento_carga": numero_intento_carga,
        }
    return _ingest_dataset(
        csv_text,
        AFFECTACIONES_DATA_CONTRACT,
        "afectaciones_urbanas",
        persist_affectaciones,
        source_filename,
        content_bytes,
        delivery_options,
    )


def ingest_its(csv_text: str | bytes, source_filename: str | None = None, content_bytes: bytes | None = None) -> Dict[str, Any]:
    """Valida y prepara los registros de Control y Gestión ITS para persistencia (mismo patrón que afectaciones)."""
    return _ingest_dataset(csv_text, ITS_DATA_CONTRACT, "control_gestion_its", persist_its, source_filename, content_bytes)


def list_affectaciones(limit: int = 10, municipio_id: str | None = None) -> Dict[str, Any]:
    """Devuelve los registros recientes de afectaciones urbanas desde PostgreSQL.

    municipio_id se acepta por compatibilidad con /analysis/cuadro-mando pero no
    filtra: esta tabla legacy de Postgres (independiente del lake) no tiene columna
    de municipio."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, external_id, nombre, descripcion, direccion, latitud, longitud, fecha_inicio, hora_inicio, fecha_fin, hora_fin, tipo_intervencion, tipo_afectacion, titularidad, impacto_pmr, notas FROM afectaciones_urbanas ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        serialized = []
        for row in rows:
            values = list(row)
            if len(values) < 16:
                values = values + [None] * (16 - len(values))
            serialized.append(
                {
                    "id": values[0],
                    "external_id": values[1],
                    "nombre": values[2],
                    "descripcion": values[3],
                    "direccion": values[4],
                    "latitud": values[5],
                    "longitud": values[6],
                    "fecha_inicio": values[7],
                    "hora_inicio": values[8],
                    "fecha_fin": values[9],
                    "hora_fin": values[10],
                    "tipo_intervencion": values[11],
                    "tipo_afectacion": values[12],
                    "titularidad": values[13],
                    "impacto_pmr": values[14],
                    "notas": values[15],
                }
            )
        return {"is_valid": True, "errors": [], "rows": serialized}
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)], "rows": []}


def list_its(limit: int = 10, municipio_id: str | None = None) -> Dict[str, Any]:
    """Devuelve los registros recientes de dispositivos ITS desde PostgreSQL.

    municipio_id se acepta por compatibilidad con /analysis/cuadro-mando pero no
    filtra: esta tabla legacy de Postgres (independiente del lake) no tiene columna
    de municipio."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, external_id, categoria, nombre, descripcion, direccion, latitud, longitud, titularidad FROM control_gestion_its ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        serialized = []
        for row in rows:
            values = list(row)
            if len(values) < 9:
                values = values + [None] * (9 - len(values))
            serialized.append(
                {
                    "id": values[0],
                    "external_id": values[1],
                    "categoria": values[2],
                    "nombre": values[3],
                    "descripcion": values[4],
                    "direccion": values[5],
                    "latitud": values[6],
                    "longitud": values[7],
                    "titularidad": values[8],
                }
            )
        return {"is_valid": True, "errors": [], "rows": serialized}
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)], "rows": []}


def get_its_kpis(table: str = "control_gestion_its") -> Dict[str, Any]:
    """KPIs simples de inventario ITS (sin ponderación, eso es específico de afectaciones): total por categoria y titularidad."""
    qualified_table = f"hive.raw.{table}"
    try:
        total_rows = run_trino_query(f"SELECT count(*) FROM {qualified_table}")
        total = total_rows[0][0] if total_rows else 0

        if total == 0:
            return {
                "is_valid": True,
                "source": "trino",
                "table": qualified_table,
                "kpis": {"total_dispositivos": 0},
                "por_categoria": [],
                "por_titularidad": [],
            }

        categoria_rows = run_trino_query(
            f"SELECT categoria, count(*) FROM {qualified_table} GROUP BY categoria ORDER BY 2 DESC"
        )
        titularidad_rows = run_trino_query(
            f"SELECT titularidad, count(*) FROM {qualified_table} GROUP BY titularidad ORDER BY 2 DESC"
        )

        return {
            "is_valid": True,
            "source": "trino",
            "table": qualified_table,
            "kpis": {"total_dispositivos": total},
            "por_categoria": [{"categoria": r[0], "count": r[1]} for r in categoria_rows],
            "por_titularidad": [{"titularidad": r[0], "count": r[1]} for r in titularidad_rows],
        }
    except Exception as exc:
        return {"is_valid": False, "source": "trino", "table": qualified_table, "errors": [str(exc)]}


def build_affectaciones_analysis_context(limit: int = 200) -> Dict[str, Any]:
    """Construye un contexto resumido para la interpretación de las afectaciones urbanas."""
    raw = list_affectaciones(limit=limit)
    if not raw["is_valid"]:
        return {
            "is_valid": False,
            "errors": raw["errors"],
            "dimension": "afectaciones_urbanas",
            "summary": {},
            "rows": [],
        }

    rows = raw["rows"]
    by_tipo_intervencion = Counter((r.get("tipo_intervencion") or "sin_dato").strip() for r in rows)
    by_tipo_afectacion = Counter((r.get("tipo_afectacion") or "sin_dato").strip() for r in rows)
    by_titularidad = Counter((r.get("titularidad") or "sin_dato").strip() for r in rows)

    # Impacto/criticidad por vía y tipo (Q3), calculado sobre los mismos registros de PostgreSQL.
    variacion_impacto = calcular_variacion_impacto(rows)
    by_nivel = Counter(d["nivel"] for d in variacion_impacto["detalle_variacion"])

    sample_rows = rows[:15]
    return {
        "is_valid": True,
        "errors": [],
        "dimension": "afectaciones_urbanas",
        "summary": {
            "total_rows": len(rows),
            "tipos_intervencion": dict(by_tipo_intervencion),
            "tipos_afectacion": dict(by_tipo_afectacion),
            "titularidades": dict(by_titularidad),
            "variacion_media_pct": variacion_impacto["variacion_media_pct"],
            "count_alto_critico": variacion_impacto["count_alto_critico"],
            "pct_alto_critico": variacion_impacto["pct_alto_critico"],
            "distribucion_niveles": dict(by_nivel),
            "ranking_criticidad_vias": variacion_impacto["ranking_criticidad_vias"],
        },
        "rows": sample_rows,
    }


def _afectaciones_ollama_summary(limit: int) -> Dict[str, Any]:
    layer4 = get_afectaciones_layer4_summary("afectaciones_urbanas")
    analytical = build_affectaciones_analysis_context(limit=limit)
    if not analytical.get("is_valid", False) and not layer4.get("is_valid", False):
        return {"is_valid": False, "errors": [*layer4.get("errors", []), *analytical.get("errors", [])]}
    return {
        "is_valid": True,
        "summary": {
            "total_afectaciones": layer4.get("kpis", {}).get("total_afectaciones", 0),
            "top_vias": layer4.get("summary", {}).get("top_vias", []),
            "por_tipo_afectacion": layer4.get("summary", {}).get("por_tipo_afectacion", []),
            "por_hora": layer4.get("summary", {}).get("por_hora", []),
            "variacion_media_pct": analytical.get("summary", {}).get("variacion_media_pct", 0.0),
            "count_alto_critico": analytical.get("summary", {}).get("count_alto_critico", 0),
            "pct_alto_critico": analytical.get("summary", {}).get("pct_alto_critico", 0.0),
            "distribucion_niveles": analytical.get("summary", {}).get("distribucion_niveles", {}),
            "ranking_criticidad_vias": analytical.get("summary", {}).get("ranking_criticidad_vias", []),
        },
        "sample_rows": analytical.get("rows", [])[:10],
    }


def _movilidad_trafico_ollama_summary(limit: int) -> Dict[str, Any]:
    layer5 = build_movilidad_trafico_layer5()
    if not layer5.get("is_valid", False):
        return {"is_valid": False, "errors": layer5.get("errors", ["No se pudo calcular la congestión."])}
    return {
        "is_valid": True,
        "summary": {
            "total_registros": layer5.get("total_registros", 0),
            "metricas": layer5.get("metricas", {}),
            "vias_prioritarias": layer5.get("prioridades", [])[:10],
        },
        "sample_rows": [],
    }


def _movilidad_parking_ollama_summary(limit: int) -> Dict[str, Any]:
    resumen = get_dimension_analytics_summary("movilidad_parking", limit=limit)
    if not resumen.get("is_valid", False):
        return {"is_valid": False, "errors": resumen.get("errors", ["No se pudo leer el resumen de parking."])}
    rows_ordenadas = sorted(resumen.get("rows", []), key=lambda row: row.get("pct_saturacion") or 0, reverse=True)
    return {"is_valid": True, "summary": {"parkings_mas_saturados": rows_ordenadas[:10]}, "sample_rows": []}


def _control_gestion_its_ollama_summary(limit: int) -> Dict[str, Any]:
    layer5 = build_dimension_layer5("control_gestion_its")
    if not layer5.get("is_valid", False):
        return {"is_valid": False, "errors": layer5.get("errors", ["No se pudo calcular la cobertura ITS."])}
    return {
        "is_valid": True,
        "summary": {
            "total_dispositivos": layer5.get("total_registros", 0),
            "distribucion_por_categoria": layer5.get("reglas", []),
            "vias_con_mas_dispositivos": layer5.get("ranking_ubicaciones", []),
        },
        "sample_rows": [],
    }


# Cada dimensión con preguntas analíticas registra aquí cómo construir su resumen para Ollama.
# afectaciones_urbanas conserva su lectura de negocio a medida; el resto reutiliza capa 4/5
# ya genéricas (build_dimension_layer5, get_dimension_analytics_summary) — añadir una nueva
# dimensión es registrar su builder, no crear una rama de código en build_ollama_analysis_context.
DIMENSION_OLLAMA_SUMMARY_BUILDERS: Dict[str, Any] = {
    "afectaciones_urbanas": _afectaciones_ollama_summary,
    "gestion_afectaciones_urbanas": _afectaciones_ollama_summary,
    "movilidad_trafico": _movilidad_trafico_ollama_summary,
    "movilidad_parking": _movilidad_parking_ollama_summary,
    "control_gestion_its": _control_gestion_its_ollama_summary,
}

FOCUS_INSTRUCTIONS = {
    "zonas_recurrentes": "Enfatiza zonas/vias recurrentes y concentración espacial.",
    "tipos_presion": "Prioriza la comparación entre tipos de afectación y su presión relativa.",
    "acumulacion_temporal": "Destaca franjas horarias de acumulación y momentos críticos.",
    "duracion_localizacion": "Resalta casos de mayor duración y su localización.",
    "riesgo_saturacion": "Identifica zonas con simultaneidad de afectaciones y riesgo de saturación.",
    "accesibilidad_pmr": "Analiza el impacto potencial en accesibilidad peatonal y movilidad PMR.",
    "nodos_congestion": "Prioriza vías y franjas con mayor riesgo de congestión, apoyándote en las recomendaciones ya calculadas para cada vía.",
    "saturacion_parking": "Señala qué parkings y franjas horarias concentran mayor saturación (por encima del 80% de ocupación).",
    "cobertura_its": "Evalúa la cobertura de dispositivos ITS por categoría y vía, señalando posibles carencias de cobertura.",
}


def build_ollama_analysis_context(
    dataset: str = "afectaciones_urbanas",
    limit: int = 200,
    question: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Prepara el payload estructurado para que Ollama interprete la capa 4/5 de cualquier dimensión."""
    selected_question = question or {
        "id": "AQ1",
        "title": "¿Qué zonas concentran mayor número de afectaciones urbanas activas o recurrentes?",
        "focus": "zonas_recurrentes",
    }

    summary_builder = DIMENSION_OLLAMA_SUMMARY_BUILDERS.get(dataset)
    if summary_builder is None:
        return {
            "is_valid": False,
            "errors": [f"No hay contexto analítico disponible todavía para la dimensión '{dataset}'."],
            "payload": None,
        }

    built = summary_builder(limit)
    if not built.get("is_valid", False):
        return {"is_valid": False, "errors": built.get("errors", ["No se pudo construir el contexto analítico."]), "payload": None}

    focus_instruction = FOCUS_INSTRUCTIONS.get(selected_question.get("focus"), "Enfoca la respuesta en implicaciones operativas.")
    return {
        "is_valid": True,
        "errors": [],
        "payload": {
            "question": selected_question.get("title", "¿Qué está ocurriendo y qué recomendaciones operativas destacan?"),
            "question_id": selected_question.get("id", "AQ1"),
            "focus": selected_question.get("focus", "general"),
            "instruction": (
                "Actúa como analista urbano local y usa este payload como contexto para Ollama. "
                "Responde en español con un resumen ejecutivo breve, el punto crítico más relevante y 3 "
                f"recomendaciones operativas. {focus_instruction} Usa únicamente los datos del payload. "
                "No inventes hechos ni desconozcas el contexto proporcionado."
            ),
            "summary": built["summary"],
            "sample_rows": built.get("sample_rows", []),
        },
    }


def call_ollama_analysis(context_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Envía un contexto analítico al endpoint local de generación de Ollama."""
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "llama3.2")
    prompt = (
        f"{context_payload['instruction']}\n\n"
        f"Pregunta: {context_payload['question']}\n"
        f"Contexto JSON:\n{json.dumps(context_payload, ensure_ascii=False, default=str)}"
    )
    request_body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 120, "temperature": 0.2},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/generate",
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        timeout = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300"))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        generated_text = result.get("response")
        if not generated_text:
            return {"is_valid": False, "errors": ["Ollama no devolvió texto en la respuesta."], "response": None}
        return {"is_valid": True, "errors": [], "model": model, "response": generated_text}
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return {
            "is_valid": False,
            "errors": [f"No se pudo obtener una interpretación de Ollama: {exc}"],
            "model": model,
            "response": None,
        }
