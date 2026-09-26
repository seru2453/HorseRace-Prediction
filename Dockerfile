FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir ".[local]"
COPY . .
EXPOSE 8000
CMD ["python", "apps/api/flask_app.py"]
