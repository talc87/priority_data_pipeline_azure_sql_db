# Use the base image for Python 3.10 on Debian 11 (Bullseye)
FROM python:3.10.4-slim-bullseye as python-base

# Set the working directory
WORKDIR /app

# Install system dependencies for pyodbc and Microsoft ODBC Driver
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    apt-utils \
    gnupg2 \
    unixodbc-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Download the GPG key to a temporary location and then move it to trusted.gpg.d
RUN mkdir -p /tmp/keys && \
    curl https://packages.microsoft.com/keys/microsoft.asc -o /tmp/keys/microsoft.asc && \
    gpg --dearmor /tmp/keys/microsoft.asc && \
    mv /tmp/keys/microsoft.asc.gpg /etc/apt/trusted.gpg.d/microsoft.gpg && \
    rm -rf /tmp/keys

# Add the Microsoft package repository for Debian 11 (Bullseye)
RUN curl https://packages.microsoft.com/config/debian/11/prod.list > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y msodbcsql18 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application files
# COPY . .
COPY . /app


ENV metadataDbName=metadataDB
ENV configDbName=priority_dwh_admin
ENV configCollectionName=configCollection
ENV datatypeMappingCollectionName=datatypeMapping

# Expose the port for Flask
EXPOSE 5000

# Run the Flask application
CMD ["python", "app.py"]
