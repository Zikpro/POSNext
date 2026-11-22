// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils"
import {
	afterEach,
	beforeAll,
	beforeEach,
	describe,
	expect,
	it,
	vi,
} from "vitest"
import { defineComponent, h, ref } from "vue"
import {
	BEEP_MIN_INTERVAL_MS,
	resetBarcodeBeepForTests,
} from "@/utils/barcodeBeep"
import { useSearchInput } from "@/composables/useSearchInput"

beforeAll(() => {
	globalThis.__ = (text, args = []) =>
		text.replace(/\{(\d+)\}/g, (_, i) => args[i])
})

const NOT_FOUND = Object.assign(new Error("not found"), {
	exc_type: "BarcodeNotFoundError",
})

let wrapper
let api
let itemStore
let onItemFound
let searchInput
let otherInput

/** Mounts the composable with a search bar and a second, unrelated field. */
function mountSearch({
	searchByBarcode = vi.fn(),
	isAnyDialogOpen = ref(false),
} = {}) {
	itemStore = {
		searchTerm: "",
		clearSearch: vi.fn(() => {
			itemStore.searchTerm = ""
		}),
		setSearchTerm: vi.fn((value) => {
			itemStore.searchTerm = value
		}),
		searchByBarcode,
	}
	onItemFound = vi.fn(() => true)

	const Host = defineComponent({
		setup() {
			api = useSearchInput({ itemStore, onItemFound, isAnyDialogOpen })
			return () =>
				h("div", [
					h("input", {
						id: "item-search",
						ref: api.searchInputRef,
						onKeydown: api.handleKeyDown,
					}),
					h("input", { id: "customer-search" }),
				])
		},
	})
	wrapper = mount(Host, { attachTo: document.body })
	searchInput = document.getElementById("item-search")
	otherInput = document.getElementById("customer-search")

	// jsdom has no layout: give the search bar a size and make it the top element
	searchInput.getBoundingClientRect = () => ({
		left: 10,
		top: 10,
		width: 300,
		height: 32,
	})
	document.elementFromPoint = vi.fn(() => searchInput)
}

function pressKey(target, key, modifiers = {}) {
	const event = new KeyboardEvent("keydown", {
		key,
		bubbles: true,
		cancelable: true,
		...modifiers,
	})
	target.dispatchEvent(event)
	return event
}

/** Types a barcode into the search bar and presses Enter, like a scanner. */
async function scan(barcode) {
	searchInput.value = barcode
	itemStore.searchTerm = barcode
	pressKey(searchInput, "Enter")
	await flushPromises()
	await vi.advanceTimersByTimeAsync(0)
}

afterEach(() => {
	wrapper?.unmount()
	wrapper = null
	vi.useRealTimers()
	document.elementFromPoint = undefined
})

describe("F4 scanner prefix", () => {
	beforeEach(() => mountSearch())

	it("moves the cursor to an empty search bar at once, from another field", () => {
		searchInput.value = "old text"
		itemStore.searchTerm = "old text"
		otherInput.value = "Jane"
		otherInput.focus()

		const event = pressKey(otherInput, "F4")

		// Synchronous: the barcode keys that follow F4 must already land in the search bar
		expect(document.activeElement).toBe(searchInput)
		expect(searchInput.value).toBe("")
		expect(itemStore.clearSearch).toHaveBeenCalled()
		expect(event.defaultPrevented).toBe(true)
		expect(otherInput.value).toBe("Jane")
	})

	it("works when nothing has focus", () => {
		document.activeElement?.blur()
		pressKey(document.body, "F4")
		expect(document.activeElement).toBe(searchInput)
	})

	it.each([
		["Alt", { altKey: true }],
		["Ctrl", { ctrlKey: true }],
		["Shift", { shiftKey: true }],
		["Meta", { metaKey: true }],
	])("leaves %s+F4 alone", (_name, modifiers) => {
		otherInput.focus()
		const event = pressKey(otherInput, "F4", modifiers)
		expect(document.activeElement).toBe(otherInput)
		expect(event.defaultPrevented).toBe(false)
	})

	it("ignores other keys", () => {
		otherInput.focus()
		for (const key of ["a", "1", "F2", "F5", "Enter", "Escape"]) {
			const event = pressKey(otherInput, key)
			expect(event.defaultPrevented).toBe(false)
		}
		expect(document.activeElement).toBe(otherInput)
		expect(itemStore.clearSearch).not.toHaveBeenCalled()
	})

	it("stops listening once the sale screen is unmounted", () => {
		wrapper.unmount()
		wrapper = null
		const event = pressKey(document.body, "F4")
		expect(event.defaultPrevented).toBe(false)
	})

	it("then looks up the barcode the scanner types, ending with Enter", async () => {
		vi.useFakeTimers()
		itemStore.searchByBarcode.mockResolvedValue({ item_code: "APPLE" })
		otherInput.focus()

		pressKey(otherInput, "F4")
		await scan("5012345678900")

		expect(itemStore.searchByBarcode).toHaveBeenCalledWith("5012345678900")
		expect(onItemFound).toHaveBeenCalledWith({ item_code: "APPLE" }, false)
	})
})

