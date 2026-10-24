FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DB_PATH=/app/data/runna.db
EXPOSE 8080
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8080"]
