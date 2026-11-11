# WMEW 99.9 FM — Mewgenics Radio Engine

A configurable radio broadcast generator, local player, and 24/7 livestreaming engine that recreates the in-game radio mode from *Mewgenics* (WMEW 99.9 FM — "99.9 Lives").

Features song intros/outros (exclusive OR), call letters, gamestart intros, commercial breaks with announcer bumpers, transitionals, Jimmy "The Hammer" Valentine segments, and dynamic anti-repeat shuffle pools for 24/7 continuous streaming.

---

## Running the Script (`wmew_radio.py`)

Run the script using Python 3:

```bash
python wmew_radio.py [OPTIONS...]
```

### 1. Operating Modes

| Flag | Argument | Description | Example |
| :--- | :--- | :--- | :--- |
| `--web` | *(none)* | **(Default mode)** Launches the zero-dependency web dashboard & player at `http://127.0.0.1:8080`. Play audio in browser, inspect live queue, skip tracks, and adjust probabilities with live sliders. | `python wmew_radio.py --web` |
| `--stream` | `<URL>` | Continuous 24/7 livestreaming engine via FFmpeg. Decodes audio gaplessly and pipes it into a persistent FFmpeg encoder pushing RTMP (YouTube/Twitch) or Icecast. | `python wmew_radio.py --stream "rtmp://..." --loop-visual still_image.jpg` |

---

### 2. General & Mode Options

| Flag | Argument | Default | Description |
| :--- | :--- | :--- | :--- |
| `-c`, `--config` | `<FILE>` | `config.json` | Path to custom configuration JSON file. |
| `--radio-dir` | `<DIR>` | *(from config)* | Path to directory containing audio subfolders (`songs`, `intros`, `outros`, etc.). |
| `--port` | `<PORT>` | `8080` | Port for the `--web` server. |
| `--host` | `<IP>` | `127.0.0.1` | Network interface to bind for `--web` (e.g. `0.0.0.0` for LAN access). |
| `--no-browser` | *(flag)* | `false` | Prevents automatically opening your default web browser on `--web` launch. |
| `--loop-visual` | `<FILE>` | *(none)* | Background image (`.jpg`, `.png`) or video (`.mp4`) to loop for YouTube/Twitch livestreaming in `--stream` mode. |

---

### Example Commands

```bash
# 1. Open interactive web player & live configuration sliders
python wmew_radio.py --web

# 2. Stream 24/7 to YouTube Live with background artwork
python wmew_radio.py --stream "rtmp://a.rtmp.youtube.com/live2/YOUR_STREAM_KEY" --loop-visual still_image.jpg
```

---

## Configuration File (`config.json`)

[`config.json`](config.json) defines the broadcast probabilities, playback behavior, anti-repeat history buffer sizes, and stream encoding settings. Modify this file directly to customize your station, or adjust values dynamically through the `--web` dashboard.

---

### Section 1: Top-Level Settings

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `radio_dir` | `string` | `"radio"` | Path (relative or absolute) to the root directory containing the audio subfolders. |

---

### Section 2: `probabilities`

All probabilities are decimal values between `0.0` (`0%`) and `1.0` (`100%`).

| Key | Default | Description |
| :--- | :--- | :--- |
| `intro_outro_chance` | `0.70` | Overall chance of an intro OR outro playing with a song (exclusive OR). The remaining chance plays neither. |
| `intro_vs_outro_split` | `0.50` | When an intro/outro is triggered, the proportion that becomes an **Intro** (`0.50` = 50% Intro / 50% Outro; `1.0` = always Intro). |
| `gamestart_intro_after_call_letter_chance` | `0.15` | Chance that a `gamestart_intros/` monologue plays immediately after a station ID (`call_letters/`). |
| `commercial_break_after_song_chance` | `0.25` | Chance of a Commercial Break occurring after a song. |
| `transitional_after_song_chance` | `0.20` | Chance of a transitional clip after a song. |
| `segment_after_song_chance` | `0.10` | Chance of a Jimmy "The Hammer" Valentine segment after a song. |
| `call_letter_after_song_chance` | `0.15` | Chance of a station ID directly following a song (when no other interstitial played). |
| `call_letter_after_interstitial_chance` | `0.50` | Chance of a station ID playing after a Commercial Break, Transitional, or Segment finishes. |

> **Post-Song Probability Normalization:**
> In `"exclusive"` mode, if the sum of post-song probabilities (`commercial + transitional + segment + direct_call`) exceeds `1.0`, they are automatically normalized proportionally to equal 100% total (with 0% chance of "nothing").

---

