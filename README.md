# Proyecto Alba Verano - MVP Dimension Afectaciones Urbanas

Este proyecto implementa un MVP independiente inspirado en ayuntamiento, centrado en una sola dimension: afectaciones urbanas.

Objetivo del proyecto:
- cargar CSV de afectaciones urbanas,
- validar estructura y reglas de negocio,
- persistir registros y trazabilidad en PostgreSQL,
- publicar el fichero validado en SeaweedFS S3,
- consultar los datos con Hive/Trino y calcular KPIs reproducibles,
- preparar contexto para que una IA interprete los resultados sin sustituir los calculos.

## Estado actual

Implementado:
- Backend FastAPI con endpoints de salud, ingesta y consulta.
- Persistencia en PostgreSQL de registros de afectaciones.
- Contrato de datos y reglas de negocio para la dimension.
- Frontend en React (Vite) con vista de Dimensiones y Analisis.
- Endpoints para preparar contexto y solicitar interpretaciones a Ollama.
- SeaweedFS S3 con buckets staging, raw, curated y analytics.
- NiFi para el movimiento de staging a raw.
- Hive Metastore y Trino para consulta SQL sobre raw y tablas Iceberg.
- KPIs SQL, capas curated/analytics Iceberg bajo demanda y reglas descriptivas de capa 5.
- Superset para consultar las tablas Iceberg de Trino y construir dashboards BI.
- Suite de pruebas automatizadas del backend.

Pendiente:
- conectar la interfaz con los KPIs SQL y la capa 5,
- mostrar la interpretacion de Ollama en el frontend,
- registrar prompts y respuestas LLM en PostgreSQL.

## Estructura principal

```text
proyectoalba_verano/
├── backend/
│   ├── app/
│   │   ├── analysis_questions.py
│   │   ├── contracts.py
│   │   ├── main.py
│   │   └── services.py
│   ├── tests/
│   │   └── test_validation.py
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── main.jsx
│   │   └── styles.css
│   ├── index.html
│   ├── package.json
│   ├── vite.config.js
│   └── afectaciones_ejemplo.csv
├── docker-compose.yml
├── memoriacambios.txt
└── README.md
```

## Contrato y reglas de negocio

Archivo:
- backend/app/contracts.py

Dimension activa:
- afectaciones_urbanas

Columnas requeridas:
- id
- nombre
- descripcion
- direccion
- latitud
- longitud
- fecha_inicio
- hora_inicio
- fecha_fin
- hora_fin
- tipo_intervencion
- tipo_afectacion
- titularidad
- impacto_pmr
- notas

Campos con valor obligatorio:
- id
- nombre
- fecha_inicio
- hora_inicio
- fecha_fin
- hora_fin
- tipo_intervencion
- tipo_afectacion

Los demás campos deben figurar en la cabecera para mantener el contrato común, pero pueden quedar vacíos si no aplica o el origen no dispone del dato.

Validaciones de negocio activas:
- campos obligatorios no vacios,
- fechas en formato YYYY-MM-DD y horas en formato HH:MM,
- fecha y hora de fin posteriores a fecha y hora de inicio,
- tipo_intervencion dentro de valores permitidos,
- tipo_afectacion dentro de valores permitidos.

## Endpoints disponibles

Operativos del MVP:
- GET /health
- GET /db-check
- POST /gobierno/contratos/sincronizar
- POST /ingesta/validar
- POST /ingesta/afectaciones
- GET /afectaciones

Analisis y contexto para IA:
- GET /analysis/preguntas
- GET /analysis/preguntas?dimension=afectaciones_urbanas
- GET /analysis/contexto/afectaciones
- GET /analysis/contexto/ollama/afectaciones
- POST /analysis/interpretar/ollama/afectaciones
- GET /analysis/sql/afectaciones-kpis
- POST /iceberg/gold/afectaciones/construir
- GET /analysis/layer5/afectaciones
- POST /ingesta/movilidad?dataset=movilidad_trafico
- POST /ingesta/recepciones/its
- POST /ingesta/recepciones/ocupacion
- POST /hive/bronze/preservar?dataset=movilidad_trafico
- POST /hive/bronze/validar?dataset=movilidad_trafico
- POST /iceberg/silver/normalizar?dataset=movilidad_trafico

Notas:
- El backend prepara el contexto analítico y llama a Ollama mediante `/api/generate`.
- Por defecto usa `OLLAMA_BASE_URL=http://localhost:11434`, `OLLAMA_MODEL=llama3.2` y un máximo de 120 tokens de salida; estas variables se pueden cambiar.
- El endpoint GET permite inspeccionar el contexto antes de ejecutar el modelo.

