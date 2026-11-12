FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HOTDEAL_DATA_DIR=/app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data

EXPOSE 8000
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
