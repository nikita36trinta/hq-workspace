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

**`BASE_URL=https://mynutriplan.ru npm test` WRITES TO PRODUCTION.** The journey
test submits an email, so it creates real leads and bumps the funnel counters —
one run added 2 leads and ~9 quiz hits, which had to be cleaned out of
`data/leads.jsonl` and `data/counters.json` by hand. There is no `notrack` mode
on this app yet (ЧистаяСделка has one, `/?notrack=1`). Until there is, run
against prod only deliberately, and expect to clean up after.

Payments are out of scope by construction: without ЮKassa keys the app answers
`503 payments not configured`, so the suite covers everything up to the paywall
and can never spend money.
