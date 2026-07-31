FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Exactly 1 worker required: the APScheduler scheduler runs in-process
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8000", "--access-logfile", "-", "main:app"]
