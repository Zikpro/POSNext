import { describe, expect, it } from "vitest"

import { ceilCurrency, floorCurrency } from "../currency"

// Discounted prices round UP and discount amounts round DOWN, so a discount is never
// more than entered or allowed (not even a fraction of a penny).
describe("ceilCurrency / floorCurrency", () => {
	it("round a discounted price up and a discount amount down", () => {
		expect(ceilCurrency(4.49 * 0.9)).toBe(4.05) // 4.041
		expect(floorCurrency(22.45 * 0.1)).toBe(2.24) // 2.245
	})

	it("leave an exact amount alone despite float error", () => {
		expect(ceilCurrency(100 * 0.9)).toBe(90) // 90.00000000000001
		expect(ceilCurrency(4.05)).toBe(4.05)
		expect(floorCurrency(0.1 + 0.2)).toBe(0.3) // 0.30000000000000004
		expect(floorCurrency(2.24)).toBe(2.24)
	})
})
