# Estructura del Data Lake (SeaweedFS S3)

Este documento describe la estructura real de carpetas del lake (verificada por
inspección directa de los buckets, no solo por diseño teórico) y las convenciones
que sigue el pipeline de ingesta actual.

## Cómo acceder al lake

El lake vive en SeaweedFS (S3-compatible), no en el repo. `data/lake/` en el repo
es un resto de pruebas locales antiguas y **no se usa** en el flujo actual (ver
`docs/integracion_stack_datos.md`).

- **Navegación visual (sin firmar peticiones)**: http://localhost:8888/buckets/
  — es la UI del filer de SeaweedFS, expuesta en `docker-compose.yml`
  (`seaweedfs-filer`, puerto `8888`, solo accesible en `127.0.0.1`).
- **CLI (`mc`)**: `mc.exe` en la raíz del repo (no versionado, ver `.gitignore`).
  ```powershell
  .\mc.exe alias set locallake http://localhost:9100 seaweed_access_key seaweed_secret_key
  .\mc.exe ls locallake/raw/afectaciones_urbanas/
  .\mc.exe cat locallake/raw/afectaciones_urbanas/CORDOBA/gestion_afectaciones_urbanas/gestion_afectaciones_urbanas.csv
  ```
- **SQL (Trino)**: `docker exec proyecto-trino trino --server http://localhost:8080 --catalog lake --execute "SELECT * FROM lake.curated.afectaciones_urbanas LIMIT 10"`
- **Python (boto3)**, ya instalado en `.venv`:
  ```python
  import boto3
  s3 = boto3.client("s3", endpoint_url="http://localhost:9100",
                     aws_access_key_id="seaweed_access_key",
                     aws_secret_access_key="seaweed_secret_key",
                     region_name="us-east-1")
  ```
  Credenciales hardcodeadas en `docker-compose.yml` e `infra/seaweedfs/s3.json` — solo válidas en local.

Un navegador normal da "access denied": el S3 de SeaweedFS exige peticiones
firmadas (AWS SigV4); un `GET` sin firmar desde el navegador se rechaza. Por
eso hace falta `mc`, `boto3`/`aws cli`, o la UI del filer (que no exige firma).

## Estructura por bucket = capa

```
lake (SeaweedFS S3)
│
├── staging/          [vestigial — el pipeline actual NO escribe aquí]
│
├── raw/               [Capa 0-1: CSV crudo, inmutable + trazabilidad]
│   ├── <dimension>/<municipio>/<dataset>/<archivo>.csv
│   └── _traceability/<dimension>/<municipio>/<dataset>/<archivo>.csv.rows.csv
│
├── curated/            [Capa 3-4: normalizado + tabla Iceberg tipada]
│   ├── _normalized/<dimension>/<municipio>/<dataset>/<delivery_id>/normalized.csv
│   └── iceberg/<dataset>/{data/*.parquet, metadata/*.{json,avro,stats}}
│
└── analytics/           [Capa 5: KPIs / marts agregados, Iceberg]
    └── iceberg/<tabla_resumen>/{data/*.parquet, metadata/*.{json,avro,stats}}
```

### `staging/`

Bucket vestigial. El diseño original (fase A de `integracion_stack_datos.md`)
preveía `staging -> NiFi -> raw`, pero el backend actual escribe **directamente
a `raw`** (`publish_upload_to_s3`, `backend/app/services.py`). El único modo de
que algo llegue a `staging` es una prueba manual o un flujo NiFi aparte. Se
vació el 2026-09-07 (contenía solo ficheros de prueba, ver "Limpieza" más abajo).

### `raw/`

CSV crudo tal cual llegó, organizado como `<dimensión>/<municipio>/<dataset>/<archivo>`.
`_traceability/` guarda, por cada entrega, un CSV paralelo con un hash
(`row_id_tecnico`) por fila — permite atar cualquier fila de una tabla curada
a la entrega y el fichero exactos de los que vino.

Datasets activos: `afectaciones_urbanas`, `movilidad` (carriles_bici, parking,
trafico, plazas_reservadas), `control_gestion_its`, `ocupacion_permanente_espacio_publico`.

### `curated/`

Dos cosas distintas conviven aquí, no confundir:

- **`_normalized/`**: CSV (no Iceberg) por cada entrega individual, con dos
  columnas añadidas de linaje (`dataset_id`, `row_id_tecnico`). Se publica
  también como tabla Hive externa (`hive.normalized.<dataset>`) que apunta al
  último CSV normalizado.
- **`iceberg/<dataset>/`**: la tabla Iceberg tipada real, consultable por SQL
  en Trino (`lake.curated.<dataset>`). Es la que usan los endpoints
  `/analysis/layer4` y `/analysis/layer5`.

> Nota histórica: existió una copia huérfana en `curated/<dataset>/` (raíz,
> sin pasar por `iceberg/`) — un snapshot Iceberg de una versión antigua del
> código, sin ningún catálogo apuntándola. Se confirmó con
> `SHOW CREATE TABLE` en Trino que la tabla activa vive en
> `s3://curated/iceberg/<dataset>`, y se borró la huérfana (2026-09-07).

### `analytics/`

Tablas Iceberg agregadas (`iceberg/<tabla>_resumen/` y los marts `eq1..eq4`),
consumidas por dashboards y por `/analysis/cuadro-mando`.

## Convención de nombre de municipio

**Antes (bug):** el `municipio_id`/`entity` se usaba tal cual llegaba en la
petición para construir la clave S3 (`<dimension>/<entity>/<dataset>/...`),
sin normalizar. Resultado real observado en el lake: `CORDOBA`, `malaga`,
`MALAGA`, `SEVILLA` conviviendo como prefijos distintos para el mismo
municipio, fragmentando los datos de un mismo sitio en varias carpetas.

**Ahora (corregido en `backend/app/services.py`):** `register_delivery`,
`preserve_dimension_delivery` y `run_automatic_dimension_pipeline` normalizan
`entity`/`municipio_id` con `normalize_catalog_value` (minúsculas, sin tildes,
sin espacios en los extremos) antes de construir cualquier clave S3 o
`logical_key` de entrega. Es la misma función que ya normalizaba valores de
catálogo en la Capa 4 (`_nivel_zona`), reutilizada aquí por consistencia.

Esto cubre los tres puntos de entrada de ingesta (`/ingesta/afectaciones`,
`/ingesta/movilidad`, `/ingesta/capa1/preservar`) y también los endpoints que
saltan `resolve_upload_municipio` (`/ingesta/its/capa0`,
`/ingesta/ocupacion/capa0`, `/hive/bronze/preservar`), porque el fix está en
la capa de servicio (el embudo real de todos ellos), no en cada endpoint.

Los filtros SQL por municipio (`lower(split_part(dataset_id, '|', 1)) =
lower(...)`) ya comparaban en minúsculas, así que el fix no rompe el
filtrado por municipio de un usuario con rol municipal — solo evita que se
sigan creando prefijos nuevos inconsistentes.

**Migración retroactiva (hecha el 2026-09-07):** las carpetas históricas con
casing inconsistente (`CORDOBA/`, `MALAGA/`, `SEVILLA/` en `raw/` y
`curated/_normalized/`) se migraron a minúsculas. Regla aplicada por objeto:

- Si el destino en minúsculas no existía → mover sin más.
- Si ya existía un objeto en minúsculas con el **mismo contenido** (hash
  sha256 igual) → se trata de un duplicado, se borra la copia con casing
  antiguo y se conserva la de minúsculas.
- Si ya existía un objeto en minúsculas con **contenido distinto** (dos
  entregas reales distintas que casualmente comparten nombre de fichero) →
  **no se sobreescribe nada**: se renombra la variante con casing antiguo
  añadiendo un sufijo `__from_<CASING_ORIGINAL>` antes de la extensión, para
  no perder ninguna de las dos.

Colisiones de contenido distinto encontradas y resueltas así (5 casos, todas
`MALAGA` vs `malaga`, ninguna con `CORDOBA`/`SEVILLA` porque esas no tenían
ya una carpeta en minúsculas previa):

- `raw/control_gestion_its/malaga/control_gestion_its/control_gestion_its__from_MALAGA.csv`
- `raw/movilidad/malaga/movilidad_parking/movilidad_parking__from_MALAGA.csv`
- y las 3 `_traceability` correspondientes a esos dos más a
  `ocupacion_permanente_espacio_publico`.