## Arranque local

### 1) Entorno Python

```powershell
Set-Location .
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
pip install pytest httpx
```

### 2) Base de datos PostgreSQL

```powershell
docker compose up -d
```

Configuracion esperada por defecto:
- host: localhost
- port: 5433
- db: datalake
- user: datalake
- password: datalake_local

Ollama debe estar disponible en el puerto `11434` del equipo anfitrión. En Docker Compose, el backend lo alcanza mediante `host.docker.internal`.

Superset queda disponible en `http://localhost:9190` con usuario `admin` y contraseña `admin`. En Superset, añade una conexión Trino con esta URI:

```text
trino://admin@trino:8080/lake
```

Después de generar las capas analíticas, los datasets disponibles son `lake.curated.afectaciones_urbanas` y `lake.analytics.afectaciones_urbanas_resumen`.

### Generar datos sintéticos

El script `scripts/generar_afectaciones_generales.py` genera un CSV válido para la dimensión `afectaciones_urbanas`. Por defecto crea 250 filas en `frontend/afectaciones_generales_sinteticas.csv`:

```powershell
.venv\Scripts\python.exe scripts\generar_afectaciones_generales.py
```

El método utiliza el archivo de origen indicado en `--source` como distribución empírica: selecciona filas observadas con reemplazo, conserva sus relaciones entre intervención, afectación, titularidad, PMR, descripción y duración, desplaza la fecha de inicio a la ventana solicitada, mantiene la hora observada y añade una perturbación espacial de hasta 0,0008 grados. Los identificadores se regeneran para evitar duplicados. No se imponen porcentajes artificiales ni se presentan los datos sintéticos como observaciones reales.

La semilla (`--seed`) hace reproducible el remuestreo. Antes de usar el archivo se valida contra `AFFECTACIONES_DATA_CONTRACT`, por lo que las filas incompletas del origen se descartan y las filas generadas mantienen las 15 columnas requeridas, fechas coherentes y categorías permitidas.

También permite cambiar el tamaño, la semilla y la fecha inicial:

```powershell
.venv\Scripts\python.exe scripts\generar_afectaciones_generales.py --rows 1000 --seed 7 --start-date 2026-09-01 --days 180 --output frontend\afectaciones_generales_1000.csv
```

### 3) Ejecutar API

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

### 4) Ejecutar frontend React (desarrollo)

```powershell
Set-Location .\frontend
npm install
npm run dev
```

El frontend de desarrollo queda en:
- http://127.0.0.1:5173

El proxy de Vite envia las llamadas API a:
- http://127.0.0.1:8000

El frontend servido por Docker en `http://localhost:8080` usa Nginx para reenviar las rutas `/health`, `/afectaciones`, `/ingesta` y `/analysis` al contenedor backend.

### Contratos y gobierno del dato

Los contratos técnicos de `backend/app/contracts.py` son la fuente única de validación. `POST /gobierno/contratos/sincronizar` los registra en PostgreSQL (`esquemas`) y publica en OpenMetadata sus dominios, columnas y valores permitidos como tags. Si OpenMetadata no está disponible, el contrato queda guardado en PostgreSQL y la respuesta indica estado `partial`; la validación del backend no depende de la disponibilidad del catálogo.

### Capa 0 de Movilidad

La Capa 0 registra la recepción y la identidad lógica de cada dataset antes de persistir o transformar sus filas. La dimensión `movilidad` admite cuatro datasets:

| Dataset | Grano | Naturaleza | Fecha funcional |
|---|---|---|---|
| `movilidad_carriles_bici` | tramo de carril bici en una ubicación | inventario | no |
| `movilidad_trafico` | medición de tráfico en ubicación y momento | serie temporal | sí |
| `movilidad_plazas_reservadas` | plaza reservada en una ubicación | inventario | no |
| `movilidad_parking` | medición de ocupación de parking en ubicación y momento | serie temporal | sí |

La recepción se prueba mediante `POST /ingesta/movilidad` con el parámetro `dataset`. El resultado incluye `logical_key`, hash SHA-256, decisión de duplicado/conflicto, canal, emisor y metadatos del grano. Esta primera fase no persiste todavía las filas de negocio; esa será la siguiente capa.

Campos mínimos por dataset:

