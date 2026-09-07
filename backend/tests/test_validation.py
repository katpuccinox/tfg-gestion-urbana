# Archivo: test_validation.py
# Resumen: Pruebas automáticas para comprobar la validación de archivos CSV en el backend.
# Autor: Alba
# Fecha: 2026-07-27

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import app.services as services_module
import app.main as main_module
from fastapi.testclient import TestClient

from app.contracts import (
    AFFECTACIONES_DATA_CONTRACT,
    ITS_DATA_CONTRACT,
    MOVILIDAD_DATA_CONTRACTS,
    OCUPACION_PERMANENTE_DATA_CONTRACT,
)
from app.auth import ROLE_EDITOR, require_write_access
from app.contracts_manager import ContractsManager
from app.main import app
from app.policies import require_policy_accepted
from app.services import list_affectaciones, list_its, parse_csv_rows, persist_affectaciones, validate_csv_text

FAKE_EDITOR_USER = {"id": 999, "name": "Editor de prueba", "email": "editor@test.local", "municipio_id": "MALAGA", "role": ROLE_EDITOR, "activo": True}


def test_validate_csv_accepts_required_columns_and_rows():
    # Comprueba que un CSV correcto pasa la validación sin errores.
    csv_text = "id,nombre,fecha\n1,Ayuntamiento,2024-01-01\n2,Parque,2024-02-01\n"
    result = validate_csv_text(csv_text, required_columns=["id", "nombre", "fecha"])

    assert result["is_valid"] is True
    assert result["errors"] == []
    assert result["row_count"] == 2


def test_affectaciones_contract_requires_complete_edint_header():
    csv_text = "id,nombre,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion\n1,Obra,2026-08-01,08:00,2026-08-01,10:00,obra,corte_trafico\n"

    result = validate_csv_text(
        csv_text,
        required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
        business_rules=AFFECTACIONES_DATA_CONTRACT,
    )

    assert result["is_valid"] is False
    assert "Faltan columnas requeridas: descripcion, direccion, latitud, longitud" in result["errors"][0]


def test_affectaciones_contract_accepts_empty_optional_values():
    csv_text = (
        "id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,"
        "tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas\n"
        "1,Obra,,,,,2026-08-01,08:00,2026-08-01,10:00,obra,corte_trafico,,,\n"
    )

    result = validate_csv_text(
        csv_text,
        required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
        business_rules=AFFECTACIONES_DATA_CONTRACT,
    )

    assert result["is_valid"] is True


def test_layer5_builds_data_driven_rules_from_analytic_rows(monkeypatch):
    monkeypatch.setattr(services_module, "run_trino_query_with_columns", lambda sql: {
        "columns": ["nombre", "direccion", "hora_inicio", "tipo_intervencion", "tipo_afectacion"],
        "rows": [
            ("Corte centro", "Calle Mayor", "08:00", "obra", "corte_trafico"),
            ("Evento centro", "Calle Mayor", "18:00", "evento", "ocupacion_acera"),
        ],
    })

    result = services_module.build_afectaciones_layer5()

    assert result["is_valid"] is True
    assert result["total_registros"] == 2
    assert result["reglas"][0]["tipo_intervencion"] == "obra"
    assert result["reglas"][0]["nivel"] == "Crítico"
    assert result["ranking_vias"][0]["via"] == "Calle Mayor"


def test_layer5_endpoint_exposes_service_result(monkeypatch):
    expected = {"is_valid": True, "model": "reglas_descriptivas", "total_registros": 1}
    monkeypatch.setattr(main_module, "build_afectaciones_layer5", lambda table, municipio_prefix=None: expected)

    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        response = TestClient(app).get("/analysis/layer5/afectaciones")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == expected


def test_layer4_summary_endpoint_exposes_service_result(monkeypatch):
    expected = {
        "is_valid": True,
        "source": "hive.analytics.afectaciones_urbanas_resumen",
        "kpis": {"total_afectaciones": 42},
        "summary": {"top_vias": [{"via": "Calle Mayor", "count": 12}]},
    }
    monkeypatch.setattr(main_module, "get_afectaciones_layer4_summary", lambda table, municipio_prefix=None: expected)

    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        response = TestClient(app).get("/analysis/layer4/afectaciones/resumen")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == expected


def test_layer4_summary_aggregates_kpis_without_counting_groups_as_records(monkeypatch):
    def fake_query(sql):
        if "sum(duracion_media_horas * total_afectaciones)" in sql:
            return [(250, 11.75, 70)]
        if "GROUP BY direccion" in sql:
            return [("Calle Mayor", 41)]
        if "GROUP BY tipo_afectacion" in sql:
            return [("corte_trafico", 88)]
        if "GROUP BY hora_inicio" in sql:
            return [(8, 44)]
        raise AssertionError(f"Consulta inesperada: {sql}")

    monkeypatch.setattr(services_module, "run_trino_query", fake_query)

    result = services_module.get_afectaciones_layer4_summary()

    assert result["is_valid"] is True
    assert result["kpis"] == {
        "total_afectaciones": 250,
        "duracion_media_horas": 11.75,
        "afectaciones_pmr": 70,
        "porcentaje_pmr": 28.0,
    }
    assert result["summary"]["top_vias"] == [{"via": "Calle Mayor", "count": 41}]


def test_ollama_context_endpoint_builds_structured_payload(monkeypatch):
    expected = {
        "is_valid": True,
        "errors": [],
        "payload": {
            "question": "¿Qué está ocurriendo?",
            "instruction": "Contexto para Ollama",
            "summary": {
                "total_afectaciones": 42,
                "top_vias": [{"via": "Calle Mayor", "count": 12}],
            },
        },
    }
    monkeypatch.setattr(main_module, "build_ollama_analysis_context", lambda dataset="afectaciones_urbanas", limit=200, question=None: expected)

    response = TestClient(app).get("/analysis/contexto/ollama/afectaciones_urbanas")

    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is True
    assert body["payload"]["summary"]["total_afectaciones"] == 42
    assert body["payload"]["summary"]["top_vias"][0]["via"] == "Calle Mayor"
    assert "instruction" in body["payload"]
    assert "Ollama" in body["payload"]["instruction"] or "ollama" in body["payload"]["instruction"].lower()


def test_mobility_layer0_endpoint_registers_dataset_without_ingesting_rows(monkeypatch):
    expected = {
        "is_valid": True,
        "status": "received",
        "decision": "accepted",
        "logical_key": "municipio_demo|movilidad|movilidad_trafico|_|v1",
    }
    monkeypatch.setattr(main_module, "register_mobility_delivery", lambda filename, content, **kwargs: {
        **expected,
        "dataset": kwargs["dataset"],
        "dimension": "movilidad",
        "grain": "una medición de tráfico en una ubicación y momento",
        "nature": "serie_temporal",
        "functional_date": True,
        "content_size_bytes": len(content),
    })
    content = b"id,nombre,latitud,longitud,fecha_inicio,hora_inicio\n1,Sensor,36.7,-4.4,2026-08-26,08:00\n"

    response = TestClient(app).post(
        "/ingesta/movilidad?dataset=movilidad_trafico",
        files={"file": ("movilidad_trafico.csv", content, "text/csv")},
    )

    assert response.status_code == 401


def test_mobility_layer0_rejects_unknown_dataset():
    content = b"id,nombre,latitud,longitud\n1,Sensor,36.7,-4.4\n"

    response = TestClient(app).post(
        "/ingesta/movilidad?dataset=movilidad_inexistente",
        files={"file": ("movilidad.csv", content, "text/csv")},
    )

    assert response.status_code == 401


def test_mobility_layer0_rejects_missing_columns(monkeypatch):
    monkeypatch.setattr(main_module, "register_mobility_delivery", lambda filename, content, **kwargs: {
        "is_valid": False,
        "status": "rejected",
        "decision": "invalid_schema",
        "errors": ["Faltan columnas requeridas"],
    })

    response = TestClient(app).post(
        "/ingesta/movilidad?dataset=movilidad_trafico",
        files={"file": ("movilidad.csv", b"id,nombre\n1,Sensor\n", "text/csv")},
    )

    assert response.status_code == 401


def test_layer0_accepts_known_dataset_without_validating_its_schema(monkeypatch):
    monkeypatch.setattr(services_module, "register_delivery", lambda *args, **kwargs: {
        "is_valid": True,
        "decision": "accepted",
        "status": "received",
    })

    result = services_module.register_dimension_delivery(
        "movilidad.csv",
        b"id,nombre\n1,Sensor\n",
        "movilidad_trafico",
    )

    assert result["is_valid"] is True
    assert result["decision"] == "accepted"


