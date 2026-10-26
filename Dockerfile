# Image for container hosts (Render, Fly.io, Railway, plain Docker).
# Secrets are not baked in: pass them as environment variables (see render.yaml / README.md).
FROM python:3.12-slim

# ffmpeg records the stream; tzdata makes TZ=Europe/Rome work in a slim image
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tapo_uploader.py tapo_motion.py ./
RUN mkdir -p /data

# DATA_DIR must be a persistent volume: it holds the token, the state and the clips waiting for the upload
ENV DATA_DIR=/data \
    TZ=Europe/Rome \
    PYTHONUNBUFFERED=1

# Record around motion, one upload a day. For continuous recording use: python -u tapo_uploader.py run
CMD ["python", "-u", "tapo_motion.py", "run"]
