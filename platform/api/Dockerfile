FROM docker.io/library/python:3.14.0-slim-bookworm@sha256:d13fa0424035d290decef3d575cea23d1b7d5952cdf429df8f5542c71e961576

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/platform-api
COPY platform/api/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes --requirement /tmp/requirements.lock \
    && rm /tmp/requirements.lock
COPY platform/api/pyproject.toml ./pyproject.toml
COPY platform/api/src ./src
RUN python -m pip install --no-cache-dir --no-deps .

USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["lap-platform-api"]
