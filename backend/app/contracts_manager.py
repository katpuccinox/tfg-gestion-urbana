import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import psycopg2
import requests

from app.contracts import (
    AFFECTACIONES_DATA_CONTRACT,
    ITS_DATA_CONTRACT,
    MOVILIDAD_DATA_CONTRACTS,
    OCUPACION_PERMANENTE_DATA_CONTRACT,
)

log = logging.getLogger("contracts")

CONTRACTS = {
    "afectaciones_urbanas": AFFECTACIONES_DATA_CONTRACT,
    "control_gestion_its": ITS_DATA_CONTRACT,
    "ocupacion_permanente_espacio_publico": OCUPACION_PERMANENTE_DATA_CONTRACT,
    **MOVILIDAD_DATA_CONTRACTS,
}

CANONICAL_DOMAINS = {
    "movilidad": "Movilidad",
    "afectaciones_urbanas": "Afectaciones Urbanas",
    "ocupacion_permanente_espacio_publico": "Ocupación Permanente",
    "control_gestion_its": "Control y Gestión ITS",
    "cruces": "Cruces Multidimensión",
}

# Marts de Capa 4 que cruzan más de una dimensión (build_dimension_layer4 y las
# funciones build_eq*_mart de services.py) -- no pertenecen a una sola dimensión,
# así que se agrupan bajo el dominio "cruces" en vez de heredar el de una tabla curada.
CROSS_MART_TABLES = {
    "trafico_obra_activa",
    "eq1_parking_trafico",
    "eq2_afectaciones_its",
    "eq3_ocupacion_afectaciones",
    "eq4_ocupacion_carga_trafico",
    "afectaciones_trafico",
}

OM_SERVICE_NAME = "espacio-datos-trino"

_TRINO_TYPE_TO_OM = {
    "varchar": "VARCHAR",
    "char": "CHAR",
    "bigint": "BIGINT",
    "integer": "INT",
    "double": "DOUBLE",
    "real": "FLOAT",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "decimal": "DECIMAL",
}


def _om_data_type(trino_type: str) -> str:
    base = trino_type.split("(")[0].strip().lower()
    if base.startswith("timestamp"):
        return "TIMESTAMP"
    return _TRINO_TYPE_TO_OM.get(base, "VARCHAR")


def get_postgres_conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "datalake"),
        user=os.getenv("POSTGRES_USER", "datalake"),
        password=os.getenv("POSTGRES_PASSWORD", "datalake_local"),
    )


def list_catalog_status() -> list:
    """Lee el estado de gobierno de cada dataset (tabla `esquemas`, poblada por
    ContractsManager.synchronize()). Es la vista que consume el panel de
    administración para "gestión de catálogo/contratos"."""
    conn = get_postgres_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, dimension, display_name, status, updated_at FROM esquemas ORDER BY dimension, id"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"id": row[0], "dimension": row[1], "display_name": row[2], "status": row[3], "updated_at": row[4].isoformat() if row[4] else None}
        for row in rows
    ]


def set_catalog_status(dataset_id: str, status: str) -> Dict[str, Any]:
    if status not in ("active", "inactive"):
        raise ValueError(f"Estado no válido: {status}")
    conn = get_postgres_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE esquemas SET status = %s, updated_at = NOW() WHERE id = %s RETURNING id, dimension, display_name, status, updated_at",
        (status, dataset_id),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not row:
        raise KeyError(f"Dataset no encontrado en el catálogo: {dataset_id}")
    return {"id": row[0], "dimension": row[1], "display_name": row[2], "status": row[3], "updated_at": row[4].isoformat() if row[4] else None}


