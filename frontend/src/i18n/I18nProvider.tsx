/** Holds the chosen language, loads its text, and keeps <html lang dir> in step.
 *
 * The site opens in English on every first visit, whatever the browser's
 * language; a language the user picks is remembered on this device. Other
 * languages' text is loaded only when chosen, so English users never download it.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { I18nContext, createI18n, englishMessages } from "./context";
import type { MessageTable } from "./context";
import { DEFAULT_LANGUAGE, isLanguageCode, languageOf } from "./languages";
import type { LanguageCode } from "./languages";

const STORAGE = "astra.language";

const LOADERS = import.meta.glob<MessageTable>(["./locales/*.json", "!./locales/en.json"], {
  import: "default",
});

interface State {
  /** The language on screen. */
  code: LanguageCode;
  messages: MessageTable;
  /** The language last chosen; differs from `code` while its text loads. */
  wanted: LanguageCode;
}

function savedLanguage(): LanguageCode {
  try {
    const saved = localStorage.getItem(STORAGE);
    return isLanguageCode(saved) ? saved : DEFAULT_LANGUAGE;
  } catch {
    return DEFAULT_LANGUAGE; // storage unavailable: stay in English
  }
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>(() => ({
    code: DEFAULT_LANGUAGE,
    messages: englishMessages,
    wanted: savedLanguage(),
  }));
  const { code, wanted } = state;

  useEffect(() => {
    if (wanted === code || wanted === DEFAULT_LANGUAGE) return;
    const loader = LOADERS[`./locales/${wanted}.json`];
    if (!loader) return;
    let stale = false;
    loader().then(
      (messages) => {
        // a later choice made while this one was loading wins
        if (!stale) setState((s) => (s.wanted === wanted ? { code: wanted, messages, wanted } : s));
      },
      () => {
        // keep the language on screen if the file cannot be loaded
        if (!stale) setState((s) => (s.wanted === wanted ? { ...s, wanted: s.code } : s));
      },
    );
    return () => {
      stale = true;
    };
  }, [wanted, code]);

  const setLanguage = useCallback((next: LanguageCode) => {
    try {
      localStorage.setItem(STORAGE, next);
    } catch {
      /* not fatal: the choice lasts for this visit */
    }
    setState((s) =>
      next === DEFAULT_LANGUAGE
        ? { code: next, messages: englishMessages, wanted: next }
        : { ...s, wanted: next },
    );
  }, []);

  const value = useMemo(
    () => createI18n(languageOf(state.code), state.messages, setLanguage),
    [state.code, state.messages, setLanguage],
  );

  useEffect(() => {
    const root = document.documentElement;
    root.lang = value.language.code;
    root.dir = value.language.dir;
    document.title = value.t("app.title");
  }, [value]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}
