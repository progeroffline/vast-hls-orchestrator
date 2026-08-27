"""Shared constants: API endpoints, GPU allow-list, and HLS rendition names."""

API_BASE = "https://console.vast.ai/api/v0"
API_V1_BASE = "https://console.vast.ai/api/v1"
BAD_STATES = {"destroyed", "error", "exited", "failed", "offline", "unknown"}
DEFAULT_GPUS = ["RTX 3060", "RTX A2000", "RTX 4060"]
RENDITIONS = ["1080p", "720p", "480p", "360p"]
