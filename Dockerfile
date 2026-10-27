# Optional deployment as a container (local web UI on port 8080)
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV R3TR_UI_HOST=0.0.0.0
ENV R3TR_UI_PORT=8080
EXPOSE 8080

# run.py binds to 127.0.0.1 by default; inside the container bind on 0.0.0.0
CMD ["python", "run.py", "--host", "0.0.0.0"]
