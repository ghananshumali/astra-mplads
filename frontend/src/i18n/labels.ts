/** Translated names for things the backend identifies by key.
 *
 * The backend sends rule ids, agent names, stages and action ids with English
 * text alongside. The name is looked up by key; the English text is the
 * fallback for anything the translation files do not know yet.
 */
import { isAgentKey, isStage } from "../lib/format";
import type { I18n, MessageKey } from "./context";

export function agentText(i18n: I18n, agent: string, part: "label" | "short" | "role"): string {
  return isAgentKey(agent) ? i18n.t(`agent.${agent}.${part}`) : agent;
}

export function ruleTitle(i18n: I18n, ruleId: string, fallback: string): string {
  return i18n.tOr(`rule.${ruleId}`, fallback);
}

export function stageLabel(i18n: I18n, stage: string): string {
  return isStage(stage) ? i18n.t(`stage.${stage}`) : stage;
}

export function actionText(
  i18n: I18n,
  actionId: string,
  part: "label" | "detail",
  fallback: string,
): string {
  return i18n.tOr(`action.${actionId}.${part}`, fallback);
}

export function houseName(i18n: I18n, house: string): string {
  return house === "RS" ? i18n.t("house.RS") : i18n.t("house.LS");
}

/** The entity-resolution agent's duplication pattern, as it records it. */
const DUP_MODES: Record<string, MessageKey> = {
  "identical recommendation double-entry": "dup.mode.identical",
  "same-recommender near-duplicate": "dup.mode.sameRecommender",
  "cross-MP overlap (constituency-boundary duplication)": "dup.mode.crossMp",
  "the portal lists both the original recommendation and a sanctioned record": "dup.mode.portalPair",
};

export function dupMode(i18n: I18n, mode: string): string {
  const key = DUP_MODES[mode];
  return key ? i18n.t(key) : mode;
}
