FROM python:3.11-slim

WORKDIR /app

# Instala dependencias usando el requirements que está dentro de la carpeta tono-bot
COPY tono-bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copia tu proyecto (la carpeta tono-bot completa) al contenedor
COPY tono-bot /app/tono-bot

# Entramos a tu proyecto
WORKDIR /app/tono-bot

# Asegura que Python encuentre el paquete "src"
ENV PYTHONPATH=/app/tono-bot

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "10000"]
