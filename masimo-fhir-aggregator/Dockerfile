FROM mcr.microsoft.com/cbl-mariner/base/python:3@sha256:d1dd9c1296ec1a311efc2dd35d74a9c8f2be3c5e264d9a26c847b1123807312c

WORKDIR /app

# Stdlib only (see requirements.txt): nothing to install.
COPY aggregator.py .

ENV PYTHONUNBUFFERED=1

CMD ["python3", "-u", "aggregator.py", "--once"]
