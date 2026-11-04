# motion-detect

检测电脑屏幕录像里「大部分时间几乎不变，但偶尔大幅变动」的时间点。

适用于：录屏回放、自动截关键帧、跳过静止段等场景。对鼠标移动、光标闪烁、时钟等小变化不敏感。

## 依赖

- Python 3.10+
- `ffmpeg` / `ffprobe`（PATH，或项目内 `tools/ffmpeg/bin`）
- Python 包：`numpy` / `opencv-python-headless` / `matplotlib`

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 需自行安装 ffmpeg（如 brew install ffmpeg）
```

### Windows（推荐）

1. 双击 `setup.bat`：自动创建 `.venv`、安装依赖、下载便携 ffmpeg 到 `tools\\ffmpeg\\bin`（优先 GitHub 固定版本并校验 SHA256）
2. 把视频拖到 `detect.bat` 上（或双击后输入路径）
3. 结果会写到 `output\\<视频名>\\`（`events.json`、`frames\\`、`scores.png`）

> `detect.bat` 若发现环境缺失，会自动先跑 `setup.bat --no-pause`。

## 用法（命令行）

```bash
# 基本检测，结果打到 stdout
python detect_changes.py rec.mp4

# 写出 JSON + 导出稳定后关键帧 + 分数曲线（调参用）
python detect_changes.py rec.mp4 \
  -o events.json \
  --export-frames frames/ \
  --score-csv scores.csv \
  --score-plot scores.png

# 只关心整页级切换，提高阈值
python detect_changes.py rec.mp4 --high 0.45

# 忽略任务栏/时钟：准备一张同分辨率灰度 mask（白=检测，黑=忽略）
python detect_changes.py rec.mp4 --mask mask.png
```

## 输出字段

每个事件：

| 字段 | 含义 |
|------|------|
| `start` | 相对参考画面开始大幅变化的时间（秒） |
| `settled` | 新画面重新稳定的时间（秒），截图建议用这个 |
| `score` | 稳定后相对旧参考画面的变化块占比 |
| `unsettled` | 可选；视频在 CHANGING 中结束时为 `true`，此时 `settled` 取最后一帧时间 |
| `frame` | 若指定 `--export-frames`，为导出的 PNG 路径 |

## 调参建议

1. 先跑一遍带 `--score-csv` / `--score-plot` 的检测，看 `vs_ref` 曲线分布。
2. 打字、鼠标移动通常 `vs_ref < 0.05`；翻页/切窗口通常 `> 0.3`。
3. 最关键参数是 `--high`（默认 `0.20`）。
4. 动画较长时增大 `--settle`（默认 3 帧 @4fps ≈ 0.75s）。
5. 速度不够可降 `--fps 2` 或减小 `--width`。
6. `--min-gap` 抑制过密事件时**不会**更新参考帧，间隔满足后仍会补报（避免中间态丢失）。

## 测试

```bash
python -m unittest discover -s tests -v
```

## 算法简述

1. ffmpeg 抽低帧率灰度缩略帧。
2. 按网格分块，统计「变化块占比」。
3. STABLE → CHANGING →（连续 calm 帧）确认事件 → 更新参考帧。
