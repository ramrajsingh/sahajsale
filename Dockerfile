FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shop ./shop
COPY sidekick ./sidekick
EXPOSE 8000
# ROLE=sidekick runs the SahajSale dashboard + brain; any other ROLE runs a demo shop service
CMD ["sh", "-c", "if [ \"$ROLE\" = \"sidekick\" ]; then exec uvicorn sidekick.app:app --host 0.0.0.0 --port 8000; else exec uvicorn shop.app:app --host 0.0.0.0 --port 8000; fi"]
