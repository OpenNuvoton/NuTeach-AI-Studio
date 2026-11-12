"""Local image/audio/anomalous-sound studio with strict TensorFlow Lite export."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")

from .path_limits import use_ascii_temp_dir

use_ascii_temp_dir(Path(__file__).resolve().parent.parent / "workspace" / "tmp")

__version__ = "2.1.1"
