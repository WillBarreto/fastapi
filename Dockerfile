FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistema necesarias
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements primero para cache de dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar el resto de la aplicación
COPY . .

# Puerto que usará la aplicación
EXPOSE 8080

# Comando para ejecutar la aplicación - FORMA CORRECTA PARA RAILWAY
# Usamos sh -c para expandir la variable de entorno
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
