import { describe, expect, it } from "vitest"

import { discountPercent, isWithinLimit, maxDiscountAmount } from "../discountLimit"

// The till's caps (item 5). `limit` is the settings store's discountLimit: null = any.

describe("maxDiscountAmount", () => {
	it("is the limit as a share of the price", () => {
		expect(maxDiscountAmount({ originalTotal: 100, subtotal: 100, limit: 10 })).toBe(10)
		// 15% of 59.97 is 8.9955: rounded DOWN, never over the limit
		expect(maxDiscountAmount({ originalTotal: 59.97, subtotal: 59.97, limit: 15 })).toBe(8.99)
	})

	it("is nothing at a limit of 0", () => {
		expect(maxDiscountAmount({ originalTotal: 100, subtotal: 100, limit: 0 })).toBe(0)
	})

	it("is the whole subtotal when any discount is allowed, or at 100", () => {
		expect(maxDiscountAmount({ originalTotal: 100, subtotal: 80, limit: null })).toBe(80)
		expect(maxDiscountAmount({ originalTotal: 100, subtotal: 100, limit: 100 })).toBe(100)
	})

	it("counts a price change against the limit (measured on the original price)", () => {
		// Price cut from 100 to 95, limit 10%: only 5 more may come off
		expect(maxDiscountAmount({ originalTotal: 100, subtotal: 95, limit: 10 })).toBe(5)
		// Price cut beyond the limit already: nothing more
		expect(maxDiscountAmount({ originalTotal: 100, subtotal: 85, limit: 10 })).toBe(0)
	})

	it("applies the limit on top of an offer", () => {
		expect(maxDiscountAmount({ originalTotal: 100, subtotal: 100, limit: 10, offerPercent: 20 })).toBe(30)
		expect(maxDiscountAmount({ originalTotal: 100, subtotal: 100, limit: 90, offerPercent: 20 })).toBe(100)
	})

	it("scales with quantity", () => {
		expect(maxDiscountAmount({ originalTotal: 3 * 12.5, subtotal: 37.5, limit: 10 })).toBe(3.75)
	})
})

describe("isWithinLimit", () => {
	it("accepts exactly the limit and refuses a penny more", () => {
		expect(isWithinLimit({ originalTotal: 100, total: 90, limit: 10 })).toBe(true)
		expect(isWithinLimit({ originalTotal: 100, total: 89.99, limit: 10 })).toBe(false)
	})

	it("refuses any reduction at a limit of 0 and accepts anything with no limit", () => {
		expect(isWithinLimit({ originalTotal: 100, total: 99.99, limit: 0 })).toBe(false)
		expect(isWithinLimit({ originalTotal: 100, total: 100, limit: 0 })).toBe(true)
		expect(isWithinLimit({ originalTotal: 100, total: 1, limit: null })).toBe(true)
	})

	it("never lets rounding take a discount over the limit (as the server)", () => {
		// 10% off 4.49 = 4.041: 4.05 is the lowest allowed, 4.04 would be 10.02%
		expect(isWithinLimit({ originalTotal: 4.49, total: 4.05, limit: 10 })).toBe(true)
		expect(isWithinLimit({ originalTotal: 4.49, total: 4.04, limit: 10 })).toBe(false)
		expect(isWithinLimit({ originalTotal: 0.99, total: 0.9, limit: 10 })).toBe(true)
		expect(isWithinLimit({ originalTotal: 0.99, total: 0.89, limit: 10 })).toBe(false)
	})

	it("whole-sale: 10% of 22.45 allows 2.24 off, not 2.25", () => {
		expect(maxDiscountAmount({ originalTotal: 22.45, subtotal: 22.45, limit: 10 })).toBe(2.24)
	})

	it("allows an offer's discount plus the limit", () => {
		expect(isWithinLimit({ originalTotal: 100, total: 70, limit: 10, offerPercent: 20 })).toBe(true)
		expect(isWithinLimit({ originalTotal: 100, total: 69.99, limit: 10, offerPercent: 20 })).toBe(false)
	})
})

describe("discountPercent", () => {
	it("is the discount as a percent to 2 places", () => {
		expect(discountPercent(100, 89.99)).toBe(10.01)
		expect(discountPercent(0, 5)).toBe(0)
	})
})
