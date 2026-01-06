# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

COPY requirements.txt ./

# ✅ BuildKit 캐시를 사용해 pip 다운로드/휠 빌드를 재사용
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# 프로젝트 전체 복사 (public/, templates/, scripts/, main.py 등 포함)
COPY . .

CMD ["sh", "-c", "gunicorn -b :$PORT --workers 1 --threads 8 --timeout 120 main:app"]
