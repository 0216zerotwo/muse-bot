import { cn } from "@/lib/utils";

/** Warm --c-* shimmer bar; static under prefers-reduced-motion. */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("c-skeleton rounded-lg", className)} aria-hidden="true" />;
}

/** Inline shimmer lines for loading regions inside an existing card. */
export function SkeletonLines({
  lines = 2,
  label = "正在加载",
  className,
}: {
  lines?: number;
  label?: string;
  className?: string;
}) {
  const widths = ["w-3/5", "w-4/5", "w-1/2", "w-2/3"];
  return (
    <div role="status" aria-busy="true" aria-label={label} className={cn("flex flex-col gap-2.5", className)}>
      {Array.from({ length: lines }, (_, index) => (
        <Skeleton key={index} className={cn("h-3.5", widths[index % widths.length])} />
      ))}
    </div>
  );
}

/** Rounded companion card placeholder for panels, summaries, and sheets. */
export function SkeletonCard({
  lines = 2,
  label = "正在加载",
  status = true,
  className,
}: {
  lines?: number;
  label?: string;
  status?: boolean;
  className?: string;
}) {
  return (
    <div
      {...(status ? { role: "status", "aria-busy": true, "aria-label": label } : { "aria-hidden": true })}
      className={cn("c-card rounded-[22px] p-4", className)}
    >
      <Skeleton className="h-4 w-2/5" />
      {lines > 0 && (
        <div className="mt-2.5 flex flex-col gap-2">
          {Array.from({ length: lines }, (_, index) => (
            <Skeleton key={index} className={cn("h-3", index === lines - 1 ? "w-3/5" : "w-4/5")} />
          ))}
        </div>
      )}
    </div>
  );
}

/** Grouped iOS-style row placeholders matching MenuGroup geometry. */
export function SkeletonRows({
  rows = 3,
  withIcon = true,
  label = "正在加载",
  className,
}: {
  rows?: number;
  withIcon?: boolean;
  label?: string;
  className?: string;
}) {
  const titleWidths = ["w-28", "w-36", "w-24", "w-32"];
  const detailWidths = ["w-44", "w-36", "w-40", "w-28"];
  return (
    <div
      role="status"
      aria-busy="true"
      aria-label={label}
      className={cn("c-card divide-y overflow-hidden rounded-2xl", className)}
      style={{ borderColor: "var(--c-border)" }}
    >
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="flex min-h-12 items-center gap-3 px-4 py-3 c-divider">
          {withIcon && <Skeleton className="h-7 w-7 shrink-0 rounded-lg" />}
          <div className="min-w-0 flex-1 space-y-1.5">
            <Skeleton className={cn("h-3.5", titleWidths[index % titleWidths.length])} />
            <Skeleton className={cn("h-2.5", detailWidths[index % detailWidths.length])} />
          </div>
        </div>
      ))}
    </div>
  );
}

/** 3-column tile placeholders for media galleries and vendor catalogs. */
export function SkeletonGrid({
  count = 6,
  variant = "media",
  label = "正在加载",
  className,
}: {
  count?: number;
  variant?: "media" | "vendor";
  label?: string;
  className?: string;
}) {
  return (
    <div role="status" aria-busy="true" aria-label={label} className={cn("grid grid-cols-3 gap-2", className)}>
      {Array.from({ length: count }, (_, index) =>
        variant === "vendor" ? (
          <div key={index} className="c-card flex flex-col items-center gap-2 rounded-2xl px-2 pb-3 pt-3.5" aria-hidden="true">
            <Skeleton className="h-10 w-10 rounded-[11px]" />
            <Skeleton className="h-3 w-14" />
          </div>
        ) : (
          <Skeleton key={index} className="aspect-square w-full rounded-xl" />
        ),
      )}
    </div>
  );
}
