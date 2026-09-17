FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && useradd --create-home --uid 10001 nexus
COPY nexus ./nexus
USER nexus
EXPOSE 8000
CMD ["uvicorn", "nexus.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
