AFFECTACIONES_DATA_CONTRACT = {
    "dimension": "afectaciones_urbanas",
    "validation_rule_version": "capa2-v1",
    "required_columns": [
        "id",
        "nombre",
        "descripcion",
        "direccion",
        "latitud",
        "longitud",
        "fecha_inicio",
        "hora_inicio",
        "fecha_fin",
        "hora_fin",
        "tipo_intervencion",
        "tipo_afectacion",
        "titularidad",
        "impacto_pmr",
        "notas",
    ],
    "required_value_columns": [
        "id",
        "nombre",
        "fecha_inicio",
        "hora_inicio",
        "fecha_fin",
        "hora_fin",
        "tipo_intervencion",
        "tipo_afectacion",
    ],
    "allowed_values": {
        "tipo_intervencion": ["obra", "evento", "actuacion_municipal", "mantenimiento", "cierre", "otro"],
        "tipo_afectacion": ["corte_trafico", "ocupacion_calzada", "restriccion_paso", "ocupacion_acera", "trafico", "movilidad", "servicios", "otro", "intervencion"],
    },
    "date_columns": ["fecha_inicio", "fecha_fin"],
    "time_columns": ["hora_inicio", "hora_fin"],
    "temporal_coherence": True,
    "coordinate_columns": ["latitud", "longitud"],
    # Genérico: combina fecha+hora en timestamp y calcula duración — resuelto por
    # _dimension_curated_select_sql en capa 4, ya no hace falta una rama de código aparte.
    "timestamp_pairs": [
        ["fecha_inicio", "hora_inicio", "fecha_hora_inicio"],
        ["fecha_fin", "hora_fin", "fecha_hora_fin"],
    ],
    "duration_minutes": {
        "start_date": "fecha_inicio", "start_time": "hora_inicio",
        "end_date": "fecha_fin", "end_time": "hora_fin",
        "output": "duracion_minutos",
    },
    "boolean_columns": {
        "impacto_pmr": {"true": ["si", "sí"], "false": ["no"]},
    },
    "rules": [
        "Los campos obligatorios no pueden estar vacíos.",
        "El CSV debe incluir las 15 columnas del contrato, aunque los campos opcionales puedan quedar vacíos.",
        "Las fechas deben cumplir el formato YYYY-MM-DD y las horas el formato HH:MM.",
        "La fecha y hora de fin debe ser posterior a la fecha y hora de inicio.",
        "Los valores de tipo_intervencion y tipo_afectacion deben estar dentro de la lista permitida.",
    ],
}

# ITS es un inventario (una fila = un dispositivo), no un event log: sin fecha funcional ni reglas temporales.
# accesibilidad_pmr es un eje independiente de categoria: un semáforo puede ser o no acústico,
# no es un tipo de dispositivo distinto (de ahí boolean_columns en vez de un valor de catálogo
# aparte). Permite además cruzar directamente con impacto_pmr de afectaciones (AQ6/EQ3).
ITS_DATA_CONTRACT = {
    "dimension": "control_gestion_its",
    "validation_rule_version": "capa2-v1",
    "columns": ["id", "categoria", "nombre", "descripcion", "direccion", "latitud", "longitud", "titularidad", "accesibilidad_pmr"],
    "required_columns": ["id", "categoria", "latitud", "longitud"],
    "required_value_columns": ["id", "categoria", "latitud", "longitud"],
    "allowed_values": {
        "categoria": ["panel_mensaje_variable", "semaforo", "camara_trafico", "radar_trafico", "foto_rojo"],
        "titularidad": ["municipal", "autonomica", "estatal", "concesionada", "otra"],
    },
    "date_columns": [],
    "coordinate_columns": ["latitud", "longitud"],
    "boolean_columns": {
        "accesibilidad_pmr": {"true": ["si", "sí"], "false": ["no"]},
    },
    "rules": [
        "Los campos obligatorios (id, categoria, latitud, longitud) no pueden estar vacíos.",
        "categoria debe pertenecer al catálogo de dispositivos ITS.",
        "titularidad, si se informa, debe pertenecer a su catálogo.",
        "accesibilidad_pmr es opcional: indica si el dispositivo tiene adaptación de accesibilidad (p. ej. un semáforo acústico), independiente de su categoría.",
        "No aplica validación de coherencia temporal: no hay fecha_inicio/fecha_fin de negocio.",
    ],
}

