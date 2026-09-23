import { describe, expect, it } from "vitest"
import { roundCurrency } from "@/utils/currency"

/**
 * Mirrors computeBackendRate() from useInvoice.js. The real function closes over
 * the taxInclusive ref inside the composable, so taxInclusive is a parameter here.
 * Keep this in sync with the source.
 */
function computeBackendRate(item, taxInclusive = false) {
	const qty = item.quantity || item.qty || 1
	const priceListRate = item.price_list_rate || item.rate || 0
	const discountAmount = item.discount_amount || 0

	if (taxInclusive) {
		return roundCurrency(priceListRate - discountAmount / qty)
	}

	const pricingRules = item.pricing_rules
	const hasPricingRule = Array.isArray(pricingRules)
		? pricingRules.length > 0
		: Boolean(pricingRules)
	const hasDiscount =
		(item.discount_percentage || 0) > 0 || discountAmount > 0 || hasPricingRule

	if (!hasDiscount) {
		return roundCurrency(
			item.is_rate_manually_edited === 1 ? item.rate || 0 : priceListRate,
		)
	}

	return qty > 0 ? roundCurrency((item.amount || 0) / qty) : item.rate || 0
}

/** Line amount as recalculateItem() computes it, before any discount. */
const lineAmount = (qty, rate) => roundCurrency(qty * rate)

/** What ERPNext derives: discount_amount = price_list_rate - rate. */
const erpnextDiscount = (priceListRate, rate) => roundCurrency(priceListRate - rate)

const weighedItem = (qty, priceListRate, overrides = {}) => ({
	quantity: qty,
	price_list_rate: priceListRate,
	rate: priceListRate,
	amount: lineAmount(qty, priceListRate),
	discount_percentage: 0,
	discount_amount: 0,
	...overrides,
})

describe("computeBackendRate — no genuine discount", () => {
	// The reported bug: 2.28 / 0.764 = 2.984... which rounded down to 2.98,
	// a penny under price_list_rate, so ERPNext invented a 0.01 discount.
	it.each([
		[0.764, 2.28],
		[0.79, 2.36],
		[2.456, 7.34],
		[0.762, 2.28],
	])("qty %s keeps rate at 2.99 and yields no discount", (qty, expectedAmount) => {
		const item = weighedItem(qty, 2.99)

		const rate = computeBackendRate(item)

		expect(rate).toBe(2.99)
		expect(erpnextDiscount(2.99, rate)).toBe(0)
		// ERPNext recomputes the line amount from the unit rate.
		expect(lineAmount(qty, rate)).toBe(expectedAmount)
	})

	it("reproduces the old lossy round-trip to prove the bug was real", () => {
		// amount / qty is what the code used to return.
		expect(roundCurrency(2.28 / 0.764)).toBe(2.98)
		expect(erpnextDiscount(2.99, 2.98)).toBe(0.01)
	})

	it("leaves whole quantities unchanged", () => {
		const item = weighedItem(3, 2.99)
		expect(computeBackendRate(item)).toBe(2.99)
		expect(erpnextDiscount(2.99, computeBackendRate(item))).toBe(0)
	})

	it("uses the edited rate when the rate was manually edited", () => {
		const item = weighedItem(0.764, 2.99, {
			rate: 2.5,
			amount: lineAmount(0.764, 2.5),
			is_rate_manually_edited: 1,
		})
		// The manual reduction is a real price change and must survive.
		expect(computeBackendRate(item)).toBe(2.5)
	})
})

describe("computeBackendRate — genuine discounts still work", () => {
	it("keeps the net-rate derivation for a percentage discount", () => {
		const base = lineAmount(0.764, 2.99) // 2.28
		const discount = roundCurrency((base * 10) / 100) // 0.23
		const item = weighedItem(0.764, 2.99, {
			discount_percentage: 10,
			discount_amount: discount,
			amount: roundCurrency(base - discount), // 2.05
		})

		const rate = computeBackendRate(item)

		expect(rate).toBe(roundCurrency(2.05 / 0.764))
		expect(rate).toBeLessThan(2.99)
		expect(erpnextDiscount(2.99, rate)).toBeGreaterThan(0)
	})

	it("keeps the net-rate derivation for a fixed amount discount", () => {
		const item = weighedItem(0.764, 2.99, {
			discount_amount: 0.5,
			amount: roundCurrency(lineAmount(0.764, 2.99) - 0.5),
		})

		expect(computeBackendRate(item)).toBeLessThan(2.99)
	})

	it("keeps the net-rate derivation when a pricing rule is attached", () => {
		const item = weighedItem(0.764, 2.99, {
			pricing_rules: ["PRLE-0001"],
			amount: 2.0,
		})

		expect(computeBackendRate(item)).toBe(roundCurrency(2.0 / 0.764))
	})
})

describe("computeBackendRate — tax-inclusive behaviour unchanged", () => {
	it("returns the price list rate when there is no discount", () => {
		expect(computeBackendRate(weighedItem(0.764, 2.99), true)).toBe(2.99)
	})

	it("subtracts the per-unit discount", () => {
		const item = weighedItem(2, 2.99, { discount_amount: 1 })
		expect(computeBackendRate(item, true)).toBe(roundCurrency(2.99 - 0.5))
	})
})
