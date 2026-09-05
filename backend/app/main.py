# Archivo: main.py
# Resumen: Punto de entrada del backend FastAPI para comprobar salud, validar CSV y preparar una tabla inicial.
# Autor: Alba Sánchez Ibáñez
# Fecha: 2026-07-27

import os
import json
from pathlib import Path
from datetime import datetime
from typing import Any, Dict

import psycopg2
from fastapi import Depends, FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.analysis_questions import ANALYSIS_QUESTIONS
from app.ml import get_model_metrics, predict_congestion_ml, train_congestion_model
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
    get_afectaciones_trafico_resumen,
    get_carriles_bici_kpis,
    get_its_kpis,
    get_its_pmr_coverage,
    get_lake_root,
    get_catalog_entry_csv,
    get_eq1_parking_trafico,
    get_eq3_ocupacion_afectaciones,
    get_eq4_ocupacion_carga_trafico,
    get_ingest_delivery_detail,
    list_catalog_entries,
    get_ocupacion_superficie_por_via,
    get_parking_por_via,
    normalize_catalog_value,
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
    try:
        train_congestion_model()
    except Exception:
        pass  # se reintenta on-demand la primera vez que se pida /api/ml/movilidad/modelo


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


@app.get("/admin/ingestas/{delivery_id}")
def admin_detalle_ingesta(delivery_id: int, user: dict = Depends(require_roles(ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMIDOR))) -> Dict[str, object]:
    """Detalle de una entrega: eventos de recepción e incidencias de validación
    fila a fila (metadatos de Capa 0-2 que la tabla resumen no puede mostrar)."""
    detail = get_ingest_delivery_detail(delivery_id)
    if not detail.get("is_valid"):
        raise HTTPException(status_code=404, detail=detail.get("errors", ["No encontrado"]))
    if user["role"] == ROLE_CONSUMIDOR and detail["entrega"].get("visibilidad") != "compartido":
        raise HTTPException(status_code=403, detail="Esta entrega no está marcada como compartida.")
    return detail


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


