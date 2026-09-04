# Archivo: main.py
# Resumen: Punto de entrada del backend FastAPI para comprobar salud, validar CSV y preparar una tabla inicial.
# Autor: Alba Sánchez Ibáñez
# Fecha: 2026-07-27

import os
import json
from collections import Counter
from pathlib import Path
from datetime import datetime
from typing import Any, Dict

import psycopg2
from fastapi import Depends, FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.analysis_questions import ANALYSIS_QUESTIONS
from app.auth import (
    LoginRequest,
    RegisterRequest,
    UpdateUserRequest,
    ROLE_ADMIN,
    ROLE_AUDITOR,
    ROLE_CONSUMIDOR,
    authenticate_user,
    create_token,
    get_current_user,
    init_auth_table,
    delete_user,
    list_users,
    register_user,
    require_admin,
    require_audit_access,
    require_roles,
    require_write_access,
    resolve_upload_municipio,
    update_user,
)
from app.policies import (
    PublishPolicyRequest,
    accept_active_policy,
    get_active_policy,
    has_accepted_active_policy,
    init_policies_table,
    list_policy_acceptance_audit,
    publish_policy_version,
    require_policy_accepted,
)
from app.contracts import DATASET_CONTRACTS
from app.contracts_manager import ContractsManager, list_catalog_status, list_catalog_sync_history, set_catalog_status
from app.services import (
    list_ingest_deliveries,
    build_afectaciones_layers,
    build_afectaciones_layer5,
    build_dimension_layer5,
    build_movilidad_trafico_layer5,
    build_ollama_analysis_context,
    call_ollama_analysis,
    get_afectaciones_kpis,
    get_afectaciones_layer4_summary,
    get_its_kpis,
    get_lake_root,
    ingest_affectaciones,
    ingest_its,
    inspect_s3_lake_object,
    list_affectaciones,
    list_its,
    move_staged_file_to_raw,
    normalize_dimension_dataset,
    predecir_congestion_via,
    register_mobility_delivery,
    register_dimension_delivery,
    preserve_dimension_delivery,
    resolve_lake_file_path,
    run_automatic_dimension_pipeline,
    validate_csv_text,
)

app = FastAPI(title="Plataforma de datos municipales")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def initialize_authentication() -> None:
    init_auth_table()
    init_policies_table()
    try:
        ContractsManager().save_contracts_to_postgres()
    except Exception:
        pass  # el catálogo se puede sincronizar más tarde desde el panel de administración


@app.post("/auth/login")
def login(request: LoginRequest) -> Dict[str, object]:
    user = authenticate_user(request)
    return {"user": user, "token": create_token(user["id"], user["email"], user["role"])}


@app.post("/auth/register")
def register(request: RegisterRequest, admin: dict = Depends(require_admin)) -> Dict[str, object]:
    # Solo un administrador autenticado puede dar de alta cuentas municipales.
    return {"user": register_user(request)}


@app.get("/auth/me")
def auth_me(user: dict = Depends(get_current_user)) -> Dict[str, object]:
    return {"user": user}


@app.get("/politicas/vigente")
def politica_vigente(user: dict = Depends(get_current_user)) -> Dict[str, object]:
    policy = get_active_policy()
    return {**policy, "ya_aceptada": has_accepted_active_policy(user["id"])}


@app.post("/politicas/aceptar")
def aceptar_politica(user: dict = Depends(get_current_user)) -> Dict[str, object]:
    return accept_active_policy(user["id"])


@app.post("/admin/politicas")
def publicar_politica(request: PublishPolicyRequest, admin: dict = Depends(require_admin)) -> Dict[str, object]:
    return publish_policy_version(request, admin["id"])


@app.get("/admin/politicas/aceptaciones")
def auditoria_politicas(user: dict = Depends(require_audit_access)) -> Dict[str, object]:
    return {"aceptaciones": list_policy_acceptance_audit()}


@app.get("/admin/usuarios")
def admin_listar_usuarios(admin: dict = Depends(require_admin)) -> Dict[str, object]:
    return {"usuarios": list_users()}


