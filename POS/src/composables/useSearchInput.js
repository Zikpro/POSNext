import { ref, watch, nextTick, onMounted, onUnmounted } from "vue"
import { QueuedMutex } from "@/utils/mutex"
import { parseError } from "@/utils/errorHandler"

/** How long the scan error under the search bar stays visible (ms). */
export const SCAN_ERROR_DURATION = 5000

/** exc_type the barcode API raises when no item matches the barcode. */
const BARCODE_NOT_FOUND = "BarcodeNotFoundError"

/**
 * Composable for search input, barcode scanning, and auto-add logic.
 *
 * Owns all search-input state, timers, and event handlers with proper
 * concurrency control.  Extracted from ItemsSelector.vue.
 *
 * Concurrency model:
 *   - On Enter (or auto-add timeout), the barcode is **snapshotted** from the
 *     DOM input immediately, then the input is cleared so the next scan starts
 *     into a clean field.
 *   - The snapshot is pushed into a {@link QueuedMutex}-backed queue
 *     (`processBarcodeScan`), which processes barcode lookups sequentially.
 *   - This guarantees no barcode is ever lost, even when scanning different
 *     items faster than the API can respond (~50 ms between scans).
 *
 * A failed scan sets `scanError`, shown under the search bar for
 * SCAN_ERROR_DURATION. A newer failure replaces it (a repeat of the same one
 * counts up) and a successful scan clears it, so messages never stack.
 *
 * @param {Object} options
 * @param {Object} options.itemStore          - Pinia item-search store
 * @param {(item: Object, autoAdd: boolean) => boolean} options.onItemFound
 *        Component's selectItem(). Returns true if item was accepted.
 * @param {import('vue').Ref<boolean>} options.isAnyDialogOpen
 */
