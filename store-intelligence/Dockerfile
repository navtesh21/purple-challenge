# Intelligence API image. Deliberately light — the CV stack lives in a separate
# build context (pipeline/requirements.txt) and is never installed here.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# psycopg2-binary ships wheels, so no build toolchain is needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data
COPY dashboard ./dashboard
COPY tools ./tools

# Non-root runtime user.
RUN useradd --create-home appuser && chown -R appuser /srv
USER appuser

EXPOSE 8000

# uvicorn serves the FastAPI app; lifespan creates tables and seeds POS on boot.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
