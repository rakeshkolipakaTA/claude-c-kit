# cckit usage API as a container. Build from the repository root:  docker build -f deploy/Dockerfile -t cckit-usage .
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --create-home cckit && mkdir /data && chown cckit /data
USER cckit
# SQLite lives on a volume: mount a persistent disk at /data (a container without one loses every row on restart)
ENV CCKIT_DB=/data/usage.db PORT=8080
EXPOSE 8080
# Required at run time (never bake them into the image): CCKIT_TEAM_KEY, CCKIT_ADMIN_TOKEN.  Optional: CCKIT_ALLOWED_DOMAINS
CMD ["sh", "-c", "cckit server run --host 0.0.0.0 --port ${PORT}"]
