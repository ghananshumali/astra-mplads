/** Reusable ASTRA UI primitives. */
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Info,
  Loader2,
  SearchX,
  XCircle,
} from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";

import type { ReviewStatus } from "../../api/types";
import { useI18n } from "../../i18n/context";
import { RISK_META, STATUS_META, riskLevel } from "../../lib/format";
import "./ui.css";

/* --------------------------------------------------------------- Card */
export function Card({
  title,
  subtitle,
  actions,
  children,
  tight,
  className = "",
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  tight?: boolean;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <div className="grow">
            <div className="card-title">{title}</div>
            {subtitle && <div className="card-sub">{subtitle}</div>}
          </div>
          {actions}
        </header>
      )}
      <div className={`card-body${tight ? " tight" : ""}`}>{children}</div>
    </section>
  );
}

/* --------------------------------------------------------------- Chip */
export function Chip({
  children,
  color,
  bg,
  border,
  dot,
}: {
  children: ReactNode;
  color?: string;
  bg?: string;
  border?: string;
  dot?: boolean;
}) {
  return (
    <span
      className="chip"
      style={{
        color: color ?? "var(--text-2)",
        background: bg ?? "var(--surface-3)",
        borderColor: border ?? "transparent",
      }}
    >
      {dot && <i className="chip-dot" />}
      {children}
    </span>
  );
}

/* ---------------------------------------------------------- RiskBadge */
export function RiskBadge({
  score,
  showBar = false,
  size = "md",
}: {
  score: number;
  showBar?: boolean;
  size?: "sm" | "md";
}) {
  const meta = RISK_META[riskLevel(score)];
  return (
    <div className="row gap-2">
      <span
        className="chip"
        style={{
          color: meta.color,
          background: meta.bg,
          borderColor: meta.border,
        }}
      >
        <span className="risk-score" style={{ fontSize: size === "sm" ? 11 : 12 }}>
          {score.toFixed(0)}
          <span className="den">/100</span>
        </span>
      </span>
      {showBar && (
        <span className="risk-bar" aria-hidden>
          <span
            style={{ width: `${Math.min(score, 100)}%`, background: meta.color }}
          />
        </span>
      )}
    </div>
  );
}

export function RiskLabel({ score }: { score: number }) {
  const { t } = useI18n();
  const level = riskLevel(score);
  const meta = RISK_META[level];
  return (
    <Chip color={meta.color} bg={meta.bg} border={meta.border} dot>
      {t(`risk.${level}`)}
    </Chip>
  );
}

/* -------------------------------------------------------- StatusChip */
export function StatusChip({ status }: { status: ReviewStatus }) {
  const { t } = useI18n();
  const known = status in STATUS_META ? status : "pending";
  const m = STATUS_META[known];
  return (
    <Chip color={m.color} bg={m.bg} dot>
      {t(`status.${known}.short`)}
    </Chip>
  );
}

/* ------------------------------------------------------------ Banner */
export function Banner({
  tone = "info",
  icon,
  children,
}: {
  tone?: "info" | "warn" | "danger" | "success" | "neutral";
  icon?: ReactNode;
  children: ReactNode;
}) {
  const fallback = {
    info: <Info size={15} />,
    warn: <AlertTriangle size={15} />,
    danger: <XCircle size={15} />,
    success: <CheckCircle2 size={15} />,
    neutral: <Info size={15} />,
  }[tone];
  return (
    <div className={`banner banner-${tone}`}>
      {icon ?? fallback}
      <div>{children}</div>
    </div>
  );
}

/* ------------------------------------------------------------- Empty */
export function Empty({
  title,
  hint,
  icon,
  action,
}: {
  title: string;
  hint?: string;
  icon?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty fade-in">
      <div className="empty-icon">{icon ?? <SearchX size={20} />}</div>
      <div className="empty-title">{title}</div>
      {hint && <div className="text-sm" style={{ maxWidth: 380 }}>{hint}</div>}
      {action}
    </div>
  );
}

/* ----------------------------------------------------------- Loading */
export function Spinner({ size = 16 }: { size?: number }) {
  return <Loader2 size={size} className="spin" />;
}

export function Skeleton({
  h = 14,
  w = "100%",
  style,
}: {
  h?: number;
  w?: number | string;
  style?: React.CSSProperties;
}) {
  return <div className="skeleton" style={{ height: h, width: w, ...style }} />;
}

export function SkeletonRows({ rows = 6, h = 40 }: { rows?: number; h?: number }) {
  return (
    <div className="stack gap-2" style={{ padding: "var(--s-3)" }}>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} h={h} />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ Errors */
export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  const { t } = useI18n();
  const msg = error instanceof Error ? error.message : t("error.generic");
  return (
    <Empty
      icon={<AlertTriangle size={20} />}
      title={t("error.title")}
      hint={msg}
      action={
        onRetry ? (
          <button className="btn btn-sm" onClick={onRetry}>
            {t("error.retry")}
          </button>
        ) : undefined
      }
    />
  );
}

/* --------------------------------------------------------- Collapse */
export function Collapse({
  title,
  children,
  defaultOpen = false,
  meta,
}: {
  title: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  meta?: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="collapse">
      <button
        className="collapse-head"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <ChevronRight size={15} className={`chev${open ? " open" : ""}`} />
        <span className="grow">{title}</span>
        {meta}
      </button>
      {open && <div className="collapse-body fade-in">{children}</div>}
    </div>
  );
}

/* --------------------------------------------------------- Segmented */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="segmented" role="group">
      {options.map((o) => (
        <button
          key={o.value}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------- Tabs */
export function Tabs({
  value,
  onChange,
  items,
}: {
  value: string;
  onChange: (v: string) => void;
  items: { value: string; label: string; count?: number }[];
}) {
  return (
    <div className="tabs" role="tablist">
      {items.map((t) => (
        <button
          key={t.value}
          role="tab"
          className="tab"
          aria-selected={value === t.value}
          onClick={() => onChange(t.value)}
        >
          {t.label}
          {t.count !== undefined && <span className="tab-count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ Tooltip */
export function Tip({ text, children }: { text: string; children: ReactNode }) {
  return (
    <span className="tip" data-tip={text}>
      {children}
    </span>
  );
}
