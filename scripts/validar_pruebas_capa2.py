"""Ejecuta la validacion real de Capa 2 (validate_csv_text) contra los CSV de
prueba en pruebas_validacion/, cada uno violando una regla especifica de una
dimension distinta (Tabla 36). No requiere backend levantado ni autenticacion:
llama directamente a la logica de validacion del servicio.

Uso: .venv/Scripts/python.exe scripts/validar_pruebas_capa2.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.contracts import AFFECTACIONES_DATA_CONTRACT, ITS_DATA_CONTRACT, MOVILIDAD_DATA_CONTRACTS
from app.services import validate_csv_text

PRUEBAS_DIR = os.path.join(os.path.dirname(__file__), "..", "pruebas_validacion")

CASOS = [
    ("afectaciones_urbanas_fecha_invalida.csv", AFFECTACIONES_DATA_CONTRACT,
     "Afectaciones urbanas: fecha_fin/hora_fin anterior a fecha_inicio/hora_inicio"),
    ("movilidad_trafico_flujo_negativo.csv", MOVILIDAD_DATA_CONTRACTS["movilidad_trafico"],
     "Movilidad trafico: trafico_flujo negativo (debe ser entero >= 0)"),
    ("movilidad_parking_libres_mayor_total.csv", MOVILIDAD_DATA_CONTRACTS["movilidad_parking"],
     "Movilidad parking: ocupacion_libres > ocupacion_total"),
    ("control_gestion_its_categoria_invalida.csv", ITS_DATA_CONTRACT,
     "Control Gestion ITS: categoria fuera del catalogo permitido"),
]


def main():
    for filename, contract, descripcion in CASOS:
        path = os.path.join(PRUEBAS_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            csv_text = f.read()

        result = validate_csv_text(csv_text, contract["required_columns"], contract)

        print(f"\n{'=' * 70}")
        print(f"CASO: {descripcion}")
        print(f"Fichero: {filename}")
        print(f"{'=' * 70}")
        print(f"validation_status : {result['validation_status']}")
        print(f"row_count         : {result['row_count']}")
        print(f"errors            : {result['errors']}")
        print(f"incidencias ({len(result.get('incidencias', []))}):")
        for inc in result.get("incidencias", []):
            print(f"  - codigo_regla: {inc['codigo_regla']}")
            print(f"    tipo_regla: {inc['tipo_regla']}  |  severidad: {inc.get('severidad')}")
            print(f"    campo_afectado: {inc.get('campo_afectado')}  |  fila origen: {inc.get('row_number_origen')}")
            print(f"    descripcion_incidencia: {inc['descripcion_incidencia']}")

        out_path = os.path.join(PRUEBAS_DIR, filename.replace(".csv", "_resultado_capa2.json"))
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n  -> Resultado completo guardado en {os.path.basename(out_path)}")


if __name__ == "__main__":
    main()