### Section 3: `behavior`

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `post_song_mode` | `string` | `"exclusive"` | `"exclusive"` selects at most one event after a song; `"sequential"` rolls each event independently in order. |
| `start_with_call_letter` | `boolean` | `true` | If `true`, the broadcast begins with a station ID on initial launch. |
| `force_gamestart_intro_on_boot` | `boolean` | `false` | If `true`, forces a `gamestart_intros/` monologue on the very first cycle on startup. |
| `play_commercial_break_bumper` | `boolean` | `true` | If `true`, commercial breaks start with an announcer clip from `commercialbreak/` before playing ads. |
| `allow_interstitial_after_outro` | `boolean` | `false` | If `false` *(recommended)*, when a song plays an Outro, no Commercial Break, Transitional, or Segment can play after it. (An interstitial can still precede an Intro into the next song). |
| `guarantee_call_letter_after_segment_without_gamestart` | `boolean` | `true` | If `true`, whenever a Jimmy "The Hammer" Valentine segment finishes, it is guaranteed to play a Call Letter without triggering a gamestart intro. |
| `commercials_per_break_min` | `integer` | `1` | Minimum number of ads per commercial break. |
| `commercials_per_break_max` | `integer` | `3` | Maximum number of ads per commercial break. |

---

### Section 4: `anti_repeat`

Controls how many recent items in each category are held in memory before they are eligible to repeat. Clamped to `pool_size - 1`.

| Key | Default | Total Pool Size | Description |
| :--- | :--- | :--- | :--- |
| `song_history_size` | `25` | `54` songs | Avoids repeating the last 25 songs. |
| `commercial_history_size` | `20` | `61` ads | Avoids repeating the last 20 commercial ads. |
| `transitional_history_size` | `15` | `47` clips | Avoids repeating the last 15 transitionals. |
| `segment_history_size` | `10` | `19` segments | Avoids repeating the last 10 Jimmy Valentine segments. |
| `gamestart_history_size` | `8` | `18` intros | Avoids repeating the last 8 gamestart intros. |
| `call_letter_history_size` | `2` | `5` IDs | Avoids repeating the last 2 call letters. |

---

### Section 5: `stream`

Controls audio and video encoding parameters for 24/7 streaming via FFmpeg.

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `sample_rate` | `integer` | `44100` | Audio sample rate in Hz (`44100` or `48000`). |
| `channels` | `integer` | `2` | Audio channels (`2` for stereo). |
| `audio_bitrate` | `string` | `"192k"` | Target audio encoding bitrate for FFmpeg (`"128k"`, `"192k"`, `"256k"`, `"320k"`). |
| `silence_between_tracks_ms` | `integer` | `150` | Silence gap in milliseconds between tracks for natural pacing. Set to `0` for zero-gap concatenation. |
| `thread_queue_size` | `integer` | `1024` | Buffer size for input thread queues to avoid pipeline blocking. |
| `video_codec` | `string` | `"libx264"` | Video encoder codec. |
| `video_preset` | `string` | `"ultrafast"` | x264 CPU encoding preset (`"ultrafast"`, `"superfast"`, `"veryfast"`). |
| `video_tune` | `string` | `"stillimage"` | x264 tuning profile (`"stillimage"`, `"zerolatency"`, or `""`). |
| `video_width` | `integer` | `854` | Video frame width in pixels (e.g. `854` for 480p, `256` for low bandwidth). |
| `video_height` | `integer` | `480` | Video frame height in pixels (e.g. `480` for 480p, `144` for low bandwidth). |
| `video_bitrate` | `string` | `"800k"` | Target video bitrate. |
| `video_maxrate` | `string` | `"1000k"` | Maximum video bitrate peak. |
| `video_bufsize` | `string` | `"2000k"` | Rate control buffer size. |
| `video_pix_fmt` | `string` | `"yuv420p"` | Pixel format expected by RTMP ingests (YouTube/Twitch). |
| `video_fps` | `integer` | `2` | Video output framerate (frames per second). |
| `video_gop_size` | `integer` | `4` | Keyframe distance (GOP). At 2 fps, GOP 4 = a keyframe every 2 seconds. |

---

## 24/7 Production Deployment

For continuous unattended livestreaming on Linux servers (e.g., Ubuntu on Azure / AWS / VPS):
* Refer to [`UBUNTU_LIVESTREAM_GUIDE.md`](UBUNTU_LIVESTREAM_GUIDE.md) for step-by-step instructions on setting up FFmpeg, Python, YouTube RTMP ingestion, and headless execution.
* Use the provided [`wmew_radio.service`](wmew_radio.service) systemd unit file to automatically manage, start, and auto-restart the broadcast service on boot.

