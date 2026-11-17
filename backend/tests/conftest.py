"""Point the app at a throwaway data directory and a known admin before anything imports it."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="researchx-test-")
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-pass"
os.environ["TRUST_PROXY"] = "true"