def test_layer2_rejects_schema_that_layer0_can_receive():
    app.dependency_overrides[require_write_access] = lambda: FAKE_EDITOR_USER
    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        response = TestClient(app).post(
            "/hive/bronze/validar?dataset=movilidad_trafico",
            files={"file": ("movilidad.csv", b"id,nombre\n1,Sensor\n", "text/csv")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is False
    assert body["validation_metadata"]["resultado_validacion"] == "rechazado"
    assert any(incident["codigo_regla"] == "CSV_REQUIRED_COLUMNS" for incident in body["incidencias"])


def test_layer3_normalizes_business_rows_separately_from_control_metadata():
    content = (
        b"id,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,trafico_flujo,trafico_vehiculo\n"
        b"sensor-1,36.72,-4.42,2026-08-01,08:00,2026-08-01,10:00,42,PESADO\n"
    )

    result = services_module.normalize_dimension_dataset(
        content,
        dataset="movilidad_trafico",
        contract=MOVILIDAD_DATA_CONTRACTS["movilidad_trafico"],
        entity="malaga",
        period="2026-08",
    )

    assert result["is_valid"] is True
    assert result["control"]["dataset_id"] == "malaga|movilidad|movilidad_trafico|2026-08|v1"
    assert result["control"]["validacion_id"] == result["validation_metadata"]["validacion_id"]
    assert result["business_rows"] == [{
        "id": "sensor-1",
        "latitud": "36.72",
        "longitud": "-4.42",
        "fecha_inicio": "2026-08-01",
        "hora_inicio": "08:00",
        "fecha_fin": "2026-08-01",
        "hora_fin": "10:00",
        "trafico_flujo": "42",
        "trafico_vehiculo": "pesado",
        "dataset_id": "malaga|movilidad|movilidad_trafico|2026-08|v1",
        "row_id_tecnico": result["business_rows"][0]["row_id_tecnico"],
    }]


def test_layer3_flattens_embedded_line_breaks_for_hive_csv_rows():
    content = (
        b'id,categoria,nombre,descripcion,latitud,longitud\n'
        b'ITS-1,semaforo,Equipo,"Linea uno\nLinea dos",36.72,-4.42\n'
    )

    result = services_module.normalize_dimension_dataset(
        content,
        dataset="control_gestion_its",
        contract=ITS_DATA_CONTRACT,
        entity="CORDOBA",
        period="Anual",
    )

    assert result["is_valid"] is True
    assert result["business_rows"][0]["descripcion"] == "Linea uno Linea dos"


def test_layer3_does_not_normalize_a_dataset_rejected_by_layer2():
    app.dependency_overrides[require_write_access] = lambda: FAKE_EDITOR_USER
    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        response = TestClient(app).post(
            "/iceberg/silver/normalizar?dataset=movilidad_trafico",
            files={"file": ("movilidad.csv", b"id,nombre\n1,Sensor\n", "text/csv")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is False
    assert body["status"] == "rejected"
    assert body["control"] is None
    assert body["business_rows"] == []


def test_delivery_period_accepts_dates_in_declared_quarter():
    content = b"id,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,trafico_flujo\n1,36.7,-4.4,2026-04-01,08:00,2026-06-30,09:00,100\n"

    result = services_module.validate_delivery_period(
        content,
        MOVILIDAD_DATA_CONTRACTS["movilidad_trafico"],
        2026,
        "Trimestre 2",
    )

    assert result["is_valid"] is True


def test_delivery_period_rejects_dates_outside_declared_quarter():
    content = b"id,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,trafico_flujo\n1,36.7,-4.4,2026-07-01,08:00,2026-07-01,09:00,100\n"

    result = services_module.validate_delivery_period(
        content,
        MOVILIDAD_DATA_CONTRACTS["movilidad_trafico"],
        2026,
        "Trimestre 2",
    )

    assert result["is_valid"] is False
    assert result["incidencias"][0]["codigo_regla"] == "CSV_DECLARED_PERIOD_MISMATCH"


def test_automatic_pipeline_continues_to_layer4_with_warnings(monkeypatch):
    content = b"id,latitud,longitud\n1,36.7,-4.4\n"
    monkeypatch.setattr(services_module, "preserve_dimension_delivery", lambda *args, **kwargs: {
        "is_valid": True,
        "decision": "accepted",
        "status": "preserved",
        "delivery_id": 42,
        "logical_key": "MALAGA|movilidad|movilidad_carriles_bici|Anual|v1",
    })
    monkeypatch.setattr(services_module, "validate_csv_text", lambda *args, **kwargs: {
        "is_valid": True,
        "validation_status": "accepted_with_warnings",
        "errors": [],
        "incidencias": [{"severity": "advertencia"}],
        "row_count": 1,
        "validation_metadata": {"resultado_validacion": "apto_para_ingesta"},
    })
    monkeypatch.setattr(services_module, "persist_validation_incidents", lambda *args, **kwargs: None)
    monkeypatch.setattr(services_module, "persist_validation_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(services_module, "mark_dataset_ingested", lambda *args, **kwargs: None)
    monkeypatch.setattr(services_module, "persist_dataset_control", lambda *args, **kwargs: None)
    monkeypatch.setattr(services_module, "normalize_dimension_dataset", lambda *args, **kwargs: {
        "is_valid": True,
        "status": "normalized",
        "errors": [],
        "incidencias": [{"severity": "advertencia"}],
        "business_rows": [{"id": "1", "row_id_tecnico": "row-1", "dataset_id": "dataset-1"}],
    })
    monkeypatch.setattr(services_module, "publish_normalized_dataset", lambda *args, **kwargs: {
        "is_valid": True,
        "status": "published",
        "table": "hive.normalized.movilidad_carriles_bici",
    })
    monkeypatch.setattr(services_module, "build_dimension_layer4", lambda *args, **kwargs: {
        "is_valid": True,
        "curated_table": "lake.curated.movilidad_carriles_bici",
        "analytics_table": "lake.analytics.movilidad_carriles_bici_resumen",
    })
    monkeypatch.setattr(services_module, "record_pipeline_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr(services_module, "update_delivery_status", lambda *args: None)

    result = services_module.run_automatic_dimension_pipeline(
        "carriles.csv",
        content,
        dataset="movilidad_carriles_bici",
        municipio_id="MALAGA",
        period="Anual",
    )

    assert result["is_valid"] is True
    assert result["status"] == "completed_with_warnings"
    assert [stage["status"] for stage in result["pipeline"]] == [
        "accepted", "preserved", "accepted_with_warnings", "normalized", "published"
    ]


def test_automatic_pipeline_stops_when_layer2_rejects(monkeypatch):
    content = b"id,nombre\n1,Sensor\n"
    later_layers_called = []
    monkeypatch.setattr(services_module, "preserve_dimension_delivery", lambda *args, **kwargs: {
        "is_valid": True,
        "decision": "accepted",
        "status": "preserved",
        "delivery_id": 43,
        "logical_key": "MALAGA|movilidad|movilidad_trafico|Anual|v1",
    })
    monkeypatch.setattr(services_module, "validate_csv_text", lambda *args, **kwargs: {
        "is_valid": False,
        "validation_status": "rejected",
        "errors": ["Faltan columnas requeridas"],
        "incidencias": [{"severity": "bloqueante"}],
        "row_count": 1,
        "validation_metadata": {"resultado_validacion": "rechazado"},
    })
    monkeypatch.setattr(services_module, "persist_validation_incidents", lambda *args, **kwargs: None)
    monkeypatch.setattr(services_module, "persist_validation_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(services_module, "mark_dataset_ingested", lambda *args, **kwargs: None)
    monkeypatch.setattr(services_module, "normalize_dimension_dataset", lambda *args, **kwargs: later_layers_called.append("layer3"))
    monkeypatch.setattr(services_module, "publish_normalized_dataset", lambda *args, **kwargs: later_layers_called.append("publish"))
    monkeypatch.setattr(services_module, "build_dimension_layer4", lambda *args, **kwargs: later_layers_called.append("layer4"))
    monkeypatch.setattr(services_module, "record_pipeline_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr(services_module, "update_delivery_status", lambda *args: None)

    result = services_module.run_automatic_dimension_pipeline(
        "trafico.csv",
        content,
        dataset="movilidad_trafico",
        municipio_id="MALAGA",
        period="Anual",
    )

    assert result["is_valid"] is False
    assert result["status"] == "rejected"
    assert result["stopped_at"] == "layer2"
    assert later_layers_called == []


def test_ingesta_preservar_endpoint_runs_automatic_pipeline(monkeypatch):
    captured = {}

    def fake_pipeline(filename, content, **kwargs):
        captured.update({"filename": filename, "content": content, **kwargs})
        return {"is_valid": True, "status": "completed", "pipeline": [{"layer": 4, "status": "published"}]}

    monkeypatch.setattr(main_module, "run_automatic_dimension_pipeline", fake_pipeline)
    app.dependency_overrides[require_write_access] = lambda: FAKE_EDITOR_USER
    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        response = TestClient(app).post(
            "/ingesta/capa1/preservar?dataset=movilidad_carriles_bici&municipio_id=MALAGA&period=Anual&anio=2026",
            files={"file": ("carriles.csv", b"id,latitud,longitud\n1,36.7,-4.4\n", "text/csv")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert captured["dataset"] == "movilidad_carriles_bici"
    assert captured["municipio_id"] == "MALAGA"
    assert captured["period"] == "Anual"


def test_bronze_endpoint_preserves_accepted_csv(monkeypatch):
    content = b"id,nombre,latitud,longitud,fecha_inicio,hora_inicio\n1,Sensor,36.7,-4.4,2026-08-26,08:00\n"
    delivery = {"is_valid": True, "decision": "accepted", "status": "received", "logical_key": "municipio|movilidad|movilidad_trafico|2026|v1", "delivery_id": 7}
    monkeypatch.setattr(services_module, "register_dimension_delivery", lambda *args, **kwargs: delivery)
    monkeypatch.setattr(services_module, "get_lake_dataset_paths", lambda dataset: {"root": Path("/lake"), "staging_dir": Path("/lake/staging") / dataset, "raw_dir": Path("/lake/raw") / dataset})
    monkeypatch.setattr(services_module, "publish_upload_to_s3", lambda filename, payload, *args, **kwargs: {"provider": "test", "uri": f"s3://raw/{kwargs.get('dataset_name', args[0] if args else 'dataset')}/{filename}", "object_exists": True})
    monkeypatch.setattr(services_module, "register_preserved_object", lambda delivery_id, filename, payload, lake: {"status": "preserved", "size_bytes": len(payload), "content_sha256": services_module.hashlib.sha256(payload).hexdigest()})
    monkeypatch.setattr(services_module, "publish_row_traceability_manifest", lambda filename, payload, key, dataset, *args, **kwargs: {"status": "published", "row_count": 1})
    monkeypatch.setattr(services_module, "update_delivery_status", lambda delivery_id, status: None)
    monkeypatch.setenv("S3_ENDPOINT", "http://s3.test")

    app.dependency_overrides[require_write_access] = lambda: FAKE_EDITOR_USER
    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        response = TestClient(app).post(
            "/hive/bronze/preservar?dataset=movilidad_trafico",
            files={"file": ("movilidad.csv", content, "text/csv")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "preserved"
    assert body["row_count"] == 1
    assert body["lake"]["preservation"]["size_bytes"] == len(content)
    assert body["lake"]["traceability"]["row_count"] == 1


def test_mobility_contracts_match_the_four_declared_grains():
    assert MOVILIDAD_DATA_CONTRACTS["movilidad_carriles_bici"]["required_columns"] == ["id", "latitud", "longitud"]
    assert "carril_bici_longitud" in MOVILIDAD_DATA_CONTRACTS["movilidad_carriles_bici"]["columns"]
    assert MOVILIDAD_DATA_CONTRACTS["movilidad_trafico"]["required_columns"][-1] == "trafico_flujo"
    assert MOVILIDAD_DATA_CONTRACTS["movilidad_trafico"]["allowed_values"]["trafico_vehiculo"] == ["general", "moto", "ligero", "pesado"]
    assert "tipo_plaza" in MOVILIDAD_DATA_CONTRACTS["movilidad_plazas_reservadas"]["required_columns"]
    assert "plaza_numero" in MOVILIDAD_DATA_CONTRACTS["movilidad_plazas_reservadas"]["columns"]
    assert MOVILIDAD_DATA_CONTRACTS["movilidad_parking"]["required_columns"][-1] == "ocupacion_libres"
    assert MOVILIDAD_DATA_CONTRACTS["movilidad_parking"]["date_columns"] == ["fecha"]


def test_contract_headers_are_normalized_without_changing_csv_content():
    assert services_module.normalize_contract_header(" LATITUD ") == "latitud"
    assert services_module.normalize_contract_header("ConLongitud") == "longitud"
    assert services_module.normalize_contract_header("Fecha de inicio") == "fecha_de_inicio"


def test_its_contract_matches_inventory_schema():
    assert ITS_DATA_CONTRACT["required_columns"] == ["id", "categoria", "latitud", "longitud"]
    assert "nombre" in ITS_DATA_CONTRACT["columns"]
    assert ITS_DATA_CONTRACT["allowed_values"]["categoria"] == [
        "panel_mensaje_variable", "semaforo", "camara_trafico", "radar_trafico", "foto_rojo"
    ]


def test_permanent_occupation_contract_matches_authorization_schema():
    assert OCUPACION_PERMANENTE_DATA_CONTRACT["required_columns"] == [
        "id", "tipo_ocupacion", "latitud", "longitud", "estado_autorizacion"
    ]
    assert OCUPACION_PERMANENTE_DATA_CONTRACT["allowed_values"]["tipo_ocupacion"] == [
        "terraza", "quiosco", "puesto", "concesion", "otro"
    ]
    assert OCUPACION_PERMANENTE_DATA_CONTRACT["allowed_values"]["estado_autorizacion"] == [
        "activa", "suspendida", "caducada", "en_revision"
    ]


def test_its_layer0_endpoint_registers_dataset(monkeypatch):
    monkeypatch.setattr(main_module, "register_dimension_delivery", lambda filename, content, dataset, *args, **kwargs: {
        "is_valid": True, "status": "received", "decision": "accepted", "dataset": dataset, "dimension": "control_gestion_its"
    })
    response = TestClient(app).post(
        "/ingesta/recepciones/its",
        files={"file": ("control_gestion_its.csv", b"id,categoria,latitud,longitud\n1,semaforo,36.7,-4.4\n", "text/csv")},
    )

    assert response.status_code == 401


def test_permanent_occupation_layer0_endpoint_registers_dataset(monkeypatch):
    monkeypatch.setattr(main_module, "register_dimension_delivery", lambda filename, content, dataset, *args, **kwargs: {
        "is_valid": True, "status": "received", "decision": "accepted", "dataset": dataset, "dimension": "ocupacion_permanente_espacio_publico"
    })
    response = TestClient(app).post(
        "/ingesta/recepciones/ocupacion",
        files={"file": ("ocupacion_permanente_espacio_publico.csv", b"id,tipo_ocupacion,latitud,longitud,estado_autorizacion\n1,terraza,36.7,-4.4,activa\n", "text/csv")},
    )

    assert response.status_code == 401


def test_contracts_manager_serializes_technical_contracts_without_new_validation_rules():
    manager = ContractsManager(contracts={"movilidad_trafico": MOVILIDAD_DATA_CONTRACTS["movilidad_trafico"]})

    payload = manager._contract_payload("movilidad_trafico", MOVILIDAD_DATA_CONTRACTS["movilidad_trafico"])

    assert payload["dimension"] == "movilidad"
    assert "fecha_inicio" in payload["required_columns"]
    traffic_column = next(column for column in payload["columns"] if column["name"] == "trafico_vehiculo")
    assert traffic_column["allowed"] == ["general", "moto", "ligero", "pesado"]


def test_contracts_manager_reports_openmetadata_disabled(monkeypatch):
    monkeypatch.setenv("OM_ENABLED", "false")
    manager = ContractsManager(contracts={"control_gestion_its": ITS_DATA_CONTRACT})

    result = manager.provision_to_openmetadata()

    assert result["enabled"] is False
    assert result["status"] == "disabled"


def test_ollama_interpretation_endpoint_returns_model_response(monkeypatch):
    context = {
        "is_valid": True,
        "errors": [],
        "payload": {"question": "¿Qué ocurre?", "instruction": "Analiza", "summary": {"total_afectaciones": 4}},
    }
    monkeypatch.setattr(main_module, "build_ollama_analysis_context", lambda dataset="afectaciones_urbanas", limit=200, question=None: context)
    monkeypatch.setattr(
        main_module,
        "call_ollama_analysis",
        lambda payload: {"is_valid": True, "errors": [], "model": "llama3.2", "response": "Hay cuatro afectaciones."},
    )

    response = TestClient(app).post("/analysis/interpretar/ollama/afectaciones_urbanas")

    assert response.status_code == 200
    assert response.json()["response"] == "Hay cuatro afectaciones."
    assert response.json()["model"] == "llama3.2"
    assert response.json()["context"]["summary"]["total_afectaciones"] == 4


def test_call_ollama_analysis_posts_standard_generate_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return b'{"response":"Interpretacion generada"}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    monkeypatch.setattr(services_module.urllib.request, "urlopen", fake_urlopen)

    result = services_module.call_ollama_analysis({"question": "¿Qué ocurre?", "instruction": "Analiza", "summary": {}})

    assert result["is_valid"] is True
    assert result["response"] == "Interpretacion generada"
    assert captured["url"] == "http://ollama:11434/api/generate"
    assert services_module.json.loads(captured["body"])["model"] == "qwen2.5:7b"
    assert services_module.json.loads(captured["body"])["stream"] is False
    assert services_module.json.loads(captured["body"])["options"]["num_predict"] == 400


def test_validate_csv_rejects_missing_required_columns():
    # Verifica que se detectan columnas obligatorias que faltan.
    csv_text = "id,fecha\n1,2024-01-01\n"
    result = validate_csv_text(csv_text, required_columns=["id", "nombre", "fecha"])

    assert result["is_valid"] is False
    assert "Faltan columnas requeridas: nombre" in result["errors"][0]


def test_validate_csv_rejects_empty_required_values():
    # Comprueba que los valores obligatorios vacíos se marcan como error.
    csv_text = "id,nombre,fecha\n1,,2024-01-01\n"
    result = validate_csv_text(csv_text, required_columns=["id", "nombre", "fecha"])

    assert result["is_valid"] is False
    assert "Fila 2" in result["errors"][0]


def test_validate_csv_rejects_invalid_date_format():
    csv_text = "id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas\n1,Obra,,,,,2024/01/01,08:00,2024-01-01,10:00,obra,trafico,,,\n"
    result = validate_csv_text(
        csv_text,
        required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
        business_rules=AFFECTACIONES_DATA_CONTRACT,
    )

    assert result["is_valid"] is False
    assert "Formato de fecha inválido" in result["errors"][0]
    assert result["validation_status"] == "rejected"
    assert result["incidencias"][-1]["codigo_regla"] == "CSV_DATE_FORMAT"
    assert result["incidencias"][-1]["campo_afectado"] == "fecha_inicio"
    assert result["incidencias"][-1]["row_number_origen"] == 2
    assert result["incidencias"][-1]["row_id_tecnico"]


def test_validate_csv_rejects_unexpected_business_value():
    csv_text = "id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas\n1,Obra,,,,,2024-01-01,08:00,2024-01-01,10:00,obra,foo,,,\n"
    result = validate_csv_text(
        csv_text,
        required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
        business_rules=AFFECTACIONES_DATA_CONTRACT,
    )

    assert result["is_valid"] is False
    assert "no está permitido" in result["errors"][0]


def test_validate_csv_accepts_temporally_coherent_affectation():
    csv_text = "id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas\n1,Obra,,,,,2024-01-01,23:30,2024-01-02,01:00,obra,trafico,,,\n"
    result = validate_csv_text(
        csv_text,
        required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
        business_rules=AFFECTACIONES_DATA_CONTRACT,
    )

    assert result["is_valid"] is True


def test_validate_csv_rejects_end_before_or_equal_to_start():
    for end_time in ("07:59", "08:00"):
        csv_text = f"id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas\n1,Obra,,,,,2024-01-01,08:00,2024-01-01,{end_time},obra,trafico,,,\n"
        result = validate_csv_text(
            csv_text,
            required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
            business_rules=AFFECTACIONES_DATA_CONTRACT,
        )

        assert result["is_valid"] is False
        assert "posterior al inicio" in result["errors"][0]


def test_validate_csv_rejects_invalid_time_format():
    csv_text = "id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas\n1,Obra,,,,,2024-01-01,8:00,2024-01-01,10:00,obra,trafico,,,\n"
    result = validate_csv_text(
        csv_text,
        required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
        business_rules=AFFECTACIONES_DATA_CONTRACT,
    )

    assert result["is_valid"] is False
    assert "Formato de hora inválido" in result["errors"][0]


def test_validate_csv_classifies_extra_columns_as_non_blocking_warning():
    csv_text = "id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas,origen_externo\n1,Obra,,Calle Mayor,,,2024-01-01,08:00,2024-01-01,10:00,obra,trafico,,,,sistema_origen\n"
    result = validate_csv_text(
        csv_text,
        required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
        business_rules=AFFECTACIONES_DATA_CONTRACT,
    )

    assert result["is_valid"] is True
    assert result["validation_status"] == "accepted_with_warnings"
    assert result["incidencias"][0]["codigo_regla"] == "CSV_EXTRA_COLUMNS"
    assert result["incidencias"][0]["tipo_regla"] == "no_bloqueante"
    assert result["incidencias"][0]["severidad"] == "advertencia"
    assert result["incidencias"][0]["accion_requerida"] == "revisar"


def test_validate_csv_does_not_warn_about_declared_optional_columns():
    content = b"id,nombre,latitud,longitud,direccion,tipo_plaza,plaza_numero,plaza_longitud\n1,Reserva,36.7,-4.4,Calle,taxi,2,5\n"
    contract = MOVILIDAD_DATA_CONTRACTS["movilidad_plazas_reservadas"]

    result = services_module.validate_csv_text(content, contract["required_columns"], contract)

    assert result["is_valid"] is True
    assert result["validation_status"] == "ready_for_ingestion"
    assert result["incidencias"] == []


def test_validate_csv_marks_clean_dataset_ready_for_ingestion():
    csv_text = (
        "id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,"
        "tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas\n"
        "1,Obra,,Calle Mayor,36.72,-4.42,2026-08-01,08:00,2026-08-01,10:00,obra,corte_trafico,municipal,no,\n"
    )

    result = validate_csv_text(
        csv_text,
        required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
        business_rules=AFFECTACIONES_DATA_CONTRACT,
    )

    assert result["is_valid"] is True
    assert result["validation_status"] == "ready_for_ingestion"


def test_layer2_validation_exposes_required_metadata_for_a_clean_dataset():
    csv_text = (
        "id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,"
        "tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas\n"
        "1,Obra,,Calle Mayor,36.72,-4.42,2026-08-01,08:00,2026-08-01,10:00,obra,corte_trafico,municipal,no,\n"
    )

    result = validate_csv_text(
        csv_text,
        required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
        business_rules=AFFECTACIONES_DATA_CONTRACT,
    )

    metadata = result["validation_metadata"]
    assert metadata["validacion_id"]
    assert metadata["resultado_validacion"] == "apto_para_ingesta"
    assert metadata["numero_registros_evaluados"] == 1
    assert metadata["numero_registros_validos"] == 1
    assert metadata["estado_ciclo_vida_dataset"] == "en_consolidacion"


def test_layer2_rejects_negative_or_non_integer_traffic_flow():
    csv_text = (
        "id,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,trafico_flujo\n"
        "sensor-1,36.72,-4.42,2026-08-01,08:00,2026-08-01,10:00,-1\n"
    )

    result = validate_csv_text(
        csv_text,
        required_columns=MOVILIDAD_DATA_CONTRACTS["movilidad_trafico"]["required_columns"],
        business_rules=MOVILIDAD_DATA_CONTRACTS["movilidad_trafico"],
    )

    assert result["is_valid"] is False
    assert result["incidencias"][-1]["codigo_regla"] == "CSV_NON_NEGATIVE_INTEGER"
    assert result["validation_metadata"]["resultado_validacion"] == "rechazado"


def test_layer2_rejects_parking_available_spaces_above_total():
    csv_text = (
        "id,latitud,longitud,fecha,hora,ocupacion_total,ocupacion_libres\n"
        "parking-1,36.72,-4.42,2026-08-01,08:00,10,11\n"
    )

    result = validate_csv_text(
        csv_text,
        required_columns=MOVILIDAD_DATA_CONTRACTS["movilidad_parking"]["required_columns"],
        business_rules=MOVILIDAD_DATA_CONTRACTS["movilidad_parking"],
    )

    assert result["is_valid"] is False
    assert result["incidencias"][-1]["codigo_regla"] == "CSV_FIELD_RELATION"


def test_layer2_rejects_invalid_coordinates_and_reversed_authorization_dates():
    csv_text = (
        "id,tipo_ocupacion,latitud,longitud,estado_autorizacion,fecha_inicio_autorizacion,fecha_fin_autorizacion\n"
        "ocupacion-1,terraza,136.72,-4.42,activa,2026-08-02,2026-08-01\n"
    )

    result = validate_csv_text(
        csv_text,
        required_columns=OCUPACION_PERMANENTE_DATA_CONTRACT["required_columns"],
        business_rules=OCUPACION_PERMANENTE_DATA_CONTRACT,
    )

    assert result["is_valid"] is False
    assert any(incident["codigo_regla"] == "CSV_COORDINATE_FORMAT" for incident in result["incidencias"])
    assert any("fecha_fin_autorizacion" in incident["descripcion_incidencia"] for incident in result["incidencias"])


def test_validate_csv_classifies_temporal_error_as_blocking():
    csv_text = "id,nombre,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion\n1,Obra,2024-01-02,08:00,2024-01-01,10:00,obra,trafico\n"
    result = validate_csv_text(
        csv_text,
        required_columns=AFFECTACIONES_DATA_CONTRACT["required_columns"],
        business_rules=AFFECTACIONES_DATA_CONTRACT,
    )

    assert result["is_valid"] is False
    assert result["incidencias"][0]["tipo_regla"] == "bloqueante"


def test_validate_csv_rejects_inconsistent_columns():
    # Asegura que una fila con menos columnas que la cabecera se detecte.
    csv_text = "id,nombre,fecha\n1,Ayuntamiento\n"
    result = validate_csv_text(csv_text, required_columns=["id", "nombre", "fecha"])

    assert result["is_valid"] is False
    assert "Número de columnas inconsistente" in result["errors"][0]


def test_validate_csv_rejects_empty_header():
    # Comprueba que una cabecera vacía se considere inválida.
    csv_text = "\n1,Ayuntamiento,2024-01-01\n"
    result = validate_csv_text(csv_text, required_columns=["id", "nombre", "fecha"])

    assert result["is_valid"] is False
    assert "La primera fila (cabecera) está vacía" in result["errors"][0]


def test_parse_csv_rows_builds_named_records_from_affectaciones_csv():
    # Comprueba que un CSV de afectaciones se transforma en registros con nombres de columna.
    csv_text = "id,nombre,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion\n1,Obra,2024-01-01,08:00,2024-01-01,10:00,obra,intervencion\n"
    result = parse_csv_rows(csv_text)

    assert result["is_valid"] is True
    assert result["rows"][0]["id"] == "1"
    assert result["rows"][0]["nombre"] == "Obra"
    assert result["rows"][0]["tipo_intervencion"] == "obra"


def test_persist_affectaciones_inserts_rows(monkeypatch):
    # Comprueba que los registros válidos se envían a la base de datos mediante la función de persistencia.
    class FakeCursor:
        def __init__(self):
            self.executed = []

        def execute(self, query, params=None):
            self.executed.append((query, params))

        def close(self):
            return None

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()
            self.committed = False

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.committed = True

        def close(self):
            return None

    fake_connection = FakeConnection()
    monkeypatch.setattr(services_module, "get_db_connection", lambda: fake_connection)

    rows = [{"id": "1", "nombre": "Obra", "fecha_inicio": "2024-01-01", "tipo_intervencion": "obra", "tipo_afectacion": "intervencion"}]
    result = persist_affectaciones(rows)

    assert result["is_valid"] is True
    assert result["inserted"] == 1
    assert fake_connection.committed is True


def test_ingesta_affectaciones_returns_summary_structure(monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.executed = []

        def execute(self, query, params=None):
            self.executed.append((query, params))

        def close(self):
            return None

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()
            self.committed = False

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.committed = True

        def close(self):
            return None

    fake_connection = FakeConnection()
    monkeypatch.setattr(services_module, "get_db_connection", lambda: fake_connection)

    csv_text = "id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas\n1,Obra,,,,,2024-01-01,08:00,2024-01-01,10:00,obra,intervencion,,,\n"
    result = services_module.ingest_affectaciones(csv_text)

    assert result["status"] == "success"
    assert result["summary"]["rows_received"] == 1
    assert result["summary"]["rows_inserted"] == 1


def test_publish_upload_to_lake_keeps_optional_local_mirror(monkeypatch, tmp_path):
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))
    monkeypatch.setenv("LAKE_LOCAL_MIRROR", "true")

    result = services_module.publish_upload_to_lake("afectaciones.csv", b"id,nombre\n1,Obra\n")

    assert result["is_valid"] is True
    assert (lake_root / "staging" / "afectaciones_urbanas" / "afectaciones.csv").exists()
    assert not (lake_root / "raw" / "afectaciones_urbanas" / "afectaciones.csv").exists()
    assert result["lake"]["stage"] == "bronze"
    assert result["lake"]["staging_file_exists"] is True


def test_publish_upload_to_lake_uses_explicit_dataset_paths(monkeypatch, tmp_path):
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))
    monkeypatch.setenv("LAKE_LOCAL_MIRROR", "true")

    result = services_module.publish_upload_to_lake("afectaciones.csv", b"id,nombre\n1,Obra\n", dataset_name="movilidad")

    assert result["lake"]["dataset"] == "movilidad"
    assert (lake_root / "staging" / "movilidad" / "afectaciones.csv").exists()
    assert result["lake"]["staging_path"].endswith("staging\\movilidad\\afectaciones.csv") or result["lake"]["staging_path"].endswith("staging/movilidad/afectaciones.csv")


def test_publish_upload_to_lake_writes_isolated_bronze_s3_key(monkeypatch, tmp_path):
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))
    monkeypatch.setenv("LAKE_LOCAL_MIRROR", "false")
    monkeypatch.setenv("S3_ENDPOINT", "http://seaweedfs-s3:8333")
    monkeypatch.setattr(
        services_module,
        "publish_upload_to_s3",
        lambda filename, content_bytes, dataset_name, dimension, entity: {
            "provider": "seaweedfs",
            "bucket": "raw",
            "key": f"{dimension}/{entity}/{dataset_name}/{filename}",
            "uri": f"s3://raw/{dimension}/{entity}/{dataset_name}/{filename}",
            "object_exists": True,
        },
    )

    result = services_module.publish_upload_to_lake("afectaciones.csv", b"id,nombre\n1,Obra\n")

    assert result["is_valid"] is True
    assert result["lake"]["s3"]["is_valid"] is True
    assert result["lake"]["local_mirror_enabled"] is False
    assert not (lake_root / "staging" / "afectaciones_urbanas" / "afectaciones.csv").exists()
    assert result["lake"]["s3"]["uri"] == "s3://raw/afectaciones_urbanas/municipio_demo/afectaciones_urbanas/afectaciones.csv"
    assert result["lake"]["bronze_key"] == "afectaciones_urbanas/municipio_demo/afectaciones_urbanas/afectaciones_csv"


def test_register_hive_table_from_csv_uses_real_header(monkeypatch):
    statements = []
    monkeypatch.setattr(services_module, "run_trino_statement", lambda sql: statements.append(sql))

    content = b"id,nombre,descripcion,direccion,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion\n1,Obra,desc,calle,08:00,2024-01-02,10:00,obra,corte_trafico\n"
    result = services_module.register_hive_table_from_csv("afectaciones.csv", content, dataset_name="afectaciones_urbanas")

    assert result["is_valid"] is True
    assert result["table"] == "hive.raw.afectaciones_urbanas"
    assert result["columns"] == ["id", "nombre", "descripcion", "direccion", "hora_inicio", "fecha_fin", "hora_fin", "tipo_intervencion", "tipo_afectacion"]
    assert any("CREATE SCHEMA IF NOT EXISTS hive.raw" in s for s in statements)
    assert any("DROP TABLE IF EXISTS hive.raw.afectaciones_urbanas" in s for s in statements)
    create_stmt = next(s for s in statements if s.startswith("CREATE TABLE"))
    assert "descripcion varchar" in create_stmt
    assert "hora_inicio varchar" in create_stmt
    assert "external_location = 's3a://raw/afectaciones_urbanas/municipio_demo/afectaciones_urbanas/'" in create_stmt


def test_publish_upload_to_lake_registers_hive_table_when_trino_configured(monkeypatch, tmp_path):
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))
    monkeypatch.setenv("LAKE_LOCAL_MIRROR", "false")
    monkeypatch.setenv("S3_ENDPOINT", "http://seaweedfs-s3:8333")
    monkeypatch.setenv("TRINO_HOST", "trino")
    monkeypatch.setattr(
        services_module,
        "publish_upload_to_s3",
        lambda filename, content_bytes, dataset_name, dimension, entity: {
            "provider": "seaweedfs",
            "bucket": "raw",
            "key": f"{dimension}/{entity}/{dataset_name}/{filename}",
            "uri": f"s3://raw/{dimension}/{entity}/{dataset_name}/{filename}",
            "object_exists": True,
        },
    )
    monkeypatch.setattr(
        services_module,
        "register_hive_table_from_csv",
        lambda filename, content_bytes, dataset_name, dimension, entity: {"is_valid": True, "table": f"hive.raw.{dataset_name}", "columns": ["id", "nombre"]},
    )

    result = services_module.publish_upload_to_lake("afectaciones.csv", b"id,nombre\n1,Obra\n")

    assert result["lake"]["hive"]["enabled"] is True
    assert result["lake"]["hive"]["is_valid"] is True
    assert result["lake"]["hive"]["table"] == "hive.raw.afectaciones_urbanas"


def test_publish_upload_to_lake_skips_hive_registration_without_trino_host(monkeypatch, tmp_path):
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))
    monkeypatch.setenv("LAKE_LOCAL_MIRROR", "false")
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.delenv("TRINO_HOST", raising=False)

    result = services_module.publish_upload_to_lake("afectaciones.csv", b"id,nombre\n1,Obra\n")

    assert result["lake"]["hive"]["enabled"] is False


def test_move_file_to_raw_moves_staged_file(monkeypatch, tmp_path):
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))

    staging_dir = lake_root / "staging" / "afectaciones_urbanas"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_path = staging_dir / "afectaciones.csv"
    staged_path.write_bytes(b"id,nombre\n1,Obra\n")

    result = services_module.move_staged_file_to_raw("afectaciones.csv", dataset_name="afectaciones_urbanas")

    assert result["is_valid"] is True
    assert result["lake"]["stage"] == "raw"
    assert (lake_root / "raw" / "afectaciones_urbanas" / "afectaciones.csv").exists()
    assert not staged_path.exists()


def test_ingesta_afectaciones_writes_lake_when_db_fails(monkeypatch, tmp_path):
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))
    monkeypatch.setenv("LAKE_LOCAL_MIRROR", "true")

    def fail_db_connection():
        raise RuntimeError("db down")

    monkeypatch.setattr(services_module, "get_db_connection", fail_db_connection)

    result = services_module.ingest_affectaciones(
        "id,nombre,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion\n1,Obra,2024-01-01,08:00,2024-01-01,10:00,obra,intervencion\n",
        source_filename="afectaciones.csv",
        content_bytes=b"id,nombre,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion\n1,Obra,2024-01-01,08:00,2024-01-01,10:00,obra,intervencion\n",
    )

    assert result["status"] == "error"
    assert "db down" in result["errors"][0]
    assert "lake" not in result or result["lake"] == {}