export function useSearchInput({ itemStore, onItemFound, isAnyDialogOpen }) {
	// --- Reactive state (exposed) ---
	const searchInputRef = ref(null)
	const scannerEnabled = ref(false)
	const autoAddEnabled = ref(false)
	/** @type {import('vue').Ref<{id: number, kind: 'not_found'|'failed', barcode: string, reason: string, count: number}|null>} */
	const scanError = ref(null)

	// --- Internal (non-reactive) ---
	let autoSearchTimer = null
	let scanErrorTimer = null
	let scanErrorId = 0
	const barcodeQueue = new QueuedMutex({ timeout: 10000, name: "BarcodeSearch" })

	// ---- Timer helpers ----

	function clearAutoSearchTimer() {
		if (autoSearchTimer) {
			clearTimeout(autoSearchTimer)
			autoSearchTimer = null
		}
	}

	// ---- Focus ----

	function focusSearchInput() {
		nextTick(() => {
			if (searchInputRef.value) {
				searchInputRef.value.focus()
			}
		})
	}

	/** True when the search bar is on screen and no dialog, overlay or lock screen covers it. */
	function isSearchInputReachable(input) {
		if (!input || input.disabled || !input.isConnected) return false
		const rect = input.getBoundingClientRect()
		if (!rect.width || !rect.height) return false
		if (typeof document.elementFromPoint !== "function") return true
		const topElement = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
		return input === topElement || input.contains(topElement)
	}

	/**
	 * Scanners set up with an F4 prefix send F4 before the barcode. F4 moves the
	 * cursor to an empty search bar wherever it was, synchronously, because the
	 * barcode keys follow within milliseconds. Alt+F4 and other modified presses
	 * are left alone. While a dialog or overlay is open F4 does nothing, so a
	 * scan cannot reach the cart behind it.
	 */
	function handleGlobalKeyDown(event) {
		if (event.key !== "F4" || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) {
			return
		}
		// Stop the browser's own F4 action (e.g. opening the address bar list)
		event.preventDefault()

		const input = searchInputRef.value
		if (isAnyDialogOpen?.value || !isSearchInputReachable(input)) return

		clearAutoSearchTimer()
		itemStore.clearSearch()
		input.value = ""
		input.focus()
	}

	// ---- Scan error message ----

	function clearScanError() {
		if (scanErrorTimer) {
			clearTimeout(scanErrorTimer)
			scanErrorTimer = null
		}
		scanError.value = null
	}

	/** Show a scan error, replacing the current one; the same error again counts up. */
	function showScanError(kind, barcode, reason = "") {
		const current = scanError.value
		const isRepeat = current?.kind === kind && current.barcode === barcode && current.reason === reason
		scanError.value = { id: ++scanErrorId, kind, barcode, reason, count: isRepeat ? current.count + 1 : 1 }

		if (scanErrorTimer) clearTimeout(scanErrorTimer)
		scanErrorTimer = setTimeout(clearScanError, SCAN_ERROR_DURATION)
	}

	/** Why a lookup failed, when the barcode was not simply unknown. */
	function getLookupFailureReason(error) {
		if (!error?.exc_type && (error instanceof TypeError || navigator.onLine === false)) {
			return __("The till can't reach the server. Check the connection and scan again.")
		}
		return parseError(error).message
	}

	// ---- Clear ----

	/** Atomic clear: timer -> store -> DOM input.value -> refocus */
	function clearSearchAndResetInput() {
		clearAutoSearchTimer()
		itemStore.clearSearch()
		if (searchInputRef.value) {
			searchInputRef.value.value = ""
		}
		if (scannerEnabled.value || autoAddEnabled.value) {
			focusSearchInput()
		}
	}

	// ---- Event handlers ----

	function handleKeyDown(event) {
		if (event.key === "Enter") {
			event.preventDefault()
			clearAutoSearchTimer()

			// Snapshot the barcode NOW from the DOM input, before anything overwrites it
			const barcode = searchInputRef.value?.value?.trim() || itemStore.searchTerm?.trim()
			if (barcode) {
				// Clear input immediately so next scan starts clean
				itemStore.clearSearch()
				if (searchInputRef.value) searchInputRef.value.value = ""

				// Queue the search with the captured barcode
				processBarcodeScan(barcode, autoAddEnabled.value)
			}
			return
		}
		// All other keys: no special handling needed.
		// Dead scanner-speed-detection code removed.
	}

	/**
	 * Handles the `input` event on the search <input>.
	 *
	 * Two independent timers exist by design:
	 *   1. itemStore.setSearchTerm() triggers the store's own debounce for
	 *      updating the displayed item grid.
	 *   2. autoSearchTimer (500 ms) triggers auto-add behaviour — completely
	 *      separate from display.
	 */
	function handleSearchInput(event) {
		const value = event.target.value

		// Guard: ignore stale empty events after search was already cleared
		if (!value && !itemStore.searchTerm) {
			return
		}

		itemStore.setSearchTerm(value)

		clearAutoSearchTimer()

		// Auto-add: after user stops typing for 500 ms, trigger barcode search
		if (autoAddEnabled.value && value.trim().length > 0) {
			autoSearchTimer = setTimeout(() => {
				const barcode = searchInputRef.value?.value?.trim() || itemStore.searchTerm?.trim()
				if (barcode) {
					itemStore.clearSearch()
					if (searchInputRef.value) searchInputRef.value.value = ""
					processBarcodeScan(barcode, true)
				}
			}, 500)
		}
	}

	/** Clicking the search input clears search + timer atomically. */
	function handleSearchClick() {
		clearSearchAndResetInput()
	}

	/**
	 * Queue a barcode scan for sequential processing.
	 *
	 * The barcode string is already captured (snapshotted) by the caller —
	 * it is never read from shared state here. The {@link QueuedMutex}
	 * ensures scans execute one at a time so every scan is resolved before
	 * the next begins, preventing double-adds and lost barcodes.
	 *
	 * Lookup: exact barcode match via `itemStore.searchByBarcode()`.
	 * An unknown barcode shows "not found"; any other failure (offline,
	 * server error, item not for sale) shows its actual reason.
	 *
	 * @param {string}  barcode      - Pre-captured barcode value
	 * @param {boolean} forceAutoAdd - When true, item is added without user click
	 */
	function processBarcodeScan(barcode, forceAutoAdd) {
		const shouldAutoAdd = forceAutoAdd || (scannerEnabled.value && autoAddEnabled.value)

		barcodeQueue.withLock(async () => {
			let item = null
			try {
				item = await itemStore.searchByBarcode(barcode)
			} catch (error) {
				if (error?.exc_type !== BARCODE_NOT_FOUND) {
					console.error("Barcode API error:", error)
					showScanError("failed", barcode, getLookupFailureReason(error))
					focusSearchInput()
					return
				}
			}

			if (item) {
				clearScanError()
				onItemFound(item, shouldAutoAdd)
			} else {
				// Note: we cannot fall back to filteredItems here because
				// clearSearch() was called before the API request, so
				// filteredItems would contain ALL cached items (not search results).
				showScanError("not_found", barcode)
			}
			focusSearchInput()
		})
	}

	// ---- Toggles ----

	function toggleBarcodeScanner() {
		scannerEnabled.value = !scannerEnabled.value

		if (scannerEnabled.value) {
			autoAddEnabled.value = true
			focusSearchInput()
		} else {
			autoAddEnabled.value = false
		}
	}

	function toggleAutoAdd() {
		autoAddEnabled.value = !autoAddEnabled.value

		if (autoAddEnabled.value && !scannerEnabled.value) {
			scannerEnabled.value = true
		}

		if (!autoAddEnabled.value) {
			clearAutoSearchTimer()
		}

		if (autoAddEnabled.value) {
			focusSearchInput()
		}
	}

	// ---- Dialog-close watcher ----
	// Refocuses the search bar when all dialogs close (scanner/auto-add modes)
	const stopDialogWatcher = watch(isAnyDialogOpen, (isOpen, wasOpen) => {
		if (wasOpen && !isOpen && (scannerEnabled.value || autoAddEnabled.value)) {
			focusSearchInput()
		}
	})

	// ---- Global F4 listener ----
	// Capture phase, so F4 is handled before any focused field sees it
	onMounted(() => window.addEventListener("keydown", handleGlobalKeyDown, true))

	// ---- Cleanup ----
	function cleanup() {
		clearAutoSearchTimer()
		clearScanError()
		stopDialogWatcher()
		window.removeEventListener("keydown", handleGlobalKeyDown, true)
	}

	onUnmounted(cleanup)

	return {
		// State
		searchInputRef,
		scannerEnabled,
		autoAddEnabled,
		scanError,

		// Event handlers
		handleSearchInput,
		handleKeyDown,
		handleSearchClick,
		dismissScanError: clearScanError,

		// Toggles
		toggleBarcodeScanner,
		toggleAutoAdd,

		// Utilities
		focusSearchInput,
		clearSearchAndResetInput,
		cleanup,
	}
}