- `movilidad_carriles_bici`: `id`, `latitud`, `longitud`; opcionales `nombre`, `direccion`, `carril_bici_longitud`.
- `movilidad_trafico`: `id`, `latitud`, `longitud`, `fecha_inicio`, `hora_inicio`, `fecha_fin`, `hora_fin`, `trafico_flujo`; `trafico_vehiculo` admite `general`, `moto`, `ligero` y `pesado`.
- `movilidad_plazas_reservadas`: `id`, `latitud`, `longitud`, `tipo_plaza`; `tipo_plaza` admite `eléctrico`, `electrico_recarga`, `moto`, `bicicleta`, `taxi` y `carga_descarga`.
- `movilidad_parking`: `id`, `latitud`, `longitud`, `fecha`, `hora`, `ocupacion_libres`; `ocupacion_total` es opcional.

### Bronze/raw: preservación desde el frontend

En `http://localhost:8080`, selecciona un CSV y pulsa `Preservar Bronze/raw`. El backend ejecuta primero el control de admisión de la recepción y, si la entrega es aceptada, conserva el fichero original sin modificar en `staging` para su promoción a `hive.raw`. La respuesta muestra el `content_sha256`, el tamaño, la URI de almacenamiento y el manifiesto de trazabilidad con `row_id_tecnico` y `fila_origen`.

Esta acción no inserta filas de negocio ni transforma columnas. La acción `Subir CSV` mantiene el flujo completo de ingesta existente.

La identificación del dataset no depende exclusivamente del nombre del fichero: el backend normaliza las cabeceras y el frontend detecta la estructura por sus campos característicos. Así, un fichero enviado como `DATOS_AGOSTO.CSV` puede reconocerse como tráfico, parking, ITS u otra dimensión. La clave lógica de recepción sigue basándose en municipio/entidad, dimensión, dataset, periodo y versión del esquema. Un mismo contexto con contenido idéntico se marca como duplicado; contenido diferente se marca como conflicto y solo se admite mediante reemplazo controlado. La detección de duplicados de filas o solapamientos de fechas pertenece a la validación de contenido de la capa siguiente, no a Capa 0.

El frontend Docker admite archivos de hasta 50 MB. La acción `Subir CSV` identifica por el nombre los datasets `movilidad_carriles_bici`, `movilidad_trafico`, `movilidad_plazas_reservadas`, `movilidad_parking`, `control_gestion_its` y `ocupacion_permanente`, y los envía a la preservación Bronze/raw correspondiente. Los archivos de afectaciones siguen usando su endpoint de ingesta completo.

Las otras dimensiones también tienen su recepción inicial de Capa 0:

- `control_gestion_its`: `id`, `categoria`, `latitud`, `longitud` son obligatorios; `categoria` admite `panel_mensaje_variable`, `semaforo`, `camara_trafico`, `radar_trafico` y `foto_rojo`. El resto de campos (`nombre`, `descripcion`, `direccion`, `titularidad`) son opcionales.
- `ocupacion_permanente_espacio_publico`: `id`, `tipo_ocupacion`, `latitud`, `longitud` y `estado_autorizacion` son obligatorios. Los catálogos son `terraza`, `quiosco`, `puesto`, `concesion`, `otro`; titularidad `municipal`, `privada`, `concesionada`, `otra`; y estado `activa`, `suspendida`, `caducada`, `en_revision`.

Sus endpoints son `POST /ingesta/its/capa0` y `POST /ingesta/ocupacion/capa0`. Igual que en Movilidad, solo registran la recepción y la identidad lógica; la persistencia de negocio se implementará en la siguiente capa.

El registro común de Capa 0 conserva además `recepcion_id`, `fecha_hora_recepcion`, `canal_entrada`, `nombre_fichero_original`, `tamano_fichero`, `estado_recepcion`, `indicador_conflicto` y `decision_sobre_conflicto`. Cada cambio genera un evento en `ingesta_eventos_recepcion` con `evento_recepcion_id`, `recepcion_id`, `numero_intento_carga`, `fecha_hora_evento`, `actor_evento`, `resultado_evento` y `observacion_evento`.

## Despliegue con dominio propio