def test_ingesta_afectaciones_endpoint_returns_summary(monkeypatch):
    # Comprueba que el endpoint de carga devuelve un resumen claro de éxito y registros insertados.
    class FakeCursor:
        def __init__(self):
            self.executed = []

        def execute(self, query, params=None):
            self.executed.append((query, params))

        def fetchone(self):
            if self.executed and "SELECT id, content_sha256" in self.executed[-1][0]:
                return None
            return (1,)

        def close(self):
            return None

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()
            self.committed = False

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.committed = True

        def rollback(self):
            return None

        def close(self):
            return None

    fake_connection = FakeConnection()
    monkeypatch.setattr(services_module, "get_db_connection", lambda: fake_connection)

    client = TestClient(app)
    response = client.post(
        "/ingesta/afectaciones",
        files={"file": ("afectaciones.csv", b"id,nombre,descripcion,direccion,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,tipo_intervencion,tipo_afectacion,titularidad,impacto_pmr,notas\n1,Obra,,,,,2024-01-01,08:00,2024-01-01,10:00,obra,intervencion,,,\n", "text/csv")},
    )

    assert response.status_code == 401


def test_register_delivery_handles_duplicate_conflict_and_replace(monkeypatch):
    deliveries = []
    next_id = [1]

    class FakeCursor:
        def __init__(self):
            self.result = None

        def execute(self, query, params=None):
            if "SELECT id, content_sha256" in query:
                logical_key = params[0]
                matching = [item for item in deliveries if item["logical_key"] == logical_key]
                self.result = (matching[-1]["id"], matching[-1]["content_sha256"], matching[-1]["status"]) if matching else None
            elif "UPDATE ingesta_entregas" in query:
                delivery_id = params[1] if "status = %s" in query else params[0]
                for item in deliveries:
                    if item["id"] == delivery_id:
                        item["status"] = "replaced"
            elif "INSERT INTO ingesta_entregas" in query:
                self.result = (next_id[0],)
                deliveries.append({
                    "id": next_id[0],
                    "logical_key": params[0],
                    "content_sha256": params[7],
                    "status": "received",
                })
                next_id[0] += 1

        def fetchone(self):
            return self.result

        def close(self):
            return None

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(services_module, "get_db_connection", lambda: FakeConnection())

    first = services_module.register_delivery("afectaciones.csv", b"contenido")
    deliveries[-1]["status"] = "completed"
    duplicate = services_module.register_delivery("afectaciones.csv", b"contenido")
    conflict = services_module.register_delivery("afectaciones.csv", b"contenido distinto")
    replaced = services_module.register_delivery("afectaciones.csv", b"contenido distinto", replace=True)

    assert first["decision"] == "accepted"
    assert duplicate["decision"] == "duplicate"
    assert conflict["decision"] == "conflict"
    assert replaced["decision"] == "replaced"
    assert replaced["logical_key"] == "municipio_demo|afectaciones_urbanas|gestion_afectaciones_urbanas|_|v1"


