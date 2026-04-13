FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY backend/05_INTAKE/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy only what the services need
COPY backend/05_INTAKE/ /app/backend/05_INTAKE/
COPY backend/02_MIDDLE/ /app/backend/02_MIDDLE/
COPY backend/01_INITIAL/ /app/backend/01_INITIAL/
COPY backend/03_SEARCH/ /app/backend/03_SEARCH/
COPY backend/utils/ /app/backend/utils/
COPY .env /app/.env
