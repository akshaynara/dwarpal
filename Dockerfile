# Cloud Run:  gcloud run deploy dwarpal --source . --region asia-south1 --allow-unauthenticated
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DWARPAL_DATA_DIR=/tmp/dwarpal
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
EXPOSE 8080
CMD ["sh", "-c", "dwarpal serve --host 0.0.0.0 --port ${PORT:-8080}"]
