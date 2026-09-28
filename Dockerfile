FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mailer.py server.py store.py ./
COPY public ./public

ENV JTQ_HOST=0.0.0.0
ENV JTQ_PUBLIC=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["python", "server.py"]
