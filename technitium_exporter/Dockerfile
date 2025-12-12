FROM python:3.13-slim

# OCI image metadata (used by GitHub Packages, Docker Hub, etc.)

LABEL org.opencontainers.image.title="Technitium DNS Prometheus Exporter" \
      org.opencontainers.image.description="Lightweight Prometheus exporter for Technitium DNS Server exposing dashboard, DNS, zone, DHCP, and top-query metrics for Grafana." \
      org.opencontainers.image.url="github.com/guycalledseven/technitium-dns-prometheus-exporter" \
      org.opencontainers.image.source="github.com/guycalledseven/technitium-dns-prometheus-exporter" \
      org.opencontainers.image.documentation="github.com/guycalledseven/technitium-dns-prometheus-exporter/#readme" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.authors="Neven Jacmenović" \
      org.opencontainers.image.vendor="Community" \
      org.opencontainers.image.version="1.0.0"

# Runtime	  

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY technitium_exporter.py .

ENV EXPORTER_PORT=9105

EXPOSE 9105

CMD ["python", "technitium_exporter.py"]
