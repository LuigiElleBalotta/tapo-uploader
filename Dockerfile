# Image for container hosts (Render, Fly.io, Railway, plain Docker).
# Secrets are not baked in: pass them as environment variables (see render.yaml / README.md).
FROM python:3.12-slim

# ffmpeg records the stream; tzdata makes TZ=Europe/Rome work in a slim image
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# requirements-web.txt includes requirements.txt and adds what the multi-account web app needs
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt
COPY tapo_uploader.py tapo_motion.py camera_context.py camera_worker.py ./
COPY tapo_web ./tapo_web
RUN mkdir -p /data

# DATA_DIR must be a persistent volume: it holds the token, the state and the clips waiting for the upload
ENV DATA_DIR=/data \
    TZ=Europe/Rome \
    PYTHONUNBUFFERED=1

# Record around motion, one upload a day (single user, configured with environment variables).
# For continuous recording use: python -u tapo_uploader.py run
# For the multi-account web app use: python -u -m tapo_web run   (with MULTIUSER=1, see README.md)
CMD ["python", "-u", "tapo_motion.py", "run"]