@app.post("/admin/usuarios")
def admin_crear_usuario(request: RegisterRequest, admin: dict = Depends(require_admin)) -> Dict[str, object]:
    return {"usuario": register_user(request)}


@app.patch("/admin/usuarios/{user_id}")
def admin_actualizar_usuario(user_id: int, request: UpdateUserRequest, admin: dict = Depends(require_admin)) -> Dict[str, object]:
    return {"usuario": update_user(user_id, request)}


@app.delete("/admin/usuarios/{user_id}")
def admin_borrar_usuario(user_id: int, admin: dict = Depends(require_admin)) -> Dict[str, object]:
    return delete_user(user_id, admin["id"])


@app.get("/admin/ingestas")
def admin_supervision_ingestas(
    municipio_id: str | None = None,
    dataset: str | None = None,
    status: str | None = None,
    limit: int = 200,
    user: dict = Depends(require_roles(ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMIDOR)),
) -> Dict[str, object]:
    # Soberanía del dato: consumidor (sin municipio propio) solo ve lo que cada
    # municipio ha marcado explícitamente como "compartido" al subirlo.
    only_shared = user["role"] == ROLE_CONSUMIDOR
    return {"entregas": list_ingest_deliveries(municipio_id=municipio_id, dataset=dataset, status_filter=status, limit=limit, only_shared=only_shared)}


@app.get("/catalogo/publico")
def catalogo_publico() -> Dict[str, object]:
    """Entorno abierto: catálogo de datasets consultable SIN autenticación, para que
    cualquier proveedor o consumidor potencial (dentro o fuera del espacio de datos)
    pueda descubrir qué dimensiones existen y su contrato técnico antes de pedir acceso.
    No expone datos, solo el esquema (columnas, obligatoriedad, valores permitidos)."""
    manager = ContractsManager()
    estados = {item["id"]: item["status"] for item in list_catalog_status()}
    catalogo = []
    for dataset_id, contract in manager.contracts.items():
        if estados.get(dataset_id, "active") != "active":
            continue
        payload = manager._contract_payload(dataset_id, contract)
        catalogo.append({
            "id": payload["id"],
            "dimension": payload["dimension"],
            "display_name": payload["display_name"],
            "description": payload["description"],
            "columns": payload["columns"],
            "required_columns": payload["required_columns"],
        })
    return {"count": len(catalogo), "catalogo": catalogo}


@app.get("/admin/catalogo")
def admin_listar_catalogo(user: dict = Depends(require_audit_access)) -> Dict[str, object]:
    return {"catalogo": list_catalog_status()}


class CatalogStatusRequest(BaseModel):
    status: str


