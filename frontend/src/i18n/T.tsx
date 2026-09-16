/** A translated sentence with React elements in it.
 *
 *   <T k="overview.noCases" values={{ scope: <b>{scopeLabel}</b> }} />
 *
 * Each `{name}` in the template is replaced by `values[name]`, so the word
 * order stays the translator's, not the code's.
 */
import { Fragment } from "react";
import type { ReactNode } from "react";

import { useI18n } from "./context";
import type { MessageKey } from "./context";

export function T({ k, values }: { k: MessageKey; values?: Record<string, ReactNode> }) {
  const { template } = useI18n();
  const parts = template(k).split(/(\{\w+\})/g);
  return (
    <>
      {parts.map((part, i) => {
        const name = /^\{(\w+)\}$/.exec(part)?.[1];
        const node = name !== undefined && values && name in values ? values[name] : part;
        return <Fragment key={i}>{node}</Fragment>;
      })}
    </>
  );
}
