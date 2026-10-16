# Imagen de la API del agente (api_server.py). Se construye DESDE LA RAÍZ del repo (la de la demo o la principal):
#   docker build -f "experimental_proyects/Vera Intelligence/10. documentos/deploy_api/Dockerfile" -t vera-intelligence-api .
#   docker run -d --restart always -p 8080:8080 --env-file ./api.env vera-intelligence-api
# `--restart always` es necesario: la API se cierra sola al detectar un Data Map nuevo y el supervisor la levanta con el nuevo.
# `api.env` lleva los secretos (ver env.api.example); NO se copia a la imagen ni se versiona.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY ["experimental_proyects/Vera Intelligence/4. scripts/requirements-api.txt", "./requirements-api.txt"]
RUN pip install --no-cache-dir -r requirements.txt -r requirements-api.txt

COPY . .
WORKDIR "/app/experimental_proyects/Vera Intelligence/4. scripts"

ENV VI_API_HOST=0.0.0.0 \
    VI_API_PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8
EXPOSE 8080
# UN solo proceso: sesiones y contadores viven en memoria.
CMD ["python", "api_server.py"]
