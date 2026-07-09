FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

COPY . .

CMD gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --timeout 0
