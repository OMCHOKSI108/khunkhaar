"""Visual Baseline and Screenshot Comparison Engine."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from playwright.async_api import Page


def compute_image_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


async def compare_page_with_baseline(
    page: Page,
    baseline_path: Path,
    output_diff_path: Path | None = None,
    threshold: float = 0.02,
) -> dict[str, Any]:
    """Captures current page screenshot and compares against stored baseline image."""
    current_bytes = await page.screenshot(full_page=False)
    current_hash = compute_image_hash(current_bytes)

    if not baseline_path.exists():
        # First run: establish baseline
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_bytes(current_bytes)
        return {
            "status": "baseline_created",
            "diff_percentage": 0.0,
            "baseline_hash": current_hash,
            "current_hash": current_hash,
            "is_regression": False,
        }

    baseline_bytes = baseline_path.read_bytes()
    baseline_hash = compute_image_hash(baseline_bytes)

    if current_hash == baseline_hash:
        return {
            "status": "identical",
            "diff_percentage": 0.0,
            "baseline_hash": baseline_hash,
            "current_hash": current_hash,
            "is_regression": False,
        }

    # Evaluate perceptual pixel mismatch using browser HTML5 canvas
    diff_stats = await page.evaluate(
        """async ([imgABytes, imgBBytes]) => {
          const loadImg = (b64) => new Promise((resolve) => {
            const img = new Image();
            img.onload = () => resolve(img);
            img.src = 'data:image/png;base64,' + b64;
          });

          try {
            const imgA = await loadImg(imgABytes);
            const imgB = await loadImg(imgBBytes);
            const w = Math.min(imgA.width, imgB.width);
            const h = Math.min(imgA.height, imgB.height);
            if (w === 0 || h === 0) return {diff_pct: 1.0, mismatch: true};

            const canvas = document.createElement('canvas');
            canvas.width = w; canvas.height = h;
            const ctx = canvas.getContext('2d');

            ctx.drawImage(imgA, 0, 0);
            const dataA = ctx.getImageData(0, 0, w, h).data;
            ctx.clearRect(0, 0, w, h);
            ctx.drawImage(imgB, 0, 0);
            const dataB = ctx.getImageData(0, 0, w, h).data;

            let diffCount = 0;
            const totalPixels = w * h;
            for (let i = 0; i < dataA.length; i += 4) {
              const diff = Math.abs(dataA[i] - dataB[i]) + Math.abs(dataA[i+1] - dataB[i+1]) + Math.abs(dataA[i+2] - dataB[i+2]);
              if (diff > 30) diffCount++;
            }

            return {diff_pct: diffCount / totalPixels, mismatch: (diffCount / totalPixels) > 0.01};
          } catch (e) {
            return {diff_pct: 0.05, mismatch: true};
          }
        }""",
        [
            __import__("base64").b64encode(baseline_bytes).decode("ascii"),
            __import__("base64").b64encode(current_bytes).decode("ascii"),
        ],
    )

    diff_pct = float(diff_stats.get("diff_pct", 0.0))
    is_regression = diff_pct > threshold

    if is_regression and output_diff_path:
        output_diff_path.parent.mkdir(parents=True, exist_ok=True)
        output_diff_path.write_bytes(current_bytes)

    return {
        "status": "regression" if is_regression else "within_tolerance",
        "diff_percentage": round(diff_pct * 100, 2),
        "baseline_hash": baseline_hash,
        "current_hash": current_hash,
        "is_regression": is_regression,
    }