def test_register_delivery_records_layer0_reception_metadata(monkeypatch):
    executed = []

    class FakeCursor:
        def __init__(self):
            self.result = None

        def execute(self, query, params=None):
            executed.append((query, params))
            if "SELECT id, content_sha256" in query:
                self.result = None
            elif "INSERT INTO ingesta_entregas" in query:
                self.result = (7,)

        def fetchone(self):
            return self.result

        def close(self):
            return None

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(services_module, "get_db_connection", FakeConnection)

    result = services_module.register_delivery(
        "afectaciones.csv",
        b"contenido",
        entity="municipio_prueba",
        entry_channel="api",
        sender="integracion_prueba",
    )

    assert result["reception"] == {"entry_channel": "api", "sender": "integracion_prueba"}
    assert result["recepcion_id"] == result["delivery_id"]
    assert result["nombre_fichero_original"] == "afectaciones.csv"
    assert result["tamano_fichero"] == len(b"contenido")
    assert result["estado_recepcion"] == "received"
    assert result["indicador_conflicto"] is False
    assert result["decision_sobre_conflicto"] == "accepted"
    assert result["numero_intento_carga"] == 1
    assert result["fecha_hora_inicio_remision"]
    assert result["fecha_hora_recepcion"]
    event_insert = next(params for query, params in executed if "INSERT INTO ingesta_eventos_recepcion" in query)
    assert event_insert[:4] == (7, "received", "integracion_prueba", "api")
    assert event_insert[5:10] == (7, 1, "integracion_prueba", "success", "Entrega registrada y pendiente de validación.")