@app.patch("/admin/catalogo/{dataset_id}")
def admin_actualizar_catalogo(dataset_id: str, request: CatalogStatusRequest, admin: dict = Depends(require_admin)) -> Dict[str, object]:
    try:
        return {"dataset": set_catalog_status(dataset_id, request.status)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class CongestionPredictionRequest(BaseModel):
    direccion: str
    franja_horaria: str | None = None
    dia_semana: str | None = None


@app.get("/api/ml/movilidad/reglas-congestion")
def reglas_congestion() -> Dict[str, object]:
    """Capa 5: prioridad de intervención por vía, calculada sobre lake.curated.movilidad_trafico
    (percentiles propios de cada vía, cruzados con obras activas de afectaciones_urbanas)."""
    resultado = build_movilidad_trafico_layer5()
    if not resultado.get("is_valid"):
        raise HTTPException(status_code=500, detail=resultado.get("errors", ["Error calculando congestión"]))
    return {
        "success": True,
        "dimension": "movilidad_trafico",
        "modelo": resultado["model"],
        "total_registros": resultado["total_registros"],
        "metricas": resultado["metricas"],
        "prioridades": resultado["prioridades"],
    }


@app.post("/api/ml/movilidad/predecir")
def predecir_congestion(payload: CongestionPredictionRequest) -> Dict[str, object]:
    """Estima la congestión esperada en una vía a partir de su propio histórico de tráfico
    (percentiles reales, no un umbral fijo) y si hay una obra activa en ese momento."""
    resultado = predecir_congestion_via(payload.direccion, payload.franja_horaria, payload.dia_semana)
    if not resultado.get("is_valid"):
        raise HTTPException(status_code=404, detail=resultado.get("errors", ["Sin histórico suficiente"]))
    return {"success": True, **resultado}

frontend_root = Path(__file__).resolve().parents[2] / "frontend"
frontend_dist = frontend_root / "dist"
frontend_assets = frontend_dist / "assets"

if frontend_assets.exists():
    app.mount("/assets", StaticFiles(directory=frontend_assets), name="frontend-assets")


@app.get("/")
def root() -> FileResponse:
    # En producción local, sirve el build de React; si no existe, usa el index de desarrollo.
    built_index = frontend_dist / "index.html"
    dev_index = frontend_root / "index.html"
    frontend_path = built_index if built_index.exists() else dev_index
    return FileResponse(frontend_path)


@app.get("/health")
def health() -> Dict[str, str]:
    # Devuelve un estado básico del servicio para comprobar que el backend responde.
    return {"status": "ok", "service": "backend"}


@app.get("/db-check")
def db_check() -> Dict[str, str]:
    # Intenta conectar con PostgreSQL usando las variables de entorno del entorno local.
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "datalake")
    user = os.getenv("POSTGRES_USER", "datalake")
    password = os.getenv("POSTGRES_PASSWORD", "datalake_local")

    try:
        conn = psycopg2.connect(host=host, port=port, dbname=db, user=user, password=password)
        conn.close()
        return {"status": "ok", "database": "postgres"}
    except Exception as exc:
        return {"status": "error", "database": str(exc)}


