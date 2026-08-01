FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py auth.py plan.py plan_ai.py dish_photos.py dishes.json ./
COPY static ./static
ENV DATA_DIR=/app/data
EXPOSE 8790
# --no-proxy-headers обязателен, и вот почему. ProxyHeaders-мидлварь uvicorn
# берёт из X-Forwarded-For ЛЕВЫЙ элемент цепочки, а левый элемент подставляет
# клиент: nginx с $proxy_add_x_forwarded_for дописывает настоящий адрес СПРАВА,
# к тому, что прислали. То есть при включённой мидлвари любой посетитель
# назначает себе адрес одной строкой заголовка и обходит все наши лимиты.
# Замерено: с включённой мидлварью в журнал ложится подставной 9.9.9.9.
# Отключаем её и разбираем заголовок сами (_client_ip в app.py) — там берётся
# ПРАВЫЙ элемент и только от доверенного пира из NUTRI_TRUSTED_PROXIES.
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8790","--no-proxy-headers"]
