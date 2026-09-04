import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import psycopg2
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-change-this-secret-32-bytes-long")
security = HTTPBearer()

ROLE_ADMIN = "admin_estatal"
ROLE_EDITOR = "editor_municipio"
ROLE_LECTOR = "lector_municipio"
ROLE_CONSUMIDOR = "consumidor"
ROLE_AUDITOR = "auditor"
# Alias retrocompatible: el rol histórico "usuario_local" pasa a ser editor_municipio
# (era el que de facto subía datos). init_auth_table() migra las filas existentes.
ROLE_USUARIO = ROLE_EDITOR
ALL_ROLES = {ROLE_ADMIN, ROLE_EDITOR, ROLE_LECTOR, ROLE_CONSUMIDOR, ROLE_AUDITOR}
WRITE_ROLES = {ROLE_ADMIN, ROLE_EDITOR}
MUNICIPIO_SCOPED_ROLES = {ROLE_EDITOR, ROLE_LECTOR}


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    municipio_id: str
    role: str = ROLE_EDITOR


class UpdateUserRequest(BaseModel):
    role: str | None = None
    activo: bool | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


def get_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "datalake"),
        user=os.getenv("POSTGRES_USER", "datalake"),
        password=os.getenv("POSTGRES_PASSWORD", "datalake_local"),
    )


def init_auth_table() -> None:
    connection = get_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(120) NOT NULL,
                        email VARCHAR(255) UNIQUE NOT NULL,
                        municipio_id VARCHAR(120) NOT NULL,
                        password_hash TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS municipio_id VARCHAR(120)")
                cursor.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(30) NOT NULL DEFAULT '{ROLE_EDITOR}'")
                cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS activo BOOLEAN NOT NULL DEFAULT true")
                # Migración: el rol histórico "usuario_local" pasa a ser editor_municipio.
                cursor.execute(f"UPDATE users SET role = '{ROLE_EDITOR}' WHERE role = 'usuario_local'")
                for name, email, password, municipio_id, role in (
                    ("Alcaldía de Málaga", "malaga@demo.local", "malaga123", "MALAGA", ROLE_EDITOR),
                    ("Ayuntamiento de Sevilla", "sevilla@demo.local", "sevilla123", "SEVILLA", ROLE_EDITOR),
                    ("Ayuntamiento de Valencia", "valencia@demo.local", "valencia123", "VALENCIA", ROLE_EDITOR),
                    ("Ayuntamiento de Córdoba", "cordoba@demo.local", "cordoba123", "CORDOBA", ROLE_EDITOR),
                    ("Administración de la Plataforma", "admin@plataforma.local", "admin123", "PLATAFORMA", ROLE_ADMIN),
                    ("Lectura Málaga", "lector.malaga@demo.local", "lector123", "MALAGA", ROLE_LECTOR),
                    ("Consumidor de datos", "consumidor@demo.local", "consumidor123", "PLATAFORMA", ROLE_CONSUMIDOR),
                    ("Auditoría del espacio de datos", "auditor@demo.local", "auditor123", "PLATAFORMA", ROLE_AUDITOR),
                ):
                    cursor.execute("SELECT 1 FROM users WHERE email = %s", (email,))
                    if not cursor.fetchone():
                        cursor.execute(
                            "INSERT INTO users (name, email, municipio_id, password_hash, role) VALUES (%s, %s, %s, %s, %s)",
                            (name, email, municipio_id, hash_password(password), role),
                        )
    finally:
        connection.close()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_password: str) -> bool:
    try:
        salt_hex, digest_hex = stored_password.split("$", 1)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 310_000
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_token(user_id: int, email: str, role: str = ROLE_USUARIO) -> str:
    expiration = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "role": role, "exp": expiration},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def register_user(request: RegisterRequest) -> dict[str, Any]:
    role = request.role if request.role in ALL_ROLES else ROLE_EDITOR
    connection = get_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO users (name, email, municipio_id, password_hash, role) VALUES (%s, %s, %s, %s, %s) RETURNING id, name, email, municipio_id, role, activo",
                    (request.name, request.email.lower(), request.municipio_id.upper(), hash_password(request.password), role),
                )
                user = cursor.fetchone()
    except psycopg2.IntegrityError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="El correo ya está registrado")
    finally:
        connection.close()
    return {"id": user[0], "name": user[1], "email": user[2], "municipio_id": user[3], "role": user[4], "activo": user[5]}