def record_catalog_sync(triggered_by: int | None, datasets: list, status: str) -> None:
    """Gobernanza común: cada sincronización del catálogo de contratos (las reglas
    técnicas que todos los miembros del espacio de datos comparten) queda registrada
    -- quién la lanzó, cuándo y sobre qué datasets -- para que la evolución de esas
    reglas sea auditable y no un cambio silencioso."""
    conn = get_postgres_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS contratos_historial (
            id SERIAL PRIMARY KEY,
            sincronizado_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sincronizado_por INTEGER,
            status TEXT NOT NULL,
            datasets_json JSONB NOT NULL
        )
        """
    )
    cur.execute(
        "INSERT INTO contratos_historial (sincronizado_por, status, datasets_json) VALUES (%s, %s, %s::jsonb)",
        (triggered_by, status, json.dumps(datasets)),
    )
    conn.commit()
    cur.close()
    conn.close()


def list_catalog_sync_history(limit: int = 100) -> list:
    conn = get_postgres_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, sincronizado_en, sincronizado_por, status, datasets_json FROM contratos_historial ORDER BY sincronizado_en DESC LIMIT %s",
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "id": row[0], "sincronizado_en": row[1].isoformat() if row[1] else None,
            "sincronizado_por": row[2], "status": row[3], "datasets": row[4],
        }
        for row in rows
    ]


def is_dataset_active(dataset_id: str) -> bool:
    """True si el dataset no aparece en `esquemas` (aún no sincronizado, se
    considera activo por defecto) o si su estado es 'active'."""
    conn = get_postgres_conn()
    cur = conn.cursor()
    cur.execute("SELECT status FROM esquemas WHERE id = %s", (dataset_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row is None or row[0] == "active"


def _column_contract(contract: Dict[str, Any], name: str) -> Dict[str, Any]:
    allowed = contract.get("allowed_values", {})
    return {
        "name": name,
        "dataType": "VARCHAR",
        "required": name in contract.get("required_columns", []),
        "allowed": allowed.get(name, []),
        "description": "Campo del contrato técnico de la dimensión.",
    }


class ContractsManager:
    """Persiste contratos técnicos y los publica en OpenMetadata como gobierno."""

    def __init__(self, contracts=None):
        self.contracts = contracts or CONTRACTS
        self.om_api = os.getenv("OM_API", "http://localhost:9140/api/v1").rstrip("/")
        self.om_user = os.getenv("OM_USER", "admin@openmetadata.org")
        self.om_password = os.getenv("OM_PASSWORD", "admin")
        self.om_enabled = os.getenv("OM_ENABLED", "true").lower() == "true"

    def _contract_payload(self, contract_id: str, contract: Dict[str, Any]) -> Dict[str, Any]:
        columns = contract.get("columns") or contract.get("required_columns", [])
        return {
            "id": contract_id,
            "dimension": contract.get("dimension", contract_id),
            "display_name": contract_id.replace("_", " ").title(),
            "description": f"Contrato técnico de {contract_id}.",
            "columns": [_column_contract(contract, name) for name in columns],
            "required_columns": contract.get("required_columns", []),
            "rules": contract.get("rules", []),
        }

    def save_contracts_to_postgres(self) -> Dict[str, Any]:
        conn = get_postgres_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS esquemas (
                id TEXT PRIMARY KEY,
                dimension TEXT NOT NULL,
                display_name TEXT NOT NULL,
                description TEXT NOT NULL,
                columns_json JSONB NOT NULL,
                contract_json JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE esquemas ADD COLUMN IF NOT EXISTS contract_json JSONB")
        cur.execute("ALTER TABLE esquemas ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
        cur.execute("ALTER TABLE esquemas ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        saved = []
        for contract_id, contract in self.contracts.items():
            payload = self._contract_payload(contract_id, contract)
            cur.execute("""
                INSERT INTO esquemas (id, dimension, display_name, description, columns_json, contract_json, status)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, 'active')
                ON CONFLICT (id) DO UPDATE SET
                    dimension = EXCLUDED.dimension,
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    columns_json = EXCLUDED.columns_json,
                    contract_json = EXCLUDED.contract_json,
                    updated_at = NOW()
            """, (
                contract_id,
                payload["dimension"],
                payload["display_name"],
                payload["description"],
                json.dumps(payload["columns"], ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False),
            ))
            saved.append(contract_id)
        conn.commit()
        cur.close()
        conn.close()
        return {"is_valid": True, "saved": saved}

    def _login(self) -> str:
        encoded_password = base64.b64encode(self.om_password.encode()).decode()
        response = requests.post(
            f"{self.om_api}/users/login",
            json={"email": self.om_user, "password": encoded_password},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()["accessToken"]

    @staticmethod
    def _headers(token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _ensure_domain(self, token: str, domain_id: str, display_name: str) -> None:
        headers = self._headers(token)
        response = requests.get(f"{self.om_api}/domains/name/{domain_id}", headers=headers, timeout=10)
        if response.ok:
            return
        if response.status_code != 404:
            response.raise_for_status()
        response = requests.post(
            f"{self.om_api}/domains",
            headers=headers,
            json={"name": domain_id, "displayName": display_name, "domainType": "Aggregate", "description": f"Dimensión {display_name}."},
            timeout=10,
        )
        if response.status_code not in (200, 201, 409):
            response.raise_for_status()

    def _ensure_classification(self, token: str) -> None:
        headers = self._headers(token)
        response = requests.get(f"{self.om_api}/classifications/name/Validation", headers=headers, timeout=10)
        if response.ok:
            return
        if response.status_code != 404:
            response.raise_for_status()
        response = requests.post(
            f"{self.om_api}/classifications",
            headers=headers,
            json={"name": "Validation", "description": "Valores permitidos declarados en los contratos técnicos."},
            timeout=10,
        )
        if response.status_code not in (200, 201, 409):
            response.raise_for_status()

    def _ensure_parent_tag(self, token: str) -> None:
        headers = self._headers(token)
        response = requests.get(f"{self.om_api}/tags/name/Validation.Allowed", headers=headers, timeout=10)
        if response.ok:
            return
        if response.status_code != 404:
            response.raise_for_status()
        response = requests.post(
            f"{self.om_api}/tags",
            headers=headers,
            json={"name": "Allowed", "classification": "Validation", "description": "Valores permitidos, agrupados por columna."},
            timeout=10,
        )
        if response.status_code not in (200, 201, 409):
            response.raise_for_status()

    def _ensure_tag(self, token: str, value: str) -> None:
        # Un tag hijo (Validation.Allowed.<valor>) exige que la clasificación y el
        # tag padre ya existan -- OpenMetadata no los crea implícitamente.
        self._ensure_classification(token)
        self._ensure_parent_tag(token)
        headers = self._headers(token)
        tag_fqn = f"Validation.Allowed.{value}"
        response = requests.get(f"{self.om_api}/tags/name/{tag_fqn}", headers=headers, timeout=10)
        if response.ok:
            return
        if response.status_code != 404:
            response.raise_for_status()
        response = requests.post(
            f"{self.om_api}/tags",
            headers=headers,
            json={"name": value, "classification": "Validation", "description": f"Valor permitido: {value}", "parent": "Validation.Allowed"},
            timeout=10,
        )
        if response.status_code not in (200, 201, 409):
            response.raise_for_status()

    def _domain_for_table(self, table_name: str) -> str | None:
        """Deriva el dominio de gobierno de una tabla física a partir de qué
        contrato resuelve a ese nombre de tabla curada (o de los cruces
        multidimensión conocidos, que no pertenecen a una sola dimensión)."""
        if table_name in CROSS_MART_TABLES:
            return "cruces"
        from app.services import resolve_curated_table_name
        base = table_name[: -len("_resumen")] if table_name.endswith("_resumen") else table_name
        for contract_id, contract in self.contracts.items():
            if resolve_curated_table_name(contract_id) == base:
                return contract.get("dimension", base)
        return None

    def _describe_columns(self, qualified_table: str) -> list:
        """Columnas y tipos REALES de una tabla física de Trino (no los declarados
        a mano en el contrato) -- así el catálogo técnico refleja el esquema tal
        cual quedó materializado en Iceberg, tipos inferidos incluidos."""
        from app.services import run_trino_query
        try:
            rows = run_trino_query(f"DESCRIBE {qualified_table}")
        except Exception:
            return []
        columns = []
        for row in rows:
            name, trino_type = row[0], row[1]
            column = {"name": name, "dataType": _om_data_type(trino_type)}
            if column["dataType"] in ("VARCHAR", "CHAR"):
                column["dataLength"] = 255
            columns.append(column)
        return columns

    def _ensure_database_service(self, token: str) -> None:
        headers = self._headers(token)
        response = requests.get(f"{self.om_api}/services/databaseServices/name/{OM_SERVICE_NAME}", headers=headers, timeout=10)
        if response.ok:
            return
        if response.status_code != 404:
            response.raise_for_status()
        body = {
            "name": OM_SERVICE_NAME,
            "serviceType": "Trino",
            "description": "Motor de consulta SQL real del espacio de datos (Trino sobre Iceberg/Hive).",
            "connection": {
                "config": {
                    "type": "Trino",
                    "hostPort": "trino:8080",
                    "username": "admin",
                    "catalog": "lake",
                }
            },
        }
        response = requests.post(f"{self.om_api}/services/databaseServices", headers=headers, json=body, timeout=10)
        if response.status_code not in (200, 201, 409):
            response.raise_for_status()

    def _upsert_lake_table(self, token: str, schema_name: str, table_name: str) -> bool:
        headers = self._headers(token)
        columns = self._describe_columns(f"lake.{schema_name}.{table_name}")
        if not columns:
            return False
        requests.put(f"{self.om_api}/databases", headers=headers, json={"name": "lake", "service": OM_SERVICE_NAME}, timeout=10).raise_for_status()
        requests.put(
            f"{self.om_api}/databaseSchemas", headers=headers,
            json={"name": schema_name, "database": f"{OM_SERVICE_NAME}.lake"}, timeout=10,
        ).raise_for_status()
        body: Dict[str, Any] = {
            "name": table_name,
            "databaseSchema": f"{OM_SERVICE_NAME}.lake.{schema_name}",
            "columns": columns,
            "tableType": "Regular",
        }
        domain = self._domain_for_table(table_name)
        if domain:
            body["domain"] = domain
        requests.put(f"{self.om_api}/tables", headers=headers, json=body, timeout=10).raise_for_status()
        return True

    def _provision_lake_tables(self, token: str) -> Dict[str, Any]:
        """Registra en OpenMetadata las tablas FÍSICAS reales de lake.curated y
        lake.analytics (columnas y tipos reales, vía DESCRIBE en Trino) -- a
        diferencia de los contratos abstractos de más abajo, esto incluye también
        los cruces multidimensión materializados en Capa 4 (EQ1-4/6, obra activa)
        y cualquier contrato personalizado creado desde el panel, porque se
        descubren consultando el catálogo real en vez de una lista fija."""
        from app.services import run_trino_query
        self._ensure_database_service(token)
        registradas: list = []
        errores: list = []
        for schema_name in ("curated", "analytics"):
            try:
                rows = run_trino_query(f"SELECT table_name FROM lake.information_schema.tables WHERE table_schema = '{schema_name}'")
            except Exception as exc:
                errores.append({"schema": schema_name, "error": str(exc)})
                continue
            for (table_name,) in rows:
                try:
                    if self._upsert_lake_table(token, schema_name, table_name):
                        registradas.append(f"{schema_name}.{table_name}")
                except Exception as exc:
                    errores.append({"table": f"{schema_name}.{table_name}", "error": str(exc)})
        return {"tables": registradas, "errores": errores}

    def provision_lake_tables_to_openmetadata(self) -> Dict[str, Any]:
        """Punto de entrada independiente (p. ej. para un botón "resincronizar
        tablas" que no necesite tocar contratos/dominios)."""
        if not self.om_enabled:
            return {"enabled": False, "status": "disabled", "tables": []}
        token = self._login()
        result = self._provision_lake_tables(token)
        return {"enabled": True, "status": "synchronized" if not result["errores"] else "partial", **result}

    def provision_to_openmetadata(self) -> Dict[str, Any]:
        if not self.om_enabled:
            return {"enabled": False, "status": "disabled", "domains": [], "tables": []}
        token = self._login()
        domains = set()
        for contract_id, contract in self.contracts.items():
            dimension = contract.get("dimension", contract_id)
            if dimension not in domains:
                self._ensure_domain(token, dimension, CANONICAL_DOMAINS.get(dimension, dimension.replace("_", " ").title()))
                domains.add(dimension)
            payload = self._contract_payload(contract_id, contract)
            for column in payload["columns"]:
                for value in column["allowed"]:
                    self._ensure_tag(token, value)
        self._ensure_domain(token, "cruces", CANONICAL_DOMAINS["cruces"])
        domains.add("cruces")
        lake_result = self._provision_lake_tables(token)
        return {
            "enabled": True,
            "status": "synchronized" if not lake_result["errores"] else "partial",
            "domains": sorted(domains),
            "tables": lake_result["tables"],
            "errores": lake_result["errores"],
        }

    def synchronize(self, triggered_by: int | None = None) -> Dict[str, Any]:
        postgres_result = self.save_contracts_to_postgres()
        try:
            metadata_result = self.provision_to_openmetadata()
        except Exception as exc:
            log.warning("OpenMetadata no disponible: %s", exc)
            metadata_result = {"enabled": self.om_enabled, "status": "unavailable", "errors": [str(exc)]}
        status_value = "synchronized" if metadata_result["status"] != "unavailable" else "partial"
        record_catalog_sync(triggered_by, postgres_result.get("saved", []), status_value)
        return {
            "is_valid": True,
            "status": status_value,
            "postgres": postgres_result,
            "openmetadata": metadata_result,
            "synchronized_at": datetime.now(timezone.utc).isoformat(),
        }
