FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 프로젝트 전체 복사 (public/, templates/, scripts/, main.py 등 포함)
COPY . .

# Cloud Run은 PORT 환경변수를 주입합니다.
CMD ["sh", "-c", "gunicorn -b :$PORT --workers 1 --threads 8 --timeout 120 main:app"]
