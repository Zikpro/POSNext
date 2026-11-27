/**
 * The user's discount limit at the till (POS Role Rule, or POS Settings when no rule
 * applies), as the server checks it (pos_next.api.discount_rules): a percent of the
 * original price, compared in money, exactly. The lowest price a limit allows is
 * rounded UP to the penny, so a discount never goes even a fraction over the limit
 * (10% off 4.49 allows 4.05, not 4.04). `limit` is the settings store's
 * discountLimit: null means any discount is allowed.
 */
import { ceilCurrency, roundCurrency } from "@/utils/currency"

/**
 * The most a discount may take off `subtotal`.
 * @param {object} args
 * @param {number} args.originalTotal - what the line or sale costs at its original price
 * @param {number} args.subtotal - what it costs before this discount (after any price change)
 * @param {number|null} args.limit - the user's limit in percent, null for any
 * @param {number} [args.offerPercent] - what an offer already takes off; the limit applies on top
 * @returns {number}
 */
export function maxDiscountAmount({ originalTotal, subtotal, limit, offerPercent = 0 }) {
	const before = Math.max(0, Number(subtotal) || 0)
	if (limit === null || limit === undefined) {
		return before
	}
	const allowed = Math.min(100, Math.max(0, Number(limit) || 0) + Math.max(0, Number(offerPercent) || 0))
	const lowestTotal = ceilCurrency((Number(originalTotal) || 0) * (1 - allowed / 100))
	return Math.max(0, roundCurrency(before - lowestTotal))
}

/** Whether `total` stays within the limit (exactly: not even a penny under the lowest allowed). */
export function isWithinLimit({ originalTotal, total, limit, offerPercent = 0 }) {
	if (limit === null || limit === undefined) {
		return true
	}
	const maxOff = maxDiscountAmount({ originalTotal, subtotal: originalTotal, limit, offerPercent })
	return roundCurrency(Number(total)) >= roundCurrency(Number(originalTotal) - maxOff)
}

/** The discount `total` gives on `originalTotal`, in percent (2 decimals). */
export function discountPercent(originalTotal, total) {
	const original = Number(originalTotal) || 0
	if (original <= 0) {
		return 0
	}
	return Math.round(((original - Number(total)) / original) * 10000) / 100
}
