FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py plan.py plan_ai.py dish_photos.py dishes.json ./
COPY static ./static
ENV DATA_DIR=/app/data
EXPOSE 8790
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8790"]
