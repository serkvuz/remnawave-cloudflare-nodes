FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

RUN mkdir -p logs data

RUN printf '#!/bin/sh\nexec python -m src.cli "$@"\n' > /usr/local/bin/cli && chmod +x /usr/local/bin/cli

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "src"]
