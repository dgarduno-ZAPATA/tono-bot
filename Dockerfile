FROM python:3.11-slim

# 1. Carpeta de trabajo
WORKDIR /app

# 2. Dependencias (capa cacheada)
COPY tono-bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Código de la aplicación
# tono-bot/ → /app/  (src/, data/ quedan en /app/src/, etc.)
# brand/    → /brand/ (brand_config.py hace 3 niveles arriba desde /app/src/)
COPY tono-bot/ .
COPY brand/ /brand/

# 4. Usuario non-root (seguridad: si el contenedor es comprometido, no tiene privilegios de admin)
RUN adduser --disabled-password --no-create-home appuser \
    && mkdir -p /app/db \
    && chown -R appuser:appuser /app /brand
USER appuser

# 5. Python config
ENV PYTHONPATH=/app
# Explicitly set WEB_CONCURRENCY=1 — the app uses in-memory state (pending_messages,
# dedup sets, per-JID locks) that would break with multiple workers.
# This prevents Render from auto-setting a higher value.
ENV WEB_CONCURRENCY=1

EXPOSE 8080

# 6. Health check (Render/Docker saben si la app está viva)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# 7. Arranque
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
