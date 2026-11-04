import assert from "node:assert/strict";
import test from "node:test";
import { calculateGrid } from "./gridCalculator.js";

const example = { lowerPrice: 10.87, upperPrice: 24.38, densityPercent: 2 };
const close = (actual, expected, tolerance = 1e-10) =>
  assert.ok(
    Math.abs(actual - expected) <= tolerance,
    `${actual} should be within ${tolerance} of ${expected}`,
  );

test("capital sums buy levels while the 100-share midpoint estimate is unchanged", () => {
  const { result, errors } = calculateGrid(example);
  assert.deepEqual(errors, {});
  assert.equal(result.midpointPrice, 17.625);
  assert.equal(result.sharesPerGrid, 100);
  assert.equal(result.costPerGrid, 1762.5);
  assert.equal(result.arithmetic.gridCount, 39);
  assert.equal(result.geometric.gridCount, 41);
  close(result.arithmetic.priceSum, 680.62);
  close(result.arithmetic.requiredCapital, 68062);
  close(result.arithmetic.averageBuyPrice, 17.45179487179487);
  close(result.geometric.priceSum, 679.0060791821979);
  close(result.geometric.requiredCapital, 67900.60791821979);
  close(result.geometric.averageBuyPrice, 16.56112388249263);
  close(result.arithmetic.spacing, 0.3464102564102564);
  close(result.geometric.risePercent, 1.9896729078289843);
  close(result.geometric.dropPercent, 1.9508572300522165);
});

test("hand-calculated ladders include the lower buy level and exclude the upper anchor", () => {
  const arithmeticResult = calculateGrid({
    lowerPrice: 100,
    upperPrice: 108,
    densityPercent: (2 / 104) * 100,
  }).result;
  // Four buys at 106, 104, 102 and 100, with no extra buy at the 108 anchor.
  const arithmetic = arithmeticResult.arithmetic;
  assert.equal(arithmetic.gridCount, 4);
  assert.equal(arithmetic.priceSum, 412);
  assert.equal(arithmetic.averageBuyPrice, 103);
  assert.equal(arithmetic.requiredCapital, 41200);
  assert.equal(arithmeticResult.costPerGrid, 10400);

  const geometricResult = calculateGrid({
    lowerPrice: 100,
    upperPrice: 121,
    densityPercent: 10,
  }).result;
  // Two buys at 110 and 100, with no extra buy at the 121 anchor.
  const geometric = geometricResult.geometric;
  assert.equal(geometric.gridCount, 2);
  close(geometric.priceSum, 210);
  close(geometric.averageBuyPrice, 105);
  close(geometric.requiredCapital, 21000);
  assert.equal(geometricResult.costPerGrid, 11050);
});

test("closed-form funding agrees with independently enumerated buy levels", () => {
  const inputs = [
    ...[0.05, 2, 17, 250].map((densityPercent) => ({
      ...example,
      densityPercent,
    })),
    { lowerPrice: 0.03, upperPrice: 0.05, densityPercent: 0.5 },
    { lowerPrice: 100, upperPrice: 100.0001, densityPercent: 1e-8 },
  ];
  for (const input of inputs) {
    const { result, errors } = calculateGrid(input);
    assert.deepEqual(errors, {});
    for (const method of ["arithmetic", "geometric"]) {
      const grid = result[method];
      let expectedSum = 0;
      for (let i = 1; i <= grid.gridCount; i++) {
        expectedSum +=
          method === "arithmetic"
            ? input.upperPrice - i * grid.spacing
            : input.upperPrice / grid.ratio ** i;
      }
      close(grid.priceSum, expectedSum, Math.max(1e-10, expectedSum * 1e-12));
      close(
        grid.requiredCapital,
        expectedSum * 100,
        Math.max(1e-8, expectedSum * 1e-10),
      );
      close(grid.averageBuyPrice, expectedSum / grid.gridCount, 1e-9);
      assert.ok(grid.averageBuyPrice >= input.lowerPrice - 1e-10);
      assert.ok(grid.averageBuyPrice <= input.upperPrice);
    }
  }
});

