FROM python:3.11-slim

# ffmpeg нужен для распознавания, эффектов и обложек
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# deno: JavaScript-рантайм, без которого yt-dlp не расшифровывает ссылки YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

CMD ["python", "bot.py"]
