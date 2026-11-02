FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KHUNKHAAR_ROOT=/app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.lock.txt /app/
RUN pip install --no-cache-dir -r requirements.lock.txt

# Copy KHUNKHAAR codebase
COPY . /app

EXPOSE 8899

CMD ["python", "core/cli.py", "server", "8899"]
