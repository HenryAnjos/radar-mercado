FROM python:3.12-slim

# git é necessário para instalar a pyquotex direto do GitHub
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data deve ser um Volume do Railway para o placar sobreviver a reinícios e deploys
ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=America/Sao_Paulo

CMD ["python", "-m", "src.main"]
