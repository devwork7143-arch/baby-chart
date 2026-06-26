FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py parser.py render.py nap.py ./
# Container defaults: persist data on the mounted volume, and bind all
# interfaces so the platform's router/healthcheck can reach the app.
# (Local non-Docker runs keep their 127.0.0.1 default.)
ENV DATA_DIR=/data \
    HOST=0.0.0.0
CMD ["python", "bot.py"]
