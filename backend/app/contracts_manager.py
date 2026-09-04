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
}


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
        self.om_user = os.getenv("OM_USER", "admin@open-metadata.org")
        self.om_password = os.getenv("OM_PASSWORD", "Admin1234!")
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
                    status = 'active',
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

    def _ensure_tag(self, token: str, value: str) -> None:
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

    def provision_to_openmetadata(self) -> Dict[str, Any]:
        if not self.om_enabled:
            return {"enabled": False, "status": "disabled", "domains": [], "tables": []}
        token = self._login()
        domains = set()
        tables = []
        for contract_id, contract in self.contracts.items():
            dimension = contract.get("dimension", contract_id)
            if dimension not in domains:
                self._ensure_domain(token, dimension, CANONICAL_DOMAINS.get(dimension, dimension.replace("_", " ").title()))
                domains.add(dimension)
            payload = self._contract_payload(contract_id, contract)
            for column in payload["columns"]:
                for value in column["allowed"]:
                    self._ensure_tag(token, value)
            tables.append(contract_id)
        return {"enabled": True, "status": "synchronized", "domains": sorted(domains), "tables": tables}

    def synchronize(self) -> Dict[str, Any]:
        postgres_result = self.save_contracts_to_postgres()
        try:
            metadata_result = self.provision_to_openmetadata()
        except Exception as exc:
            log.warning("OpenMetadata no disponible: %s", exc)
            metadata_result = {"enabled": self.om_enabled, "status": "unavailable", "errors": [str(exc)]}
        return {
            "is_valid": True,
            "status": "synchronized" if metadata_result["status"] != "unavailable" else "partial",
            "postgres": postgres_result,
            "openmetadata": metadata_result,
            "synchronized_at": datetime.now(timezone.utc).isoformat(),
        }
