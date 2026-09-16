/** The languages the site can be read in, listed as they appear in the menu:
 *  English first (the default and the reference text), then the others, each
 *  shown in its own script. Adding a language means adding it here and adding
 *  its file under ./locales; tests/test_translations.py checks the file. */

export type LanguageCode = "en" | "hi";

export interface Language {
  code: LanguageCode;
  /** The language's name in its own script, as the menu shows it. */
  native: string;
  english: string;
  /** BCP 47 tag for dates and plural rules. */
  locale: string;
  dir: "ltr" | "rtl";
}

export const LANGUAGES: Language[] = [
  { code: "en", native: "English", english: "English", locale: "en-IN", dir: "ltr" },
  { code: "hi", native: "हिन्दी", english: "Hindi", locale: "hi-IN", dir: "ltr" },
];

/** The site always opens in English; another language only when chosen. */
export const DEFAULT_LANGUAGE: LanguageCode = "en";

export function languageOf(code: string | null | undefined): Language {
  return LANGUAGES.find((l) => l.code === code) ?? LANGUAGES[0];
}

export function isLanguageCode(code: unknown): code is LanguageCode {
  return typeof code === "string" && LANGUAGES.some((l) => l.code === code);
}
