FROM python:3.12-slim
# tidszonedata til dansk tid (slim-billedet har det ikke)
RUN pip install --no-cache-dir tzdata
ENV PYTHONUNBUFFERED=1 ELOPLADNING_CONFIG=/data/config.json
WORKDIR /app
COPY elopladning ./elopladning
COPY static ./static
COPY config.example.json ./
RUN useradd --system --uid 1000 elopladning && mkdir /data && chown elopladning /data
USER elopladning
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "elopladning"]
