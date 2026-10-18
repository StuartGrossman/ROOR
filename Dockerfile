FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/service/bridge
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY archive.py signals.py live_api.py cloud_api.py live_reader.py supervisor.py ./
CMD ["python", "supervisor.py"]
