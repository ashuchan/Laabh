FROM python:3.12-slim
RUN pip install --no-cache-dir httpx==0.27.0
WORKDIR /app
COPY docker/watchdog/healthcheck.py .
CMD ["python", "healthcheck.py"]