@app.post("/gobierno/contratos/sincronizar")
def sincronizar_contratos(admin: dict = Depends(require_admin)) -> Dict[str, object]:
    """Persiste los contratos técnicos y los publica en OpenMetadata si está habilitado."""
    try:
        return ContractsManager().synchronize(triggered_by=admin["id"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No se pudieron sincronizar los contratos: {exc}")


@app.get("/admin/catalogo/historial")
def admin_historial_catalogo(user: dict = Depends(require_audit_access)) -> Dict[str, object]:
    """Gobernanza común: historial de cambios en las reglas técnicas compartidas."""
    return {"historial": list_catalog_sync_history()}


@app.post("/ingesta/validar")
def validar_archivo(file: UploadFile = File(...)) -> Dict[str, object]:
    # Revisa que el archivo sea un CSV y lo valida con la lógica del servicio.
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")

    content = file.file.read().decode("utf-8")
    result = ingest_affectaciones(content)
    return {
        "filename": file.filename,
        "is_valid": result["is_valid"],
        "errors": result["errors"],
        "row_count": result["row_count"],
        "rows": result["rows"],
        "inserted": result.get("inserted", 0),
        "lake": result.get("lake", {}),
        "incidencias": result.get("incidencias", []),
    }


@app.post("/ingesta/afectaciones")
def cargar_afectaciones(
    file: UploadFile = File(...),
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
    visibilidad: str = "compartido",
    current_user: dict = Depends(require_write_access),
    _policy: dict = Depends(require_policy_accepted),
) -> Dict[str, object]:
    # Carga un CSV de afectaciones urbanas y devuelve un resumen de la ingestión.
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")

    entity = resolve_upload_municipio(current_user, entity)
    municipio_id = municipio_id or entity
    content = file.file.read()
    result = ingest_affectaciones(
        content,
        source_filename=file.filename,
        content_bytes=content,
        entity=entity,
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
        visibilidad=visibilidad,
    )
    return {
        "filename": file.filename,
        "is_valid": result["is_valid"],
        "status": result.get("status", "success" if result.get("is_valid") else "error"),
        "errors": result["errors"],
        "row_count": result["row_count"],
        "inserted": result.get("inserted", 0),
        "message": "Ingesta completada" if result.get("is_valid") else "Ingesta rechazada",
        "summary": result.get("summary", {
            "rows_received": result.get("row_count", 0),
            "rows_inserted": result.get("inserted", 0),
            "errors_count": len(result.get("errors", [])),
        }),
        "lake": result.get("lake", {}),
        "delivery": result.get("delivery", {}),
        "incidencias": result.get("incidencias", []),
    }


@app.get("/ingesta/lake/estado")
def verificar_lake(filename: str | None = None, dataset: str = "afectaciones_urbanas") -> Dict[str, object]:
    if not filename:
        raise HTTPException(status_code=400, detail="Se requiere el nombre del archivo")

    lake_root = get_lake_root()
    paths = {
        "staging": str(lake_root / "staging" / dataset / filename),
        "raw": str(resolve_lake_file_path(filename, dataset)),
    }
    local_status = "raw" if Path(paths["raw"]).exists() else "staging" if Path(paths["staging"]).exists() else "not_found"
    s3 = inspect_s3_lake_object(filename, dataset)
    return {
        "filename": filename,
        "dataset": dataset,
        "status": s3.get("status", local_status) if s3.get("enabled") else local_status,
        "raw_file_exists": Path(paths["raw"]).exists(),
        "staging_file_exists": Path(paths["staging"]).exists(),
        "paths": paths,
        "s3": s3,
        "lake_root": str(lake_root),
    }


@app.post("/ingesta/lake/mover")
def mover_lake(filename: str, dataset: str = "afectaciones_urbanas") -> Dict[str, object]:
    result = move_staged_file_to_raw(filename, dataset_name=dataset)
    return {
        "filename": filename,
        "dataset": dataset,
        "is_valid": result["is_valid"],
        "status": "moved" if result["is_valid"] else "error",
        "errors": result["errors"],
        "lake": result.get("lake", {}),
    }


@app.get("/analysis/sql/afectaciones-kpis")
def afectaciones_kpis_sql(table: str = "afectaciones_urbanas") -> Dict[str, object]:
    # KPI Q3 de afectaciones urbanas calculado con Trino sobre la tabla Hive/SeaweedFS.
    return get_afectaciones_kpis(table)


@app.post("/analysis/layers/afectaciones")
def construir_capas_afectaciones() -> Dict[str, object]:
    # Construye únicamente las capas tipada y descriptiva; no resuelve preguntas ni impacto avanzado.
    return build_afectaciones_layers()


@app.post("/iceberg/gold/afectaciones/construir")
def construir_capas_gold_afectaciones() -> Dict[str, object]:
    """Materializa las capas Iceberg Curated y Analytics de afectaciones."""
    return build_afectaciones_layers()


@app.get("/analysis/layer5/afectaciones")
def construir_layer5_afectaciones(table: str = "afectaciones_urbanas") -> Dict[str, object]:
    """Construye reglas y rankings reproducibles para la capa 5."""
    return build_afectaciones_layer5(table)


@app.get("/analysis/layer5/{dataset}")
def construir_layer5_dimension(dataset: str) -> Dict[str, object]:
    """Capa 5 genérica: reglas de frecuencia para cualquier dataset con layer5_group_by en su contrato."""
    return build_dimension_layer5(dataset)


@app.get("/analysis/layer4/afectaciones/resumen")
def get_layer4_summary(table: str = "afectaciones_urbanas") -> Dict[str, object]:
    """Devuelve un resumen operativo de la capa 4 para el frontend y la capa analítica."""
    return get_afectaciones_layer4_summary(table)


def _number(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _counter(counter: Counter, label: str, limit: int = 8) -> list[Dict[str, object]]:
    return [{label: key, "count": value} for key, value in counter.most_common(limit)]


def _sum_by(rows: list[Dict[str, Any]], group_key: str, value_key: str, label: str, limit: int = 8) -> list[Dict[str, object]]:
    totals: Dict[str, float] = {}
    for row in rows:
        group = str(row.get(group_key) or "Sin dato")
        totals[group] = totals.get(group, 0.0) + _number(row.get(value_key))
    return [{label: key, "count": round(value, 1)} for key, value in sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]]


def _list_dimension_records(dataset: str, municipio_id: str | None, limit: int = 500) -> Dict[str, Any]:
    # municipio_id=None => sin filtrar (vista agregada de todos los municipios, para
    # los roles admin_estatal/consumidor del cuadro de mando).
    try:
        host = os.getenv("POSTGRES_HOST", "postgres")
        port = os.getenv("POSTGRES_PORT", "5432")
        db = os.getenv("POSTGRES_DB", "datalake")
        user = os.getenv("POSTGRES_USER", "datalake")
        password = os.getenv("POSTGRES_PASSWORD", "datalake_local")
        conn = psycopg2.connect(host=host, port=port, dbname=db, user=user, password=password)
        cur = conn.cursor()
        if municipio_id:
            cur.execute(
                "SELECT id, data FROM dimension_records WHERE dataset = %s AND municipio_id = %s ORDER BY id DESC LIMIT %s",
                (dataset, municipio_id, limit),
            )
        else:
            cur.execute(
                "SELECT id, data FROM dimension_records WHERE dataset = %s ORDER BY id DESC LIMIT %s",
                (dataset, limit),
            )
        rows = []
        for row_id, data in cur.fetchall():
            payload = data if isinstance(data, dict) else json.loads(data)
            rows.append({"id": row_id, **payload})
        cur.close()
        conn.close()
        return {"is_valid": True, "errors": [], "rows": rows}
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)], "rows": []}


