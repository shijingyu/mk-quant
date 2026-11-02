import assert from "node:assert/strict";
import test from "node:test";
import {
  beijingInputToISO,
  formatBeijing,
  toBeijingInput,
  displayBeijingTimes,
} from "./time.js";

test("Beijing midnight selects the previous UTC day at 16:00", () => {
  assert.equal(
    beijingInputToISO("2026-11-02T00:00"),
    "2026-11-01T16:00:00.000Z",
  );
  assert.equal(
    beijingInputToISO("2026-11-02T08:30:15"),
    "2026-11-02T00:30:15.000Z",
  );
});

test("copying an existing UTC experiment preserves the original instant", () => {
  assert.equal(toBeijingInput("2026-09-01"), "2026-09-01T08:00:00");
  assert.equal(toBeijingInput("2026-09-01T00:00:00"), "2026-09-01T08:00:00");
  const original = "2026-09-30T23:59:59.000Z";
  assert.equal(beijingInputToISO(toBeijingInput(original)), original);
});

test("dataset epoch values show Beijing time with missing values explicit", () => {
  const timestamp = Date.parse("2026-11-01T16:00:00Z");
  assert.match(formatBeijing(timestamp), /2026\/11\/02 00:00/);
  assert.equal(formatBeijing(timestamp), formatBeijing(timestamp / 1000));
  assert.equal(formatBeijing(null), "—");
  assert.throws(() => beijingInputToISO(""), /北京时间/);
});

test("decision and provenance display converts nested times without mutating snapshots", () => {
  const raw = {
    timestamp: 1793548800000,
    score: 42,
    decisions: [{ execution_timestamp: "2026-11-01T16:00:00Z" }],
    intervals: { train_start: "2026-11-01T16:00:00Z" },
  };
  const result = displayBeijingTimes(raw);
  assert.equal(
    result.decisions[0].execution_timestamp,
    "2026/11/02 00:00:00 北京时间",
  );
  assert.equal(result.intervals.train_start, "2026/11/02 00:00:00 北京时间");
  assert.equal(result.score, 42);
  assert.equal(raw.decisions[0].execution_timestamp, "2026-11-01T16:00:00Z");
});
