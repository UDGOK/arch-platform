# Vercel Serverless Entry Point
# Wraps the FastAPI app for Vercel's Python runtime

import sys
from pathlib import Path

# Add api directory to path
sys.path.insert(0, str(Path(__file__).parent))

from server import app

# Vercel Python runtime expects 'app' as the handler
app = app
