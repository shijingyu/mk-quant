const SHARES_PER_GRID = 100;

function positiveNumber(value) {
  if (
    (typeof value !== "number" && typeof value !== "string") ||
    (typeof value === "string" && !value.trim())
  ) {
    return NaN;
  }
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : NaN;
}

function coveringGridCount(rawCount) {
  // Ignore only floating-point noise around exact integers, not a partial grid.
  const nearest = Math.round(rawCount);
  const tolerance = Math.min(1e-8, 8 * Number.EPSILON * Math.max(1, rawCount));
  return Math.max(
    1,
    Math.abs(rawCount - nearest) <= tolerance ? nearest : Math.ceil(rawCount),
  );
}

/**
 * Density is a percent: arithmetic uses the midpoint as its price reference;
 * geometric uses the increase from one grid price to the next higher price.
 * Counts round up, then both methods divide the entire interval evenly.
 * The standalone 100-share estimate uses the midpoint. Total capital sums the
 * N buy levels below the upper bound, including the lower bound, at 100 shares.
 */
export function calculateGrid({ lowerPrice, upperPrice, densityPercent } = {}) {
  const lower = positiveNumber(lowerPrice);
  const upper = positiveNumber(upperPrice);
  const density = positiveNumber(densityPercent);
  const errors = {};

  if (!Number.isFinite(lower)) errors.lowerPrice = "请输入大于 0 的下限价格";
  if (!Number.isFinite(upper)) errors.upperPrice = "请输入大于 0 的上限价格";
  if (!Number.isFinite(density))
    errors.densityPercent = "请输入大于 0 的网格密度";
  if (Number.isFinite(lower) && Number.isFinite(upper) && upper <= lower)
    errors.upperPrice = "上限价格必须大于下限价格";
  if (Object.keys(errors).length) return { errors, result: null };

  const range = upper - lower;
  const midpointPrice = lower + range / 2;
  const densityFraction = density / 100;
  // log1p keeps narrow intervals accurate; log differences avoid ratio overflow.
  const relativeRange = range / lower;
  const logRange = Number.isFinite(relativeRange)
    ? Math.log1p(relativeRange)
    : Math.log(upper) - Math.log(lower);
  const arithmeticRawCount = range / midpointPrice / densityFraction;
  const geometricRawCount = logRange / Math.log1p(densityFraction);
  const arithmeticCount = coveringGridCount(arithmeticRawCount);
  const geometricCount = coveringGridCount(geometricRawCount);

  if (
    !Number.isFinite(arithmeticRawCount) ||
    !Number.isFinite(geometricRawCount) ||
    !Number.isSafeInteger(arithmeticCount) ||
    !Number.isSafeInteger(geometricCount)
  ) {
    return {
      errors: { densityPercent: "网格数量超出可计算范围，请增大网格密度" },
      result: null,
    };
  }

  const spacing = range / arithmeticCount;
  const logStep = logRange / geometricCount;
  const ratio = Math.exp(logStep);
  const risePercent = Math.expm1(logStep) * 100;
  const dropPercent = -Math.expm1(-logStep) * 100;
  const costPerGrid = midpointPrice * SHARES_PER_GRID;
  const arithmeticAverageBuyPrice =
    lower + (spacing * (arithmeticCount - 1)) / 2;
  const arithmeticPriceSum = arithmeticAverageBuyPrice * arithmeticCount;
  // Sum U / r^i, i=1..N, without allocating a price array. expm1 avoids
  // cancellation when the grid spacing or the entire price interval is tiny.
  const geometricPriceSum =
    geometricCount === 1
      ? lower
      : upper * (-Math.expm1(-logRange) / Math.expm1(logStep));
  const geometricAverageBuyPrice = geometricPriceSum / geometricCount;
  const arithmeticCapital = arithmeticPriceSum * SHARES_PER_GRID;
  const geometricCapital = geometricPriceSum * SHARES_PER_GRID;

  if (
    ![
      spacing,
      ratio,
      risePercent,
      dropPercent,
      costPerGrid,
      arithmeticPriceSum,
      geometricPriceSum,
      arithmeticAverageBuyPrice,
      geometricAverageBuyPrice,
      arithmeticCapital,
      geometricCapital,
    ].every((value) => Number.isFinite(value) && value > 0) ||
    ratio <= 1
  ) {
    return {
      errors: { general: "价格或密度超出可计算精度，请调整输入范围" },
      result: null,
    };
  }

  return {
    errors: {},
    result: {
      lowerPrice: lower,
      upperPrice: upper,
      densityPercent: density,
      midpointPrice,
      sharesPerGrid: SHARES_PER_GRID,
      costPerGrid,
      arithmetic: {
        rawCount: arithmeticRawCount,
        gridCount: arithmeticCount,
        spacing,
        densityPercent: (spacing / midpointPrice) * 100,
        priceSum: arithmeticPriceSum,
        averageBuyPrice: arithmeticAverageBuyPrice,
        requiredCapital: arithmeticCapital,
      },
      geometric: {
        rawCount: geometricRawCount,
        gridCount: geometricCount,
        ratio,
        risePercent,
        dropPercent,
        priceSum: geometricPriceSum,
        averageBuyPrice: geometricAverageBuyPrice,
        requiredCapital: geometricCapital,
      },
    },
  };
}
