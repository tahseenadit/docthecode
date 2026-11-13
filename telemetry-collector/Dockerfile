FROM python:3.13-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY collector ./collector
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn collector.app:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers"]
