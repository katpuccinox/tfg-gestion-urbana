# Integracion del Stack de Datos (Airflow, NiFi, Trino, OpenMetadata, SeaweedFS)

## 1. Objetivo

Incorporar progresivamente en `proyectoalba_verano` el stack elegido, manteniendo estable el MVP actual de afectaciones urbanas:

- FastAPI + PostgreSQL + React
- Ingesta CSV validada

## 2. Rol de cada tecnologia

- **SeaweedFS**: almacenamiento objeto compatible S3 para capas `staging`, `raw`, `curated`, `analytics`.
- **NiFi**: ingestion ETL desde `staging` hacia `raw` con trazabilidad y control de flujo.
- **Hive Metastore**: catalogo tecnico de esquemas, tablas y ubicaciones en S3.
- **Trino**: query engine SQL sobre Hive Metastore, SeaweedFS S3 y tablas Iceberg.
- **Iceberg**: formato de tablas analiticas en curated/analytics.
- **OpenMetadata**: gobierno, catalogo y lineage sobre Trino, HMS y SeaweedFS.
- **Elasticsearch + PostgreSQL**: busqueda interna y persistencia de estado.
- **Superset**: dashboards BI sobre Trino.
- **Prometheus + Grafana**: metricas y observabilidad.
- **Airflow**: orquestacion de ingestas y metadatos.

## 3. Estado actual del proyecto

Actualmente `proyectoalba_verano` tiene:

- PostgreSQL (docker compose)
- backend FastAPI
- frontend React
- SeaweedFS S3
- NiFi con flujo S3 validado
- Trino arrancado con catalogo provisional
- Iceberg analítico
- Superset añadido para dashboards BI sobre el catálogo Iceberg `lake`.

## 3.1 Flujo de datos actual y evolución

El flujo inicial del MVP queda así:

1. El usuario sube un CSV desde la interfaz.
2. El backend valida el archivo y prepara registros estructurados.
3. Los datos se persisten en PostgreSQL para alimentar la API y la UI actual.

La evolución prevista del flujo es la siguiente:

1. Las fuentes entregan datos a NiFi.
2. NiFi publica objetos en SeaweedFS S3 (`staging` y `raw`).
3. Hive Metastore registra esquemas, tablas y ubicaciones S3.
4. Trino consulta tablas Hive e Iceberg.
5. Superset consume Trino para dashboards BI.
6. OpenMetadata gobierna Trino, HMS y SeaweedFS.
7. Elasticsearch y PostgreSQL soportan busqueda y estado interno.
8. Airflow orquesta ingestas, controles y metadatos.

La implementación local usa SeaweedFS S3 como fuente de verdad. Cada CSV Bronze/raw se aísla por dimensión, entidad y dataset:

- `s3://raw/<dimension>/<entidad>/<dataset>/<archivo>.csv`

FastAPI registra la recepción y publica el CSV en ese prefijo. Hive registra una tabla externa sobre el mismo directorio y Trino consulta esa tabla. La carpeta `data/lake` no se monta en el despliegue Docker: se mantiene solo como apoyo de pruebas locales heredadas.

Con este enfoque, el MVP sigue funcionando sin cambios bruscos y se añade una capa de datos más robusta por fases.

## 4. Arquitectura objetivo 

```text
[Fuentes] -> NiFi -> SeaweedFS S3 -> Hive Metastore -> Trino -> Superset
              |                    |
              +-> OpenMetadata      +-> KPIs
              +-> Elasticsearch + PostgreSQL
Airflow -> ingestas y metadatos
Prometheus + Grafana -> observabilidad transversal
```

## 5. Plan de incorporacion por fases

## Fase A - Base S3 + ingestiones (SeaweedFS + NiFi)

Objetivo: desacoplar la ingesta del backend y empezar arquitectura de lago.

1. Levantar SeaweedFS en compose local con:
- `master`, `volume`, `filer`, `s3`
- buckets: `staging`, `raw`, `curated`, `analytics`

2. Ajustar backend para doble escritura opcional:
- persistencia actual en PostgreSQL (sin romper MVP)
- copia del archivo a `staging` en SeaweedFS

3. Levantar NiFi y crear flujo minimo:
- `ListS3(staging)` -> `FetchS3Object` -> validacion basica -> `PutS3Object(raw)` -> `DeleteS3Object(staging)`

4. Validaciones de salida:
- carga CSV desde UI
- objeto aparece en `raw`
- endpoint backend sigue respondiendo igual

## Fase B - Capa SQL (Trino)

Objetivo: consultar datos del lago sin pasar por PostgreSQL.

1. Levantar Trino con catalogo `hive` apuntando a SeaweedFS S3.
2. Crear esquema inicial para afectaciones (`hive.bronze` o `hive.raw`).
3. Añadir endpoint backend para consulta de prueba:
- ejemplo: `GET /analysis/sql/afectaciones-kpis`

4. Mantener paridad temporal:
- UI puede seguir consumiendo `/afectaciones` desde PostgreSQL
- nueva vista analitica consume KPI desde Trino

## Fase C - Catalogo y gobernanza (OpenMetadata)

Objetivo: descubrir y gobernar activos de datos.

