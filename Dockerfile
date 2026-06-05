FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai

# Install Python deps
COPY requirements.txt web/requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

# Copy project
COPY . .

EXPOSE 5000
CMD ["python", "-m", "web.app", "--host", "0.0.0.0", "--port", "5000"]
