FROM python:3.11-slim

# 1. Carpeta de trabajo
WORKDIR /app

# 2. Dependencias (capa cacheada)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Código de la aplicación
COPY . .

# 4. Usuario non-root (seguridad: si el contenedor es comprometido, no tiene privilegios de admin)
RUN adduser --disabled-password --no-create-home appuser \
    && mkdir -p /app/db \
    && chown -R appuser:appuser /app
USER appuser

# 5. Python config
ENV PYTHONPATH=/app

EXPOSE 8080

# 6. Health check (Render/Docker saben si la app está viva)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# 7. Arranque
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