describe("F4 while a dialog or overlay is open", () => {
	it("does nothing when a registered dialog is open", () => {
		mountSearch({ isAnyDialogOpen: ref(true) })
		otherInput.value = "12.50"
		otherInput.focus()

		pressKey(otherInput, "F4")

		expect(document.activeElement).toBe(otherInput)
		expect(otherInput.value).toBe("12.50")
		expect(itemStore.clearSearch).not.toHaveBeenCalled()
	})

	it("does nothing when an overlay covers the search bar", () => {
		mountSearch()
		const overlay = document.createElement("div")
		document.body.appendChild(overlay)
		document.elementFromPoint = vi.fn(() => overlay)
		otherInput.focus()

		pressKey(otherInput, "F4")

		expect(document.activeElement).toBe(otherInput)
		overlay.remove()
	})

	it("does nothing when the search bar is hidden", () => {
		mountSearch()
		searchInput.getBoundingClientRect = () => ({
			left: 0,
			top: 0,
			width: 0,
			height: 0,
		})
		otherInput.focus()

		pressKey(otherInput, "F4")

		expect(document.activeElement).toBe(otherInput)
	})
})

describe("scan error under the search bar", () => {
	beforeEach(() => {
		vi.useFakeTimers()
		vi.spyOn(console, "error").mockImplementation(() => {})
		mountSearch()
	})

	afterEach(() => vi.restoreAllMocks())

	it("shows an unknown barcode as not found, without adding anything", async () => {
		itemStore.searchByBarcode.mockRejectedValue(NOT_FOUND)

		await scan("999000111")

		expect(api.scanError.value).toMatchObject({
			kind: "not_found",
			barcode: "999000111",
			count: 1,
		})
		expect(onItemFound).not.toHaveBeenCalled()
	})

	it("treats an empty lookup result as not found", async () => {
		itemStore.searchByBarcode.mockResolvedValue(null)
		await scan("999000111")
		expect(api.scanError.value).toMatchObject({
			kind: "not_found",
			barcode: "999000111",
		})
	})

	it("stays visible after 5 seconds", async () => {
		itemStore.searchByBarcode.mockRejectedValue(NOT_FOUND)
		await scan("999000111")

		await vi.advanceTimersByTimeAsync(5000)
		expect(api.scanError.value).toMatchObject({ barcode: "999000111" })
	})

	it("stays visible after 30 seconds", async () => {
		itemStore.searchByBarcode.mockRejectedValue(NOT_FOUND)
		await scan("999000111")

		await vi.advanceTimersByTimeAsync(30000)
		expect(api.scanError.value).toMatchObject({ barcode: "999000111" })
	})

	it("counts repeats of the same barcode instead of stacking", async () => {
		itemStore.searchByBarcode.mockRejectedValue(NOT_FOUND)
		await scan("999000111")
		await vi.advanceTimersByTimeAsync(4000)
		await scan("999000111")

		expect(api.scanError.value).toMatchObject({
			barcode: "999000111",
			count: 2,
		})
	})

	it("replaces the message when a different barcode fails", async () => {
		itemStore.searchByBarcode.mockRejectedValue(NOT_FOUND)
		await scan("111")
		await scan("222")
		expect(api.scanError.value).toMatchObject({ barcode: "222", count: 1 })
	})

	it("ends with one message after rapid repeated scans", async () => {
		itemStore.searchByBarcode.mockRejectedValue(NOT_FOUND)
		for (let i = 0; i < 5; i++) {
			searchInput.value = "999000111"
			pressKey(searchInput, "Enter")
		}
		await flushPromises()
		await vi.advanceTimersByTimeAsync(0)

		expect(itemStore.searchByBarcode).toHaveBeenCalledTimes(5)
		expect(api.scanError.value).toMatchObject({
			barcode: "999000111",
			count: 5,
		})
	})

	it("clears at once when a valid item is scanned, and the item is added", async () => {
		itemStore.searchByBarcode
			.mockRejectedValueOnce(NOT_FOUND)
			.mockResolvedValueOnce({ item_code: "APPLE" })
		await scan("999000111")
		expect(api.scanError.value).not.toBeNull()

		await scan("5012345678900")

		expect(api.scanError.value).toBeNull()
		expect(onItemFound).toHaveBeenCalledWith({ item_code: "APPLE" }, false)
	})

	it("can be dismissed", async () => {
		itemStore.searchByBarcode.mockRejectedValue(NOT_FOUND)
		await scan("999000111")
		api.dismissScanError()
		expect(api.scanError.value).toBeNull()
	})

	it("shows a connection problem as such, not as not found", async () => {
		itemStore.searchByBarcode.mockRejectedValue(
			new TypeError("Failed to fetch"),
		)

		await scan("5012345678900")

		expect(api.scanError.value).toMatchObject({
			kind: "failed",
			barcode: "5012345678900",
		})
		expect(api.scanError.value.reason).toMatch(/can't reach the server/)
	})

	it("shows the server's reason for other failures", async () => {
		const error = Object.assign(new Error("417"), {
			exc_type: "ValidationError",
			messages: [
				"Error searching by barcode: Item SOAP is not allowed for sales",
			],
		})
		itemStore.searchByBarcode.mockRejectedValue(error)

		await scan("5012345678900")

		expect(api.scanError.value).toMatchObject({
			kind: "failed",
			reason: "Error searching by barcode: Item SOAP is not allowed for sales",
		})
		expect(onItemFound).not.toHaveBeenCalled()
	})

	it("keeps the scanner working after an error", async () => {
		itemStore.searchByBarcode
			.mockRejectedValueOnce(new TypeError("Failed to fetch"))
			.mockResolvedValueOnce({ item_code: "APPLE" })
		await scan("5012345678900")
		await scan("5012345678900")

		expect(onItemFound).toHaveBeenCalledTimes(1)
		expect(api.scanError.value).toBeNull()
		expect(searchInput.value).toBe("")
	})
})

/** Replaces the browser AudioContext with a stub that records each oscillator it creates. */
function installFakeAudio(state = "running") {
	const contexts = []
	globalThis.AudioContext = class {
		constructor() {
			this.state = state
			this.currentTime = 0
			this.destination = {}
			this.oscillators = []
			contexts.push(this)
		}
		createOscillator() {
			const oscillator = {
				type: "",
				frequency: { setValueAtTime: vi.fn() },
				connect: vi.fn(),
				start: vi.fn(),
				stop: vi.fn(),
			}
			this.oscillators.push(oscillator)
			return oscillator
		}
		createGain() {
			return {
				gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
				connect: vi.fn(),
			}
		}
		resume() {
			return Promise.resolve()
		}
	}
	return contexts
}

describe("unknown-barcode beep", () => {
	let contexts

	const beepCount = () => contexts.flatMap((context) => context.oscillators).length

	beforeEach(() => {
		vi.useFakeTimers()
		vi.spyOn(console, "error").mockImplementation(() => {})
		resetBarcodeBeepForTests()
		contexts = installFakeAudio("running")
		mountSearch()
	})

	afterEach(() => {
		vi.restoreAllMocks()
		delete globalThis.AudioContext
	})

	it("beeps when an unknown barcode is scanned", async () => {
		itemStore.searchByBarcode.mockRejectedValue(NOT_FOUND)

		await scan("999000111")

		expect(beepCount()).toBe(1)
		expect(api.scanError.value).toMatchObject({ kind: "not_found" })
	})

	it("does not beep for a successful scan", async () => {
		itemStore.searchByBarcode.mockResolvedValue({ item_code: "APPLE" })

		await scan("5012345678900")

		expect(beepCount()).toBe(0)
		expect(onItemFound).toHaveBeenCalledTimes(1)
	})

	it("does not beep for a network or server error", async () => {
		itemStore.searchByBarcode
			.mockRejectedValueOnce(new TypeError("Failed to fetch"))
			.mockRejectedValueOnce(
				Object.assign(new Error("417"), {
					exc_type: "ValidationError",
					messages: ["Error searching by barcode: Item SOAP is not allowed for sales"],
				}),
			)

		await scan("5012345678900")
		await scan("5012345678901")

		expect(beepCount()).toBe(0)
		expect(api.scanError.value).toMatchObject({ kind: "failed" })
	})

	it("gives one beep for rapid repeated invalid scans, then another after the interval", async () => {
		itemStore.searchByBarcode.mockRejectedValue(NOT_FOUND)

		for (let i = 0; i < 5; i++) {
			await scan("999000111")
		}
		expect(beepCount()).toBe(1)
		expect(api.scanError.value).toMatchObject({ count: 5 })

		await vi.advanceTimersByTimeAsync(BEEP_MIN_INTERVAL_MS)
		await scan("999000111")
		expect(beepCount()).toBe(2)
	})

	it("keeps the message when audio is blocked by the browser", async () => {
		resetBarcodeBeepForTests()
		contexts = installFakeAudio("suspended")
		itemStore.searchByBarcode.mockRejectedValue(NOT_FOUND)

		await scan("999000111")

		expect(beepCount()).toBe(0)
		expect(api.scanError.value).toMatchObject({
			kind: "not_found",
			barcode: "999000111",
		})
	})
})
