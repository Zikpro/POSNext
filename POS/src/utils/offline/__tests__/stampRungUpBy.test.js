// Node environment: no window, so importing workerClient does not start a Web Worker.
import { afterEach, describe, expect, it } from "vitest"
import { stampRungUpBy } from "../workerClient"

afterEach(() => {
	delete globalThis.document
})

describe("stampRungUpBy: who rang up a sale saved offline", () => {
	it("records the logged-in user from the login cookie", () => {
		globalThis.document = { cookie: "sid=abc; user_id=cashier%40example.com; full_name=Cashier" }
		expect(stampRungUpBy({ customer: "C1" })).toEqual({
			customer: "C1",
			pos_next_rung_up_by: "cashier@example.com",
		})
	})

	it("keeps a value already recorded (an edited sale keeps who rang it up)", () => {
		globalThis.document = { cookie: "user_id=manager%40example.com" }
		const invoice = { customer: "C1", pos_next_rung_up_by: "cashier@example.com" }
		expect(stampRungUpBy(invoice)).toBe(invoice)
	})

	it("does not record Guest or a missing user", () => {
		globalThis.document = { cookie: "user_id=Guest" }
		expect(stampRungUpBy({ customer: "C1" })).toEqual({ customer: "C1" })
		globalThis.document = { cookie: "" }
		expect(stampRungUpBy({ customer: "C1" })).toEqual({ customer: "C1" })
	})

	it("leaves the original object unchanged", () => {
		globalThis.document = { cookie: "user_id=cashier%40example.com" }
		const invoice = { customer: "C1" }
		stampRungUpBy(invoice)
		expect(invoice).toEqual({ customer: "C1" })
	})
})
