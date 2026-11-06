FROM python:3.15-slim
RUN pip install --no-cache-dir psycopg[binary]==3.3.6
WORKDIR /app
COPY server.py .
USER 65534:65534
CMD ["python", "server.py"]