@app.get("/analysis/cuadro-mando")
def get_cuadro_mando(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, object]:
    """Devuelve KPIs agregados para el cuadro de mando, sin alterar la pantalla de dimensiones.

    admin_estatal y consumidor ven la vista agregada de todos los municipios
    (sin filtro); editor_municipio/lector_municipio quedan acotados al suyo."""
    municipio_id = None if current_user["role"] in (ROLE_ADMIN, ROLE_CONSUMIDOR) else current_user["municipio_id"]
    affectations = list_affectaciones(limit=500, municipio_id=municipio_id)
    its = list_its(limit=500, municipio_id=municipio_id)
    traffic = _list_dimension_records("movilidad_trafico", municipio_id, 500)
    parking = _list_dimension_records("movilidad_parking", municipio_id, 500)
    reserved = _list_dimension_records("movilidad_plazas_reservadas", municipio_id, 500)
    bike = _list_dimension_records("movilidad_carriles_bici", municipio_id, 500)
    occupancy = _list_dimension_records("ocupacion_permanente_espacio_publico", municipio_id, 500)

    affectation_rows = affectations.get("rows", [])
    its_rows = its.get("rows", [])
    traffic_rows = traffic.get("rows", [])
    parking_rows = parking.get("rows", [])
    reserved_rows = reserved.get("rows", [])
    bike_rows = bike.get("rows", [])
    occupancy_rows = occupancy.get("rows", [])
    parking_entries = []
    for row in parking_rows:
        total = _number(row.get("ocupacion_total"))
        if total > 0:
            parking_entries.append({"row": row, "rate": round((total - _number(row.get("ocupacion_libres"))) / total * 100, 1)})

    crossed = [*affectation_rows, *traffic_rows, *parking_rows, *occupancy_rows]
    priority_zones = [
        {"via": via, "score": min(count * 18, 100), "nivel": "alto" if count >= 3 else "medio", "reasons": [f"{count} registros cruzados"]}
        for via, count in Counter(row.get("direccion") or "Sin vía" for row in crossed).most_common(6)
    ]

    return {
        "is_valid": True,
        "municipio_id": municipio_id,
        "kpis": {
            "total_afectaciones": len(affectation_rows),
            "total_dispositivos_its": len(its_rows),
            "total_registros_movilidad": len(traffic_rows) + len(parking_rows) + len(reserved_rows) + len(bike_rows),
            "total_ocupaciones": len(occupancy_rows),
            "trafico_total": round(sum(_number(row.get("trafico_flujo")) for row in traffic_rows)),
            "ocupacion_parking_media": round(sum(entry["rate"] for entry in parking_entries) / len(parking_entries), 1) if parking_entries else 0,
            "superficie_ocupada_m2": round(sum(_number(row.get("ocupacion_superficie")) for row in occupancy_rows), 1),
            "plazas_reservadas": round(sum(_number(row.get("plaza_numero")) for row in reserved_rows)),
        },
        "summary": {
            "top_vias": _counter(Counter(row.get("direccion") or "Sin vía" for row in affectation_rows), "via"),
            "por_tipo_afectacion": _counter(Counter(row.get("tipo_afectacion") or "Sin tipo" for row in affectation_rows), "tipo_afectacion"),
            "por_hora": _counter(Counter(row.get("hora_inicio") or "Sin hora" for row in affectation_rows), "hora"),
        },
        "mobility": {
            "trafico_por_via": _sum_by(traffic_rows, "direccion", "trafico_flujo", "via"),
            "plazas_por_tipo": _sum_by(reserved_rows, "tipo_plaza", "plaza_numero", "tipo_plaza"),
            "parking_ocupacion": [{"parking": entry["row"].get("nombre") or entry["row"].get("direccion") or "Parking", "count": entry["rate"]} for entry in parking_entries[:8]],
        },
        "its": {"por_categoria": _counter(Counter(row.get("categoria") or "Sin categoría" for row in its_rows), "categoria")},
        "occupancy": {
            "por_tipo": _counter(Counter(row.get("tipo_ocupacion") or "Sin tipo" for row in occupancy_rows), "tipo_ocupacion"),
            "superficie_por_via": _sum_by(occupancy_rows, "direccion", "ocupacion_superficie", "via"),
        },
        "priority_zones": priority_zones,
        "errors": [*affectations.get("errors", []), *its.get("errors", []), *traffic.get("errors", []), *parking.get("errors", []), *reserved.get("errors", []), *bike.get("errors", []), *occupancy.get("errors", [])],
    }