El frontend usa siempre rutas relativas y `nginx.conf` acepta cualquier dominio
(`server_name _;`), así que pasar de `localhost` a un dominio real no requiere tocar
ninguna ruta del código. Delante del stack hay un servicio `caddy` (HTTPS automático
vía Let's Encrypt) que hoy sirve en local sin configurar nada.

Pasos para desplegar con un dominio real:

1. Copia `.env.example` a `.env` si no existe ya, y ajusta:
   - `DOMAIN=tu-dominio.es`
   - `ACME_EMAIL=tu-email@dominio.es`
   - `JWT_SECRET=` una cadena larga y aleatoria (obligatorio cambiarlo antes de exponer el proyecto públicamente)
   - `OLLAMA_BASE_URL=` si Ollama no corre en la misma máquina con Docker Desktop
2. Apunta el registro DNS (A) de ese dominio a la IP del servidor.
3. `docker compose up -d --build` (o solo `docker compose up -d caddy` si el resto ya estaba desplegado).

Caddy obtiene y renueva el certificado HTTPS automáticamente; no hace falta gestionar
certificados a mano. El puerto 8080 (acceso directo HTTP al frontend, usado en local)
sigue disponible en paralelo; ciérralo por firewall si quieres forzar que todo el
tráfico externo pase por HTTPS.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_validation.py
```

Build de frontend:

```powershell
Set-Location .\frontend
npm run build
```

Resultado verificado en la sesion actual:
- 62 tests del backend pasados

## Flujo funcional actual

1. Subir CSV desde el frontend a `POST /ingesta/afectaciones`.
2. Registrar la entrega con entidad, dataset, periodo, versión, canal y emisor; calcular su hash y aplicar la política de duplicado, conflicto o reemplazo.
3. Guardar un evento auditable para cada recepción y cambio de estado en PostgreSQL.
4. Validar el contrato y guardar incidencias en PostgreSQL.
5. Persistir filas validas en PostgreSQL para la UI y el contexto de analisis.
6. Publicar el CSV en SeaweedFS S3/staging y su manifiesto de trazabilidad.
7. NiFi mueve el objeto de staging a raw.
8. Hive/Trino consultan `hive.raw.afectaciones_urbanas` para la entrada y `lake` para las capas analíticas.
9. De forma manual bajo demanda, `POST /iceberg/gold/afectaciones/construir` crea las tablas Iceberg `lake.curated.afectaciones_urbanas` y `lake.analytics.afectaciones_urbanas_resumen`.
10. La interfaz actual muestra PostgreSQL y solo prepara el contexto de Q3; aun no muestra los KPIs SQL ni capa 5.

### Metadatos de recepción

`POST /ingesta/afectaciones` acepta además de `file` los parámetros `entity`, `dataset`, `period`, `schema_version`, `entry_channel`, `sender` y `replace`. Por defecto usa `municipio_demo`, `web_manual` y `usuario_local`, evitando depender de una entidad externa al proyecto.

### Preservación del original

Bronze/raw conserva el CSV recibido sin añadir columnas ni modificar su contenido. SeaweedFS S3 es la fuente de verdad y organiza cada entrega como `s3://raw/<dimension>/<entidad>/<dataset>/<archivo>.csv`. Se registra ubicación, tamaño, hash SHA-256, formato y estado de preservación en `ingesta_objetos_preservados`. Los identificadores técnicos por fila se publican en un manifiesto independiente dentro de `_traceability`, sin alterar el CSV original. La carpeta `data/lake` se conserva solo para pruebas locales heredadas y no forma parte del despliegue Docker.

### Resultado de validación

La validación Bronze clasifica cada CSV como `rejected`, `accepted_with_warnings` o `ready_for_ingestion`. Cada incidencia guarda un código de regla, severidad, mensaje, campo afectado, fila de origen, `row_id_tecnico` cuando aplica y acción requerida. La evidencia se conserva en `ingesta_incidencias`; los errores bloqueantes impiden promover datos a Silver.

### Iceberg Silver y Gold

La normalización Silver transforma `hive.raw.afectaciones_urbanas` en `lake.curated.afectaciones_urbanas` mediante una CTAS Iceberg con formato Parquet. Gold se materializa como `lake.analytics.afectaciones_urbanas_resumen`, también como tabla Iceberg. Las ubicaciones nuevas usan los prefijos `curated/iceberg/` y `analytics/iceberg/`; los Parquet históricos se conservan.

## Proximo paso recomendado

Se ha dejado preparado el siguiente bloque de integracion del stack de datos:
- servicio NiFi en docker-compose para preparar la etapa de ingestión del lago,
- rutas de staging/raw en backend,
- puntos de extension para mover archivos y conectarse a un flujo automatizado.

El siguiente objetivo práctico es:
- abrir la UI de NiFi en http://localhost:9090/nifi,
- crear un flujo simple de ejemplo: ListFile -> PutFile en raw,
- conectar ese movimiento con el layout de lago ya preparado por dataset.

## Integracion del stack de datos 

Para incorporar de forma ordenada Airflow, NiFi, Trino, OpenMetadata y SeaweedFS, se ha documentado una guia por fases en:

- docs/integracion_stack_datos.md

La guia incluye:
- rol de cada tecnologia,
- arquitectura objetivo,
- plan de adopcion por fases,
- orden recomendado de integracion,
- riesgos y mitigaciones.

