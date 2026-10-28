"""PyInstaller entry point for Beam.exe (see .github/workflows/build.yml)."""
import sys

from multidrop.windows import main

sys.exit(main())
