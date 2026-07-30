# NutriPlan E2E

Playwright suite over the funnel that earns money: landing → quiz → norm →
paywall. Mobile first (390×844, iPhone 13 metrics), the same journey repeated
at 1440×900.

```sh
cd e2e
npm install
npx playwright install chromium   # once
npm test                          # builds the image, runs it on :8791, tests it
npm run report                    # html report after a failure
```

## What it covers

- the whole quiz walked step by step, without pinning the step order — the
  helper answers whatever the current step asks, so copy and A/B changes don't
  produce false failures
- the norm stays locked until the email is given (that gate is the reason the
  email is asked for at all)
- consent wording next to every place personal data is collected, with live
  links to the documents
- one spec per defect found by hand, each naming its incident: the silent dead
  end on the body-metrics step, the 99px phantom band above the plan screen,
  the missing consent line on `/login`
- the legal set (`/privacy`, `/consent`, `/offer`) is served and is a document,
  not a stub

## Two rules that came out of building it

**The suite runs its own container on port 8791, never 8790.** A hand-started
`uvicorn` on the app's default port silently absorbed a whole run once, and the
suite "passed" against code that predated the fixes it was testing.
`reuseExistingServer` is off for the same reason: test the image built from
this tree, not whatever happens to be listening.

**Every run is in notrack mode, automatically.** `specs/_fixtures.ts` puts the
`np_notrack` cookie on the context before the first navigation, so counters do
not move, leads are not written and no email goes out. This is not a
convenience — the first prod run, before the mode existed, added 2 live leads
and ~9 quiz hits that had to be dug out of `data/leads.jsonl` and
`data/counters.json` by hand. Relying on a person remembering a flag was not
going to hold.

`BASE_URL=https://mynutriplan.ru npm test` therefore runs against production
safely — verified: leads 0, `quiz_slim` unchanged after a full pass. The two
specs that observe goal calls are skipped there: the real Metrika snippet
overwrites the `window.npGoal` stub, so the contract can only be watched where
no counter is installed.

Payments are out of scope by construction: without ЮKassa keys the app answers
`503 payments not configured`, so the suite covers everything up to the paywall
and can never spend money.

## Правки — локально, деплой — один раз

Не гоняй итерации через прод. Поднимай площадку рядом:

```
./dev.sh          # http://localhost:8790, исходники примонтированы, uvicorn --reload
./dev.sh stop
```

Правка `.py` или `static/*` видна по F5: ни `docker cp`, ни перезапуска. Данные
пишутся в `.devdata/` (в .gitignore), поэтому боевые лиды и счётчики не
трогаются.

Прогон против локальной площадки: `BASE_URL=http://localhost:8790 npx playwright test`.
Без `BASE_URL` набор сам собирает образ и поднимает свой контейнер на 8791 —
это и есть режим для CI.

Прод трогаем ОДИН раз, когда правка готова и тесты зелёные. Раньше я деплоил
каждую итерацию: 12 секунд на перезапуск, и каждое промежуточное состояние
висело на живом сайте.
