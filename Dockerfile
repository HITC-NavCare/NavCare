# NAVCare API image. Cloud Run sets $PORT; locally it defaults to 8080.
FROM python:3.12-slim

# Print logs immediately (Cloud Run reads stdout) and skip .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install packages first so this layer is cached when only our code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
