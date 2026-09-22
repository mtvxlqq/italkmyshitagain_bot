FROM python:3.13-slim

# ffmpeg — для видео и музыки (в Debian он собран с libx264)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# база (отложенные посты, статистика) — в отдельной папке, чтобы её можно было подключить как том
ENV DB_PATH=/app/data/bot.db \
    PYTHONUNBUFFERED=1
RUN mkdir -p /app/data
VOLUME /app/data

CMD ["python", "bot.py"]
