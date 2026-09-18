FROM python:3.12-slim
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN pip install --no-cache-dir playwright==1.55.0 && playwright install --with-deps chromium
COPY browser.py /check.py
USER 65534:65534
ENTRYPOINT ["python", "/check.py"]