# Capa 0 de Movilidad: contratos de recepción por dataset y grano.
MOVILIDAD_DATA_CONTRACTS = {
    "movilidad_carriles_bici": {
        "dimension": "movilidad",
        "validation_rule_version": "capa2-v1",
        "grain": "un tramo de carril bici registrado en una ubicación",
        "nature": "inventario",
        "functional_date": False,
        "columns": ["id", "nombre", "latitud", "longitud", "direccion", "carril_bici_longitud"],
        "required_columns": ["id", "latitud", "longitud"],
        "required_value_columns": ["id", "latitud", "longitud"],
        "coordinate_columns": ["latitud", "longitud"],
        "numeric_columns": ["carril_bici_longitud"],
    },
    "movilidad_trafico": {
        "dimension": "movilidad",
        "validation_rule_version": "capa2-v1",
        "grain": "una medición de tráfico en una ubicación y momento",
        "nature": "serie_temporal",
        "functional_date": True,
        "columns": ["id", "nombre", "latitud", "longitud", "direccion", "fecha_inicio", "hora_inicio", "fecha_fin", "hora_fin", "trafico_flujo", "trafico_vehiculo"],
        "required_columns": ["id", "latitud", "longitud", "fecha_inicio", "hora_inicio", "fecha_fin", "hora_fin", "trafico_flujo"],
        "required_value_columns": ["id", "latitud", "longitud", "fecha_inicio", "hora_inicio", "fecha_fin", "hora_fin", "trafico_flujo"],
        "date_columns": ["fecha_inicio", "fecha_fin"],
        "time_columns": ["hora_inicio", "hora_fin"],
        "temporal_coherence": True,
        "coordinate_columns": ["latitud", "longitud"],
        "non_negative_integer_columns": ["trafico_flujo"],
        "allowed_values": {"trafico_vehiculo": ["general", "moto", "ligero", "pesado"]},
        "time_features": ["fecha_inicio", "hora_inicio"],
        "timestamp_pairs": [["fecha_inicio", "hora_inicio", "fecha_hora_inicio"]],
    },
    "movilidad_plazas_reservadas": {
        "dimension": "movilidad",
        "validation_rule_version": "capa2-v1",
        "grain": "una plaza reservada registrada en una ubicación",
        "nature": "inventario",
        "functional_date": False,
        "columns": ["id", "nombre", "latitud", "longitud", "direccion", "tipo_plaza", "plaza_numero", "plaza_longitud"],
        "required_columns": ["id", "latitud", "longitud", "tipo_plaza"],
        "required_value_columns": ["id", "latitud", "longitud", "tipo_plaza"],
        "coordinate_columns": ["latitud", "longitud"],
        "allowed_values": {"tipo_plaza": ["eléctrico", "electrico_recarga", "moto", "bicicleta", "taxi", "carga_descarga", "pmr"]},
        "numeric_columns": ["plaza_longitud"],
    },
    "movilidad_parking": {
        "dimension": "movilidad",
        "validation_rule_version": "capa2-v1",
        "grain": "una medición de ocupación de parking en una ubicación y momento",
        "nature": "serie_temporal",
        "functional_date": True,
        "columns": ["id", "nombre", "latitud", "longitud", "direccion", "fecha", "hora", "ocupacion_total", "ocupacion_libres"],
        "required_columns": ["id", "latitud", "longitud", "fecha", "hora", "ocupacion_libres"],
        "required_value_columns": ["id", "latitud", "longitud", "fecha", "hora", "ocupacion_libres"],
        "date_columns": ["fecha"],
        "time_columns": ["hora"],
        "coordinate_columns": ["latitud", "longitud"],
        "non_negative_integer_columns": ["ocupacion_total", "ocupacion_libres"],
        "less_or_equal_rules": [["ocupacion_libres", "ocupacion_total"]],
        "time_features": ["fecha", "hora"],
        # Los computed_columns se evalúan sobre las columnas crudas de origen (todavía texto),
        # así que cada fórmula debe castear por su cuenta: no puede referenciar el alias ya
        # tipado de ocupacion_total/ocupacion_libres definido más arriba en el mismo SELECT.
        "computed_columns": {
            "ocupacion_rate": (
                "(try_cast(nullif(trim(ocupacion_total), '') AS double) - try_cast(nullif(trim(ocupacion_libres), '') AS double)) "
                "/ NULLIF(try_cast(nullif(trim(ocupacion_total), '') AS double), 0)"
            ),
            "saturado": (
                "(try_cast(nullif(trim(ocupacion_total), '') AS double) - try_cast(nullif(trim(ocupacion_libres), '') AS double)) "
                "/ NULLIF(try_cast(nullif(trim(ocupacion_total), '') AS double), 0) > 0.80"
            ),
        },
        "layer5_group_by": "saturado",
    },
}

OCUPACION_PERMANENTE_DATA_CONTRACT = {
    "dimension": "ocupacion_permanente_espacio_publico",
    "validation_rule_version": "capa2-v1",
    "grain": "una ocupación estable autorizada en una ubicación",
    "nature": "inventario",
    "functional_date": "opcional",
    "columns": [
        "id", "nombre_establecimiento", "tipo_ocupacion", "direccion", "latitud", "longitud",
        "ocupacion_longitud", "ocupacion_superficie", "titularidad", "estado_autorizacion",
        "fecha_inicio_autorizacion", "fecha_fin_autorizacion", "notas",
    ],
    "required_columns": ["id", "tipo_ocupacion", "latitud", "longitud", "estado_autorizacion"],
    "required_value_columns": ["id", "tipo_ocupacion", "latitud", "longitud", "estado_autorizacion"],
    "allowed_values": {
        "tipo_ocupacion": ["terraza", "quiosco", "puesto", "concesion", "otro"],
        "titularidad": ["municipal", "privada", "concesionada", "otra"],
        "estado_autorizacion": ["activa", "suspendida", "caducada", "en_revision"],
    },
    "date_columns": ["fecha_inicio_autorizacion", "fecha_fin_autorizacion"],
    "coordinate_columns": ["latitud", "longitud"],
    "optional_date_order_rules": [["fecha_inicio_autorizacion", "fecha_fin_autorizacion"]],
    "numeric_columns": ["ocupacion_longitud", "ocupacion_superficie"],
}


DATASET_CONTRACTS = {
    "gestion_afectaciones_urbanas": AFFECTACIONES_DATA_CONTRACT,
    "afectaciones_urbanas": AFFECTACIONES_DATA_CONTRACT,
    "control_gestion_its": ITS_DATA_CONTRACT,
    "ocupacion_permanente_espacio_publico": OCUPACION_PERMANENTE_DATA_CONTRACT,
    **MOVILIDAD_DATA_CONTRACTS,
}
