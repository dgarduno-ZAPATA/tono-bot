FROM python:3.11-slim

# 1. Definimos la carpeta de trabajo
WORKDIR /app

# 2. Copiamos los requerimientos (Directo, sin buscar carpetas raras)
COPY requirements.txt .

# 3. Instalamos dependencias (incluyendo httpx que acabas de agregar)
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copiamos TODO el código (src, data, main.py, etc) a la carpeta /app
COPY . .

# 5. Aseguramos que Python encuentre tus módulos
ENV PYTHONPATH=/app

# 6. Exponemos el puerto 8080 (Estándar de Render)
EXPOSE 8080

# 7. Arrancamos la app
# Nota: Cambié el puerto a 8080 para coincidir con lo habitual en Render
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
