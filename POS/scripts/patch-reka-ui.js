#!/usr/bin/env node
/**
 * Patch reka-ui DialogOverlay to remove the broken "prevent" modifier
 * on its onPointerdown handler.
 *
 * Why this exists:
 *   reka-ui 2.9.8's DialogOverlayImpl registers:
 *     withModifiers(() => {}, ["left","prevent"])
 *   The "prevent" modifier calls event.preventDefault() on every pointerdown
 *   over the overlay, which breaks left-click interactions inside dialogs.
 *
 * How this works:
 *   Runs as a yarn `postinstall` script. After `yarn install` places reka-ui
 *   under node_modules, we rewrite the two overlay files (ESM .js + CJS .cjs)
 *   on disk so the subsequent Vite builds read the already-fixed source.
 *   Idempotent — safe to run on every install.
 *
 * Why NOT a Vite plugin:
 *   Our previous Vite plugins (transform, renderChunk) silently fail to fire
 *   on Frappe Cloud. Patching on disk during `yarn install` sidesteps the
 *   Vite plugin pipeline entirely.
 */
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

const PACKAGE_ROOT = path.resolve(__dirname, "..")
const REKA_DIR = path.join(PACKAGE_ROOT, "node_modules", "reka-ui")
const PACKAGE_JSON = path.join(REKA_DIR, "package.json")

// Two files to patch. Note the different whitespace between them: the ESM
// build has no space after the comma, the CJS build has one. The regex
// below tolerates optional whitespace around the comma.
const TARGETS = [
	path.join(REKA_DIR, "dist", "Dialog", "DialogOverlayImpl.js"),
	path.join(REKA_DIR, "dist", "Dialog", "DialogOverlayImpl.cjs"),
]

// Matches ["left","prevent"] or ["left", "prevent"] with any whitespace
// between the two string literals.
const BROKEN_PATTERN = /\["left"\s*,\s*"prevent"\]/g
const FIXED_LITERAL = '["left"]'

const STATUS = {
	PATCHED: "patched",
	ALREADY_CLEAN: "already-clean",
	MISSING: "missing",
	UNEXPECTED: "unexpected",
}

function log(level, msg) {
	const prefix = "[patch-reka-ui]"
	if (level === "error") console.error(`${prefix} ${msg}`)
	else console.log(`${prefix} ${msg}`)
}

function detectInstalledVersion() {
	try {
		const pkg = JSON.parse(fs.readFileSync(PACKAGE_JSON, "utf8"))
		return pkg.version || "unknown"
	} catch (e) {
		return null
	}
}

function patchFile(filePath) {
	const relPath = path.relative(PACKAGE_ROOT, filePath)
	if (!fs.existsSync(filePath)) {
		return { file: relPath, status: STATUS.MISSING }
	}
	const original = fs.readFileSync(filePath, "utf8")

	// Does the file have the broken pattern?
	BROKEN_PATTERN.lastIndex = 0
	const matches = original.match(BROKEN_PATTERN)
	if (matches && matches.length > 0) {
		const patched = original.replace(BROKEN_PATTERN, FIXED_LITERAL)
		fs.writeFileSync(filePath, patched, "utf8")
		return {
			file: relPath,
			status: STATUS.PATCHED,
			occurrences: matches.length,
		}
	}

	// Not broken. Is it already fixed (handler present with the "left"-only form)?
	const alreadyFixedSignature = /onPointerdown[\s\S]*?withModifiers[\s\S]*?\["left"\]/
	if (alreadyFixedSignature.test(original)) {
		return { file: relPath, status: STATUS.ALREADY_CLEAN }
	}

	return { file: relPath, status: STATUS.UNEXPECTED }
}

function main() {
	if (!fs.existsSync(REKA_DIR)) {
		log(
			"info",
			`reka-ui not installed under ${path.relative(PACKAGE_ROOT, REKA_DIR)} — skipping (nothing to patch).`,
		)
		return 0
	}

	const version = detectInstalledVersion()
	log("info", `reka-ui version detected: ${version || "unknown"}`)

	const results = TARGETS.map(patchFile)
	let patchedCount = 0
	let unexpectedCount = 0

	for (const r of results) {
		switch (r.status) {
			case STATUS.PATCHED:
				log("info", `✓ Patched ${r.file} (${r.occurrences} occurrence${r.occurrences === 1 ? "" : "s"})`)
				patchedCount++
				break
			case STATUS.ALREADY_CLEAN:
				log("info", `- Already clean: ${r.file}`)
				break
			case STATUS.MISSING:
				log("info", `- Not present (ok to skip): ${r.file}`)
				break
			case STATUS.UNEXPECTED:
				log(
					"error",
					`! Unexpected content in ${r.file} — neither the broken pattern nor the known fixed signature matched. ` +
						`reka-ui's internal structure may have changed and this patch may need to be updated.`,
				)
				unexpectedCount++
				break
		}
	}

	log("info", `done — ${patchedCount} file(s) patched, ${unexpectedCount} with unexpected content`)
	// Non-zero only on unexpected content so an upstream change surfaces loudly.
	return unexpectedCount > 0 ? 1 : 0
}

process.exit(main())
