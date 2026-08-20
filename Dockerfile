FROM python:3.12-slim

# tzdata: sem ele o zoneinfo não resolve America/Fortaleza e tudo vira UTC
RUN pip install --no-cache-dir tzdata \
 && useradd -m -u 1000 onpro \
 && mkdir -p /var/tmp/onpro_alerta && chown onpro:onpro /var/tmp/onpro_alerta

WORKDIR /app
COPY onpro_alerta.py /app/
RUN chmod +x /app/onpro_alerta.py

USER onpro
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=America/Fortaleza \
    ONPRO_STATE=/var/tmp/onpro_alerta \
    ONPRO_EMPRESAS=/app/config/empresas.json

ENTRYPOINT ["python3"]
CMD ["/app/onpro_alerta.py", "--agendar"]
