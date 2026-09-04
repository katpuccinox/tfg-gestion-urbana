import os

SECRET_KEY = os.getenv("SUPERSET_SECRET_KEY", "proyecto-alba-superset-local-secret")
SQLALCHEMY_DATABASE_URI = os.getenv(
    "SUPERSET_DATABASE_URI",
    "sqlite:////app/superset_home/superset.db",
)
WTF_CSRF_ENABLED = True
ENABLE_PROXY_FIX = True
FAB_ADD_SECURITY_API = True
SQLALCHEMY_TRACK_MODIFICATIONS = False

# Configuración exclusiva de desarrollo local para permitir el iframe del frontend municipal.
PUBLIC_ROLE_LIKE = "Gamma"
GUEST_ROLE_NAME = "Gamma"
TALISMAN_ENABLED = False
HTTP_HEADERS = {"X-Frame-Options": "ALLOWALL"}
TALISMAN_CONFIG = {
    "content_security_policy": None,
    "force_https": False,
    "frame_options": None,
}

FEATURE_FLAGS = {
    "ENABLE_TEMPLATE_PROCESSING": False,
    "EMBEDDED_SUPERSET": True,
}
