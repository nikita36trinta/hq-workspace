FROM python:3.12-slim
RUN pip install --no-cache-dir aiosmtpd==1.4.6 fastapi==0.115.6 "uvicorn[standard]==0.34.0"
WORKDIR /app
COPY inbound_smtp.py inbox_admin.py /app/