def test_layer0_generic_endpoint_authorizes_known_dataset(monkeypatch):
    expected = {
        "is_valid": True,
        "decision": "accepted",
        "status": "received",
        "recepcion_id": 7,
        "municipio_id": "MALAGA",
        "dimension_id": "movilidad",
        "dataset": "movilidad_trafico",
        "dimension": "movilidad",
        "anio": 2026,
        "periodo": "2026-08",
        "version_esquema": "v1",
        "fecha_hora_recepcion": "2026-08-31T10:00:00+00:00",
        "canal_entrada": "api",
        "usuario_o_sistema_emisor": "integracion",
        "nombre_fichero_original": "trafico.csv",
        "estado_recepcion": "received",
        "numero_intento_carga": 1,
        "indicador_conflicto": False,
        "decision_sobre_conflicto": "accepted",
    }
    monkeypatch.setattr(main_module, "register_dimension_delivery", lambda *args, **kwargs: expected)

    response = TestClient(app).post(
        "/ingesta/recepciones?dataset=movilidad_trafico&municipio_id=MALAGA&anio=2026&period=2026-08&entry_channel=api&sender=integracion",
        files={"file": ("trafico.csv", b"id,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,trafico_flujo\n1,36.7,-4.4,2026-08-01,08:00,2026-08-01,09:00,100\n", "text/csv")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["capa"] == "capa_0_recepcion"
    assert body["resultado_capa0"] == "entrega_aceptada"
    assert body["paso_capa1_autorizado"] is True
    assert body["recepcion"]["dataset_id"] == "movilidad_trafico"
    assert body["recepcion"]["municipio_id"] == "MALAGA"


def test_layer0_generic_endpoint_rejects_unknown_contract(monkeypatch):
    expected = {
        "is_valid": False,
        "decision": "invalid_dataset",
        "status": "rejected",
        "dataset": "dataset_desconocido",
        "errors": ["Dataset no permitido: dataset_desconocido"],
    }
    monkeypatch.setattr(main_module, "register_dimension_delivery", lambda *args, **kwargs: expected)

    response = TestClient(app).post(
        "/ingesta/recepciones?dataset=dataset_desconocido",
        files={"file": ("datos.csv", b"id,nombre\n1,Demo\n", "text/csv")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["resultado_capa0"] == "entrega_rechazada_por_contrato_no_reconocido"
    assert body["paso_capa1_autorizado"] is False
    assert body["recepcion"]["motivo_rechazo"] == "Dataset no permitido: dataset_desconocido"


def test_layer0_generic_endpoint_uses_municipio_as_effective_entity(monkeypatch):
    captured = {}

    def fake_register(filename, content, **kwargs):
        captured.update(kwargs)
        return {
            "is_valid": True,
            "decision": "accepted",
            "status": "received",
            "recepcion_id": 9,
            "municipio_id": kwargs["municipio_id"],
            "dimension_id": "movilidad",
            "dataset": kwargs["dataset"],
            "dimension": "movilidad",
            "periodo": kwargs["period"],
            "version_esquema": kwargs["schema_version"],
            "estado_recepcion": "received",
            "indicador_conflicto": False,
            "decision_sobre_conflicto": "accepted",
        }

    monkeypatch.setattr(main_module, "register_dimension_delivery", fake_register)

    response = TestClient(app).post(
        "/ingesta/recepciones?dataset=movilidad_trafico&entity=SEVILLA&municipio_id=MALAGA&period=2026-08",
        files={"file": ("trafico.csv", b"id,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,trafico_flujo\n1,36.7,-4.4,2026-08-01,08:00,2026-08-01,09:00,100\n", "text/csv")},
    )

    assert response.status_code == 200
    assert captured["entity"] == "MALAGA"
    assert captured["municipio_id"] == "MALAGA"


def test_row_traceability_manifest_is_deterministic_and_preserves_row_count():
    content = b"id,nombre\n1,Obra\n2,Evento\n"

    first_ids = services_module.build_row_traceability_ids(content)
    second_ids = services_module.build_row_traceability_ids(content)
    manifest = services_module.build_row_traceability_manifest(content, "entidad|dimension|dataset|periodo|v1")

    assert len(first_ids) == 2
    assert first_ids == second_ids
    assert first_ids[0] != first_ids[1]
    assert manifest.count(b"\n") == 3
    assert first_ids[0].encode("utf-8") in manifest


def test_register_preserved_object_records_original_csv_metadata(monkeypatch):
    executed = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((query, params))

        def close(self):
            return None

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(services_module, "get_db_connection", FakeConnection)
    content = b"id,nombre\n1,Obra\n"
    result = services_module.register_preserved_object(
        12,
        "afectaciones.csv",
        content,
        {"s3": {"is_valid": True, "uri": "s3://staging/afectaciones_urbanas/afectaciones.csv"}},
    )

    assert result["status"] == "preserved"
    assert result["size_bytes"] == len(content)
    assert result["storage_uri"].startswith("s3://staging/")
    insert_params = next(params for query, params in executed if "INSERT INTO ingesta_objetos_preservados" in query)
    assert insert_params[0:4] == (12, "afectaciones.csv", result["storage_uri"], len(content))


def test_preserve_dimension_delivery_uses_dataset_dimension_and_entity(monkeypatch):
    captured = {"published": None, "traceability": None}

    monkeypatch.setattr(services_module, "register_dimension_delivery", lambda *args, **kwargs: {
        "is_valid": True,
        "decision": "accepted",
        "status": "received",
        "logical_key": "MALAGA|movilidad|movilidad_trafico|2026-08|v1",
        "delivery_id": 21,
        "recepcion_id": 21,
    })
    monkeypatch.setenv("S3_ENDPOINT", "http://seaweedfs-s3:8333")
    monkeypatch.setenv("LAKE_LOCAL_MIRROR", "false")
    monkeypatch.setattr(services_module, "register_preserved_object", lambda *args, **kwargs: {"enabled": True, "status": "preserved"})
    monkeypatch.setattr(services_module, "update_delivery_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        services_module,
        "publish_upload_to_s3",
        lambda filename, content, dataset_name, dimension, entity: captured.update({"published": (dataset_name, dimension, entity, filename)}) or {
            "provider": "seaweedfs",
            "bucket": "raw",
            "key": f"{dimension}/{entity}/{dataset_name}/{filename}",
            "uri": f"s3://raw/{dimension}/{entity}/{dataset_name}/{filename}",
            "object_exists": True,
        },
    )
    monkeypatch.setattr(
        services_module,
        "publish_row_traceability_manifest",
        lambda filename, content, delivery_key, dataset_name, dimension=None, entity="municipio_demo": captured.update({"traceability": (dataset_name, dimension, entity, filename)}) or {
            "enabled": True,
            "status": "published",
            "key": f"_traceability/{dimension}/{entity}/{dataset_name}/{filename}.rows.csv",
        },
    )

    result = services_module.preserve_dimension_delivery(
        "trafico.csv",
        b"id,latitud,longitud,fecha_inicio,hora_inicio,fecha_fin,hora_fin,trafico_flujo\n1,36.7,-4.4,2026-08-01,08:00,2026-08-01,09:00,100\n",
        "movilidad_trafico",
        entity="MALAGA",
        period="2026-08",
    )

    assert result["status"] == "preserved"
    assert captured["published"] == ("movilidad_trafico", "movilidad", "MALAGA", "trafico.csv")
    assert captured["traceability"] == ("movilidad_trafico", "movilidad", "MALAGA", "trafico.csv")
    assert result["lake"]["s3"]["uri"] == "s3://raw/movilidad/MALAGA/movilidad_trafico/trafico.csv"
    assert result["lake"]["staging_path"].endswith("staging\\movilidad\\MALAGA\\movilidad_trafico\\trafico.csv") or result["lake"]["staging_path"].endswith("staging/movilidad/MALAGA/movilidad_trafico/trafico.csv")


def test_afectaciones_curated_sql_types_and_filters_temporal_invalid_rows():
    sql = services_module.build_afectaciones_curated_sql()

    assert "CREATE TABLE lake.curated.afectaciones_urbanas" in sql
    assert "format = 'PARQUET'" in sql
    assert "AS double" in sql
    assert "replace(nullif(trim(latitud), ''), ',', '.')" in sql
    assert "try_cast(nullif(trim(fecha_inicio), '') AS date)" in sql
    assert "try_cast(nullif(trim(hora_inicio), '') AS time)" in sql
    assert "AS timestamp" in sql
    assert "fecha_hora_fin" in sql
    assert "WHEN 'actuación_municipal' THEN 'actuacion_municipal'" in sql
    assert "AS duracion_minutos" in sql
    assert "AS timestamp) >" in sql
    assert "row_id_tecnico" in sql


def test_afectaciones_exploitation_sql_contains_descriptive_aggregations():
    sql = services_module.build_afectaciones_exploitation_sql()

    assert "CREATE TABLE lake.analytics.afectaciones_urbanas_resumen" in sql
    assert "count(*) AS total_afectaciones" in sql
    assert "duracion_media_horas" in sql
    assert "afectaciones_pmr" in sql
    assert "GROUP BY tipo_intervencion, tipo_afectacion, direccion, hour(fecha_hora_inicio)" in sql
    assert "pct_impacto" not in sql
    assert "nivel_impacto" not in sql


def test_afectaciones_trafico_mart_sql_uses_spatial_and_temporal_windows():
    sql = services_module.build_afectaciones_trafico_mart_sql()

    assert "CREATE TABLE lake.analytics.afectaciones_trafico" in sql
    assert "t.direccion = v.direccion" in sql
    assert "flujo_antes" in sql
    assert "flujo_durante" in sql
    assert "flujo_despues" in sql
    assert "variacion_trafico_pct" in sql
    assert "impacto_estimado" in sql


def test_afectaciones_layers_endpoint_builds_both_layers(monkeypatch):
    statements = []
    monkeypatch.setattr(services_module, "run_trino_statement", lambda sql: statements.append(sql))

    client = TestClient(app)
    app.dependency_overrides[require_write_access] = lambda: FAKE_EDITOR_USER
    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        response = client.post("/iceberg/gold/afectaciones/construir")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["is_valid"] is True
    assert response.json()["curated_table"] == "lake.curated.afectaciones_urbanas"
    assert response.json()["analytics_table"] == "lake.analytics.afectaciones_urbanas_resumen"
    assert any("CREATE SCHEMA IF NOT EXISTS lake.curated" in sql for sql in statements)
    assert any("CREATE TABLE lake.analytics.afectaciones_urbanas_resumen" in sql for sql in statements)


def test_lake_move_endpoint_exposes_transition_state(monkeypatch, tmp_path):
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))

    staging_dir = lake_root / "staging" / "afectaciones_urbanas"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "afectaciones.csv").write_bytes(b"id,nombre\n1,Obra\n")

    client = TestClient(app)
    app.dependency_overrides[require_write_access] = lambda: FAKE_EDITOR_USER
    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        response = client.post(
            "/ingesta/lake/mover",
            params={"filename": "afectaciones.csv", "dataset": "afectaciones_urbanas"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "moved"
    assert body["lake"]["stage"] == "raw"


def test_lake_status_endpoint_reports_s3_staging(monkeypatch, tmp_path):
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))
    monkeypatch.setattr(
        main_module,
        "inspect_s3_lake_object",
        lambda filename, dataset: {
            "enabled": True,
            "status": "staging",
            "key": f"{dataset}/{filename}",
            "buckets": {
                "staging": {"bucket": "staging", "exists": True, "content_length": 93},
                "raw": {"bucket": "raw", "exists": False},
            },
        },
    )

    client = TestClient(app)
    response = client.get("/ingesta/lake/estado", params={"filename": "afectaciones.csv"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "staging"
    assert body["s3"]["buckets"]["staging"]["exists"] is True
    assert body["s3"]["buckets"]["raw"]["exists"] is False


def test_get_afectaciones_kpis_builds_summary_from_trino_rows(monkeypatch):
    responses = {
        "SELECT count(*) FROM hive.raw.afectaciones_urbanas": [(3,)],
        "SELECT tipo_intervencion, count(*) FROM hive.raw.afectaciones_urbanas GROUP BY tipo_intervencion ORDER BY 2 DESC": [("obra", 2), ("evento", 1)],
        "SELECT tipo_afectacion, count(*) FROM hive.raw.afectaciones_urbanas GROUP BY tipo_afectacion ORDER BY 2 DESC": [("intervencion", 2), ("trafico", 1)],
        "SELECT nombre, count(*) FROM hive.raw.afectaciones_urbanas GROUP BY nombre ORDER BY 2 DESC LIMIT 10": [("Obra", 2), ("Evento", 1)],
    }
    monkeypatch.setattr(services_module, "run_trino_query", lambda sql: responses[sql])

    result = services_module.get_afectaciones_kpis()

    assert result["is_valid"] is True
    assert result["table"] == "hive.raw.afectaciones_urbanas"
    assert result["kpis"]["total_afectaciones"] == 3
    assert result["por_tipo_intervencion"] == [{"tipo_intervencion": "obra", "count": 2}, {"tipo_intervencion": "evento", "count": 1}]
    assert result["ranking_vias"][0] == {"nombre": "Obra", "count": 2}


def test_get_afectaciones_kpis_returns_error_when_trino_fails(monkeypatch):
    def fail_query(sql):
        raise RuntimeError("trino unavailable")

    monkeypatch.setattr(services_module, "run_trino_query", fail_query)

    result = services_module.get_afectaciones_kpis()

    assert result["is_valid"] is False
    assert "trino unavailable" in result["errors"][0]


def test_calcular_variacion_impacto_applies_weights_and_clip():
    # corte_trafico(45) * obra(1.3) * franja 06:00-09:00(0.78) = 45.63 -> dentro de [2, 58]
    rows = [{"nombre": "Obra A", "tipo_afectacion": "corte_trafico", "tipo_intervencion": "obra", "hora_inicio": "07:30"}]

    result = services_module.calcular_variacion_impacto(rows)

    assert result["detalle_variacion"][0]["pct_impacto"] == 45.6
    assert result["detalle_variacion"][0]["nivel"] == "Crítico"
    assert result["variacion_media_pct"] == 45.6


def test_calcular_variacion_impacto_clips_to_upper_and_lower_bounds():
    alto = {"nombre": "Obra Alta", "tipo_afectacion": "corte_trafico", "tipo_intervencion": "obra", "hora_inicio": "17:00"}
    bajo = {"nombre": "Obra Baja", "tipo_afectacion": "ocupacion_acera", "tipo_intervencion": "mantenimiento", "hora_inicio": "02:00"}

    result = services_module.calcular_variacion_impacto([alto, bajo])

    assert result["detalle_variacion"][0]["pct_impacto"] <= services_module.IMPACTO_CLIP_MAX
    assert result["detalle_variacion"][1]["pct_impacto"] >= services_module.IMPACTO_CLIP_MIN


def test_calcular_variacion_impacto_categoriza_niveles():
    assert services_module._nivel_impacto(40) == "Crítico"
    assert services_module._nivel_impacto(30) == "Alto"
    assert services_module._nivel_impacto(20) == "Medio"
    assert services_module._nivel_impacto(5) == "Bajo"


def test_calcular_variacion_impacto_kpis_agregados():
    rows = [
        {"nombre": "A", "tipo_afectacion": "corte_trafico", "tipo_intervencion": "obra", "hora_inicio": "17:00"},
        {"nombre": "B", "tipo_afectacion": "ocupacion_acera", "tipo_intervencion": "mantenimiento", "hora_inicio": "02:00"},
    ]

    result = services_module.calcular_variacion_impacto(rows)

    assert result["count_alto_critico"] >= 1
    assert 0.0 <= result["pct_alto_critico"] <= 100.0
    assert result["variacion_media_pct"] > 0


def test_calcular_variacion_impacto_sin_datos_devuelve_ceros():
    result = services_module.calcular_variacion_impacto([])

    assert result == {
        "variacion_media_pct": 0.0,
        "count_alto_critico": 0,
        "pct_alto_critico": 0.0,
        "detalle_variacion": [],
        "ranking_criticidad_vias": [],
    }


def test_calcular_variacion_impacto_sin_columnas_opcionales_usa_valores_por_defecto():
    # Sin hora_inicio ni tipos reconocidos, debe degradar a los valores por defecto sin romper.
    rows = [{"nombre": "Registro sin extras"}]

    result = services_module.calcular_variacion_impacto(rows)

    assert result["detalle_variacion"][0]["pct_impacto"] > 0
    assert result["detalle_variacion"][0]["nivel"] in {"Crítico", "Alto", "Medio", "Bajo"}


def test_ranking_criticidad_por_via_agrupa_por_via_y_usa_el_pct_maximo():
    rows = [
        {"nombre": "Obra A1", "direccion": "Calle Mayor", "tipo_afectacion": "corte_trafico", "tipo_intervencion": "obra", "hora_inicio": "17:00"},
        {"nombre": "Obra A2", "direccion": "Calle Mayor", "tipo_afectacion": "ocupacion_acera", "tipo_intervencion": "mantenimiento", "hora_inicio": "02:00"},
        {"nombre": "Evento B", "direccion": "Avenida Sur", "tipo_afectacion": "restriccion_paso", "tipo_intervencion": "evento", "hora_inicio": "07:30"},
    ]

    result = services_module.calcular_variacion_impacto(rows)
    ranking = result["ranking_criticidad_vias"]

    calle_mayor = next(r for r in ranking if r["via"] == "Calle Mayor")
    assert calle_mayor["count"] == 2
    assert calle_mayor["pct_impacto"] == max(
        d["pct_impacto"] for d in result["detalle_variacion"] if d["via"] == "Calle Mayor"
    )
    assert ranking == sorted(ranking, key=lambda r: r["pct_impacto"], reverse=True)


def test_build_affectaciones_analysis_context_incluye_criticidad_por_via(monkeypatch):
    monkeypatch.setattr(
        services_module,
        "list_affectaciones",
        lambda limit=200: {
            "is_valid": True,
            "errors": [],
            "rows": [
                {
                    "nombre": "Obra A",
                    "direccion": "Calle Mayor",
                    "tipo_intervencion": "obra",
                    "tipo_afectacion": "corte_trafico",
                    "hora_inicio": "17:00",
                    "titularidad": "municipal",
                }
            ],
        },
    )

    context = services_module.build_affectaciones_analysis_context(limit=200)

    assert context["is_valid"] is True
    summary = context["summary"]
    assert summary["variacion_media_pct"] > 0
    assert "distribucion_niveles" in summary
    assert summary["ranking_criticidad_vias"][0]["via"] == "Calle Mayor"


def test_get_afectaciones_kpis_incluye_variacion_media_pct(monkeypatch):
    responses = {
        "SELECT count(*) FROM hive.raw.afectaciones_urbanas": [(1,)],
        "SELECT tipo_intervencion, count(*) FROM hive.raw.afectaciones_urbanas GROUP BY tipo_intervencion ORDER BY 2 DESC": [("obra", 1)],
        "SELECT tipo_afectacion, count(*) FROM hive.raw.afectaciones_urbanas GROUP BY tipo_afectacion ORDER BY 2 DESC": [("corte_trafico", 1)],
        "SELECT nombre, count(*) FROM hive.raw.afectaciones_urbanas GROUP BY nombre ORDER BY 2 DESC LIMIT 10": [("Obra A", 1)],
    }
    monkeypatch.setattr(services_module, "run_trino_query", lambda sql: responses[sql])
    monkeypatch.setattr(
        services_module,
        "run_trino_query_with_columns",
        lambda sql: {
            "columns": ["nombre", "tipo_afectacion", "tipo_intervencion", "hora_inicio"],
            "rows": [("Obra A", "corte_trafico", "obra", "17:00")],
        },
    )

    result = services_module.get_afectaciones_kpis()

    assert result["is_valid"] is True
    assert result["kpis"]["variacion_media_pct"] > 0
    assert "count_alto_critico" in result["kpis"]
    assert "pct_alto_critico" in result["kpis"]
    assert result["detalle_variacion"][0]["nivel"] in {"Crítico", "Alto", "Medio", "Bajo"}


def test_afectaciones_kpis_endpoint_returns_summary(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "get_afectaciones_kpis",
        lambda table="afectaciones_urbanas": {
            "is_valid": True,
            "table": f"hive.raw.{table}",
            "kpis": {"total_afectaciones": 3},
            "por_tipo_intervencion": [{"tipo_intervencion": "obra", "count": 2}],
            "por_tipo_afectacion": [{"tipo_afectacion": "intervencion", "count": 2}],
            "ranking_vias": [{"nombre": "Obra", "count": 2}],
        },
    )

    client = TestClient(app)
    response = client.get("/analysis/sql/afectaciones-kpis")

    assert response.status_code == 401


def test_list_affectaciones_returns_rows(monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.executed = []

        def execute(self, query, params=None):
            self.executed.append((query, params))

        def fetchall(self):
            return [(1, "1", "Obra", None, None, None, None, None, None, None, None, None, None, None, None, None)]

        def close(self):
            return None

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

        def close(self):
            return None

    fake_connection = FakeConnection()
    monkeypatch.setattr(services_module, "get_db_connection", lambda: fake_connection)

    result = list_affectaciones(limit=5)

    assert result["is_valid"] is True
    assert result["rows"][0]["nombre"] == "Obra"


def test_root_serves_frontend_html():
    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert "Centro de Inteligencia Urbana" in response.text


def test_list_affectaciones_endpoint_returns_json():
    client = TestClient(app)
    response = client.get("/afectaciones")

    assert response.status_code == 401


def test_listar_preguntas_filtra_por_dimension():
    client = TestClient(app)
    response = client.get("/analysis/preguntas", params={"dimension": "afectaciones_urbanas"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 6
    assert body["questions"][0]["id"] == "AQ1"
    assert all(q["dimension"] == "afectaciones_urbanas" for q in body["questions"])


def test_ml_congestion_rules_endpoint_returns_rules(monkeypatch):
    expected = {
        "is_valid": True,
        "model": "congestion_relativa",
        "total_registros": 10,
        "metricas": {"count_alto_critico": 3, "pct_alto_critico": 30.0},
        "prioridades": [{"direccion": "gran via", "pct_tiempo_alto_critico": 40.0}],
    }
    monkeypatch.setattr(main_module, "build_movilidad_trafico_layer5", lambda municipio_prefix=None: expected)

    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        response = TestClient(app).get("/api/ml/movilidad/reglas-congestion")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["dimension"] == "movilidad_trafico"
    assert body["prioridades"] == expected["prioridades"]


def test_ml_prediction_endpoint_returns_level(monkeypatch):
    expected = {
        "is_valid": True,
        "direccion": "Gran Via",
        "nivel_congestion": "Alto",
        "obra_activa": True,
        "recomendacion": "Reforzar señalización de la obra y vigilar la evolución del flujo en la franja crítica.",
    }
    monkeypatch.setattr(main_module, "predecir_congestion_via", lambda direccion, franja_horaria=None, dia_semana=None: expected)

    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        response = TestClient(app).post(
            "/api/ml/movilidad/predecir",
            json={"direccion": "Gran Via"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["nivel_congestion"] in {"Bajo", "Medio", "Alto", "Crítico"}
    assert "recomendacion" in body


def test_its_contract_accepts_valid_inventory_row():
    csv_text = (
        "id,categoria,nombre,latitud,longitud,titularidad\n"
        "ITS-0001,semaforo,Semaforo Centro,36.7213,-4.4214,municipal\n"
    )
    result = validate_csv_text(csv_text, required_columns=ITS_DATA_CONTRACT["required_columns"], business_rules=ITS_DATA_CONTRACT)

    assert result["is_valid"] is True
    assert result["errors"] == []


def test_its_contract_rejects_missing_required_columns():
    csv_text = "id,nombre\nITS-0001,Semaforo Centro\n"
    result = validate_csv_text(csv_text, required_columns=ITS_DATA_CONTRACT["required_columns"], business_rules=ITS_DATA_CONTRACT)

    assert result["is_valid"] is False
    assert "Faltan columnas requeridas" in result["errors"][0]


def test_its_contract_rejects_categoria_fuera_de_catalogo():
    csv_text = "id,categoria,latitud,longitud\nITS-0001,dron_vigilancia,36.7213,-4.4214\n"
    result = validate_csv_text(csv_text, required_columns=ITS_DATA_CONTRACT["required_columns"], business_rules=ITS_DATA_CONTRACT)

    assert result["is_valid"] is False
    assert "no está permitido" in result["errors"][0]


def test_its_contract_no_exige_fechas():
    # A diferencia de afectaciones, ITS no tiene date_columns ni reglas temporales.
    assert ITS_DATA_CONTRACT["date_columns"] == []
    assert "fecha_inicio" not in ITS_DATA_CONTRACT["required_columns"]


def test_build_all_data_marts_returns_valid_structure(monkeypatch):
    monkeypatch.setattr(services_module, "run_trino_statement", lambda sql: None)
    res = services_module.build_all_data_marts()
    assert res["is_valid"] is True
    assert len(res["tables"]) >= 6


def test_single_csv_bi_summary_endpoint_returns_metrics():
    client = TestClient(app)
    response = client.get("/analysis/bi/resumen-csv/gestion_afectaciones_urbanas")
    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is True
    assert "summary" in body
    assert "quality_score_pct" in body


def test_execute_question_analysis_returns_kpis_and_narrative():
    client = TestClient(app)
    response = client.get("/analysis/preguntas/ejecutar/EQ1?granularity=Mes")
    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is True
    assert body["question_id"] == "EQ1"
    assert "kpis" in body
    assert "narrative" in body


def test_simulate_multidimension_impact_endpoint_returns_projections():
    client = TestClient(app)
    response = client.post(
        "/analysis/simulador-multidimension",
        json={
            "direccion": "Avenida de Andalucía",
            "tipo_accion": "corte_trafico",
            "duracion_dias": 5,
            "ocupacion_superficie_m2": 40.0,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is True
    assert "impacto_proyectado" in body
    assert len(body["recomendaciones"]) >= 1



def test_ingest_its_persists_and_publishes_to_lake(monkeypatch, tmp_path):
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))
    monkeypatch.setenv("LAKE_LOCAL_MIRROR", "true")

    class FakeCursor:
        def execute(self, query, params=None):
            return None

        def close(self):
            return None

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(services_module, "get_db_connection", lambda: FakeConnection())

    csv_text = "id,categoria,latitud,longitud\nITS-0001,semaforo,36.7213,-4.4214\n"
    result = services_module.ingest_its(csv_text, source_filename="its.csv", content_bytes=csv_text.encode("utf-8"))

    assert result["status"] == "success"
    assert result["summary"]["rows_inserted"] == 1
    assert (lake_root / "staging" / "control_gestion_its" / "its.csv").exists()


def test_persist_its_creates_table_and_inserts_rows(monkeypatch):
    executed = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((query, params))

        def close(self):
            return None

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()
            self.committed = False

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.committed = True

        def close(self):
            return None

    fake_connection = FakeConnection()
    monkeypatch.setattr(services_module, "get_db_connection", lambda: fake_connection)

    result = services_module.persist_its([{"id": "ITS-0001", "categoria": "semaforo", "latitud": "36.7213", "longitud": "-4.4214"}])

    assert result["is_valid"] is True
    assert result["inserted"] == 1
    assert any("CREATE TABLE IF NOT EXISTS control_gestion_its" in q for q, _ in executed)
    assert any("INSERT INTO control_gestion_its" in q for q, _ in executed)


def test_list_its_returns_rows(monkeypatch):
    class FakeCursor:
        def execute(self, query, params=None):
            return None

        def fetchall(self):
            return [(1, "ITS-0001", "semaforo", "Semaforo Centro", None, None, "36.7213", "-4.4214", "municipal")]

        def close(self):
            return None

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

        def close(self):
            return None

    monkeypatch.setattr(services_module, "get_db_connection", lambda: FakeConnection())

    result = list_its(limit=5)

    assert result["is_valid"] is True
    assert result["rows"][0]["categoria"] == "semaforo"


def test_get_its_kpis_builds_summary_from_trino_rows(monkeypatch):
    responses = {
        "SELECT count(*) FROM hive.raw.control_gestion_its": [(2,)],
        "SELECT categoria, count(*) FROM hive.raw.control_gestion_its GROUP BY categoria ORDER BY 2 DESC": [("semaforo", 2)],
        "SELECT titularidad, count(*) FROM hive.raw.control_gestion_its GROUP BY titularidad ORDER BY 2 DESC": [("municipal", 2)],
    }
    monkeypatch.setattr(services_module, "run_trino_query", lambda sql: responses[sql])

    result = services_module.get_its_kpis()

    assert result["is_valid"] is True
    assert result["table"] == "hive.raw.control_gestion_its"
    assert result["kpis"]["total_dispositivos"] == 2
    assert result["por_categoria"] == [{"categoria": "semaforo", "count": 2}]
    assert result["por_titularidad"] == [{"titularidad": "municipal", "count": 2}]


def test_ingesta_its_endpoint_returns_summary(monkeypatch):
    class FakeCursor:
        def execute(self, query, params=None):
            return None

        def close(self):
            return None

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(services_module, "get_db_connection", lambda: FakeConnection())

    client = TestClient(app)
    csv_content = b"id,categoria,latitud,longitud\nITS-0001,semaforo,36.7213,-4.4214\n"
    response = client.post("/ingesta/its", files={"file": ("its.csv", csv_content, "text/csv")})

    assert response.status_code == 401


def test_its_kpis_endpoint_returns_summary(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "get_its_kpis",
        lambda table="control_gestion_its": {
            "is_valid": True,
            "table": f"hive.raw.{table}",
            "kpis": {"total_dispositivos": 2},
            "por_categoria": [{"categoria": "semaforo", "count": 2}],
            "por_titularidad": [{"titularidad": "municipal", "count": 2}],
        },
    )

    app.dependency_overrides[require_policy_accepted] = lambda: FAKE_EDITOR_USER
    try:
        client = TestClient(app)
        response = client.get("/analysis/sql/its-kpis")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is True
    assert body["kpis"]["total_dispositivos"] == 2


def test_legacy_context_endpoint_is_removed():
    client = TestClient(app)
    response = client.get("/analysis/contexto/afectaciones")

    assert response.status_code == 404
