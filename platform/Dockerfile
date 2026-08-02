FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD gunicorn -w 1 -k gthread --threads 8 --timeout 900 -b 0.0.0.0:${PORT:-8080} app:app