def authenticate_user(request: LoginRequest) -> dict[str, Any]:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, name, email, municipio_id, password_hash, role, activo FROM users WHERE email = %s",
                (request.email.lower(),),
            )
            user = cursor.fetchone()
    finally:
        connection.close()
    if not user or not verify_password(request.password, user[4]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales no válidas")
    if not user[6]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Esta cuenta está desactivada")
    return {"id": user[0], "name": user[1], "email": user[2], "municipio_id": user[3], "role": user[5], "activo": user[6]}


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict[str, Any]:
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token no válido")

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, name, email, municipio_id, role, activo FROM users WHERE id = %s", (user_id,))
            user = cursor.fetchone()
    finally:
        connection.close()

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario no encontrado")
    if not user[5]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Esta cuenta está desactivada")
    return {"id": user[0], "name": user[1], "email": user[2], "municipio_id": user[3], "role": user[4], "activo": user[5]}


def list_users() -> list[dict[str, Any]]:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, name, email, municipio_id, role, activo, created_at FROM users ORDER BY municipio_id, role, name"
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    return [
        {
            "id": row[0], "name": row[1], "email": row[2], "municipio_id": row[3],
            "role": row[4], "activo": row[5], "created_at": row[6].isoformat() if row[6] else None,
        }
        for row in rows
    ]


def update_user(user_id: int, request: UpdateUserRequest) -> dict[str, Any]:
    if request.role is not None and request.role not in ALL_ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Rol no válido: {request.role}")

    connection = get_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                if request.role is not None:
                    cursor.execute("UPDATE users SET role = %s WHERE id = %s", (request.role, user_id))
                if request.activo is not None:
                    cursor.execute("UPDATE users SET activo = %s WHERE id = %s", (request.activo, user_id))
                cursor.execute(
                    "SELECT id, name, email, municipio_id, role, activo FROM users WHERE id = %s", (user_id,)
                )
                user = cursor.fetchone()
    finally:
        connection.close()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
    return {"id": user[0], "name": user[1], "email": user[2], "municipio_id": user[3], "role": user[4], "activo": user[5]}


def delete_user(user_id: int, requesting_admin_id: int) -> dict[str, Any]:
    """Borra una cuenta del espacio de datos. No se puede borrar la propia cuenta desde
    aquí (evita que un admin se quede fuera por error) ni la última cuenta admin_estatal
    (el espacio de datos siempre necesita al menos un administrador)."""
    if user_id == requesting_admin_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No puedes borrar tu propia cuenta")

    connection = get_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT role FROM users WHERE id = %s", (user_id,))
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")

                if row[0] == ROLE_ADMIN:
                    cursor.execute("SELECT count(*) FROM users WHERE role = %s", (ROLE_ADMIN,))
                    if cursor.fetchone()[0] <= 1:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="No se puede borrar el último administrador del espacio de datos",
                        )

                cursor.execute("DELETE FROM politicas_aceptaciones WHERE user_id = %s", (user_id,))
                cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    finally:
        connection.close()
    return {"id": user_id, "deleted": True}


def resolve_upload_municipio(user: dict[str, Any], requested_municipio_id: str | None) -> str:
    """Evita que un editor/lector suba datos a nombre de otro municipio: para roles
    con ámbito municipal, el municipio de la subida es siempre el suyo propio,
    ignorando lo que venga en el parámetro de la petición."""
    if user.get("role") in MUNICIPIO_SCOPED_ROLES:
        return user["municipio_id"]
    return (requested_municipio_id or user.get("municipio_id") or "").strip() or user["municipio_id"]


def require_roles(*allowed_roles: str):
    """Factoría de dependencias FastAPI: exige que el usuario autenticado tenga
    uno de los roles indicados. `require_admin`/`require_write_access`/
    `require_audit_access` son instancias concretas de esta factoría."""
    allowed = set(allowed_roles)

    def dependency(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if user.get("role") not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Esta acción requiere uno de estos roles: {', '.join(sorted(allowed))}",
            )
        return user

    return dependency


require_admin = require_roles(ROLE_ADMIN)
require_write_access = require_roles(*WRITE_ROLES)
require_audit_access = require_roles(ROLE_ADMIN, ROLE_AUDITOR)
