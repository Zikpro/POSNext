/**
 * Short error beep for unknown barcodes, built with the Web Audio API.
 *
 * Browsers only let a page make sound after a user gesture. The context is
 * created on the first pointer or key press, and a beep played before that
 * (or while the browser keeps the context suspended) is skipped silently.
 * The visual barcode message does not depend on this module.
 */

const BEEP_FREQUENCY_HZ = 880
const BEEP_DURATION_S = 0.3
const BEEP_PEAK_GAIN = 0.5
/** Rapid bad scans share one beep: nothing plays again until this many ms later. */
export const BEEP_MIN_INTERVAL_MS = 500

let audioContext = null
let lastBeepAt = -Infinity
let unlockListening = false

function getAudioContext() {
	if (audioContext) return audioContext
	const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext
	if (!AudioContextClass) return null
	audioContext = new AudioContextClass()
	return audioContext
}

function resumeAudioContext() {
	try {
		const context = getAudioContext()
		if (context?.state === "suspended") {
			context.resume()?.catch(() => {})
		}
	} catch {
		// Audio unavailable: the visual message still shows.
	}
}

/** Creates or resumes the audio context on the first user gesture. Safe to call more than once. */
export function unlockBarcodeBeep() {
	if (unlockListening || typeof window === "undefined") return
	unlockListening = true
	window.addEventListener("pointerdown", resumeAudioContext, { capture: true, passive: true })
	window.addEventListener("keydown", resumeAudioContext, { capture: true })
}

/**
 * Plays one short beep. Returns true when a beep was started.
 * Skipped when the previous beep is less than BEEP_MIN_INTERVAL_MS old, or when audio is not running.
 */
export function playBarcodeErrorBeep(now = Date.now()) {
	if (now - lastBeepAt < BEEP_MIN_INTERVAL_MS) return false

	try {
		const context = getAudioContext()
		if (!context || context.state !== "running") return false

		const oscillator = context.createOscillator()
		const gain = context.createGain()
		const start = context.currentTime
		const end = start + BEEP_DURATION_S

		oscillator.type = "sine"
		oscillator.frequency.setValueAtTime(BEEP_FREQUENCY_HZ, start)
		gain.gain.setValueAtTime(0.0001, start)
		gain.gain.exponentialRampToValueAtTime(BEEP_PEAK_GAIN, start + 0.01)
		gain.gain.exponentialRampToValueAtTime(0.0001, end)

		oscillator.connect(gain)
		gain.connect(context.destination)
		oscillator.start(start)
		oscillator.stop(end)

		lastBeepAt = now
		return true
	} catch {
		return false
	}
}

/** Test-only: forgets the throttle and the unlock state so each test starts clean. */
export function resetBarcodeBeepForTests() {
	audioContext = null
	lastBeepAt = -Infinity
	unlockListening = false
}
