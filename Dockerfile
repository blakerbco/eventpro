FROM python:3.12-slim

# Install system dependencies for ODBC driver
RUN apt-get update && apt-get install -y --no-install-recommends \
    unixodbc-dev \
    gnupg \
    curl \
    apt-transport-https \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list | tee /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data directory (Railway volume mounts here)
RUN mkdir -p /app/data/results

EXPOSE 5000

CMD ["python", "app.py"]
