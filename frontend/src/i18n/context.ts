/** Translation lookup and language-aware formatting.
 *
 * English (`locales/en.json`) is the reference text and defines every key.
 * Another language's file may lack a key; the English text is shown instead,
 * so a missing translation can never surface as a raw key.
 *
 * Numbers keep the international form of Indian numerals in every language
 * (Indian digit grouping, Latin digits), which is what official figures use.
 * Month names follow the chosen language.
 */
import { createContext, useContext } from "react";

import en from "./locales/en.json";
import type { Language, LanguageCode } from "./languages";

export type Messages = typeof en;
export type MessageKey = keyof Messages;
/** Keys that come as a `_one` / `_other` pair, named without the suffix. */
export type PluralKey = {
  [K in MessageKey]: K extends `${infer Base}_other` ? Base : never;
}[MessageKey];
export type Vars = Record<string, string | number>;
export type MessageTable = Partial<Record<string, string>>;

export interface Formatters {
  /** "₹2.45 crore", "₹3.10 lakh", "₹48,000". */
  rupees: (value: number | null | undefined) => string;
  /** "13 Sept 2026" in the chosen language. */
  date: (iso: string | null | undefined) => string;
  /** "13 Sept, 11:42 am" in the chosen language. */
  clock: (iso: string | null | undefined) => string;
  /** "just now", "40 s ago", "3 min ago", "2 h ago", "4 d ago". */
  ago: (iso: string | null | undefined, now?: number) => string;
}

export interface I18n {
  language: Language;
  setLanguage: (code: LanguageCode) => void;
  t: (key: MessageKey, vars?: Vars) => string;
  /** Plural-aware: picks `key_one` or `key_other` by the language's rules. */
  tn: (key: PluralKey, count: number, vars?: Vars) => string;
  /** For keys built from data (a rule id, a status): falls back to the given text. */
  tOr: (key: string, fallback: string, vars?: Vars) => string;
  /** The raw template, for rich text with React elements (see `T`). */
  template: (key: MessageKey) => string;
  fmt: Formatters;
}

const ENGLISH = en as MessageTable;

export function interpolate(template: string, vars?: Vars): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in vars ? String(vars[name]) : whole,
  );
}

function pluralRules(locale: string): Intl.PluralRules {
  try {
    return new Intl.PluralRules(locale);
  } catch {
    return new Intl.PluralRules("en-IN");
  }
}

export function createI18n(
  language: Language,
  messages: MessageTable,
  setLanguage: (code: LanguageCode) => void,
): I18n {
  const lookup = (key: string): string | undefined => messages[key] ?? ENGLISH[key];
  const t = (key: MessageKey, vars?: Vars) => interpolate(lookup(key) ?? key, vars);
  const tOr = (key: string, fallback: string, vars?: Vars) =>
    interpolate(lookup(key) ?? fallback, vars);
  const rules = pluralRules(language.locale);
  const tn = (key: PluralKey, count: number, vars?: Vars) => {
    const form = rules.select(count) === "one" ? "one" : "other";
    const text = lookup(`${key}_${form}`) ?? lookup(`${key}_other`) ?? key;
    return interpolate(text, { count: count.toLocaleString("en-IN"), ...vars });
  };

  const dateLocale = `${language.locale}-u-nu-latn`;
  const parse = (iso: string | null | undefined): Date | null => {
    if (!iso) return null;
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : d;
  };

  const fmt: Formatters = {
    rupees(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "—";
      const v = Number(value);
      if (Math.abs(v) >= 1e7) return t("money.crore", { n: (v / 1e7).toFixed(2) });
      if (Math.abs(v) >= 1e5) return t("money.lakh", { n: (v / 1e5).toFixed(2) });
      return `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
    },
    date(iso) {
      const d = parse(iso);
      if (!d) return iso ? String(iso) : "—";
      return d.toLocaleDateString(dateLocale, { day: "2-digit", month: "short", year: "numeric" });
    },
    clock(iso) {
      const d = parse(iso);
      if (!d) return "—";
      return d.toLocaleString(dateLocale, {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    },
    ago(iso, now = Date.now()) {
      const d = parse(iso);
      if (!d) return "—";
      const s = Math.max(0, Math.floor((now - d.getTime()) / 1000));
      if (s < 10) return t("time.justNow");
      if (s < 60) return t("time.secondsAgo", { n: Math.floor(s / 10) * 10 });
      const m = Math.floor(s / 60);
      if (m < 60) return t("time.minutesAgo", { n: m });
      const h = Math.floor(m / 60);
      if (h < 24) return t("time.hoursAgo", { n: h });
      return t("time.daysAgo", { n: Math.floor(h / 24) });
    },
  };

  return { language, setLanguage, t, tn, tOr, template: (key) => lookup(key) ?? key, fmt };
}

export const I18nContext = createContext<I18n | null>(null);

export function useI18n(): I18n {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n must be used inside I18nProvider");
  return value;
}

export const englishMessages: MessageTable = ENGLISH;
