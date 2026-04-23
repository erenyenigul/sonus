FROM python:3.12-slim

WORKDIR /app

ENV POETRY_VIRTUALENVS_CREATE=false

RUN pip install --no-cache-dir poetry

COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --no-cache

COPY . .

ENV MUSIC_DIR=/music
ENV MAX_WORKERS=3

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
