#!/usr/bin/env python3
"""
WMEW 99.9 FM - Mewgenics Radio Engine & Streamer
================================================
Recreates the dynamic radio mode from Mewgenics.

Modes:
  --web              Launch built-in browser player & configuration dashboard.
  --stream <URL>     24/7 continuous livestreaming via FFmpeg (RTMP / Icecast).
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import http.server
import json
import pathlib
import random
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from typing import Deque, Dict, Iterator, List, Optional


# ---------------------------------------------------------------------------
# Song -> Intro/Outro Alias Mapping
# ---------------------------------------------------------------------------
SONG_PREFIX_ALIASES: Dict[str, List[str]] = {
    "crystaline_dreams": ["crystalline_dreams", "crystaline_dreams"],
    "final_boss": ["dig_your_own_grave", "final_boss"],
    "fleas_and_parasites_v1": ["fleas_parasites", "fleas_and_parasites"],
    "kat_fight": ["kat_fight", "cat_fight"],
    "the_crack_of_doom": ["crack_of_doom", "the_crack_of_doom"],
    "you_know_who": ["guillotina", "you_know_who"],
}

SONG_DISPLAY_NAMES: Dict[str, str] = {
    "9th_life": "9th Life",
    "crystaline_dreams": "Crystalline Dreams",
    "eatin_rats": "Eatin' Rats",
    "final_boss": "Dig Your Own Grave (Final Boss)",
    "fleas_and_parasites_v1": "Fleas and Parasites",
    "mom_i_really_hate_you": "Mom, I Really Hate You",
    "the_crack_of_doom": "The Crack of Doom",
    "were_dead": "We're Dead",
    "you_know_who": "Guillotina (You Know Who)",
}


def format_display_name(stem: str) -> str:
    if stem in SONG_DISPLAY_NAMES:
        return SONG_DISPLAY_NAMES[stem]
    return stem.replace("_", " ").strip().title()


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class SongEntry:
    stem: str
    display_name: str
    path: pathlib.Path
    intros: List[pathlib.Path] = dataclasses.field(default_factory=list)
    outros: List[pathlib.Path] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class RadioTrack:
    kind: str  # CALL_LETTER, GAMESTART_INTRO, SONG_INTRO, SONG, SONG_OUTRO, COMMERCIAL_BREAK, COMMERCIAL, TRANSITIONAL, SEGMENT
    path: pathlib.Path
    label: str
    song_stem: Optional[str] = None
    cycle_index: int = 0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "path": str(self.path.as_posix()),
            "filename": self.path.name,
            "label": self.label,
            "song_stem": self.song_stem,
            "cycle_index": self.cycle_index,
        }


class ShufflePool:
    """
    Randomly selects items from a list while avoiding recently played items
    up to `history_size` (automatically clamped to `len(items) - 1`).
    """

    def __init__(self, items: List, history_size: int = 10, rng: Optional[random.Random] = None):
        self.items = list(items)
        self.rng = rng or random.Random()
        self.history_size = max(0, min(history_size, max(0, len(self.items) - 1)))
        self.recent: Deque = collections.deque(maxlen=self.history_size if self.history_size > 0 else 1)

    def pick(self):
        if not self.items:
            return None
        if len(self.items) == 1:
            return self.items[0]
        recent_indices = set(self.recent) if self.history_size > 0 else set()
        candidate_indices = [i for i in range(len(self.items)) if i not in recent_indices]
        if not candidate_indices:
            candidate_indices = list(range(len(self.items)))
        chosen_idx = self.rng.choice(candidate_indices)
        if self.history_size > 0:
            self.recent.append(chosen_idx)
        return self.items[chosen_idx]


# ---------------------------------------------------------------------------
# Radio Library Loader
# ---------------------------------------------------------------------------

class RadioLibrary:
    def __init__(self, radio_dir: pathlib.Path):
        self.radio_dir = radio_dir.resolve()
        if not self.radio_dir.is_dir():
            raise FileNotFoundError(f"Radio directory not found: {self.radio_dir}")

        self.call_letters = self._glob_ogg("call_letters")
        self.commercial_breaks = self._glob_ogg("commercialbreak")
        self.commercials = self._glob_ogg("commercials")
        self.gamestart_intros = self._glob_ogg("gamestart_intros")
        self.intros = self._glob_ogg("intros")
        self.outros = self._glob_ogg("outros")
        self.segments = self._glob_ogg("segments")
        self.songs_raw = self._glob_ogg("songs")
        self.transitionals = self._glob_ogg("transitionals")

        self.songs: List[SongEntry] = []
        self._build_song_catalog()

    def _glob_ogg(self, subdir: str) -> List[pathlib.Path]:
        target = self.radio_dir / subdir
        if not target.is_dir():
            return []
        return sorted(target.glob("*.ogg"))

    @staticmethod
    def _match_song_clips(song_stem: str, clip_paths: List[pathlib.Path], kind: str) -> List[pathlib.Path]:
        prefixes = SONG_PREFIX_ALIASES.get(song_stem, [song_stem])
        matched: List[pathlib.Path] = []
        for path in clip_paths:
            clip_stem = path.stem
            for prefix in prefixes:
                pattern = rf"^{re.escape(prefix)}(?:_{kind})?(?:_?\d+)?$"
                if re.match(pattern, clip_stem, flags=re.IGNORECASE):
                    matched.append(path)
                    break
        return matched

    def _build_song_catalog(self) -> None:
        for song_path in self.songs_raw:
            stem = song_path.stem
            self.songs.append(
                SongEntry(
                    stem=stem,
                    display_name=format_display_name(stem),
                    path=song_path,
                    intros=self._match_song_clips(stem, self.intros, "intro"),
                    outros=self._match_song_clips(stem, self.outros, "outro"),
                )
            )


# ---------------------------------------------------------------------------
# Default Configuration & Loader
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "radio_dir": "radio",
    "probabilities": {
        "intro_outro_chance": 0.70,
        "intro_vs_outro_split": 0.50,
        "gamestart_intro_after_call_letter_chance": 0.15,
        "commercial_break_after_song_chance": 0.25,
        "transitional_after_song_chance": 0.20,
        "segment_after_song_chance": 0.10,
        "call_letter_after_interstitial_chance": 0.50,
        "call_letter_after_song_chance": 0.15,
    },
    "behavior": {
        "post_song_mode": "exclusive",
        "start_with_call_letter": True,
        "force_gamestart_intro_on_boot": False,
        "play_commercial_break_bumper": True,
        "allow_interstitial_after_outro": False,
        "guarantee_call_letter_after_segment_without_gamestart": True,
        "commercials_per_break_min": 1,
        "commercials_per_break_max": 3,
    },
    "anti_repeat": {
        "song_history_size": 25,
        "commercial_history_size": 20,
        "transitional_history_size": 15,
        "segment_history_size": 10,
        "gamestart_history_size": 8,
        "call_letter_history_size": 2,
    },
    "stream": {
        "sample_rate": 44100,
        "channels": 2,
        "audio_bitrate": "192k",
        "silence_between_tracks_ms": 150,
        "thread_queue_size": 1024,
        "video_codec": "libx264",
        "video_preset": "ultrafast",
        "video_tune": "stillimage",
        "video_bitrate": "800k",
        "video_maxrate": "1000k",
        "video_bufsize": "2000k",
        "video_pix_fmt": "yuv420p",
        "video_fps": 2,
        "video_gop_size": 4,
    },
}


def load_config(config_path: Optional[pathlib.Path]) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if config_path and config_path.is_file():
        with open(config_path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        for section in ("probabilities", "behavior", "anti_repeat", "stream"):
            if isinstance(user_cfg.get(section), dict):
                cfg[section].update(user_cfg[section])
        if "radio_dir" in user_cfg:
            cfg["radio_dir"] = user_cfg["radio_dir"]
    return cfg


# ---------------------------------------------------------------------------
# Radio State Machine / Scheduler Engine
# ---------------------------------------------------------------------------

class WMEWRadioEngine:
    def __init__(self, library: RadioLibrary, config: dict, seed: Optional[int] = None):
        self.library = library
        self.config = config
        self.rng = random.Random(seed)
        self.cycle_index = 0
        self.booted = False
        self._init_pools()

    def _init_pools(self) -> None:
        ar = self.config["anti_repeat"]
        self.song_pool = ShufflePool(self.library.songs, ar.get("song_history_size", 25), self.rng)
        self.call_letter_pool = ShufflePool(self.library.call_letters, ar.get("call_letter_history_size", 2), self.rng)
        self.gamestart_pool = ShufflePool(self.library.gamestart_intros, ar.get("gamestart_history_size", 8), self.rng)
        self.break_bumper_pool = ShufflePool(self.library.commercial_breaks, 1, self.rng)
        self.commercial_pool = ShufflePool(self.library.commercials, ar.get("commercial_history_size", 20), self.rng)
        self.transitional_pool = ShufflePool(self.library.transitionals, ar.get("transitional_history_size", 15), self.rng)
        self.segment_pool = ShufflePool(self.library.segments, ar.get("segment_history_size", 10), self.rng)

    def update_config(self, new_config: dict) -> None:
        self.config = new_config

    def _roll(self, probability: float) -> bool:
        p = max(0.0, min(1.0, float(probability)))
        return self.rng.random() < p

    def _emit_call_letter_sequence(self, force_gamestart: bool = False, allow_gamestart: bool = True) -> List[RadioTrack]:
        tracks: List[RadioTrack] = []
        cl_path = self.call_letter_pool.pick()
        if not cl_path:
            return tracks

        tracks.append(
            RadioTrack(
                kind="CALL_LETTER",
                path=cl_path,
                label=f"Call Letter: {format_display_name(cl_path.stem)}",
                cycle_index=self.cycle_index,
            )
        )

        gamestart_chance = self.config["probabilities"].get("gamestart_intro_after_call_letter_chance", 0.0)
        if allow_gamestart and (force_gamestart or self._roll(gamestart_chance)):
            gs_path = self.gamestart_pool.pick()
            if gs_path:
                tracks.append(
                    RadioTrack(
                        kind="GAMESTART_INTRO",
                        path=gs_path,
                        label=f"Gamestart Intro: {format_display_name(gs_path.stem)}",
                        cycle_index=self.cycle_index,
                    )
                )
        return tracks

    def _emit_commercial_break(self) -> List[RadioTrack]:
        tracks: List[RadioTrack] = []
        beh = self.config["behavior"]

        if beh.get("play_commercial_break_bumper", True):
            bumper = self.break_bumper_pool.pick()
            if bumper:
                tracks.append(
                    RadioTrack(
                        kind="COMMERCIAL_BREAK",
                        path=bumper,
                        label=f"Commercial Break Bumper: {format_display_name(bumper.stem)}",
                        cycle_index=self.cycle_index,
                    )
                )

        c_min = max(1, int(beh.get("commercials_per_break_min", 1)))
        c_max = max(c_min, int(beh.get("commercials_per_break_max", 3)))
        count = self.rng.randint(c_min, c_max)

        for i in range(count):
            ad = self.commercial_pool.pick()
            if ad:
                tracks.append(
                    RadioTrack(
                        kind="COMMERCIAL",
                        path=ad,
                        label=f"Commercial ({i + 1}/{count}): {format_display_name(ad.stem)}",
                        cycle_index=self.cycle_index,
                    )
                )
        return tracks

    def generate_next_cycle(self) -> List[RadioTrack]:
        tracks: List[RadioTrack] = []
        probs = self.config["probabilities"]
        beh = self.config["behavior"]

        # First cycle station sign-on
        if not self.booted:
            self.booted = True
            if beh.get("start_with_call_letter", True):
                force_gs = bool(beh.get("force_gamestart_intro_on_boot", False))
                tracks.extend(self._emit_call_letter_sequence(force_gamestart=force_gs))

        self.cycle_index += 1

        # Pick a Song and roll for Intro XOR Outro
        song: Optional[SongEntry] = self.song_pool.pick()
        if not song:
            return tracks

        intro_outro_chance = float(probs.get("intro_outro_chance", 0.70))
        intro_split = float(probs.get("intro_vs_outro_split", 0.50))

        play_intro = False
        play_outro = False
        if self._roll(intro_outro_chance):
            if self._roll(intro_split):
                if song.intros:
                    play_intro = True
                elif song.outros:
                    play_outro = True
            else:
                if song.outros:
                    play_outro = True
                elif song.intros:
                    play_intro = True

        if play_intro:
            intro_path = self.rng.choice(song.intros)
            tracks.append(
                RadioTrack(
                    kind="SONG_INTRO",
                    path=intro_path,
                    label=f"Song Intro: {song.display_name} ({intro_path.name})",
                    song_stem=song.stem,
                    cycle_index=self.cycle_index,
                )
            )

        tracks.append(
            RadioTrack(
                kind="SONG",
                path=song.path,
                label=f"Song: {song.display_name}",
                song_stem=song.stem,
                cycle_index=self.cycle_index,
            )
        )

        if play_outro:
            outro_path = self.rng.choice(song.outros)
            tracks.append(
                RadioTrack(
                    kind="SONG_OUTRO",
                    path=outro_path,
                    label=f"Song Outro: {song.display_name} ({outro_path.name})",
                    song_stem=song.stem,
                    cycle_index=self.cycle_index,
                )
            )

        # Post-Song Roll
        allow_interstitial = beh.get("allow_interstitial_after_outro", False) if play_outro else True

        p_comm = max(0.0, float(probs.get("commercial_break_after_song_chance", 0.25))) if allow_interstitial else 0.0
        p_trans = max(0.0, float(probs.get("transitional_after_song_chance", 0.20))) if allow_interstitial else 0.0
        p_seg = max(0.0, float(probs.get("segment_after_song_chance", 0.10))) if allow_interstitial else 0.0
        p_call_direct = max(0.0, float(probs.get("call_letter_after_song_chance", 0.15)))
        p_call_after_interstitial = max(0.0, float(probs.get("call_letter_after_interstitial_chance", 0.50)))

        mode = beh.get("post_song_mode", "exclusive").lower()

        if mode == "sequential":
            interstitial_played = False
            if self._roll(p_comm):
                tracks.extend(self._emit_commercial_break())
                interstitial_played = True
            if self._roll(p_trans):
                tr_path = self.transitional_pool.pick()
                if tr_path:
                    tracks.append(
                        RadioTrack(
                            kind="TRANSITIONAL",
                            path=tr_path,
                            label=f"Transitional: {format_display_name(tr_path.stem)}",
                            cycle_index=self.cycle_index,
                        )
                    )
                    interstitial_played = True
            seg_played = False
            if self._roll(p_seg):
                seg_path = self.segment_pool.pick()
                if seg_path:
                    tracks.append(
                        RadioTrack(
                            kind="SEGMENT",
                            path=seg_path,
                            label=f"Jimmy 'The Hammer' Valentine: {format_display_name(seg_path.stem)}",
                            cycle_index=self.cycle_index,
                        )
                    )
                    interstitial_played = True
                    seg_played = True

            if interstitial_played:
                if seg_played and beh.get("guarantee_call_letter_after_segment_without_gamestart", False):
                    tracks.extend(self._emit_call_letter_sequence(allow_gamestart=False))
                elif self._roll(p_call_after_interstitial):
                    tracks.extend(self._emit_call_letter_sequence())
            else:
                if self._roll(p_call_direct):
                    tracks.extend(self._emit_call_letter_sequence())
        else:
            total_p = p_comm + p_trans + p_seg + p_call_direct
            scale = 1.0 if total_p <= 1.0 else (1.0 / total_p)
            roll = self.rng.random()

            t1 = p_comm * scale
            t2 = t1 + (p_trans * scale)
            t3 = t2 + (p_seg * scale)
            t4 = t3 + (p_call_direct * scale)

            if roll < t1:
                tracks.extend(self._emit_commercial_break())
                if self._roll(p_call_after_interstitial):
                    tracks.extend(self._emit_call_letter_sequence())
            elif roll < t2:
                tr_path = self.transitional_pool.pick()
                if tr_path:
                    tracks.append(
                        RadioTrack(
                            kind="TRANSITIONAL",
                            path=tr_path,
                            label=f"Transitional: {format_display_name(tr_path.stem)}",
                            cycle_index=self.cycle_index,
                        )
                    )
                if self._roll(p_call_after_interstitial):
                    tracks.extend(self._emit_call_letter_sequence())
            elif roll < t3:
                seg_path = self.segment_pool.pick()
                if seg_path:
                    tracks.append(
                        RadioTrack(
                            kind="SEGMENT",
                            path=seg_path,
                            label=f"Jimmy 'The Hammer' Valentine: {format_display_name(seg_path.stem)}",
                            cycle_index=self.cycle_index,
                        )
                    )
                if beh.get("guarantee_call_letter_after_segment_without_gamestart", False):
                    tracks.extend(self._emit_call_letter_sequence(allow_gamestart=False))
                elif self._roll(p_call_after_interstitial):
                    tracks.extend(self._emit_call_letter_sequence())
            elif roll < t4:
                tracks.extend(self._emit_call_letter_sequence())

        return tracks

    def stream_tracks(self) -> Iterator[RadioTrack]:
        while True:
            for track in self.generate_next_cycle():
                yield track


# ---------------------------------------------------------------------------
# FFmpeg 24/7 Livestreaming Mode
# ---------------------------------------------------------------------------

def run_ffmpeg_stream(
    engine: WMEWRadioEngine,
    stream_url: str,
    loop_visual: Optional[str] = None,
) -> None:
    """
    Pipes decoded audio directly into a single persistent FFmpeg encoder process.
    Guarantees gapless, uninterrupted 24/7 streaming to YouTube/Twitch/Icecast.
    """
    if not shutil.which("ffmpeg"):
        print("Error: `ffmpeg` was not found in PATH. Please install FFmpeg to use --stream mode.", file=sys.stderr)
        sys.exit(1)

    stream_cfg = engine.config.get("stream", {})
    sample_rate = int(stream_cfg.get("sample_rate", 44100))
    channels = int(stream_cfg.get("channels", 2))
    bitrate = str(stream_cfg.get("audio_bitrate", "192k"))
    silence_ms = int(stream_cfg.get("silence_between_tracks_ms", 150))
    queue_size = str(stream_cfg.get("thread_queue_size", 1024))

    # Video properties
    video_codec = str(stream_cfg.get("video_codec", "libx264"))
    video_preset = str(stream_cfg.get("video_preset", "ultrafast"))
    video_tune = stream_cfg.get("video_tune", "stillimage")
    video_bitrate = str(stream_cfg.get("video_bitrate", "800k"))
    video_maxrate = str(stream_cfg.get("video_maxrate", "1000k"))
    video_bufsize = str(stream_cfg.get("video_bufsize", "2000k"))
    video_pix_fmt = str(stream_cfg.get("video_pix_fmt", "yuv420p"))
    video_fps = int(stream_cfg.get("video_fps", 2))
    video_gop_size = int(stream_cfg.get("video_gop_size", 4))
    video_width = int(stream_cfg.get("video_width", 854))
    video_height = int(stream_cfg.get("video_height", 480))

    encoder_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]

    if loop_visual:
        visual_path = pathlib.Path(loop_visual)
        if visual_path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            encoder_cmd.extend([
                "-thread_queue_size", queue_size,
                "-re",
                "-loop", "1",
                "-framerate", str(video_fps),
                "-i", str(visual_path),
            ])
        else:
            encoder_cmd.extend([
                "-thread_queue_size", queue_size,
                "-re",
                "-stream_loop", "-1",
                "-i", str(visual_path),
            ])
    else:
        encoder_cmd.extend(["-thread_queue_size", queue_size, "-re"])

    encoder_cmd.extend([
        "-thread_queue_size", queue_size,
        "-f", "s16le",
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-i", "pipe:0",
    ])

    if loop_visual:
        vf_filter = (
            f"scale={video_width}:{video_height}:force_original_aspect_ratio=decrease,"
            f"pad={video_width}:{video_height}:(ow-iw)/2:(oh-ih)/2,"
            f"format={video_pix_fmt}"
        )
        encoder_cmd.extend([
            "-vf", vf_filter,
            "-c:v", video_codec,
            "-preset", video_preset,
            "-threads", "1",  # Limits CPU usage to 1 thread
        ])
        if video_tune:
            encoder_cmd.extend(["-tune", str(video_tune)])
        encoder_cmd.extend([
            "-b:v", video_bitrate,
            "-maxrate", video_maxrate,
            "-bufsize", video_bufsize,
            "-pix_fmt", video_pix_fmt,
            "-g", str(video_gop_size),
            "-keyint_min", str(video_gop_size),
            "-r", str(video_fps),
        ])

    if stream_url.startswith("rtmp://") or stream_url.startswith("rtmps://"):
        encoder_cmd.extend([
            "-c:a", "aac",
            "-b:a", bitrate,
            "-ar", str(sample_rate),
            "-f", "flv",
            stream_url,
        ])
    elif stream_url.startswith("icecast://") or stream_url.endswith(".ogg"):
        encoder_cmd.extend([
            "-c:a", "libvorbis",
            "-b:a", bitrate,
            "-content_type", "application/ogg",
            "-f", "ogg",
            stream_url,
        ])
    else:
        encoder_cmd.extend([
            "-c:a", "aac",
            "-b:a", bitrate,
            "-f", "mpegts",
            stream_url,
        ])

    print("=== WMEW 99.9 FM 24/7 Livestreamer ===")
    print(f"Target URL: {stream_url}")
    print(f"Encoder command: {' '.join(encoder_cmd)}")
    print("-" * 72)

    silence_bytes = b"\x00" * int(sample_rate * channels * 2 * (silence_ms / 1000.0))

    while True:
        encoder_proc = subprocess.Popen(encoder_cmd, stdin=subprocess.PIPE)
        try:
            for track in engine.stream_tracks():
                print(f"[{track.kind:<16}] {track.label}", flush=True)

                decoder_cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-i", str(track.path),
                    "-f", "s16le",
                    "-acodec", "pcm_s16le",
                    "-ar", str(sample_rate),
                    "-ac", str(channels),
                    "pipe:1",
                ]
                decoder_proc = subprocess.Popen(decoder_cmd, stdout=subprocess.PIPE)
                assert decoder_proc.stdout is not None
                assert encoder_proc.stdin is not None

                while True:
                    chunk = decoder_proc.stdout.read(16384)
                    if not chunk:
                        break
                    encoder_proc.stdin.write(chunk)

                decoder_proc.wait()
                if silence_bytes:
                    encoder_proc.stdin.write(silence_bytes)
                encoder_proc.stdin.flush()

        except (BrokenPipeError, IOError) as e:
            print(f"[Stream Warning] Encoder pipe interrupted ({e}). Restarting in 3s...", file=sys.stderr)
            time.sleep(3)
        except KeyboardInterrupt:
            print("\nShutting down 24/7 livestream.")
            break
        finally:
            if encoder_proc.poll() is None:
                encoder_proc.terminate()


# ---------------------------------------------------------------------------
# Built-in Zero-Dependency Web Player & Live Configurator
# ---------------------------------------------------------------------------

INDEX_HTML_PATH = pathlib.Path(__file__).resolve().parent / "index.html"


def run_web_server(
    engine: WMEWRadioEngine,
    config_path: Optional[pathlib.Path] = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = True,
) -> None:
    radio_root = engine.library.radio_dir.resolve()

    class RadioHTTPHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass  # keep terminal output clean

        def _send_json(self, payload: dict, status: int = 200) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                if not INDEX_HTML_PATH.is_file():
                    self.send_error(404, "index.html not found")
                    return
                raw = INDEX_HTML_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            if parsed.path == "/api/config":
                self._send_json(engine.config)
                return

            if parsed.path == "/api/next":
                qs = urllib.parse.parse_qs(parsed.query)
                cycles = max(1, min(20, int(qs.get("cycles", ["2"])[0])))
                tracks = []
                for _ in range(cycles):
                    for t in engine.generate_next_cycle():
                        tracks.append(t.to_dict())
                self._send_json({"tracks": tracks})
                return

            if parsed.path == "/audio":
                qs = urllib.parse.parse_qs(parsed.query)
                rel_path = qs.get("path", [""])[0]
                target = pathlib.Path(rel_path).resolve()
                if not str(target).startswith(str(radio_root)) or not target.is_file():
                    self.send_error(404, "Audio file not found")
                    return
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/ogg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_error(404, "Not Found")

        def do_POST(self) -> None:
            if self.path == "/api/config":
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                new_cfg = json.loads(body.decode("utf-8"))
                engine.update_config(new_cfg)
                if config_path:
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(new_cfg, f, indent=2)
                self._send_json({"status": "ok"})
                return
            self.send_error(404, "Not Found")

    url = f"http://{host}:{port}"
    print("=== WMEW 99.9 FM Web Player & Configurator ===")
    print(f"Listening on: {url}")
    print("Press Ctrl+C to stop.")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    with socketserver.ThreadingTCPServer((host, port), RadioHTTPHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down web server.")


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WMEW 99.9 FM - Mewgenics Radio Web Player & FFmpeg Streamer",
    )
    parser.add_argument(
        "-c", "--config",
        type=pathlib.Path,
        default=pathlib.Path("config.json"),
        help="Path to configuration JSON file (default: config.json)",
    )
    parser.add_argument(
        "--radio-dir",
        type=pathlib.Path,
        default=None,
        help="Path to audio directory (default: from config.json)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch the built-in browser player and live configurator",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for --web server (default: 8080)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host for --web server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open browser in --web mode",
    )
    parser.add_argument(
        "--stream",
        type=str,
        metavar="URL",
        help="Stream 24/7 continuous audio via FFmpeg to RTMP or Icecast URL",
    )
    parser.add_argument(
        "--loop-visual",
        type=str,
        default=None,
        help="Optional background image (.png/.jpg) or video (.mp4) to loop when streaming",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)

    if args.radio_dir:
        cfg["radio_dir"] = str(args.radio_dir)

    radio_path = pathlib.Path(cfg.get("radio_dir", "radio"))
    if not radio_path.is_dir():
        print(f"Error: Radio audio directory '{radio_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    library = RadioLibrary(radio_path)
    engine = WMEWRadioEngine(library, cfg)

    if args.stream:
        run_ffmpeg_stream(engine, stream_url=args.stream, loop_visual=args.loop_visual)
    else:
        # Launch the web server (--web or default mode)
        run_web_server(
            engine,
            config_path=args.config,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )


if __name__ == "__main__":
    main()
