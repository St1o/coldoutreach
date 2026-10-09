FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY egrul_inn_search.py app.py index.html ./

# На хостинге порт приходит в переменной PORT; перед сайтом стоит один прокси хостинга.
ENV HOST=0.0.0.0 TRUSTED_PROXIES=1 PYTHONUNBUFFERED=1
EXPOSE 8000
USER nobody
CMD ["python", "app.py"]
