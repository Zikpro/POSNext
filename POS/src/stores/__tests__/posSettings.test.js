import { createPinia, setActivePinia } from "pinia"
import { beforeEach, describe, expect, it, vi } from "vitest"

// The server call: records what the store asked for and answers with `serverReply`.
const server = { calls: [], reply: {}, fail: false }
vi.mock("frappe-ui", () => ({
	createResource: (options) => ({
		submit: vi.fn(async (params) => {
			server.calls.push(params)
			if (server.fail) {
				options.onError?.(new Error("offline"))
				throw new Error("offline")
			}
			options.onSuccess?.(server.reply)
			return server.reply
		}),
	}),
}))

// Settings preloaded by bootstrap at page load.
let preloaded = null
vi.mock("../bootstrap", () => ({
	useBootstrapStore: () => ({ getPreloadedPOSSettings: () => preloaded }),
}))

import { usePOSSettingsStore } from "../posSettings"

let store

beforeEach(() => {
	setActivePinia(createPinia())
	server.calls = []
	server.reply = {}
	server.fail = false
	preloaded = null
	store = usePOSSettingsStore()
})

describe("till powers from POS Role Rule", () => {
	it("defaults match POS Settings defaults: no discount limit, other powers off", () => {
		expect(store.allowAnyDiscount).toBe(true)
		expect(store.discountLimit).toBeNull()
		expect(store.allowRateEdit).toBe(false)
		expect(store.allowReturnWithoutReceipt).toBe(false)
		expect(store.allowAwardLoyaltyPoints).toBe(false)
		expect(store.roleRulesSource).toBe("pos_settings")
	})

	it("a percent limit is the discount limit", () => {
		Object.assign(store.settings, { allow_any_discount: 0, max_discount_percent: 10 })
		expect(store.allowAnyDiscount).toBe(false)
		expect(store.maxDiscountPercent).toBe(10)
		expect(store.discountLimit).toBe(10)
	})

	it("a limit of 0 means no discount, not no limit", () => {
		Object.assign(store.settings, { allow_any_discount: 0, max_discount_percent: 0 })
		expect(store.discountLimit).toBe(0)
	})

	it("any discount means no limit, whatever the percent", () => {
		Object.assign(store.settings, { allow_any_discount: 1, max_discount_percent: 5 })
		expect(store.discountLimit).toBeNull()
	})

	it("reads flags sent as numbers or numeric strings", () => {
		Object.assign(store.settings, {
			allow_rate_edit: "1",
			allow_return_without_receipt: 1,
			allow_award_loyalty_points: "0",
		})
		expect(store.allowRateEdit).toBe(true)
		expect(store.allowReturnWithoutReceipt).toBe(true)
		expect(store.allowAwardLoyaltyPoints).toBe(false)
	})

	it("resetSettings puts the till powers back to their defaults", () => {
		Object.assign(store.settings, { allow_any_discount: 0, max_discount_percent: 10, allow_rate_edit: 1 })
		store.resetSettings()
		expect(store.discountLimit).toBeNull()
		expect(store.allowRateEdit).toBe(false)
	})
})

describe("loading the settings with the user's rules", () => {
	it("uses the settings preloaded at page load, without asking the server", async () => {
		preloaded = { max_discount_percent: 15, allow_any_discount: 0, role_rules_source: "rules" }
		expect(await store.loadSettings("Till A")).toBe(true)
		expect(server.calls).toEqual([])
		expect(store.discountLimit).toBe(15)
	})

	it("asks the server for the till's rules when nothing was preloaded", async () => {
		server.reply = { max_discount_percent: 12, allow_any_discount: 0, role_rules_source: "rules" }
		expect(await store.loadSettings("Till A")).toBe(true)
		expect(server.calls).toEqual([{ pos_profile: "Till A", for_till: 1 }])
		expect(store.discountLimit).toBe(12)
	})

	it("fresh (a shift was opened) asks the server even when settings were preloaded", async () => {
		preloaded = { max_discount_percent: 15, allow_any_discount: 0 }
		server.reply = { max_discount_percent: 5, allow_any_discount: 0, role_rules_source: "rules" }
		expect(await store.loadSettings("Till A", { fresh: true })).toBe(true)
		expect(server.calls).toEqual([{ pos_profile: "Till A", for_till: 1 }])
		expect(store.discountLimit).toBe(5)
	})

	it("fresh but offline falls back to the preloaded settings", async () => {
		preloaded = { max_discount_percent: 15, allow_any_discount: 0 }
		server.fail = true
		expect(await store.loadSettings("Till A", { fresh: true })).toBe(true)
		expect(store.discountLimit).toBe(15)
		expect(store.isLoading).toBe(false)
	})

	it("offline with nothing preloaded reports failure", async () => {
		server.fail = true
		expect(await store.loadSettings("Till A")).toBe(false)
	})

	it("reloadSettings asks for the till's rules too", async () => {
		store.settings.pos_profile = "Till A"
		await store.reloadSettings()
		expect(server.calls).toEqual([{ pos_profile: "Till A", for_till: 1 }])
	})
})
