// Exchange and ledger timestamps remain Unix milliseconds. Only presentation
// and date controls use Beijing time, independent of the browser's timezone.
export const BEIJING_TIMEZONE = "Asia/Shanghai";

function asDate(value) {
  if (value == null || value === "") return null;
  const numeric = typeof value === "number" || /^\d+$/.test(String(value));
  // API date strings without an offset historically mean UTC, as on the server.
  const normalized =
    typeof value === "string" && /^\d{4}-\d{2}-\d{2}T[\d:.]+$/.test(value)
      ? `${value}Z`
      : value;
  const date = new Date(
    numeric
      ? Number(value) < 1e11
        ? Number(value) * 1000
        : Number(value)
      : normalized,
  );
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatBeijing(value, options = {}) {
  const date = asDate(value);
  return date
    ? new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
        ...options,
        timeZone: BEIJING_TIMEZONE,
      }).format(date)
    : "—";
}

export function toBeijingInput(value) {
  const date = asDate(value);
  if (!date) return "";
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: BEIJING_TIMEZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(date)
      .map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`;
}

export function beijingInputToISO(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(value || "")) {
    throw new Error("请选择完整的北京时间");
  }
  const date = asDate(`${value}+08:00`);
  if (!date) throw new Error("北京时间格式无效");
  return date.toISOString();
}

// Presentation copy only: canonical snapshots and exported reports stay intact.
export function displayBeijingTimes(value, key = "") {
  if (value == null) return value;
  if (Array.isArray(value))
    return value.map((item) =>
      displayBeijingTimes(item, key === "ranges_ms" ? "timestamp" : ""),
    );
  if (typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([name, item]) => [
        name,
        displayBeijingTimes(item, name),
      ]),
    );
  if (
    /^(timestamp|time|decision_time|close_time|train_start|train_end|validation_start|validation_end)$|(_timestamp|_at)$/.test(
      key,
    )
  ) {
    const formatted = formatBeijing(value, { second: "2-digit" });
    if (formatted !== "—") return `${formatted} 北京时间`;
  }
  return value;
}