1. Levantar OpenMetadata con MySQL + Elasticsearch.
2. Registrar servicios:
- servicio database Trino
- buckets/activos relevantes de SeaweedFS

3. Definir dominios de negocio iniciales:
- movilidad
- afectaciones_urbanas
- ocupacion_permanente
- its

4. Publicar metadatos del dataset de afectaciones:
- owner
- descripcion
- tags de calidad
- frecuencia de actualizacion

## Fase D - Orquestacion (Airflow)

Objetivo: estandarizar ejecucion programada y dependencias.

1. Levantar Airflow con DAGs locales.
2. DAG minimo diario:
- comprobar llegada de archivos
- trigger flujo NiFi
- validacion de conteos
- refresco/ingestion de metadatos en OpenMetadata

3. SLA/alertas:
- fallo de ingestion
- volumen anomalo
- fallo de actualizacion de catalogo

## 6. Orden recomendado de trabajo

1. SeaweedFS
2. NiFi
3. Trino
4. OpenMetadata
5. Airflow

Razon: primero almacenamiento e ingesta, luego consulta, despues gobernanza y finalmente orquestacion.

## 7. Variables de entorno sugeridas

Agregar en backend y compose variables preparatorias:

- `S3_ENDPOINT`
- `S3_ACCESS_KEY`
- `S3_SECRET_KEY`
- `S3_BUCKET_STAGING`
- `S3_BUCKET_RAW`
- `TRINO_HOST`
- `TRINO_PORT`
- `TRINO_USER`
- `NIFI_BASE`
- `NIFI_USER`
- `NIFI_PASS`
- `OM_API`
- `OM_USER`
- `OM_PASSWORD`

## 8. Decision importante para este proyecto

En `proyectoalba_verano`, Airflow no debe sustituir al flujo interactivo del MVP. Debe añadirse como capa de orquestacion de procesos batch y de metadatos.

## 9. Riesgos y mitigaciones

- Complejidad operativa alta (5 tecnologias nuevas):
  - Mitigar con activacion por fases y compose por perfiles.
- Inconsistencia entre PostgreSQL y lago durante transicion:
  - Mitigar con doble escritura temporal y checks de conteo.
- Coste de mantenimiento:
  - Mitigar con documentacion y convenciones de naming desde el inicio.

## 10. Primer entregable tecnico concreto

Para la siguiente iteracion, el entregable recomendado es:

- compose extendido con SeaweedFS + NiFi
- backend sube archivo a `staging`
- NiFi mueve a `raw`
- endpoint de verificacion de objeto en `raw`

Con eso queda incorporado el primer bloque del stack sin romper lo actual.

## 11. Siguiente hoja de ruta

1. Documentar y estabilizar NiFi con el flujo S3 activo y el flujo local detenido.
2. Añadir verificacion S3 al backend para consultar el estado del objeto en staging y raw.
3. Eliminar la doble escritura local cuando el flujo S3 quede consolidado.
4. Incorporar Trino conectado a los buckets raw de SeaweedFS.
5. Crear consultas analiticas y exponer KPIs desde el backend.
6. Incorporar OpenMetadata para catalogo, calidad y lineage.
7. Incorporar Airflow para planificacion, controles y supervision de pipelines.

### 11.1 Paso 1: estabilizacion de NiFi

El flujo valido para la arquitectura S3 es:

```text
ListS3Object -> FetchS3Object -> PutS3Object -> DeleteS3Object
```

Configuracion operativa:

- Bucket de entrada: `staging`.
- Prefijo de entrada: `afectaciones_urbanas/`.
- Bucket de salida: `raw`.
- Clave del objeto: `${filename}`.
- Endpoint interno: `http://seaweedfs-s3:8333`.
- Credenciales: Controller Service AWS habilitado.

El flujo local `ListFile -> FetchFile -> PutFile` debe permanecer detenido para no duplicar movimientos. Las relaciones de error deben dirigirse a una cola de incidencias o a `LogAttribute`; durante la primera prueba pueden auto-terminarse.

### 11.2 Paso 4: incorporacion inicial de Trino

Se ha añadido el servicio Trino en `http://localhost:9110` y el catálogo `lake` basado en Iceberg con Hive Metastore y almacenamiento S3 en SeaweedFS.

Estado de validacion:

- `SHOW CATALOGS` devuelve `lake` y `system`.
- La consulta `SHOW SCHEMAS FROM lake` devuelve el catálogo Iceberg operativo.
- Hive Metastore es el catálogo técnico principal e Iceberg se utiliza para las tablas analíticas.
- Las tablas `lake.curated` y `lake.analytics` se crean desde el endpoint de generación de capas.

### 11.3 Arquitectura

```text
Fuentes -> NiFi -> SeaweedFS S3 -> Hive Metastore -> Trino -> Superset
                                      |
                                      +-> OpenMetadata -> Trino/HMS/SeaweedFS
                                      +-> Elasticsearch + PostgreSQL
Airflow -> ingestas y metadatos
Prometheus + Grafana -> observabilidad transversal
```

El `403` del REST catalog de SeaweedFS confirma que Hive Metastore debe ser el siguiente componente de catalogo tecnico.
