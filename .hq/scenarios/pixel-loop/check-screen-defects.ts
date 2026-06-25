#!/usr/bin/env bun
/**
 * SCREEN-DEFECTS CHECKER — a static lint for the most frequent gen-screen-llm
 * defects found during the VenusX on-device review (2026-06-15). Run it on a
 * generated app's screens tree to catch these BEFORE manual review.
 *
 *   bun check-screen-defects.ts <screensDir>
 *   (default screensDir: ./src/screens)
 *
 * Each rule maps to a numbered defect in `.hq/scenarios/pixel-loop.DRAFT.md`.
 * This is the seed of a future `verify-screen-quality` pipeline. Heuristic +
 * conservative: it flags *candidates* to eyeball, not guaranteed bugs. Exit code
 * is non-zero if any HIGH-severity findings exist.
 */
import { readdirSync, statSync } from "node:fs";

const root = process.argv[2] || "src/screens";
function walk(d: string): string[] {
  let o: string[] = [];
  for (const e of readdirSync(d)) {
    const p = `${d}/${e}`;
    if (statSync(p).isDirectory()) o = o.concat(walk(p));
    else o.push(p);
  }
  return o;
}

type Finding = { rule: string; sev: "HIGH" | "MED" | "LOW"; file: string; line: number; msg: string };
const findings: Finding[] = [];
const add = (rule: string, sev: Finding["sev"], file: string, line: number, msg: string) =>
  findings.push({ rule, sev, file, line, msg });

const files = walk(root);
const tsx = files.filter((f) => f.endsWith(".tsx"));
const styleFiles = files.filter((f) => f.endsWith(".styles.ts"));