Verificado en Trino: las tablas Iceberg de `curated`/`analytics` (`lake.curated.*`)
solo contenían `malaga` como valor de municipio en `dataset_id` — ni
`CORDOBA` ni `cordoba` aparecen ahí. Es decir, los datos de Córdoba llevan
todo este tiempo en `raw/` pero **nunca se promovieron** a las tablas
Iceberg tipadas (el `source_table` de `build_afectaciones_curated_sql` es una
única tabla externa Hive, no un barrido de todos los municipios de `raw/`).
No se ha tocado esto — es un hallazgo aparte, no un problema de casing, y
queda pendiente de decisión.

## Limpieza de datos de prueba (2026-09-07)

Se eliminaron del lake, por estar sueltos (no organizados por municipio como
exige el pipeline actual) o nombrados explícitamente como prueba:

- `staging/` completo (2 ficheros de prueba del flujo NiFi legacy).
- `raw/afectaciones_urbanas/*.csv` sueltos en la raíz del dataset
  (`afectaciones_dinamico.csv`, `afectaciones_generales_sinteticas.csv`,
  `afectaciones_sinteticas_prueba.csv`, `afectaciones_urbanas_1000.csv`,
  `gestion_afectaciones_urbanas.csv`, `prueba_s3_final.csv`,
  `s3_solo_20260819.csv`) y sus `_traceability` asociadas.
- `raw/arbolado_urbano/` completo — dataset de prueba, no es uno de los
  datasets soportados por el contrato de datos actual.
- `curated/_normalized/movilidad/PIPELINE_TEST/` y `.../test_capa2capa3_v2/`
  — carpetas de prueba nombradas explícitamente como tales.
- Copia huérfana `curated/afectaciones_urbanas/` (ver nota histórica arriba).

Segunda pasada de limpieza (mismo día, al inventariar antes de migrar el
casing) — se encontró más cruft no detectado en la primera pasada:

- `raw/afectaciones_urbanas/municipio_demo/{control_gestion_its,
  movilidad_carriles_bici, movilidad_parking, movilidad_plazas_reservadas,
  movilidad_trafico}/*` — datos de otras dimensiones archivados por error
  bajo la carpeta de dimensión `afectaciones_urbanas` (incluía un
  `viviendas_vacias_malaga_tabla.csv`, de un tema ajeno al dataset). Su
  `_traceability` tenía el formato plano antiguo (`_traceability/<dataset>/<archivo>`,
  sin municipio), lo que confirma que son restos de antes de la convención
  actual — no una entrega real mal clasificada.
- `raw/movilidad/{PIPELINE_TEST,test_capa2capa3,test_capa2capa3_v2,test_reject_v1}/`
  completos (la copia en `raw/`; las de `curated/_normalized/` ya se habían
  borrado en la primera pasada).
- Ficheros de prueba sueltos dentro de carpetas reales: `test_mini.csv`,
  `vis_test.csv` (`MALAGA/movilidad_carriles_bici`), `test_parking_delete.csv`,
  `test_parking_delete2.csv` (`SEVILLA/movilidad_parking` — con esto,
  `SEVILLA` quedó vacío del todo en `movilidad`, no tenía ningún dato real),
  `movilidad_trafico_migrate.csv`, `_q2_test.csv`, `_test.csv`
  (`municipio_demo/movilidad_trafico`), `control_gestion_its_test.csv`,
  `ocupacion_permanente_test.csv`.
- Sus `_normalized` correspondientes en `curated/` (los `SEVILLA/movilidad_parking/74,75,76`
  y `MALAGA/movilidad_carriles_bici/68` venían precisamente de esos ficheros
  de prueba) y el `arbolado_urbano/SEVILLA/.../78` que había quedado huérfano
  en `curated/_normalized` tras borrar el dataset de `raw/` en la primera pasada.

Total segunda pasada: 44 objetos eliminados + migración de casing (20
movidos limpios, 2 duplicados idénticos eliminados, 5 renombrados por
colisión de contenido distinto — ver sección "Convención de nombre de
municipio" más arriba).

No se tocaron los registros de `ingesta_entregas` en PostgreSQL (el catálogo
de entregas) — sigue teniendo el `municipio_id` con el casing con el que se
registró cada entrega en su momento; solo se migró el object storage (S3).
