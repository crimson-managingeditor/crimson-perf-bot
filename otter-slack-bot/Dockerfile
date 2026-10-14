FROM python:3.12-slim

# git is needed to pip-install the unofficial Otter library from GitHub
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Socket Mode worker: no inbound port, just an outbound connection to Slack.
CMD ["python", "app.py"]