@app.post("/ingesta/its")
def cargar_its(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_write_access),
    _policy: dict = Depends(require_policy_accepted),
) -> Dict[str, object]:
    # Carga un CSV de Control y Gestión ITS, mismo patrón que /ingesta/afectaciones.
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")

    content = file.file.read()
    result = ingest_its(content, source_filename=file.filename, content_bytes=content)
    return {
        "filename": file.filename,
        "is_valid": result["is_valid"],
        "status": result.get("status", "success" if result.get("is_valid") else "error"),
        "errors": result["errors"],
        "row_count": result["row_count"],
        "inserted": result.get("inserted", 0),
        "message": "Ingesta completada" if result.get("is_valid") else "Ingesta rechazada",
        "summary": result.get("summary", {
            "rows_received": result.get("row_count", 0),
            "rows_inserted": result.get("inserted", 0),
            "errors_count": len(result.get("errors", [])),
        }),
        "lake": result.get("lake", {}),
    }


@app.post("/ingesta/movilidad")
def registrar_movilidad(
    file: UploadFile = File(...),
    dataset: str = "movilidad_trafico",
    entity: str = "municipio_demo",
    period: str = "",
    schema_version: str = "v1",
    replace: bool = False,
    entry_channel: str = "web_manual",
    sender: str = "usuario_local",
    municipio_id: str | None = None,
    anio: int | None = None,
    fecha_hora_inicio_remision: datetime | None = None,
    visibilidad: str = "compartido",
    current_user: dict = Depends(require_write_access),
    _policy: dict = Depends(require_policy_accepted),
) -> Dict[str, object]:
    """Capa 0: registra la recepción de un dataset de Movilidad sin ingerir sus filas."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")

    entity = resolve_upload_municipio(current_user, entity)
    municipio_id = municipio_id or entity
    content = file.file.read()
    result = register_mobility_delivery(
        file.filename,
        content,
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
        visibilidad=visibilidad,
    )
    return {"filename": file.filename, **result}


@app.post("/ingesta/capa1/preservar")
def preservar_capa1(
    file: UploadFile = File(...),
    dataset: str = "movilidad_trafico",
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
    visibilidad: str = "compartido",
    current_user: dict = Depends(require_write_access),
    _policy: dict = Depends(require_policy_accepted),
) -> Dict[str, object]:
    """Capa 1: preserva el CSV aceptado en staging y genera trazabilidad por fila."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")
    entity = resolve_upload_municipio(current_user, entity)
    municipio_id = municipio_id or entity
    content = file.file.read()
    result = run_automatic_dimension_pipeline(
        file.filename,
        content,
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
        visibilidad=visibilidad,
    )
    return {"filename": file.filename, **result}


