FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PORT=8765
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sdr/ sdr/
COPY static/ static/
COPY prompts/ prompts/
COPY data/ data/
COPY scripts/ scripts/
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m appuser \
    && mkdir -p /app/var \
    && chown -R appuser:appuser /app
EXPOSE 8765
ENTRYPOINT ["sh", "-c", "mkdir -p /app/var && chown -R appuser:appuser /app/var && exec gosu appuser python scripts/start_demo.py"]
