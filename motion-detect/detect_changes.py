#!/usr/bin/env python3
"""Detect large visual changes in mostly-static screen recordings.

Designed for computer-screen capture: ignores mouse / caret / clock noise
via block-based scoring, and uses a STABLE/CHANGING state machine so that
animation / scroll / fade is reported as one event after the frame settles.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def find_tool(name: str) -> str:
    """Prefer project-local portable tools, then PATH."""
    exts = [".exe", ""] if os.name == "nt" else [""]
    local_dirs = [
        ROOT / "tools" / "ffmpeg" / "bin",
        ROOT / "tools" / "ffmpeg",
    ]
    for folder in local_dirs:
        for ext in exts:
            candidate = folder / f"{name}{ext}"
            if candidate.is_file():
                return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"{name} not found. Run setup.bat (Windows) or install ffmpeg into PATH / tools/ffmpeg/bin"
    )


def probe_size(path: str) -> tuple[int, int]:
    out = subprocess.check_output(
        [
            find_tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            path,
        ]
    ).decode()
    parts = out.strip().split(",")
    if len(parts) < 2:
        raise RuntimeError(f"ffprobe returned unexpected size output: {out!r}")
    w, h = int(parts[0]), int(parts[1])
    if w <= 0 or h <= 0:
        raise RuntimeError(f"ffprobe returned invalid size: {w}x{h}")
    return w, h


def iter_frames(path: str, fps: float, width: int):
    if fps <= 0:
        raise ValueError("fps must be > 0")
    if width <= 0:
        raise ValueError("width must be > 0")

    w0, h0 = probe_size(path)
    height = int(round(h0 * width / w0 / 2) * 2)
    if height <= 0:
        raise RuntimeError(f"invalid analysis height computed from {w0}x{h0}")

    cmd = [
        find_tool("ffmpeg"),
        "-v",
        "error",
        "-i",
        path,
        "-vf",
        f"fps={fps},scale={width}:{height}:flags=area,format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    assert proc.stderr is not None
    stderr_chunks: list[bytes] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        try:
            stderr_chunks.append(proc.stderr.read())
        except OSError:
            # Closed by the main thread after wait/join timeout.
            pass

    # Drain stderr concurrently so a chatty ffmpeg cannot deadlock on PIPE.
    drain = threading.Thread(target=_drain_stderr, daemon=True)
    drain.start()
    frame_bytes = width * height
    i = 0
    read_error: Exception | None = None
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(height, width)
            frame = cv2.GaussianBlur(frame, (3, 3), 0)
            yield i / fps, frame
            i += 1
    except Exception as exc:  # pragma: no cover - surfaced after cleanup
        read_error = exc
        raise
    finally:
        proc.stdout.close()
        # Wait for ffmpeg exit before closing stderr so the drain thread can
        # finish its blocking read under normal completion.
        ret = proc.wait()
        drain.join(timeout=5)
        if drain.is_alive():
            # Last resort: closing stderr may unblock a stuck reader.
            if proc.stderr is not None:
                proc.stderr.close()
            drain.join(timeout=1)
        elif proc.stderr is not None:
            proc.stderr.close()
        if read_error is None and ret != 0:
            stderr = b"".join(stderr_chunks)
            msg = stderr.decode(errors="replace").strip() or "no stderr"
            raise RuntimeError(f"ffmpeg failed while decoding frames (exit {ret}): {msg}")


def load_mask(mask_path: str | None, shape: tuple[int, int]) -> np.ndarray | None:
    if not mask_path:
        return None
    mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
    if mask is None:
        raise SystemExit(f"failed to read mask: {mask_path}")
    if mask.shape != shape:
        mask = cv2.resize(mask, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
    return (mask > 127).astype(np.float32)


def block_score(
    a: np.ndarray,
    b: np.ndarray,
    *,
    block: int,
    pix_t: float,
    block_t: float,
    mask: np.ndarray | None,
) -> float:
    if block <= 0:
        raise ValueError("block must be > 0")
    diff = (cv2.absdiff(a, b) > pix_t).astype(np.float32)
    if mask is not None:
        diff *= mask
    h, w = diff.shape
    h2, w2 = h // block * block, w // block * block
    if h2 == 0 or w2 == 0:
        return 0.0
    blocks = (
        diff[:h2, :w2]
        .reshape(h2 // block, block, w2 // block, block)
        .mean(axis=(1, 3))
    )
    return float((blocks > block_t).mean())


def _maybe_append_event(
    events: list[dict],
    *,
    start: float,
    settled: float,
    score_ref: float,
    high: float,
    min_gap: float,
    unsettled: bool = False,
    ignore_min_gap: bool = False,
) -> bool:
    """Append event if score/gap allow. Returns True when accepted.

    Gap is measured from the previous event's start to this event's settled
    time, so the original change onset (`start`) can be preserved while still
    waiting for min_gap to elapse before reporting.
    """
    if score_ref <= high:
        return False
    if (
        not ignore_min_gap
        and events
        and settled - events[-1]["start"] < min_gap
    ):
        return False
    ev: dict = {
        "start": round(start, 2),
        "settled": round(settled, 2),
        "score": round(score_ref, 3),
    }
    if unsettled:
        ev["unsettled"] = True
    events.append(ev)
    return True


def detect_frames(
    frames: Iterable[tuple[float, np.ndarray]],
    *,
    block: int,
    pix_t: float,
    block_t: float,
    high: float,
    low: float,
    settle: int,
    min_gap: float,
    mask_path: str | None = None,
    score_log: list[dict] | None = None,
) -> list[dict]:
    """Run the STABLE/CHANGING detector over an in-memory frame stream."""
    if block <= 0:
        raise ValueError("block must be > 0")
    if settle <= 0:
        raise ValueError("settle must be > 0")
    if min_gap < 0:
        raise ValueError("min_gap must be >= 0")

    events: list[dict] = []
    ref = prev = None
    mask = None
    state = "STABLE"
    start = 0.0
    calm = 0
    t = 0.0
    last_score_ref = 0.0
    # Settled lasting change held back by min_gap; keep original onset.
    pending_start: float | None = None
    pending_settled = 0.0
    pending_score = 0.0

    def clear_pending() -> None:
        nonlocal pending_start, pending_settled, pending_score
        pending_start = None
        pending_settled = 0.0
        pending_score = 0.0

    def remember_pending(onset: float, settled_at: float, score: float) -> None:
        nonlocal pending_start, pending_settled, pending_score
        pending_start = onset
        pending_settled = settled_at
        pending_score = score

    for t, frame in frames:
        if ref is None:
            ref = prev = frame
            mask = load_mask(mask_path, frame.shape)
            if score_log is not None:
                score_log.append(
                    {
                        "t": round(t, 3),
                        "vs_ref": 0.0,
                        "vs_prev": 0.0,
                        "state": state,
                    }
                )
            continue

        score_ref = block_score(
            ref, frame, block=block, pix_t=pix_t, block_t=block_t, mask=mask
        )
        score_prev = block_score(
            prev, frame, block=block, pix_t=pix_t, block_t=block_t, mask=mask
        )
        last_score_ref = score_ref

        if state == "STABLE":
            if pending_start is not None:
                if score_ref > high:
                    accepted = _maybe_append_event(
                        events,
                        start=pending_start,
                        settled=t,
                        score_ref=score_ref,
                        high=high,
                        min_gap=min_gap,
                    )
                    if accepted:
                        ref = frame
                        clear_pending()
                    elif score_prev >= low:
                        # Motion resumed while waiting for gap; keep onset.
                        state = "CHANGING"
                        start = pending_start
                        calm = 0
                        clear_pending()
                    else:
                        remember_pending(pending_start, t, score_ref)
                else:
                    # Drifted back to the old reference; discard deferred change.
                    clear_pending()
            elif score_ref > high:
                state = "CHANGING"
                start = t
                calm = 0
        else:
            calm = calm + 1 if score_prev < low else 0
            if calm >= settle:
                accepted = _maybe_append_event(
                    events,
                    start=start,
                    settled=t,
                    score_ref=score_ref,
                    high=high,
                    min_gap=min_gap,
                )
                if accepted:
                    ref = frame
                    state = "STABLE"
                    clear_pending()
                elif score_ref <= high:
                    # Settled back near the old reference; no lasting change.
                    state = "STABLE"
                    clear_pending()
                else:
                    # Large change suppressed by min_gap: keep old ref and the
                    # original onset until the gap window opens (or EOF).
                    remember_pending(start, t, score_ref)
                    state = "STABLE"

        if score_log is not None:
            score_log.append(
                {
                    "t": round(t, 3),
                    "vs_ref": round(score_ref, 4),
                    "vs_prev": round(score_prev, 4),
                    "state": state,
                }
            )
        prev = frame

    def _emit_eof_event(
        *,
        onset: float,
        settled_at: float,
        score: float,
        unsettled: bool,
        label: str,
    ) -> None:
        accepted = _maybe_append_event(
            events,
            start=onset,
            settled=settled_at,
            score_ref=score,
            high=high,
            min_gap=min_gap,
            unsettled=unsettled,
        )
        if accepted:
            return
        # Last chance: with ignore_min_gap, append succeeds whenever score > high.
        if score > high:
            _maybe_append_event(
                events,
                start=onset,
                settled=settled_at,
                score_ref=score,
                high=high,
                min_gap=min_gap,
                unsettled=unsettled,
                ignore_min_gap=True,
            )
            print(
                f"warning: {label} starting at {onset:.2f}s emitted at EOF "
                f"despite min_gap={min_gap} (score={score:.3f})",
                file=sys.stderr,
            )
        else:
            print(
                f"warning: video ended while changing from {onset:.2f}s but final "
                f"score={score:.3f} <= high={high}",
                file=sys.stderr,
            )

    if ref is not None and prev is not None:
        if state == "CHANGING":
            _emit_eof_event(
                onset=start,
                settled_at=t,
                score=last_score_ref,
                unsettled=True,
                label="unsettled change",
            )
        elif pending_start is not None:
            _emit_eof_event(
                onset=pending_start,
                settled_at=pending_settled,
                score=pending_score,
                unsettled=False,
                label="deferred settled change",
            )

    return events


def detect(
    path: str,
    *,
    fps: float,
    width: int,
    block: int,
    pix_t: float,
    block_t: float,
    high: float,
    low: float,
    settle: int,
    min_gap: float,
    mask_path: str | None,
    score_log: list[dict] | None,
) -> list[dict]:
    return detect_frames(
        iter_frames(path, fps, width),
        block=block,
        pix_t=pix_t,
        block_t=block_t,
        high=high,
        low=low,
        settle=settle,
        min_gap=min_gap,
        mask_path=mask_path,
        score_log=score_log,
    )


def export_frames(video: str, events: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, ev in enumerate(events, 1):
        t = ev["settled"]
        out = out_dir / f"event_{i:04d}_{t:.2f}s.png"
        # Accurate seek: -ss after -i (slower, better keyframe timing).
        subprocess.check_call(
            [
                find_tool("ffmpeg"),
                "-v",
                "error",
                "-i",
                video,
                "-ss",
                str(t),
                "-frames:v",
                "1",
                "-y",
                str(out),
            ]
        )
        ev["frame"] = str(out)


def write_score_csv(score_log: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["t", "vs_ref", "vs_prev", "state"])
        writer.writeheader()
        writer.writerows(score_log)


def write_score_plot(score_log: list[dict], events: list[dict], path: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skip plot (CSV still written)", file=sys.stderr)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    ts = [r["t"] for r in score_log]
    vs_ref = [r["vs_ref"] for r in score_log]
    vs_prev = [r["vs_prev"] for r in score_log]

    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(ts, vs_ref, label="vs_ref", linewidth=1.0)
    ax.plot(ts, vs_prev, label="vs_prev", linewidth=0.8, alpha=0.7)
    for ev in events:
        ax.axvspan(ev["start"], ev["settled"], color="orange", alpha=0.25)
        ax.axvline(ev["settled"], color="red", linewidth=0.8, alpha=0.8)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("changed-block ratio")
    ax.set_title("Screen change scores")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Detect large screen changes in mostly-static recordings."
    )
    p.add_argument("video", help="input video path")
    p.add_argument(
        "-o",
        "--output",
        default="-",
        help="JSON output path (default: stdout)",
    )
    p.add_argument("--fps", type=float, default=4.0, help="sample fps (default: 4)")
    p.add_argument(
        "--width", type=int, default=480, help="analysis width (default: 480)"
    )
    p.add_argument("--block", type=int, default=16, help="block size (default: 16)")
    p.add_argument(
        "--pix-t", type=float, default=25.0, help="pixel diff threshold (default: 25)"
    )
    p.add_argument(
        "--block-t",
        type=float,
        default=0.03,
        help="block change ratio threshold (default: 0.03)",
    )
    p.add_argument(
        "--high",
        type=float,
        default=0.20,
        help="large-change threshold (default: 0.20)",
    )
    p.add_argument(
        "--low",
        type=float,
        default=0.01,
        help="settled threshold vs previous frame (default: 0.01)",
    )
    p.add_argument(
        "--settle",
        type=int,
        default=3,
        help="consecutive calm frames to confirm settle (default: 3)",
    )
    p.add_argument(
        "--min-gap",
        type=float,
        default=1.0,
        help="minimum seconds between events (default: 1.0)",
    )
    p.add_argument(
        "--mask",
        default=None,
        help="optional grayscale mask; black=ignore, white=include",
    )
    p.add_argument(
        "--export-frames",
        metavar="DIR",
        default=None,
        help="export settled-frame PNG screenshots into DIR",
    )
    p.add_argument(
        "--score-csv",
        metavar="PATH",
        default=None,
        help="write per-frame scores to CSV for threshold tuning",
    )
    p.add_argument(
        "--score-plot",
        metavar="PATH",
        default=None,
        help="write score timeline PNG (needs matplotlib)",
    )
    return p


def validate_args(args: argparse.Namespace) -> str | None:
    if args.fps <= 0:
        return "--fps must be > 0"
    if args.width <= 0:
        return "--width must be > 0"
    if args.block <= 0:
        return "--block must be > 0"
    if args.settle <= 0:
        return "--settle must be > 0"
    if args.min_gap < 0:
        return "--min-gap must be >= 0"
    if args.pix_t < 0:
        return "--pix-t must be >= 0"
    if not (0.0 <= args.block_t <= 1.0):
        return "--block-t must be in [0, 1]"
    if not (0.0 <= args.low <= 1.0):
        return "--low must be in [0, 1]"
    if not (0.0 <= args.high <= 1.0):
        return "--high must be in [0, 1]"
    if args.low > args.high:
        return "--low must be <= --high"
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    err = validate_args(args)
    if err:
        print(err, file=sys.stderr)
        return 2

    video = args.video
    if not Path(video).is_file():
        print(f"video not found: {video}", file=sys.stderr)
        return 1

    score_log: list[dict] | None = (
        [] if (args.score_csv or args.score_plot) else None
    )
    try:
        events = detect(
            video,
            fps=args.fps,
            width=args.width,
            block=args.block,
            pix_t=args.pix_t,
            block_t=args.block_t,
            high=args.high,
            low=args.low,
            settle=args.settle,
            min_gap=args.min_gap,
            mask_path=args.mask,
            score_log=score_log,
        )

        if args.export_frames:
            export_frames(video, events, Path(args.export_frames))

        if score_log is not None:
            if args.score_csv:
                write_score_csv(score_log, Path(args.score_csv))
            if args.score_plot:
                write_score_plot(score_log, events, Path(args.score_plot))
    except (
        FileNotFoundError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
        OSError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload = {
        "video": video,
        "params": {
            "fps": args.fps,
            "width": args.width,
            "block": args.block,
            "pix_t": args.pix_t,
            "block_t": args.block_t,
            "high": args.high,
            "low": args.low,
            "settle": args.settle,
            "min_gap": args.min_gap,
            "mask": args.mask,
        },
        "count": len(events),
        "events": events,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output == "-":
        print(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output} ({len(events)} events)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
