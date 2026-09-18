FROM python:3.12-slim
RUN pip install --no-cache-dir psycopg[binary]==3.2.10
WORKDIR /app
COPY server.py .
USER 65534:65534
CMD ["python", "server.py"]
