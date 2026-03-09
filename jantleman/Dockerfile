FROM python:3.12-slim

WORKDIR /app

# Install deps in a separate layer so it's cached between deploys
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
