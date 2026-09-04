import os
from datetime import datetime, timezone
from typing import Any

import psycopg2
from fastapi import Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import get_current_user

DEFAULT_POLICY_VERSION = "v1"
DEFAULT_POLICY_TITULO = "Condiciones de uso del espacio de datos"
DEFAULT_POLICY_CONTENIDO = (
    "Al subir datos a este espacio de datos, la entidad remitente declara que: "
    "(1) tiene potestad para publicar los datos remitidos; (2) los datos no contienen "
    "información personal no anonimizada salvo que el contrato del dataset lo permita "
    "expresamente; (3) acepta que los datos publicados sean reutilizables por el resto "
    "de entidades del espacio de datos según las condiciones de cada dataset; y "
    "(4) es responsable de la veracidad y actualización de los datos remitidos."
)


class PublishPolicyRequest(BaseModel):
    version: str
    titulo: str
    contenido: str


def get_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "datalake"),
        user=os.getenv("POSTGRES_USER", "datalake"),
        password=os.getenv("POSTGRES_PASSWORD", "datalake_local"),
    )


def init_policies_table() -> None:
    connection = get_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS politicas_uso (
                        id SERIAL PRIMARY KEY,
                        version VARCHAR(30) UNIQUE NOT NULL,
                        titulo VARCHAR(255) NOT NULL,
                        contenido TEXT NOT NULL,
                        activa BOOLEAN NOT NULL DEFAULT false,
                        publicada_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        publicada_por INTEGER
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS politicas_aceptaciones (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        version VARCHAR(30) NOT NULL,
                        aceptado_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (user_id, version)
                    )
                    """
                )
                cursor.execute("SELECT 1 FROM politicas_uso WHERE activa = true")
                if not cursor.fetchone():
                    cursor.execute(
                        """
                        INSERT INTO politicas_uso (version, titulo, contenido, activa, publicada_por)
                        VALUES (%s, %s, %s, true, NULL)
                        ON CONFLICT (version) DO UPDATE SET activa = true
                        """,
                        (DEFAULT_POLICY_VERSION, DEFAULT_POLICY_TITULO, DEFAULT_POLICY_CONTENIDO),
                    )
    finally:
        connection.close()


def get_active_policy() -> dict[str, Any]:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT version, titulo, contenido, publicada_en FROM politicas_uso WHERE activa = true ORDER BY publicada_en DESC LIMIT 1"
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    if not row:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="No hay ninguna política de uso publicada")
    return {"version": row[0], "titulo": row[1], "contenido": row[2], "publicada_en": row[3].isoformat() if row[3] else None}


def has_accepted_active_policy(user_id: int) -> bool:
    active = get_active_policy()
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM politicas_aceptaciones WHERE user_id = %s AND version = %s",
                (user_id, active["version"]),
            )
            return cursor.fetchone() is not None
    finally:
        connection.close()


def accept_active_policy(user_id: int) -> dict[str, Any]:
    active = get_active_policy()
    connection = get_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO politicas_aceptaciones (user_id, version)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id, version) DO NOTHING
                    """,
                    (user_id, active["version"]),
                )
    finally:
        connection.close()
    return {"version": active["version"], "aceptado": True}


def publish_policy_version(request: PublishPolicyRequest, admin_id: int) -> dict[str, Any]:
    """Publica una nueva versión de las condiciones de uso y desactiva la vigente.
    Al ser versionada: quien ya había aceptado la versión anterior deja de estar al
    día y `has_accepted_active_policy` volverá a dar False hasta que la acepte de nuevo."""
    connection = get_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE politicas_uso SET activa = false WHERE activa = true")
                cursor.execute(
                    """
                    INSERT INTO politicas_uso (version, titulo, contenido, activa, publicada_por, publicada_en)
                    VALUES (%s, %s, %s, true, %s, NOW())
                    ON CONFLICT (version) DO UPDATE SET
                        titulo = EXCLUDED.titulo, contenido = EXCLUDED.contenido,
                        activa = true, publicada_por = EXCLUDED.publicada_por, publicada_en = NOW()
                    """,
                    (request.version, request.titulo, request.contenido, admin_id),
                )
    finally:
        connection.close()
    return get_active_policy()


def list_policy_acceptance_audit() -> list[dict[str, Any]]:
    active = get_active_policy()
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.id, u.name, u.email, u.municipio_id, u.role,
                       pa.aceptado_en
                FROM users u
                LEFT JOIN politicas_aceptaciones pa
                    ON pa.user_id = u.id AND pa.version = %s
                ORDER BY (pa.aceptado_en IS NULL) DESC, u.municipio_id, u.name
                """,
                (active["version"],),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    return [
        {
            "user_id": row[0], "name": row[1], "email": row[2], "municipio_id": row[3], "role": row[4],
            "version_vigente": active["version"],
            "aceptada": row[5] is not None,
            "aceptado_en": row[5].isoformat() if row[5] else None,
        }
        for row in rows
    ]


def require_policy_accepted(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    active = get_active_policy()
    if not has_accepted_active_policy(user["id"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "POLICY_NOT_ACCEPTED",
                "version": active["version"],
                "message": "Debes aceptar las condiciones de uso vigentes antes de subir datos.",
            },
        )
    return user