for (const f of tsx) {
  const src = await Bun.file(f).text();
  const lines = src.split("\n");

  // #0 undefined JSX component → runtime crash (SIGABRT "Element type is invalid").
  // A PascalCase tag used in JSX that is neither imported nor defined locally.
  {
    const used = new Set<string>();
    // Strip comments first so a tag NAMED in prose (e.g. "RN <Modal>") isn't
    // mistaken for a real, unimported JSX use.
    const codeOnly = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/[^\n]*/g, "$1");
    // Real JSX open tag: '<' preceded by a JSX delimiter (not an identifier char,
    // which would be a TS generic like useState<Set>) and the tag followed by
    // whitespace, '/', or '>'.
    for (const m of codeOnly.matchAll(/[\s(>{},&|?:]<([A-Z][A-Za-z0-9_]*)[\s/>]/g)) used.add(m[1]);
    const known = new Set<string>([
      "React", "Fragment",
      // TS built-in/utility types that can appear in generics, never JSX:
      "Set", "Map", "Array", "Promise", "Record", "Partial", "Required",
      "Readonly", "Pick", "Omit", "Exclude", "Extract", "ReturnType",
    ]);
    // imports: `import X`, `import {A, B}`, `import X, {A}` ...
    for (const m of src.matchAll(/import\s+([A-Za-z0-9_]+)\s*(?:,\s*\{([^}]*)\})?\s+from/g)) {
      if (m[1]) known.add(m[1]);
      if (m[2]) for (const n of m[2].split(",")) { const id = n.trim().split(/\s+as\s+/).pop()!.trim(); if (id) known.add(id); }
    }
    for (const m of src.matchAll(/import\s*\{([^}]*)\}\s*from/g))
      for (const n of m[1].split(",")) { const id = n.trim().split(/\s+as\s+/).pop()!.trim(); if (id) known.add(id); }
    // local defs: const X = / function X / class X
    for (const m of src.matchAll(/(?:const|let|function|class)\s+([A-Z][A-Za-z0-9_]*)\b/g)) known.add(m[1]);
    for (const name of used)
      if (!known.has(name) && !name.includes("."))
        add("#0 undefined-component", "HIGH", f, 1, `<${name}> is used in JSX but never imported or defined → runtime crash on render`);
  }

  // #3 non-interactive SELECTION options: pressables rendered in a .map() that
  // reference a selected/active style toggle but have NO onPress → option chips/
  // radios/checkboxes that show selected state but can't be toggled. (One-off CTA
  // buttons without onPress are NOT flagged — pixel screens are presentational and
  // get their handlers wired later.)
  const hasMappedPressable = /\.map\([\s\S]{0,400}?<(Pressable|TouchableOpacity)\b/.test(src);
  const hasSelectedStyle = /(Active|Selected|active\b|selected\b|optionSelected|pillActive|checkboxChecked)/.test(src);
  const handlers = (src.match(/onPress\s*[=:]/g) || []).length;
  if (hasMappedPressable && hasSelectedStyle && handlers === 0)
    add("#3 dead-options", "HIGH", f, 1, "mapped option chips with a selected/active style but 0 onPress — selection looks interactive but can't be toggled");
  lines.forEach((ln, i) => {
    const n = i + 1;

    // #11 COLORFUL emoji used as a UI icon (should be a real asset or omitted).
    // Only the pictographic emoji blocks — NOT monochrome UI glyphs like ✕ ✓ × → ‹ ›
    // which are legit (close buttons, back chevrons, checks).
    if (/<Text\b/.test(ln) && /[\u{1F300}-\u{1FAFF}\u{1F000}-\u{1F0FF}\u{2B00}-\u{2BFF}\u{2700}-\u{27BF}]/u.test(ln) && !/[✕✓✗×]/.test(ln))
      add("#11 emoji-icon", "MED", f, n, "colorful emoji used as an icon/glyph — use a real exported asset or omit (no 👁/🙈/🗑/emoji)");

    // #1 fake status-bar time mock
    if (/[>"']\s*\d{1,2}:\d{2}\s*[<"']/.test(ln) && /(9:41|09:41)/.test(ln))
      add("#1 fake-statusbar", "HIGH", f, n, "hard-coded status-bar time (OS renders the real one)");

    // #2 static-text input: <Text> whose content is a placeholder/example value.
    // Skip when the style name marks it as a heading/label/body (not an input).
    if (
      /<Text\b/.test(ln) &&
      /(Введите|Выберите|Например|Укажите|анна|anna|@email|@gmail|\+7\b|••)/.test(ln) &&
      !/style=\{styles\.\w*(title|Title|label|Label|hero|Hero|heading|Heading|description|Description|subtitle|Subtitle|caption|Caption)\w*\}/.test(ln)
    )
      add("#2 static-text-input", "MED", f, n, "placeholder-like text in <Text> — is this a dead input that should be <TextInput>?");

    // tab-bar baked into a screen (shared chrome should be in a navigator)
    if (/Тренды|Медкарта/.test(ln) && /<Text\b/.test(ln))
      add("chrome bottom-tab", "MED", f, n, "bottom-tab label inside a screen — tab bar should be a shared navigator, not per-screen");

    // #13 dead header theme/lang labels: an inert <Text>Light</Text>/RU/EN that
    // should be the shared <HeaderControls/> (which actually toggles theme + lang).
    if (/<Text\b[^>]*>\s*(Light|Dark|Светлая|Тёмная|RU|EN|РУ|АНГ)\s*<\/Text>/.test(ln) && !/HeaderControls/.test(src))
      add("#13 dead-header-control", "MED", f, n, "inert theme/language label in a header — use the shared <HeaderControls/> from '@/components' (it wires useTheme + setLanguage)");

  });

  // #14 navigational button left inert: a back/detail/CTA pressable but no
  // navigation wired anywhere on the screen (no useNavigation + navigate/goBack).
  {
    const navWords = /(Назад|Подробнее|Продолжить|Открыть|Перейти|Далее)/;
    const hasNavLabel = navWords.test(src);
    const usesNav = /useNavigation\(/.test(src) && /\.(navigate|goBack|reset|push)\(/.test(src);
    const hasPressable = /<(Pressable|TouchableOpacity)\b/.test(src);
    if (hasNavLabel && hasPressable && !usesNav)
      add("#14 inert-nav", "LOW", f, 1, "screen has back/detail/CTA labels and pressables but no useNavigation().navigate/goBack — navigational affordances may be inert");
  }
}

for (const f of styleFiles) {
  const src = await Bun.file(f).text();
  const lines = src.split("\n");

  // #15 theme-blind styles factory: a createStyles(...) that NEVER reads its
  // `colors` param (param renamed `_colors`, or zero `colors.` references) →
  // the screen hardcodes one palette and won't switch in dark mode. This is the
  // exact tell behind "this screen stays light" (VenusX auth screens, 2026-06-20).
  if (/createStyles\s*=\s*\(/.test(src)) {
    const ignoresParam = /createStyles\s*=\s*\(\s*_colors\b/.test(src);
    const readsColors = /\bcolors\.\w/.test(src);
    if (ignoresParam || !readsColors)
      add("#15 theme-blind-styles", "HIGH", f, 1, "createStyles ignores its `colors` param (renamed _colors or 0 `colors.` refs) — hardcoded palette, screen won't adapt to dark mode");
  }

  // crude block parse: name: { ... },
  const re = /(\b\w+)\s*:\s*\{([\s\S]*?)\n\s*\},/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) {
    const [, name, body] = m;
    const blockLine = src.slice(0, m.index).split("\n").length;

    const isInputText = /input|placeholder/i.test(name) && !/label/i.test(name) && /fontSize/.test(body) && !/flexDirection/.test(body);
    const isWhiteCard = /backgroundColor:\s*['"]#fff/i.test(body) && /borderRadius/.test(body);

    // #4 input text vertical-centering: lineHeight without textAlignVertical
    if (isInputText && /lineHeight/.test(body) && !/textAlignVertical/.test(body))
      add("#4 input-text-low", "MED", f, blockLine, `${name}: input text has lineHeight but no textAlignVertical:'center' (text sits low)`);

    // input box that won't let the TextInput fill width → tap-only-left
    if (/input|field|select/i.test(name) && /alignItems:\s*['"]flex-start['"]/.test(body) && /height/.test(body))
      add("#2b input-not-filled", "MED", f, blockLine, `${name}: alignItems:'flex-start' on a field box — child TextInput won't fill width (tap only on left / clipping)`);

    // #5c cards without a shadow
    if (isWhiteCard && /\bcard\b/i.test(name) && !/shadowColor|elevation/.test(body) && !/btn|button/i.test(name))
      add("#5c card-no-shadow", "LOW", f, blockLine, `${name}: white rounded card without a shadow (design shows elevation)`);

    // #7 input field wrapper WITH a shadow — inputs must be flat (border only);
    // only cards/info blocks get elevation (user rule, on-device review).
    const isInputWrap = /^input(wrapper|row|box|field|container)?$/i.test(name) || /^(field|select)(wrapper|box|row)$/i.test(name);
    if (isInputWrap && /shadowColor|elevation/.test(body))
      add("#7 input-has-shadow", "MED", f, blockLine, `${name}: input field has a shadow — input fields must be flat (border only); shadows are for cards/info blocks`);

    // #8b back TEXT slot in a narrow fixed width → '‹ Назад' wraps/truncates to
    // "‹ Наз…". Exclude icon back buttons (square circle: has height + borderRadius).
    if (/back/i.test(name)) {
      const w = body.match(/width:\s*(\d+)/);
      const isIcon = /borderRadius/.test(body) && /height/.test(body);
      if (w && Number(w[1]) < 80 && !isIcon)
        add("#8b back-narrow", "MED", f, blockLine, `${name}: width ${w[1]} is too narrow for '‹ Назад' (wraps/truncates) — size to content + flexShrink:0`);
    }

    // #10 off-center absolute overlay: position:absolute + center alignment but no
    // inset (top/left/right/bottom) → the overlay sits at the corner, not centered.
    if (/position:\s*['"]absolute['"]/.test(body) && /(alignItems|justifyContent):\s*['"]center['"]/.test(body) && !/\b(top|bottom|left|right|inset)\s*:/.test(body))
      add("#10 overlay-not-centered", "MED", f, blockLine, `${name}: absolute overlay centers its children but has no top/left/right/bottom — it won't center over its parent (add inset 0)`);

    // #5 literal Figma Y copied as a huge paddingTop
    const pt = body.match(/paddingTop:\s*(\d+)/);
    if (pt && Number(pt[1]) > 200)
      add("#5 literal-Y-padding", "MED", f, blockLine, `${name}: paddingTop ${pt[1]} looks like a literal Figma Y coordinate (huge top gap on tall devices)`);

    // #9 sibling button defaulted to flex:1 (full-bleed) — informational
    if (/next|primary|cta|continue/i.test(name) && /flex:\s*1/.test(body) && /height/.test(body))
      add("#9 button-flex1", "LOW", f, blockLine, `${name}: button has flex:1 (full-width) — confirm the design isn't a fixed/narrower width`);

    // #10 hard-coded DARK text colour → won't adapt to dark mode (use colors.text).
    // Near-black literal (#0../#1../#2../#3..). Match ONLY the `color:` property
    // (lowercase, boundary-prefixed) so shadowColor/borderColor/backgroundColor/
    // tintColor (camelCase capital C) don't false-positive.
    const col = body.match(/(?<![A-Za-z])color:\s*['"](#[0-3][0-9a-fA-F]{5})\b/);
    if (col)
      add("#10 hardcoded-dark-text", "MED", f, blockLine, `${name}: literal dark text colour ${col[1]} — use colors.text so it inverts in dark mode`);

    // #10b hard-coded white PAGE/screen background → stays white in dark mode.
    if (/(container|screen|page|root|wrapper|body)/i.test(name) && /backgroundColor:\s*['"]#f{3,6}\b/i.test(body) && !/borderRadius/.test(body))
      add("#10b hardcoded-page-bg", "MED", f, blockLine, `${name}: literal white page background — use colors.background (dark mode shows white otherwise)`);

    // #15 percentage-width grid cell → flex-wrap rounding drops the last column.
    // Calendars/day-grids must be explicit rows of flex:1 cells, not %-width + wrap.
    if (/width:\s*[`'"]?\s*(?:\$\{\s*100\s*\/\s*7\s*\}|14\.2)/.test(body))
      add("#15 pct-grid-cell", "MED", f, blockLine, `${name}: ~1/7 percentage width — with flexWrap, sub-pixel rounding drops the 7th column. Use explicit rows of flex:1 cells instead.`);
  }
}

// ── report ──
const order = { HIGH: 0, MED: 1, LOW: 2 } as const;
findings.sort((a, b) => order[a.sev] - order[b.sev] || a.file.localeCompare(b.file));
const byRule = new Map<string, number>();
for (const x of findings) byRule.set(x.rule, (byRule.get(x.rule) ?? 0) + 1);

console.log(`\nScreen-defects check — ${tsx.length} screens, ${findings.length} findings\n`);
for (const x of findings) {
  const short = x.file.replace(/.*\/src\/screens\//, "");
  console.log(`[${x.sev}] ${x.rule}  ${short}:${x.line}\n        ${x.msg}`);
}
console.log("\nBy rule:");
for (const [r, c] of [...byRule.entries()].sort((a, b) => b[1] - a[1])) console.log(`  ${c}\t${r}`);

const high = findings.filter((x) => x.sev === "HIGH").length;
console.log(`\n${high} HIGH-severity finding(s).`);
process.exit(high > 0 ? 1 : 0);
