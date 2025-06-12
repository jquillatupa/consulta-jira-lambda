# 1) Base Lambda Python 3.10
FROM public.ecr.aws/lambda/python:3.10

# 2) Copiamos todo el código de la función
COPY hello_world/ /var/task

# 3) Nos movemos allí y instalamos dependencias
WORKDIR /var/task
RUN pip install --no-cache-dir -r requirements.txt -t .
RUN python -m spacy download es_core_news_sm

# 4) Handler
CMD ["app.lambda_handler"]
