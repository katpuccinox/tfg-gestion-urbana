# Archivo: ml.py
# Resumen: Modelo predictivo entrenado (no basado en reglas) de nivel de congestión
# de tráfico. Complementa a predecir_congestion_via (percentiles sobre el histórico
# propio de cada vía, en services.py) con un RandomForestClassifier real, entrenado
# sobre las 9348 mediciones reales de movilidad_trafico -- con ese volumen ya
# compensa entrenar un modelo de verdad, cosa que no ocurría con los ~150 registros
# sintéticos de versiones anteriores del proyecto (ver docstring de
# predecir_congestion_via).

from typing import Any, Dict, List

from sklearn.ensemble import RandomForestClassifier
from sklearn.compose import ColumnTransformer
from sklearn.metrics import accuracy_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from app.services import run_trino_query_with_columns

FEATURE_COLUMNS = ["direccion", "dia_semana", "franja_horaria", "hora_punta", "es_fin_de_semana", "trafico_vehiculo"]
CATEGORICAL_COLUMNS = ["direccion", "dia_semana", "franja_horaria", "trafico_vehiculo"]

_modelo_cache: Dict[str, Any] = {"pipeline": None, "metrics": None}


def _percentiles_por_via(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Percentiles p50/p75/p90 de trafico_flujo, por vía -- misma regla que
    build_movilidad_trafico_layer5 (300 veh/h es crítico en una calle residencial
    y normal en una avenida). Incluye una clave "_global" para poder clasificar una
    vía que no tuviera histórico en el periodo de referencia (p. ej. una vía que
    solo aparece en el tramo de test de una evaluación temporal)."""
    flujos_por_via: Dict[str, List[float]] = {}
    for row in rows:
        flujos_por_via.setdefault(row["direccion"] or "sin_dato", []).append(float(row["trafico_flujo"] or 0))

    def percentiles(valores: List[float]) -> Dict[str, float]:
        ordenados = sorted(valores)
        def pct(p: float) -> float:
            return ordenados[min(len(ordenados) - 1, int(len(ordenados) * p))]
        return {"p50": pct(0.50), "p75": pct(0.75), "p90": pct(0.90)}

    percentiles_por_via = {via: percentiles(valores) for via, valores in flujos_por_via.items()}
    percentiles_por_via["_global"] = percentiles([v for valores in flujos_por_via.values() for v in valores])
    return percentiles_por_via


def _etiquetar(rows: List[Dict[str, Any]], percentiles_por_via: Dict[str, Dict[str, float]]) -> List[str]:
    """Clasifica cada fila contra los percentiles ya calculados de su vía. Recibir
    los percentiles como parámetro (en vez de recalcularlos sobre las mismas rows)
    es lo que permite evaluar con un split temporal: al etiquetar el tramo de test
    se usan los percentiles aprendidos SOLO del tramo de train, para no filtrar
    información del futuro (el propio umbral) hacia la evaluación."""
    labels = []
    for row in rows:
        via = row["direccion"] or "sin_dato"
        flujo = float(row["trafico_flujo"] or 0)
        p = percentiles_por_via.get(via, percentiles_por_via["_global"])
        if flujo >= p["p90"]:
            labels.append("Crítico")
        elif flujo >= p["p75"]:
            labels.append("Alto")
        elif flujo >= p["p50"]:
            labels.append("Medio")
        else:
            labels.append("Bajo")
    return labels


def _construir_pipeline() -> Pipeline:
    preprocessor = ColumnTransformer(
        transformers=[("cat", OneHotEncoder(handle_unknown="ignore"), [FEATURE_COLUMNS.index(c) for c in CATEGORICAL_COLUMNS])],
        remainder="passthrough",
    )
    return Pipeline([
        ("preprocess", preprocessor),
        ("model", RandomForestClassifier(n_estimators=200, max_depth=12, random_state=42, class_weight="balanced")),
    ])


def train_congestion_model() -> Dict[str, Any]:
    """Entrena (o reentrena) el clasificador y cachea en memoria el que se usará
    para servir predicciones. El accuracy se mide con un split TEMPORAL -- se
    ordena todo el histórico por fecha_hora_inicio, se entrena con el 80% más
    antiguo y se evalúa contra el 20% más reciente -- en vez de un split aleatorio.
    Un split aleatorio dejaría filas de la misma vía/día/franja repartidas entre
    train y test, y el modelo podría acertar memorizando ese patrón en vez de
    generalizar a un periodo que no ha visto; con tráfico, que es una serie
    temporal, eso infla el accuracy de forma artificial. El modelo que queda
    cacheado para /predecir-ml se reentrena después con el 100% del histórico
    (igual que se haría en producción tras validar la metodología), pero el
    accuracy que se reporta es siempre el de la evaluación temporal honesta."""
    result = run_trino_query_with_columns(
        f"SELECT {', '.join(FEATURE_COLUMNS + ['trafico_flujo'])}, fecha_hora_inicio "
        "FROM lake.curated.movilidad_trafico ORDER BY fecha_hora_inicio NULLS LAST"
    )
    rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
    if len(rows) < 50:
        metrics = {"is_valid": False, "errors": ["No hay suficientes mediciones reales para entrenar un modelo (mínimo 50)."]}
        _modelo_cache["pipeline"], _modelo_cache["metrics"] = None, metrics
        return metrics

    corte = int(len(rows) * 0.8)
    train_rows, test_rows = rows[:corte], rows[corte:]

    percentiles_train = _percentiles_por_via(train_rows)
    y_train = _etiquetar(train_rows, percentiles_train)
    y_test = _etiquetar(test_rows, percentiles_train)
    X_train = [[row[col] for col in FEATURE_COLUMNS] for row in train_rows]
    X_test = [[row[col] for col in FEATURE_COLUMNS] for row in test_rows]

    pipeline_evaluacion = _construir_pipeline()
    pipeline_evaluacion.fit(X_train, y_train)
    accuracy = accuracy_score(y_test, pipeline_evaluacion.predict(X_test)) if test_rows else None

    # Modelo final servido a /predecir-ml: reentrenado con el histórico completo,
    # una vez que el accuracy de arriba ya validó la metodología sobre datos no vistos.
    percentiles_completos = _percentiles_por_via(rows)
    y_full = _etiquetar(rows, percentiles_completos)
    X_full = [[row[col] for col in FEATURE_COLUMNS] for row in rows]
    pipeline = _construir_pipeline()
    pipeline.fit(X_full, y_full)

    importances = pipeline.named_steps["model"].feature_importances_
    # Agrega la importancia de cada categoría one-hot de vuelta a su columna original
    # (para que "direccion" sea una sola barra, no una por cada una de las ~85 vías),
    # usando las longitudes reales de encoder.categories_ en vez de parsear nombres
    # generados por sklearn (frágil: cambia según se le pase una lista o un DataFrame).
    encoder = pipeline.named_steps["preprocess"].named_transformers_["cat"]
    passthrough_columns = [c for c in FEATURE_COLUMNS if c not in CATEGORICAL_COLUMNS]
    importancia_por_columna: Dict[str, float] = {col: 0.0 for col in FEATURE_COLUMNS}
    cursor = 0
    for columna, categorias in zip(CATEGORICAL_COLUMNS, encoder.categories_):
        importancia_por_columna[columna] = float(sum(importances[cursor:cursor + len(categorias)]))
        cursor += len(categorias)
    for columna in passthrough_columns:
        importancia_por_columna[columna] = float(importances[cursor])
        cursor += 1

    metrics = {
        "is_valid": True,
        "modelo": "RandomForestClassifier",
        "validacion": "split temporal: entrena con el 80% del histórico más antiguo, evalúa con el 20% más reciente (nunca visto en el entrenamiento)",
        "filas_entrenamiento": len(train_rows),
        "filas_test": len(test_rows),
        "accuracy": round(accuracy, 3) if accuracy is not None else None,
        "clases": sorted(set(y_full)),
        "importancia_features": [
            {"feature": col, "importancia": round(val, 3)}
            for col, val in sorted(importancia_por_columna.items(), key=lambda item: item[1], reverse=True)
        ],
    }
    _modelo_cache["pipeline"], _modelo_cache["metrics"] = pipeline, metrics
    return metrics


def get_model_metrics() -> Dict[str, Any]:
    if _modelo_cache["metrics"] is None:
        return train_congestion_model()
    return _modelo_cache["metrics"]


def predict_congestion_ml(direccion: str, dia_semana: str, franja_horaria: str, hora_punta: bool) -> Dict[str, Any]:
    if _modelo_cache["pipeline"] is None:
        train_congestion_model()
    pipeline = _modelo_cache["pipeline"]
    if pipeline is None:
        return {"is_valid": False, "errors": ["El modelo no se pudo entrenar."]}

    es_fin_de_semana = dia_semana.strip().lower() in ("sabado", "sábado", "domingo")
    fila = [[direccion.strip().lower(), dia_semana.strip().lower(), franja_horaria, hora_punta, es_fin_de_semana, "general"]]
    prediccion = pipeline.predict(fila)[0]
    probabilidades = pipeline.predict_proba(fila)[0]
    clases = pipeline.named_steps["model"].classes_
    confianza = round(max(probabilidades), 3)
    return {
        "is_valid": True,
        "direccion": direccion,
        "nivel_congestion_ml": prediccion,
        "confianza": confianza,
        "distribucion": {clase: round(float(prob), 3) for clase, prob in zip(clases, probabilidades)},
    }
