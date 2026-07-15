# mailer — транзакционные email для проектов (Unisender Go + Reg.ru)

Переиспользуемый способ рассылки писем (ссылки на отчёты, уведомления) с доставкой в
РФ-ящики (mail.ru, yandex). Один раз завёл провайдера — дальше новый проект = одна команда.

## Креды (в общем hq-env `.hq/pipelines/.env`, gitignored)
- `UNISENDER_GO_API_KEY` — Unisender Go (отправка + управление доменами). Endpoint `go2.unisender.ru`.
- `REGRU_EMAIL` / `REGRU_PASSWORD` — Reg.ru API (DNS). **Работает только с whitelisted IP** —
  сейчас разрешён `141.105.68.17`, поэтому Reg.ru-вызовы гоняются на этом хосте (см. provision.sh).

## Полный email-сервис одной командой (отправка + приём)
```
./email_service.sh <domain> [server_ip]
```
Оркестратор делает end-to-end и идемпотентно: (1) sending-домен DKIM/SPF/DMARC/verification,
(2) inbound DNS (A mail/inbox + MX), (3) деплой/дополнение самописного приёмника на server_ip
(мультидоменный SMTP :25 + мини-админка), добавляет домен в whitelist приёмника.
**Разовые предпосылки (уровень аккаунта, не в скрипте):** Unisender Go аккаунт + tracking-домен,
назначенный дефолтным backend (иначе отправка = error 229); Reg.ru IP-whitelist для whitelist-хоста.
Отдельные шаги ниже — если нужно только что-то одно.

## Провижн только домена-отправителя (одна команда)
```
./provision.sh <domain>
```
Что делает автоматически:
1. Unisender `domain/get-dns-records` → регистрирует домен + отдаёт verification/DKIM/DMARC.
2. Reg.ru API → создаёт записи в зоне:
   - `@` TXT `unisender-go-validate-hash=…` (verification)
   - `us._domainkey` TXT `v=DKIM1; k=rsa; p=…` (DKIM)
   - `_dmarc` CNAME `<domain>.dmarc.unisender.ru.` (DMARC)
   - `@` TXT `v=spf1 include:spf.unisender.ru ~all` (SPF — если ещё нет)
3. Валидирует у Unisender (verification + DKIM). Идемпотентно (повторный запуск не дублит).

DNS пропагируется минуты–часы. Повторная валидация:
```
VALIDATE_ONLY=1 ./provision.sh <domain>
```

## Отправка письма (в любом проекте)
```python
from mailer import send_email          # UNISENDER_GO_API_KEY в env проекта
send_email(to="user@ya.ru", subject="Ваш отчёт готов",
           html="<p>…<a href='https://chistasdelka.ru/report/<id>/view'>Открыть отчёт</a></p>",
           from_email="noreply@chistasdelka.ru", from_name="ЧистаяСделка")
```

## Новый проект — чеклист
1. `./provision.sh mail.<project>.ru` (или корневой домен) → домен готов.
2. В env проекта: `UNISENDER_GO_API_KEY` + `EMAIL_FROM=noreply@<project>.ru`.
3. Импортировать `mailer.send_email` где нужно (после оплаты, уведомления и т.п.).

## Tracking-домен (домен ссылок) — обязателен для отправки, разово на аккаунт
Отправка даёт `error 229 "Custom backend domain or tracking domain required"`, пока у аккаунта нет
своего backend/tracking-домена (дефолтный shared `unieml.ru` тут в статусе «Запрещён»). Флаги
`track_links=0` и т.п. НЕ помогают, API-метода создания tracking-домена НЕТ → добавляется **в UI один раз**,
и **один домен обслуживает все проекты** (если сделать дефолтным backend), а не по одному на бренд.

Порядок (см. подробности в памяти `reference-mailer-pipeline`):
1. UI `go2.unisender.ru/ru`: закрыть онбординг-визард (анкета «Продолжить» + «Сохранить и отправить письмо
   себе» — сам тест упадёт 229, это ок), иначе Настройки не открываются.
2. Настройки → **Домены ссылок** → «Добавить домен ссылок» → напр. `track.chistasdelka.ru`.
3. Unisender просит **NS-делегирование** субдомена на `uns1/uns2/uns3.unisender.com` (лучше CNAME для
   доставки в mail.ru/yandex). Прописать через Reg.ru:
   ```
   REGRU_EMAIL=… REGRU_PASSWORD=… DOMAIN=chistasdelka.ru SUBDOMAIN=track python3 provision_tracking.py
   ```
   (гнать на whitelisted-хосте `141.105.68.17`, как provision.sh; идемпотентно).
4. Дождаться пропагации (Reg.ru публикует зону не мгновенно) → домен активируется → поставить его
   **дефолтным backend** в списке → 229 уходит.

DKIM/SPF/DMARC самого sending-домена — 100% через `./provision.sh <domain>`.

## Гочи
- Reg.ru API — только с whitelisted IP (`Настройки API → Диапазоны IP`). Добавить IP нового
  раннера, если сменится. Сейчас `141.105.68.17`.
- SPF: если у домена уже есть `v=spf1`, скрипт его НЕ трогает — доклей `include:spf.unisender.ru` вручную.
- DKIM-селектор Unisender Go = **`us`** (`us._domainkey`), не `mail`.
