FROM python:3.11-slim

WORKDIR /srv/app
COPY . /srv/app

RUN apt-get update && apt-get install -y build-essential gcc libpq-dev \
    && pip install --upgrade pip \
    && pip install -r requirements.txt \
    && apt-get remove -y build-essential gcc libpq-dev \
    && apt-get autoremove -y && apt-get clean

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/srv/app

CMD ["python", "-u", "app/main_bot.py"]
