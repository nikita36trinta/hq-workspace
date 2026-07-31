FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py auth.py plan.py plan_ai.py dish_photos.py dishes.json ./
COPY static ./static
ENV DATA_DIR=/app/data
EXPOSE 8790
# Кому верить в X-Forwarded-For — решает uvicorn, и решает ДО нашего кода: его
# ProxyHeaders-мидлварь переписывает адрес клиента раньше, чем приложение его
# увидит. Поэтому чинить подделку IP внутри app.py бесполезно, настройка живёт
# здесь. По умолчанию 127.0.0.1 — родное значение uvicorn, поведение не меняется;
# за обратным прокси задать NUTRI_TRUSTED_PROXIES с его адресом, иначе одно из
# двух: либо заголовок подделывает кто угодно, либо все посетители схлопываются
# в один IP и общий лимит режет живых людей.
ENV NUTRI_TRUSTED_PROXIES=127.0.0.1
CMD ["sh","-c","exec uvicorn app:app --host 0.0.0.0 --port 8790 --forwarded-allow-ips \"$NUTRI_TRUSTED_PROXIES\""]
