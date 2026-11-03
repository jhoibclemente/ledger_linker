# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/var/data SECURE_COOKIES=true
WORKDIR /service
COPY requirements.txt .
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 ledger && mkdir /var/data && chown ledger:ledger /var/data
COPY backend ./backend
COPY app ./app
RUN chmod -R a+rX /service/backend /service/app
EXPOSE 10000
CMD ["python", "-m", "backend.entrypoint"]
