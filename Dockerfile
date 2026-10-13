FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY ledger ledger
COPY skills skills
COPY personas personas
COPY fixtures fixtures
COPY policies policies
ENV PYTHONUNBUFFERED=1
CMD exec uvicorn ledger.service:app --host 0.0.0.0 --port ${PORT:-8080}