test("both methods align the endpoints and keep actual density within the input", () => {
  for (const densityPercent of [0.01, 0.5, 2, 17, 100, 250]) {
    const { result } = calculateGrid({ ...example, densityPercent });
    const { arithmetic, geometric, lowerPrice, upperPrice } = result;
    close(lowerPrice + arithmetic.gridCount * arithmetic.spacing, upperPrice);
    close(lowerPrice * geometric.ratio ** geometric.gridCount, upperPrice);
    assert.ok(arithmetic.densityPercent <= densityPercent + 1e-10);
    assert.ok(geometric.risePercent <= densityPercent + 1e-10);
    close(100 * (1 - 1 / geometric.ratio), geometric.dropPercent);
  }
});

test("exact integer ratios do not gain an extra grid from floating point noise", () => {
  const geometric = calculateGrid({
    lowerPrice: 100,
    upperPrice: 121,
    densityPercent: 10,
  }).result.geometric;
  assert.equal(geometric.gridCount, 2);
  close(geometric.risePercent, 10);

  const arithmetic = calculateGrid({
    lowerPrice: 100,
    upperPrice: 108,
    densityPercent: (2 / 104) * 100,
  }).result.arithmetic;
  assert.equal(arithmetic.gridCount, 4);
  assert.equal(arithmetic.spacing, 2);

  const partial = calculateGrid({
    lowerPrice: 100,
    upperPrice: 121.000001,
    densityPercent: 10,
  }).result.geometric;
  assert.equal(partial.gridCount, 3);
});

test("a density larger than the interval still makes one whole-interval grid", () => {
  const { result } = calculateGrid({
    lowerPrice: 10,
    upperPrice: 10.01,
    densityPercent: 2,
  });
  assert.equal(result.arithmetic.gridCount, 1);
  assert.equal(result.geometric.gridCount, 1);
  close(result.arithmetic.spacing, 0.01);
  close(result.geometric.risePercent, 0.1);
  close(result.costPerGrid, 1000.5);
  for (const method of ["arithmetic", "geometric"]) {
    close(result[method].priceSum, 10);
    close(result[method].averageBuyPrice, 10);
    close(result[method].requiredCapital, 1000);
  }
});

test("form strings work while blank, non-finite and non-positive values fail", () => {
  assert.deepEqual(
    calculateGrid({
      lowerPrice: "10.87",
      upperPrice: "24.38",
      densityPercent: "2",
    }),
    calculateGrid(example),
  );
  for (const field of ["lowerPrice", "upperPrice", "densityPercent"]) {
    for (const invalid of [
      "",
      "  ",
      null,
      undefined,
      true,
      [],
      "abc",
      NaN,
      Infinity,
      -1,
      0,
    ]) {
      const { result, errors } = calculateGrid({
        ...example,
        [field]: invalid,
      });
      assert.equal(result, null);
      assert.ok(errors[field], `${field}: ${String(invalid)}`);
    }
  }
  assert.equal(calculateGrid().result, null);
});

test("reversed and equal bounds return a field error instead of a stale result", () => {
  for (const upperPrice of [10.87, 1]) {
    const { result, errors } = calculateGrid({ ...example, upperPrice });
    assert.equal(result, null);
    assert.ok(errors.upperPrice);
  }
});

test("denser grids increase counts and funds without changing the per-grid amount", () => {
  const dense = calculateGrid({ ...example, densityPercent: 1 }).result;
  const sparse = calculateGrid({ ...example, densityPercent: 5 }).result;
  for (const method of ["arithmetic", "geometric"]) {
    assert.ok(dense[method].gridCount > sparse[method].gridCount);
    assert.ok(dense[method].requiredCapital > sparse[method].requiredCapital);
  }
  assert.equal(dense.costPerGrid, sparse.costPerGrid);
});

test("unsafe counts and overflowing capital produce errors rather than Infinity", () => {
  for (const input of [
    { ...example, densityPercent: Number.MIN_VALUE },
    { ...example, densityPercent: 1e-18 },
    { lowerPrice: 1e307, upperPrice: 1.1e307, densityPercent: 2 },
  ]) {
    const { result, errors } = calculateGrid(input);
    assert.equal(result, null);
    assert.ok(Object.keys(errors).length);
  }
});
