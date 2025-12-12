FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY technitium_exporter.py .

ENV EXPORTER_PORT=9105

EXPOSE 9105

CMD ["python", "technitium_exporter.py"]
