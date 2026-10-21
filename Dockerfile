FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
COPY newsagg ./newsagg
RUN pip install .

COPY config ./config

RUN useradd --create-home newsagg
USER newsagg

ENTRYPOINT ["newsagg"]
CMD ["run", "all"]
