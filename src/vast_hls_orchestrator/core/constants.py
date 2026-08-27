"""Shared constants: API endpoints, GPU allow-list, and HLS rendition names."""

API_BASE = "https://console.vast.ai/api/v0"
API_V1_BASE = "https://console.vast.ai/api/v1"
BAD_STATES = {"destroyed", "error", "exited", "failed", "offline", "unknown"}

# How many concurrent NVENC hardware encode sessions each GPU actually has.
# This is what the ABR pipeline's 4 parallel encode branches (one shared
# NVDEC decode, split into 1080p/720p/480p/360p, see remote/job_script.py)
# can actually make use of -- a GPU with more NVENC engines runs those 4
# branches with real hardware parallelism instead of time-slicing one engine.
# Cards not listed here (e.g. A100/H100/B200) have no NVENC at all and must
# never be searched for. Unknown/unlisted GPUs default to 1 (conservative).
GPU_NVENC_SESSIONS = {
    "RTX 5090": 3,
    "L40S": 3,
    "RTX 4090": 2,
    "L4": 2,
    "RTX 5080": 2,
    "RTX 5070 Ti": 2,
    "A16": 4,  # 4 physical dies on one board, ~1 encoder each
    "RTX 3060": 1,
    "RTX 3090": 1,  # far more CUDA cores than 3060, but the same single NVENC
}

# Allow-list for offer search: every card with a reasonable NVENC story for
# ABR transcoding. RTX 3090 and pure-compute cards (A100/H100/B200 -- no
# NVENC hardware at all) are deliberately excluded.
DEFAULT_GPUS = [
    "RTX 5090",
    "L40S",
    "RTX 4090",
    "L4",
    "RTX 5080",
    "RTX 5070 Ti",
    "A16",
    "RTX 3060",
]

RENDITIONS = ["1080p", "720p", "480p", "360p"]
