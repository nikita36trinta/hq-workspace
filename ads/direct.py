"""Переиспользуемый пайплайн создания рекламных кампаний в Яндекс.Директе (API v5).

Самодостаточный модуль (как analytics/mailer): один класс `Direct` + `Brief`. Создаёт кампанию
→ группы → ключи → объявления, запускает (модерация + resume), правит бюджеты, паузит,
отключает автотаргетинг, пробует формулировки на модерацию. Токен — из env YANDEX_DIRECT_TOKEN
(уровень аккаунта). НЕ зависит от гейта YANDEX_DIRECT_ENABLED (это флаг TourSniper, не Яндекса).

Использование:
    from direct import Direct, Brief, GEO_MSK_SPB
    d = Direct()                          # токен из env
    brief = Brief(name="ЧистаяСделка — Поиск ГОРЯЧИЙ", utm_campaign="sdelka_hot",
                  landing="https://chistasdelka.ru", weekly_rub=15000, geo=GEO_MSK_SPB,
                  groups=[{"name":"Проверка продавца",
                           "keywords":["проверить продавца квартиры","банкротство продавца квартиры"],
                           "title":"Проверка продавца квартиры онлайн","title2":"Долги, банкротство",
                           "text":"Отчёт по продавцу и объекту за 15 минут. Превью бесплатно. От 299 ₽"}])
    cid = d.create_campaign(brief)        # черновик (не тратит, на модерацию НЕ отправлен)
    d.disable_autotargeting(cid)          # для ПОИСКА (в РСЯ автотаргет оставляют)
    d.launch(cid)                         # модерация объявлений + resume → «Идут показы»

⚠️ Черновик не тратит и не показывается, пока не launch(). launch() шлёт на модерацию + resume.
   Реклама = деньги: запускать только с явного согласия человека и при пополненном счёте.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date

PROD = "https://api.direct.yandex.com/json/v5"
SANDBOX = "https://api-sandbox.direct.yandex.com/json/v5"
_MICRO = 1_000_000

# Гео (Yandex geo id): вся РФ / высокоплатёжеспособные центры (недвижимость, юруслуги)
REGION_RU = 225
GEO_RU = [225]
GEO_MSK_SPB = [213, 1, 2, 10174]  # Москва, Московская обл., СПб, Ленинградская обл.

# Типовые минус-слова: информационный/бесплатный/самостоятельный трафик = мусор
DEFAULT_NEGATIVES = ["бесплатно", "самостоятельно", "госуслуги", "образец", "скачать",
                     "форум", "отзывы", "вакансии", "самому", "инструкция", "как",
                     "пример", "своими руками", "заказать"]


class DirectError(Exception):
    pass


@dataclass
class Brief:
    """Бриф кампании. groups: [{name, keywords[list], title(≤56), title2(≤30), text(≤81)}]."""
    name: str
    utm_campaign: str                # уникальный utm → раздельная выручка в дашборде аналитики
    landing: str                     # базовый URL лендинга (utm допишутся)
    groups: list[dict]
    weekly_rub: int = 5000           # недельный лимит «Максимум кликов»
    geo: list[int] = field(default_factory=lambda: list(GEO_MSK_SPB))
    network: bool = False            # True → РСЯ (сети), Search off
    negatives: list[str] = field(default_factory=lambda: list(DEFAULT_NEGATIVES))
    start_date: str | None = None    # ISO; дефолт — сегодня (передавай явно, Date.now недоступен)

    def href(self) -> str:
        sep = "&" if "?" in self.landing else "?"
        return (f"{self.landing}{sep}utm_source=yandex&utm_medium=cpc"
                f"&utm_campaign={self.utm_campaign}")


class Direct:
    def __init__(self, token: str | None = None, sandbox: bool = False, client_login: str = ""):
        self.token = token or os.getenv("YANDEX_DIRECT_TOKEN", "").strip()
        if not self.token:
            raise DirectError("YANDEX_DIRECT_TOKEN не задан (env или аргумент)")
        self.base = SANDBOX if sandbox else PROD
        self.login = client_login or os.getenv("YANDEX_DIRECT_CLIENT_LOGIN", "").strip()

    def call(self, service: str, payload: dict, retries: int = 4) -> dict:
        """POST /json/v5/<service>. Ретрай на transient (1000) и 5xx. Возвращает весь ответ."""
        h = {"Authorization": f"Bearer {self.token}", "Accept-Language": "ru",
             "Content-Type": "application/json; charset=utf-8"}
        if self.login:
            h["Client-Login"] = self.login
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        r: dict = {}
        for _ in range(retries):
            req = urllib.request.Request(f"{self.base}/{service}", data=body, headers=h)
            try:
                r = json.load(urllib.request.urlopen(req, timeout=45))
            except urllib.error.HTTPError as e:  # noqa: PERF203
                r = {"_http": e.code, "_body": e.read().decode("utf-8", "ignore")[:400]}
            if r.get("error", {}).get("error_code") == 1000 or r.get("_http") in (500, 502, 503):
                time.sleep(3); continue
            return r
        return r

    def _add_ids(self, res: dict, what: str) -> list[int]:
        if res.get("_http"):
            raise DirectError(f"{what} HTTP {res['_http']}: {res.get('_body')}")
        if res.get("error"):
            raise DirectError(f"{what}: {res['error']}")
        out = []
        for it in (res.get("result", {}) or {}).get("AddResults", []):
            if it.get("Id") and not it.get("Errors"):
                out.append(it["Id"])
            else:
                raise DirectError(f"{what} add errors: {it.get('Errors')}")
        return out

    # ---------- чтение (read-only, денег не тратит) ----------
    def campaigns(self, ids: list[int] | None = None, fields: list[str] | None = None) -> list[dict]:
        sel = {"Ids": ids} if ids else {}
        f = fields or ["Id", "Name", "State", "Status", "StatusClarification"]
        r = self.call("campaigns", {"method": "get", "params": {"SelectionCriteria": sel, "FieldNames": f}})
        return (r.get("result", {}) or {}).get("Campaigns", [])

    def ads(self, campaign_ids: list[int]) -> list[dict]:
        r = self.call("ads", {"method": "get", "params": {
            "SelectionCriteria": {"CampaignIds": campaign_ids},
            "FieldNames": ["Id", "CampaignId", "AdGroupId", "State", "Status", "StatusClarification"]}})
        return (r.get("result", {}) or {}).get("Ads", [])

    def keywords(self, campaign_ids: list[int]) -> list[dict]:
        r = self.call("keywords", {"method": "get", "params": {
            "SelectionCriteria": {"CampaignIds": campaign_ids},
            "FieldNames": ["Id", "Keyword", "State", "Status", "ServingStatus"]}})
        return (r.get("result", {}) or {}).get("Keywords", [])

    # ---------- создание (черновик — НЕ тратит до launch) ----------
    def _strategy(self, weekly_rub: int, network: bool) -> dict:
        wmc = {"BiddingStrategyType": "WB_MAXIMUM_CLICKS",
               "WbMaximumClicks": {"WeeklySpendLimit": int(weekly_rub) * _MICRO}}
        off = {"BiddingStrategyType": "SERVING_OFF"}
        return {"Search": (off if network else wmc), "Network": (wmc if network else off)}

    def create_campaign(self, brief: Brief) -> int:
        """Создать кампанию→группы→ключи→объявления. Возвращает campaign_id. Черновик."""
        camp = {"Name": brief.name[:255],
                "StartDate": brief.start_date or date.today().isoformat(),
                "TextCampaign": {"BiddingStrategy": self._strategy(brief.weekly_rub, brief.network)},
                "NegativeKeywords": {"Items": [k for k in brief.negatives if k]}}
        cid = self._add_ids(self.call("campaigns", {"method": "add", "params": {"Campaigns": [camp]}}), "campaign")[0]
        for g in brief.groups:
            agid = self._add_ids(self.call("adgroups", {"method": "add", "params": {"AdGroups": [
                {"Name": g["name"][:255], "CampaignId": cid, "RegionIds": brief.geo}]}}), "adgroup")[0]
            self._add_ids(self.call("keywords", {"method": "add", "params": {"Keywords": [
                {"AdGroupId": agid, "Keyword": k[:4096]} for k in g["keywords"] if k]}}), "keywords")
            ta = {"Title": g["title"][:56], "Text": g["text"][:81], "Href": brief.href(), "Mobile": "NO"}
            if g.get("title2"):
                ta["Title2"] = g["title2"][:30]
            self._add_ids(self.call("ads", {"method": "add", "params": {"Ads": [
                {"AdGroupId": agid, "TextAd": ta}]}}), "ad")
        return cid

    # ---------- операции ----------
    def launch(self, campaign_id: int) -> dict:
        """Отправить объявления на модерацию + активировать кампанию. После этого начнёт
        показываться (и ТРАТИТЬ) по мере одобрения. Только с согласия человека!"""
        adids = [a["Id"] for a in self.ads([campaign_id])]
        if adids:
            self.call("ads", {"method": "moderate", "params": {"SelectionCriteria": {"Ids": adids}}})
        self.call("campaigns", {"method": "resume", "params": {"SelectionCriteria": {"Ids": [campaign_id]}}})
        return {"moderated_ads": len(adids)}

    def suspend(self, campaign_id: int) -> None:
        self.call("campaigns", {"method": "suspend", "params": {"SelectionCriteria": {"Ids": [campaign_id]}}})

    def delete(self, campaign_id: int) -> None:
        self.call("campaigns", {"method": "delete", "params": {"SelectionCriteria": {"Ids": [campaign_id]}}})

    def set_weekly_budget(self, campaign_id: int, rub: int, network: bool = False) -> None:
        self.call("campaigns", {"method": "update", "params": {"Campaigns": [
            {"Id": campaign_id, "TextCampaign": {"BiddingStrategy": self._strategy(rub, network)}}]}})

    # Категории автотаргетинга. «Целевые» — это запросы про то же, что в группе;
    # остальные четыре и есть та околотематика, ради которой автотаргетинг
    # выключали: на прошлом проекте через него ушло 96% расхода.
    AUTOTARGETING_EXACT_ONLY = [{"Category": "EXACT", "Value": "YES"},
                                {"Category": "ALTERNATIVE", "Value": "NO"},
                                {"Category": "COMPETITOR", "Value": "NO"},
                                {"Category": "BROADER", "Value": "NO"},
                                {"Category": "ACCESSORY", "Value": "NO"}]

    def disable_autotargeting(self, campaign_id: int) -> int:
        """Сузить автотаргетинг поисковых групп до целевых запросов.

        Раньше здесь стоял keywords.suspend, и это БОЛЬШЕ НЕ РАБОТАЕТ: Директ
        отвечает 8305 «Автотаргетинг не может быть остановлен» (проверено
        2026-08-03). Хуже того, ответ приходил в поэлементных Errors, метод их не
        читал и возвращал число «отключённых» — то есть рапортовал об успехе,
        когда не сделал ничего. Теперь вместо остановки настраиваем категории:
        оставляем EXACT, снимаем ALTERNATIVE/COMPETITOR/BROADER/ACCESSORY.

        Ещё одна засада: строки автотаргетинга Яндекс заводит с задержкой после
        создания группы, поэтому сразу после create_campaign их может не быть —
        вызывать имеет смысл ПОВТОРНО, а не один раз в потоке создания.

        Возвращает число реально настроенных групп. В РСЯ вызывать не надо:
        сети на автотаргетинге и работают.
        """
        auto = [k["Id"] for k in self.keywords([campaign_id])
                if "autotargeting" in (k.get("Keyword") or "").lower()]
        if not auto:
            return 0
        res = self.call("keywords", {"method": "update", "params": {"Keywords": [
            {"Id": i, "AutotargetingCategories": self.AUTOTARGETING_EXACT_ONLY} for i in auto]}})
        items = (res.get("result") or {}).get("UpdateResults") or []
        bad = [u for u in items if u.get("Errors")]
        if bad:
            raise DirectError(f"автотаргетинг не настроен у {len(bad)} групп: {bad[0].get('Errors')}")
        return len(auto)

    # ---------- проба формулировок на модерацию ----------
    def moderation_status(self, campaign_id: int) -> list[dict]:
        """Статусы объявлений: ACCEPTED (прошло) / REJECTED (отклонено) / MODERATION (ждёт).
        Для probe формулировок — увидеть, что пропустили, что режет закон."""
        out = []
        for a in self.ads([campaign_id]):
            st, state = a.get("Status"), a.get("State")
            verdict = ("passed" if st == "ACCEPTED" and state == "ON"
                       else "rejected" if st == "REJECTED"
                       else "moderation" if st == "MODERATION" else f"{state}/{st}")
            out.append({"ad_id": a["Id"], "adgroup_id": a.get("AdGroupId"),
                        "verdict": verdict, "clarification": a.get("StatusClarification", "")})
        return out