def _get_dataset_contract(dataset: str) -> dict:
    contract = DATASET_CONTRACTS.get(dataset)
    if contract is None:
        raise HTTPException(status_code=400, detail="Dataset no permitido")
    return contract


@app.post("/hive/bronze/validar")
def validar_capa2(file: UploadFile = File(...), dataset: str = "gestion_afectaciones_urbanas") -> Dict[str, object]:
    """Capa 2: valida la admisibilidad completa del fichero sin modificarlo."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")
    contract = _get_dataset_contract(dataset)
    result = validate_csv_text(
        file.file.read(),
        required_columns=contract["required_columns"],
        business_rules=contract,
    )
    return {"filename": file.filename, "dataset": dataset, **result}


@app.post("/iceberg/silver/normalizar")
def normalizar_capa3(
    file: UploadFile = File(...),
    dataset: str = "gestion_afectaciones_urbanas",
    entity: str = "municipio_demo",
    period: str = "",
) -> Dict[str, object]:
    """Capa 3: normaliza solo ficheros previamente admisibles en Capa 2."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")
    contract = _get_dataset_contract(dataset)
    return {
        "filename": file.filename,
        "dataset": dataset,
        **normalize_dimension_dataset(file.file.read(), dataset, contract, entity, period),
    }


@app.post("/hive/bronze/preservar")
def preservar_bronze(
    file: UploadFile = File(...),
    dataset: str = "movilidad_trafico",
    entity: str = "municipio_demo",
    period: str = "",
) -> Dict[str, object]:
    """Capa 1: registra y preserva el original antes de la validación de Capa 2."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")
    _get_dataset_contract(dataset)
    return {"filename": file.filename, **preserve_dimension_delivery(file.filename, file.file.read(), dataset, entity, period)}


@app.post("/ingesta/recepciones/its")
def registrar_recepcion_its(file: UploadFile = File(...)) -> Dict[str, object]:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")
    return {"filename": file.filename, **register_dimension_delivery(file.filename, file.file.read(), "control_gestion_its")}


@app.post("/ingesta/recepciones/ocupacion")
def registrar_recepcion_ocupacion(file: UploadFile = File(...)) -> Dict[str, object]:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")
    return {"filename": file.filename, **register_dimension_delivery(file.filename, file.file.read(), "ocupacion_permanente_espacio_publico")}


@app.post("/ingesta/its/capa0")
def registrar_its_capa0(file: UploadFile = File(...), entity: str = "municipio_demo", period: str = "", schema_version: str = "v1", municipio_id: str | None = None, anio: int | None = None, fecha_hora_inicio_remision: datetime | None = None, numero_intento_carga: int = 1) -> Dict[str, object]:
    """Capa 0: registra la recepción de control_gestion_its sin ingerir filas."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")
    content = file.file.read()
    return {"filename": file.filename, **register_dimension_delivery(file.filename, content, "control_gestion_its", entity, period, schema_version, municipio_id=municipio_id, anio=anio, fecha_hora_inicio_remision=fecha_hora_inicio_remision, numero_intento_carga=numero_intento_carga)}