@app.get("/catalogo/entregas")
def catalogo_entregas(user: dict = Depends(get_current_user), _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """Catálogo de datos navegable: entregas reales (no solo esquema), abierto a
    cualquier rol autenticado. Soberanía del dato: cada usuario ve las suyas propias
    (cualquiera que sea su visibilidad) más las de cualquier otro municipio marcadas
    "compartido" -- admin_estatal/consumidor ven además el conjunto completo."""
    municipio_scope = None if user["role"] in (ROLE_ADMIN, ROLE_CONSUMIDOR) else user["municipio_id"]
    return {"entregas": list_catalog_entries(municipio_scope)}


@app.get("/catalogo/entregas/{delivery_id}/descargar")
def catalogo_descargar(delivery_id: int, user: dict = Depends(get_current_user), _policy: dict = Depends(require_policy_accepted)) -> Response:
    """Descarga el CSV real (dato curated, ya tipado y validado) de una entrega,
    respetando la misma regla de visibilidad que /catalogo/entregas."""
    municipio_scope = None if user["role"] in (ROLE_ADMIN, ROLE_CONSUMIDOR) else user["municipio_id"]
    result = get_catalog_entry_csv(delivery_id, municipio_scope)
    if not result.get("is_valid"):
        status_code = 403 if result.get("forbidden") else 404
        raise HTTPException(status_code=status_code, detail=result.get("errors", ["No se pudo generar la descarga"]))
    return Response(
        content=result["content"],
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{result["filename"]}"'},
    )


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
def reglas_congestion(_policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
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
        "prioridades": resultado["prioridades"][:10],
    }


@app.post("/api/ml/movilidad/predecir")
def predecir_congestion(payload: CongestionPredictionRequest, _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """Estima la congestión esperada en una vía a partir de su propio histórico de tráfico
    (percentiles reales, no un umbral fijo) y si hay una obra activa en ese momento."""
    resultado = predecir_congestion_via(payload.direccion, payload.franja_horaria, payload.dia_semana)
    if not resultado.get("is_valid"):
        raise HTTPException(status_code=404, detail=resultado.get("errors", ["Sin histórico suficiente"]))
    return {"success": True, **resultado}


@app.get("/api/ml/movilidad/modelo")
def modelo_congestion_metricas(_policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """Métricas del modelo entrenado (RandomForestClassifier sobre las 9348 mediciones
    reales de tráfico): accuracy real en test y qué variables pesan más."""
    metrics = get_model_metrics()
    if not metrics.get("is_valid"):
        raise HTTPException(status_code=500, detail=metrics.get("errors", ["No se pudo entrenar el modelo"]))
    return metrics


@app.post("/api/ml/movilidad/modelo/reentrenar")
def modelo_congestion_reentrenar(admin: dict = Depends(require_admin)) -> Dict[str, object]:
    """Fuerza un reentrenamiento (p. ej. tras subir más mediciones de tráfico)."""
    return train_congestion_model()


@app.post("/api/ml/movilidad/predecir-ml")
def predecir_congestion_ml_endpoint(payload: CongestionPredictionRequest, _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """Predicción con el modelo entrenado (generaliza patrones entre vías), a
    diferencia de /predecir que solo mira el histórico propio de esa vía."""
    if not payload.dia_semana or not payload.franja_horaria:
        raise HTTPException(status_code=400, detail="dia_semana y franja_horaria son obligatorios para el modelo entrenado")
    hora_punta = payload.franja_horaria in ("06:00-09:00", "16:00-20:00")
    resultado = predict_congestion_ml(payload.direccion, payload.dia_semana, payload.franja_horaria, hora_punta)
    if not resultado.get("is_valid"):
        raise HTTPException(status_code=500, detail=resultado.get("errors", ["No se pudo predecir"]))
    return {"success": True, **resultado}


@app.get("/analysis/eq1/parking-trafico")
def eq1_parking_trafico(_policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """EQ1: efecto mariposa entre saturación de parking y tráfico circundante
    (cruce por proximidad geográfica real, no por nombre de vía -- ver plan)."""
    resultado = get_eq1_parking_trafico()
    if not resultado.get("is_valid"):
        raise HTTPException(status_code=500, detail=resultado.get("errors", ["Error calculando el cruce"]))
    return resultado


@app.get("/analysis/eq3/ocupacion-afectaciones")
def eq3_ocupacion_afectaciones(_policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """EQ3: vías donde coinciden terrazas y afectaciones activas, con marca PMR."""
    resultado = get_eq3_ocupacion_afectaciones()
    if not resultado.get("is_valid"):
        raise HTTPException(status_code=500, detail=resultado.get("errors", ["Error calculando el cruce"]))
    return resultado


@app.get("/analysis/eq4/ocupacion-carga-trafico")
def eq4_ocupacion_carga_trafico(_policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """EQ4: vías con alta densidad de terrazas y pocas plazas de carga/descarga."""
    resultado = get_eq4_ocupacion_carga_trafico()
    if not resultado.get("is_valid"):
        raise HTTPException(status_code=500, detail=resultado.get("errors", ["Error calculando el cruce"]))
    return resultado


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
def afectaciones_kpis_sql(table: str = "afectaciones_urbanas", _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
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
def construir_layer5_afectaciones(table: str = "afectaciones_urbanas", _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """Construye reglas y rankings reproducibles para la capa 5."""
    return build_afectaciones_layer5(table)


@app.get("/analysis/layer5/{dataset}")
def construir_layer5_dimension(dataset: str, _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """Capa 5 genérica: reglas de frecuencia para cualquier dataset con layer5_group_by en su contrato."""
    return build_dimension_layer5(dataset)


@app.get("/analysis/layer4/afectaciones/resumen")
def get_layer4_summary(table: str = "afectaciones_urbanas", _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """Devuelve un resumen operativo de la capa 4 para el frontend y la capa analítica."""
    return get_afectaciones_layer4_summary(table)


def _nivel_zona(nivel: str) -> str:
    """Normaliza un nivel de riesgo ('Crítico'/'Alto'...) a la clave usada por el CSS
    del frontend (risk-alto, risk-critico...): minúsculas y sin tildes."""
    return normalize_catalog_value(nivel) or "bajo"


@app.get("/analysis/cuadro-mando")
def get_cuadro_mando(current_user: Dict[str, Any] = Depends(get_current_user), _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    """Cuadro de mando construido sobre las tablas reales de Trino (lake.curated/
    lake.analytics), las mismas que ya usan los endpoints de Capa 4/5. Reemplaza la
    versión anterior, que leía de una tabla Postgres (`dimension_records`) que ningún
    código de este backend escribe -- sus datos eran de una fuente externa (NiFi),
    desconectados por completo del pipeline real de esta plataforma.

    admin_estatal y consumidor ven la vista agregada de todos los municipios;
    editor_municipio/lector_municipio filtran por su propio municipio en las
    consultas nuevas (ver limitación conocida: las funciones de Capa 5 reutilizadas
    -- build_afectaciones_layer5, build_movilidad_trafico_layer5,
    build_dimension_layer5 -- todavía no filtran por municipio)."""
    municipio_scoped = current_user["role"] not in (ROLE_ADMIN, ROLE_CONSUMIDOR)
    municipio_prefix = current_user["municipio_id"] if municipio_scoped else None

    afectaciones_resumen = get_afectaciones_layer4_summary()
    afectaciones_criticidad = build_afectaciones_layer5()
    trafico_congestion = build_movilidad_trafico_layer5()
    afectaciones_trafico = get_afectaciones_trafico_resumen()
    its_layer5 = build_dimension_layer5("control_gestion_its")
    its_pmr = get_its_pmr_coverage(municipio_prefix)
    parking_layer5 = build_dimension_layer5("movilidad_parking")
    parking_por_via = get_parking_por_via()
    reservadas_layer5 = build_dimension_layer5("movilidad_plazas_reservadas")
    carriles_bici = get_carriles_bici_kpis(municipio_prefix)
    ocupacion_layer5 = build_dimension_layer5("ocupacion_permanente_espacio_publico")
    ocupacion_superficie = get_ocupacion_superficie_por_via(municipio_prefix)
    eq1_parking_trafico = get_eq1_parking_trafico(trafico_congestion=trafico_congestion)
    eq3_ocupacion_afectaciones = get_eq3_ocupacion_afectaciones()
    eq4_ocupacion_carga = get_eq4_ocupacion_carga_trafico(trafico_congestion=trafico_congestion)
    modelo_ml = get_model_metrics()

    errors = [
        result.get("errors", [])
        for result in (
            afectaciones_resumen, afectaciones_criticidad, trafico_congestion, afectaciones_trafico,
            its_layer5, its_pmr, parking_layer5, parking_por_via, reservadas_layer5,
            carriles_bici, ocupacion_layer5, ocupacion_superficie,
            eq1_parking_trafico, eq3_ocupacion_afectaciones, eq4_ocupacion_carga, modelo_ml,
        )
        if not result.get("is_valid")
    ]

    priority_zones = []
    for entrada in (afectaciones_criticidad.get("ranking_vias") or [])[:5]:
        priority_zones.append({
            "via": entrada.get("nombre") or entrada.get("via") or "Sin vía",
            "score": round(entrada.get("pct_impacto", 0)),
            "nivel": _nivel_zona(entrada.get("nivel", "")),
            "reasons": [f"Obra ({entrada.get('tipo_intervencion') or 'sin dato'}): {entrada.get('pct_impacto', 0)}% de variación de tráfico"],
        })
    for entrada in (trafico_congestion.get("prioridades") or [])[:5]:
        pct = entrada.get("pct_tiempo_alto_critico", 0)
        nivel = "critico" if pct >= 50 else "alto" if pct >= 25 else "medio" if pct >= 10 else "bajo"
        priority_zones.append({
            "via": entrada.get("direccion") or "Sin vía",
            "score": round(pct),
            "nivel": nivel,
            "reasons": entrada.get("recomendaciones") or [],
        })
    priority_zones.sort(key=lambda zone: zone["score"], reverse=True)

    parking_saturado_pct = next(
        (regla["porcentaje"] for regla in parking_layer5.get("reglas", []) if regla.get("categoria") is True),
        0.0,
    )
    parking_ocupacion_media = (
        round(sum(entry["count"] for entry in parking_por_via.get("por_via", [])) / len(parking_por_via["por_via"]), 1)
        if parking_por_via.get("por_via") else 0.0
    )

    return {
        "is_valid": True,
        "municipio_id": municipio_prefix,
        "kpis": {
            "total_afectaciones": afectaciones_resumen.get("kpis", {}).get("total_afectaciones", 0),
            "total_dispositivos_its": its_layer5.get("total_registros", 0),
            "total_registros_movilidad": (
                trafico_congestion.get("total_registros", 0)
                + parking_layer5.get("total_registros", 0)
                + reservadas_layer5.get("total_registros", 0)
                + carriles_bici.get("kpis", {}).get("total_segmentos", 0)
            ),
            "total_ocupaciones": ocupacion_layer5.get("total_registros", 0),
            "trafico_pct_alto_critico": trafico_congestion.get("metricas", {}).get("pct_alto_critico", 0.0),
            "ocupacion_parking_media": parking_ocupacion_media,
            "parking_pct_saturado": parking_saturado_pct,
            "superficie_ocupada_m2": ocupacion_superficie.get("total_m2", 0.0),
            "plazas_reservadas": reservadas_layer5.get("total_registros", 0),
            "carriles_bici_longitud_m": carriles_bici.get("kpis", {}).get("longitud_total_m", 0.0),
            "its_pct_accesibilidad_pmr": its_pmr.get("kpis", {}).get("pct_accesibilidad_pmr", 0.0),
        },
        "summary": afectaciones_resumen.get("summary", {"top_vias": [], "por_tipo_afectacion": [], "por_hora": []}),
        "mobility": {
            "trafico_por_via": [
                {"via": p.get("direccion"), "count": p.get("pct_tiempo_alto_critico", 0)}
                for p in (trafico_congestion.get("prioridades") or [])
            ],
            "plazas_por_tipo": [
                {"tipo_plaza": r.get("categoria"), "count": r.get("registros")}
                for r in reservadas_layer5.get("reglas", [])
            ],
            "parking_ocupacion": [
                {"parking": p.get("parking"), "count": p.get("count")}
                for p in parking_por_via.get("por_via", [])
            ],
        },
        "its": {
            "por_categoria": [
                {"categoria": r.get("categoria"), "count": r.get("registros")}
                for r in its_layer5.get("reglas", [])
            ],
        },
        "occupancy": {
            "por_tipo": [
                {"tipo_ocupacion": r.get("categoria"), "count": r.get("registros")}
                for r in ocupacion_layer5.get("reglas", [])
            ],
            "superficie_por_via": ocupacion_superficie.get("por_via", []),
        },
        "obras_impacto_trafico": afectaciones_trafico.get("obras", []),
        "priority_zones": priority_zones[:8],
        "cruces": {
            "eq1_parking_trafico": eq1_parking_trafico.get("resultados", []),
            "eq3_ocupacion_afectaciones": eq3_ocupacion_afectaciones.get("vias", []),
            "eq4_ocupacion_carga_trafico": eq4_ocupacion_carga.get("vias", []),
        },
        "modelo_predictivo": {
            "accuracy": modelo_ml.get("accuracy"),
            "filas_entrenamiento": modelo_ml.get("filas_entrenamiento"),
            "filas_test": modelo_ml.get("filas_test"),
            "importancia_features": modelo_ml.get("importancia_features", []),
        },
        "errors": [item for sublist in errors for item in sublist],
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
def its_kpis_sql(table: str = "control_gestion_its", _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    # KPIs simples de inventario ITS (total por categoría/titularidad) calculados con Trino.
    return get_its_kpis(table)


@app.get("/its")
def obtener_its(limit: int = 10, _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
    result = list_its(limit=limit)
    return {
        "is_valid": result["is_valid"],
        "errors": result["errors"],
        "rows": result["rows"],
    }


@app.get("/afectaciones")
def obtener_afectaciones(limit: int = 10, _policy: dict = Depends(require_policy_accepted)) -> Dict[str, object]:
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
