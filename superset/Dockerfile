FROM apache/superset:6.0.0

USER root
RUN uv pip install --python /app/.venv/bin/python trino sqlalchemy-trino
USER superset

COPY superset_config.py /app/pythonpath/superset_config.py
