FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Daten (Datenbank, Uploads …) gehören nach /data – das ist ein Docker-Volume
ENV DATA_DIR=/data
VOLUME /data
EXPOSE 5000
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5000", "--access-logfile", "-", "app:app"]
