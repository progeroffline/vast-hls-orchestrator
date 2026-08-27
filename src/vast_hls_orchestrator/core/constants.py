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
    "RTX 4080": 2,
    "L4": 2,
    "RTX 5080": 2,
    "RTX 5070 Ti": 2,
    "A16": 4,  # 4 physical dies on one board, ~1 encoder each
    "RTX 3060": 1,
    "RTX 3090": 1,  # far more CUDA cores than 3060, but the same single NVENC
}

# Allow-list for offer search: RTX 4080 only. Benchmarked directly against
# this project's exact single-process ABR pipeline (1080p+720p+480p+360p,
# preset p3): ~11.5x realtime at ~90% NVENC utilization, vs. ~7.8x on L40S
# for the same job -- so no other GPU (L4/L40/L40S/RTX 4090/A100/H100/etc.)
# is used as a fallback here, even though several of them are still listed
# in GPU_NVENC_SESSIONS above from when the allow-list was broader.
DEFAULT_GPUS = [
    "RTX 4080",
]

RENDITIONS = ["1080p", "720p", "480p", "360p"]

# Docker flag-format string, not a JSON object -- that's what PUT /asks/<id>/
# actually expects for `env` (see docs.vast.ai/api-reference/instances/create-instance).
# "all" here matches the exact value validated on real GPU hardware via the
# "HLS Transcoder" Vast template this image ships with (see README); the
# image's own Dockerfile ENV only sets NVIDIA_DRIVER_CAPABILITIES=compute,utility,
# which is missing the "video" capability NVENC/NVDEC need.
INSTANCE_ENV = "-e NVIDIA_DRIVER_CAPABILITIES=all -e NVIDIA_VISIBLE_DEVICES=all"
