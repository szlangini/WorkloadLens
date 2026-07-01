FROM python:3.12-slim

# Install system dependencies listed in system-requirements.txt (apt column)
# Keep in sync: gcc make flex bison git
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential flex bison git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Remove artifacts that install.sh regenerates
RUN rm -rf .venv tpcds-kit tests/queries

RUN bash install.sh

ENTRYPOINT [".venv/bin/workloadlens"]
