<template>
	<Dialog v-model="showDialog" :options="{ title: __('Refund Without Receipt'), size: '3xl' }">
		<template #body-content>
			<div class="flex flex-col gap-4 text-start">
				<p v-if="isOffline" class="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3">
					{{ __('Refunds cannot be processed while offline.') }}
				</p>

				<div class="text-sm text-gray-600">
					{{ __('Customer') }}: <span class="font-semibold text-gray-900">{{ customerName || __('None') }}</span>
				</div>

				<!-- Item search -->
				<div class="relative">
					<input
						v-model="search"
						type="text"
						:placeholder="__('Search item to refund...')"
						class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
					/>
					<div v-if="matches.length" class="absolute z-10 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow max-h-56 overflow-y-auto">
						<button
							v-for="item in matches"
							:key="item.item_code"
							type="button"
							class="w-full px-3 py-2 flex justify-between text-sm hover:bg-gray-50"
							@click="addItem(item)"
						>
							<span>{{ item.item_name || item.item_code }}</span>
							<span class="text-gray-500">{{ formatCurrency(itemPrice(item)) }}</span>
						</button>
					</div>
				</div>

				<!-- Items to refund -->
				<div class="border border-gray-200 rounded-lg divide-y divide-gray-100">
					<div v-if="!rows.length" class="p-4 text-sm text-gray-500 text-center">{{ __('No items added') }}</div>
					<div v-for="(row, index) in rows" :key="row.item_code" class="p-3 flex items-center gap-3 text-sm">
						<span class="flex-1 font-medium text-gray-900">{{ row.item_name }}</span>
						<input
							v-model.number="row.qty"
							type="number"
							min="1"
							step="1"
							class="w-20 h-8 border border-gray-300 rounded-lg px-2 text-center"
						/>
						<span class="w-24 text-end text-gray-600">{{ formatCurrency(row.rate) }}</span>
						<span class="w-28 text-end font-semibold">{{ formatCurrency(lineTotal(row)) }}</span>
						<button type="button" class="text-gray-400 hover:text-red-600" :title="__('Remove')" @click="rows.splice(index, 1)">
							<FeatherIcon name="x" class="w-4 h-4" />
						</button>
					</div>
				</div>

				<!-- Refund method -->
				<div>
					<label class="block text-sm font-medium text-gray-700 mb-2">{{ __('Refund Method') }}</label>
					<div class="flex flex-wrap gap-2">
						<button
							v-for="method in refundMethods"
							:key="method.mode_of_payment"
							type="button"
							:class="[
								'px-3 py-2 rounded-lg border text-sm',
								refundMode === method.mode_of_payment ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-700 border-gray-300 hover:bg-gray-50',
							]"
							@click="refundMode = method.mode_of_payment"
						>
							{{ method.mode_of_payment }}
						</button>
					</div>
				</div>

				<div class="flex justify-between items-center bg-gray-50 rounded-lg p-3">
					<span class="text-sm text-gray-600">{{ __('Refund (before tax)') }}</span>
					<span class="text-lg font-bold text-red-600">{{ formatCurrency(subtotal) }}</span>
				</div>

				<p v-if="errorMessage" class="text-sm text-red-600">{{ errorMessage }}</p>
			</div>
		</template>
		<template #actions>
			<div class="flex justify-end gap-2">
				<Button variant="subtle" @click="showDialog = false">{{ __('Cancel') }}</Button>
				<Button variant="solid" theme="red" :loading="submitting" :disabled="!canSubmit" @click="submitRefund">
					{{ __('Refund') }}
				</Button>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
/**
 * Refund without a receipt (POS Role Rule "Refund Without Receipt"): a return Sales
 * Invoice with no original sale that puts the stock back in the till's warehouse.
 * The server refuses it for users whose rules do not allow it. No loyalty points are
 * given or reversed (there is no original sale).
 */
import { useOfflineStatus } from "@/composables/useOfflineStatus"
import { useToast } from "@/composables/useToast"
import { useItemSearchStore } from "@/stores/itemSearch"
import { usePOSCartStore } from "@/stores/posCart"
import { usePOSShiftStore } from "@/stores/posShift"
import { formatCurrency as formatCurrencyUtil, roundCurrency } from "@/utils/currency"
import { Button, Dialog, FeatherIcon, call, createResource } from "frappe-ui"
import { computed, ref, watch } from "vue"

const LOYALTY_MODE_OF_PAYMENT = "Redeem Points"

const props = defineProps({
	modelValue: Boolean,
	posProfile: String,
	posOpeningShift: String,
	currency: { type: String, default: "GBP" },
})
const emit = defineEmits(["update:modelValue", "refund-created"])