@app.post("/ingesta/ocupacion/capa0")
def registrar_ocupacion_capa0(file: UploadFile = File(...), entity: str = "municipio_demo", period: str = "", schema_version: str = "v1", municipio_id: str | None = None, anio: int | None = None, fecha_hora_inicio_remision: datetime | None = None, numero_intento_carga: int = 1) -> Dict[str, object]:
    """Capa 0: registra la recepción de ocupación permanente sin ingerir filas."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV")
    content = file.file.read()
    return {"filename": file.filename, **register_dimension_delivery(file.filename, content, "ocupacion_permanente_espacio_publico", entity, period, schema_version, municipio_id=municipio_id, anio=anio, fecha_hora_inicio_remision=fecha_hora_inicio_remision, numero_intento_carga=numero_intento_carga)}


@app.get("/analysis/sql/its-kpis")
def its_kpis_sql(table: str = "control_gestion_its") -> Dict[str, object]:
    # KPIs simples de inventario ITS (total por categoría/titularidad) calculados con Trino.
    return get_its_kpis(table)


@app.get("/its")
def obtener_its(limit: int = 10) -> Dict[str, object]:
    result = list_its(limit=limit)
    return {
        "is_valid": result["is_valid"],
        "errors": result["errors"],
        "rows": result["rows"],
    }


@app.get("/afectaciones")
def obtener_afectaciones(limit: int = 10) -> Dict[str, object]:
    result = list_affectaciones(limit=limit)
    return {
        "is_valid": result["is_valid"],
        "errors": result["errors"],
        "rows": result["rows"],
    }


@app.get("/analysis/preguntas")
def listar_preguntas(dimension: str | None = None) -> Dict[str, object]:
    if not dimension:
        return {"count": len(ANALYSIS_QUESTIONS), "questions": ANALYSIS_QUESTIONS}

    filtered = [q for q in ANALYSIS_QUESTIONS if q["dimension"] == dimension]
    return {"count": len(filtered), "questions": filtered}


@app.get("/analysis/contexto/ollama/{dimension}")
def contexto_ollama(dimension: str, limit: int = 200, question_id: str = "AQ1") -> Dict[str, object]:
    """Prepara un payload de contexto analítico para que Ollama interprete los datos de cualquier dimensión."""
    question = next((q for q in ANALYSIS_QUESTIONS if q["id"] == question_id and q["dimension"] == dimension), None)
    if question is None:
        return {
            "is_valid": False,
            "errors": [f"question_id no válido para {dimension}: {question_id}"],
            "payload": None,
        }
    return build_ollama_analysis_context(dataset=dimension, limit=limit, question=question)


@app.post("/analysis/interpretar/ollama/{dimension}")
def interpretar_ollama(dimension: str, limit: int = 200, question_id: str = "AQ1") -> Dict[str, object]:
    """Genera una interpretación textual de cualquier dimensión usando su contexto analítico y Ollama."""
    question = next((q for q in ANALYSIS_QUESTIONS if q["id"] == question_id and q["dimension"] == dimension), None)
    if question is None:
        return {
            "is_valid": False,
            "errors": [f"question_id no válido para {dimension}: {question_id}"],
            "model": None,
            "response": None,
            "context": None,
        }

    context_result = build_ollama_analysis_context(dataset=dimension, limit=limit, question=question)
    if not context_result["is_valid"]:
        return context_result

    ollama_result = call_ollama_analysis(context_result["payload"])
    return {
        "is_valid": ollama_result["is_valid"],
        "errors": ollama_result["errors"],
        "model": ollama_result.get("model"),
        "response": ollama_result.get("response"),
        "context": context_result["payload"],
    }


@app.post("/db/init")
def init_db() -> Dict[str, str]:
    # Crea la tabla inicial de datasets si aún no existe.
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "datalake")
    user = os.getenv("POSTGRES_USER", "datalake")
    password = os.getenv("POSTGRES_PASSWORD", "datalake_local")

    try:
        conn = psycopg2.connect(host=host, port=port, dbname=db, user=user, password=password)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS datasets (id SERIAL PRIMARY KEY, nombre TEXT NOT NULL, fecha TEXT NOT NULL)")
        conn.commit()
        cur.close()
        conn.close()
        return {"status": "ok", "message": "Tabla datasets creada o ya existente"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
