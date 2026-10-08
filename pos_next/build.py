"""
Build hooks for POS Next frontend compilation.
Runs during 'bench build --app pos_next' to compile Vite assets.
"""
import os
import subprocess
import sys


def build_pos_frontend():
	"""
	Build POS Next frontend assets using Vite.
	This hook runs when 'bench build --app pos_next' is executed during deployment.
	Compiles Vue/TypeScript code to JavaScript and generates the pos.html entry point.
	"""
	try:
		# Navigate to the POS directory where vite.config.js is located
		# __file__ is pos_next/build.py, parent is pos_next/, grandparent is the app root
		app_root = os.path.dirname(os.path.dirname(__file__))
		pos_dir = os.path.join(app_root, "POS")

		if not os.path.exists(pos_dir):
			print(f"⚠️  POS directory not found at {pos_dir}, skipping frontend build")
			return

		print(f"\n🔨 Building POS Next frontend assets...")
		print(f"   Running: cd {pos_dir} && yarn build")

		# Run yarn build in the POS directory
		result = subprocess.run(
			["yarn", "build"],
			cwd=pos_dir,
			check=False
		)

		if result.returncode == 0:
			print("✓ POS Next frontend assets built successfully\n")
		else:
			print(f"✗ Frontend build failed with exit code {result.returncode}\n", file=sys.stderr)
			sys.exit(1)

	except FileNotFoundError:
		print("✗ yarn command not found. Ensure Node.js and yarn are installed.\n", file=sys.stderr)
		sys.exit(1)
	except Exception as e:
		print(f"✗ Frontend build error: {str(e)}\n", file=sys.stderr)
		sys.exit(1)
