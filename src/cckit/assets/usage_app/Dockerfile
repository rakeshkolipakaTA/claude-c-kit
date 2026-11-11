# cckit usage API.   docker build -t cckit-usage .
FROM python:3.12-slim
WORKDIR /app
COPY usage_api.py .
RUN useradd --system --create-home cckit && mkdir /data && chown cckit /data
USER cckit
# SQLite lives on a volume: mount a persistent disk at /data (without one, every row is lost on restart)
ENV CCKIT_DB=/data/usage.db PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
# Set at run time, never in the image:  CCKIT_TEAM_KEY, CCKIT_ADMIN_TOKEN   (optional: CCKIT_ALLOWED_DOMAINS)
CMD ["sh", "-c", "python usage_api.py run --host 0.0.0.0 --port ${PORT}"]