const showDialog = computed({
	get: () => props.modelValue,
	set: (val) => emit("update:modelValue", val),
})

const { showSuccess } = useToast()
const { isOffline } = useOfflineStatus()
const itemStore = useItemSearchStore()
const cartStore = usePOSCartStore()
const shiftStore = usePOSShiftStore()

const search = ref("")
const rows = ref([])
const refundMode = ref("")
const paymentMethods = ref([])
const submitting = ref(false)
const errorMessage = ref("")

const customer = computed(() => {
	const value = cartStore.customer
	return (value && (value.name || value)) || shiftStore.profileCustomer || ""
})
const customerName = computed(() => cartStore.customer?.customer_name || customer.value)

const refundMethods = computed(() =>
	paymentMethods.value.filter((method) => method.mode_of_payment !== LOYALTY_MODE_OF_PAYMENT),
)

const loadPaymentMethods = createResource({
	url: "frappe.client.get",
	makeParams: () => ({ doctype: "POS Profile", name: props.posProfile }),
	onSuccess(profile) {
		paymentMethods.value = profile?.payments || []
		const preferred = refundMethods.value.find((m) => m.default) || refundMethods.value[0]
		refundMode.value = preferred?.mode_of_payment || ""
	},
})

watch(showDialog, (open) => {
	if (!open) return
	search.value = ""
	rows.value = []
	errorMessage.value = ""
	if (props.posProfile) loadPaymentMethods.reload()
})

const matches = computed(() => {
	const term = search.value.trim().toLowerCase()
	if (term.length < 2) return []
	return (itemStore.allItems || [])
		.filter((item) => `${item.item_code} ${item.item_name || ""}`.toLowerCase().includes(term))
		.slice(0, 8)
})

function itemPrice(item) {
	return Number(item.price_list_rate || item.rate) || 0
}

function addItem(item) {
	const existing = rows.value.find((row) => row.item_code === item.item_code)
	if (existing) {
		existing.qty += 1
	} else {
		rows.value.push({
			item_code: item.item_code,
			item_name: item.item_name || item.item_code,
			uom: item.uom || item.stock_uom,
			stock_uom: item.stock_uom,
			rate: itemPrice(item),
			qty: 1,
		})
	}
	search.value = ""
}

const lineTotal = (row) => roundCurrency((Number(row.qty) || 0) * row.rate)
const subtotal = computed(() => roundCurrency(rows.value.reduce((sum, row) => sum + lineTotal(row), 0)))

const canSubmit = computed(
	() =>
		!isOffline.value &&
		!submitting.value &&
		rows.value.length > 0 &&
		rows.value.every((row) => Number(row.qty) > 0) &&
		Boolean(refundMode.value) &&
		Boolean(customer.value),
)

function formatCurrency(amount) {
	return formatCurrencyUtil(Number.parseFloat(amount || 0), props.currency)
}

async function submitRefund() {
	if (!canSubmit.value) return
	submitting.value = true
	errorMessage.value = ""
	let draftName = null
	try {
		// Step 1: the draft works out tax and totals on the server
		const draft = await call("pos_next.api.invoices.update_invoice", {
			data: {
				doctype: "Sales Invoice",
				is_pos: 1,
				is_return: 1,
				update_stock: 1,
				pos_profile: props.posProfile,
				posa_pos_opening_shift: props.posOpeningShift,
				company: shiftStore.profileCompany,
				customer: customer.value,
				items: rows.value.map((row) => ({
					item_code: row.item_code,
					qty: -Math.abs(Number(row.qty)),
					uom: row.uom,
					stock_uom: row.stock_uom,
					conversion_factor: 1,
					rate: row.rate,
					price_list_rate: row.rate,
					warehouse: shiftStore.profileWarehouse,
				})),
				payments: [{ mode_of_payment: refundMode.value, amount: -subtotal.value }],
			},
		})
		draftName = draft?.name
		// Step 2: refund the whole return total (tax included) by the chosen method
		const refundRow = (draft.payments || []).find((p) => p.mode_of_payment === refundMode.value) || {
			mode_of_payment: refundMode.value,
		}
		const result = await call("pos_next.api.invoices.submit_invoice", {
			invoice: { ...draft, payments: [{ ...refundRow, amount: draft.grand_total }] },
			data: {},
		})
		draftName = null
		showSuccess(__("Refund {0} created", [result.name]))
		emit("refund-created", result)
		showDialog.value = false
	} catch (error) {
		errorMessage.value = error?.messages?.[0] || error?.message || __("Refund failed")
		if (draftName) {
			// Do not leave a half-made refund behind
			call("pos_next.api.invoices.delete_invoice", { invoice: draftName }).catch(() => {})
		}
	} finally {
		submitting.value = false
	}
}
</script>
