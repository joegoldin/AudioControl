import ctypes
import math
import struct
import threading

from loguru import logger as log
from PIL import Image, ImageDraw

from GtkHelper.GenerativeUI.ExpanderRow import ExpanderRow
from GtkHelper.GenerativeUI.ScaleRow import ScaleRow
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.EventAssigner import EventAssigner
from .AudioCore import AudioCore
from ..globals import Icons
from ..internal.PulseHelpers import get_device, change_volume, get_volumes_from_device, set_volume, mute


class _PeakMonitor:
    """Background thread reading peak audio level from a sink's monitor source."""

    PA_STREAM_RECORD = 2
    PA_SAMPLE_S16LE = 3

    class _SampleSpec(ctypes.Structure):
        _fields_ = [
            ('format', ctypes.c_int),
            ('rate', ctypes.c_uint32),
            ('channels', ctypes.c_uint8),
        ]

    # Call on_update every N reads (~50ms each → 4 = ~200ms = 5Hz)
    UPDATE_EVERY = 4

    def __init__(self, on_update=None):
        self._lib = None
        self._peak = 0.0
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._on_update = on_update

        try:
            self._lib = ctypes.CDLL(self._find_lib())
            self._lib.pa_simple_new.restype = ctypes.c_void_p
            self._lib.pa_simple_read.restype = ctypes.c_int
            self._lib.pa_simple_read.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_int)
            ]
            self._lib.pa_simple_free.restype = None
            self._lib.pa_simple_free.argtypes = [ctypes.c_void_p]
        except (OSError, TypeError):
            self._lib = None

    @staticmethod
    def _find_lib():
        """Find libpulse-simple.so.0, deriving path from loaded libpulse if needed."""
        import os
        # Try standard name first
        name = 'libpulse-simple.so.0'
        try:
            ctypes.CDLL(name)
            return name
        except OSError:
            pass
        # Derive from libpulse.so already loaded by pulsectl (works on NixOS)
        try:
            pid = os.getpid()
            with open(f'/proc/{pid}/maps', 'r') as f:
                for line in f:
                    if 'libpulse.so' in line and 'libpulsecommon' not in line:
                        path = line.strip().split()[-1]
                        lib_dir = os.path.dirname(path)
                        candidate = os.path.join(lib_dir, name)
                        if os.path.exists(candidate):
                            return candidate
        except Exception:
            pass
        return None

    @property
    def available(self):
        return self._lib is not None

    @property
    def peak(self):
        with self._lock:
            return self._peak

    def start(self, sink_name):
        self.stop()
        if not self._lib or not sink_name:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            args=(sink_name,),
            daemon=True
        )
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        with self._lock:
            self._peak = 0.0

    def _monitor_loop(self, sink_name):
        monitor = (sink_name + '.monitor').encode()
        ss = self._SampleSpec(format=self.PA_SAMPLE_S16LE, rate=8000, channels=1)
        error = ctypes.c_int(0)

        conn = self._lib.pa_simple_new(
            None,
            b'streamcontroller-vu',
            self.PA_STREAM_RECORD,
            monitor,
            b'VU Meter',
            ctypes.byref(ss),
            None,
            None,
            ctypes.byref(error)
        )

        if not conn:
            return

        # 50ms buffer at 8kHz mono s16le = 800 bytes (400 samples)
        buf_size = 800
        buf = ctypes.create_string_buffer(buf_size)
        decay = 0.0
        read_count = 0

        try:
            while self._running:
                ret = self._lib.pa_simple_read(conn, buf, buf_size, ctypes.byref(error))
                if ret < 0:
                    break

                num_samples = buf_size // 2
                samples = struct.unpack(f'<{num_samples}h', buf.raw)
                linear_peak = max(abs(s) for s in samples) / 32768.0

                # Convert to logarithmic scale (dB-like) for perceptual VU
                # -60dB floor, 0dB = 1.0 linear
                if linear_peak > 0.001:
                    db = 20.0 * math.log10(linear_peak)
                    current_peak = max(0.0, min(1.0, (db + 60.0) / 60.0))
                else:
                    current_peak = 0.0

                # Fast attack, slow decay
                if current_peak > decay:
                    decay = current_peak
                else:
                    decay = decay * 0.85 + current_peak * 0.15

                with self._lock:
                    self._peak = decay

                # Drive display updates at ~5Hz
                read_count += 1
                if self._on_update and read_count >= self.UPDATE_EVERY:
                    read_count = 0
                    try:
                        self._on_update()
                    except Exception:
                        pass
        finally:
            self._lib.pa_simple_free(conn)


class AdjustVolume(AudioCore):
    # Ticks before +/- icon reverts to plain speaker (~1s/tick)
    SCROLL_ICON_TICKS = 2
    # Refresh display every N ticks
    VU_REFRESH_INTERVAL = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.icon_keys = [Icons.VOLUME_UP, Icons.VOLUME_DOWN, Icons.MUTED, Icons.UNMUTED,
                          Icons.AUDIO_LOW, Icons.AUDIO_MEDIUM, Icons.AUDIO_HIGH]

        self.adjust: int = 1
        self.bounds = 100

        self._scroll_direction = None  # "up", "down", or None
        self._scroll_ticks_remaining = 0
        self._is_muted = False
        self._cached_volume = 0.0  # 0.0 - 1.0+
        self._lock = threading.Lock()
        self._vu_tick_counter = 0

        self._peak_monitor = _PeakMonitor(on_update=self.display_icon)

        self.create_generative_ui()

    def create_generative_ui(self):
        super().create_generative_ui()

        self.volume_adjust_row = ExpanderRow(
            action_core=self,
            var_name="adjust-expander",
            default_value=False,
            title="Volume Adjust Row",
        )

        self.volume_adjust_scale = ScaleRow(
            action_core=self,
            var_name="volume-adjust",
            default_value=1,
            min=-100,
            max=100,
            step=1,
            digits=0,
            title="Adjustment",
            draw_value=True,
            on_change=self.on_volume_adjust_change
        )

        self.volume_bound_scale = ScaleRow(
            action_core=self,
            var_name="volume-bounds",
            default_value=100,
            min=0,
            max=150,
            step=1,
            digits=0,
            title="Maximum Audio Bounds",
            draw_value=True,
            on_change=self.on_volume_bound_change
        )

        self.volume_adjust_row.add_row(self.volume_adjust_scale.widget)
        self.volume_adjust_row.add_row(self.volume_bound_scale.widget)

    def create_event_assigners(self):
        self.add_event_assigner(EventAssigner(
            id="adjust-volume-positive",
            ui_label="Adjust Volume Positive",
            default_events=[Input.Key.Events.DOWN, Input.Dial.Events.TURN_CW],
            callback=self.event_adjust_volume_positive
        ))

        self.add_event_assigner(EventAssigner(
            id="adjust-volume-negative",
            ui_label="Adjust Volume Negative",
            default_event=Input.Dial.Events.TURN_CCW,
            callback=self.event_adjust_volume_negative
        ))

        self.add_event_assigner(EventAssigner(
            id="toggle-mute",
            ui_label="Toggle Mute",
            default_event=Input.Dial.Events.SHORT_UP,
            callback=self.event_toggle_mute
        ))

    def on_update(self):
        super().on_update()
        self._start_peak_monitor()

    def device_changed(self, widget, value, old):
        super().device_changed(widget, value, old)
        self._start_peak_monitor()

    def _start_peak_monitor(self):
        if self.selected_device is not None:
            self._peak_monitor.start(self.selected_device.pulse_name)

    def event_adjust_volume_positive(self, event):
        with self._lock:
            self._scroll_direction = "up"
            self._scroll_ticks_remaining = self.SCROLL_ICON_TICKS
        self.adjust_volume()
        self._refresh_cached_volume()
        self._update_display()

    def event_adjust_volume_negative(self, event):
        with self._lock:
            self._scroll_direction = "down"
            self._scroll_ticks_remaining = self.SCROLL_ICON_TICKS
        self.adjust_volume(-1)
        self._refresh_cached_volume()
        self._update_display()

    def event_toggle_mute(self, event):
        if self.selected_device is None:
            self.show_error(1)
            return

        try:
            device = get_device(self.device_filter, self.selected_device.pulse_name)
            self._is_muted = not device.mute
            mute(device, self._is_muted)
            self._update_display()
        except Exception as e:
            log.error(f"Error toggling mute: {e}")
            self.show_error(1)

    def adjust_volume(self, modifier: int = 1):
        adjustment = self.adjust * modifier

        if self.selected_device is None:
            self.show_error(1)
            return

        try:
            device = get_device(self.device_filter, self.selected_device.pulse_name)

            if adjustment < 0:
                change_volume(device, adjustment)
                return

            volumes = get_volumes_from_device(self.device_filter, device.name)

            if len(volumes) > 0 and volumes[0] < self.bounds:
                if volumes[0] + adjustment > self.bounds:
                    set_volume(device, self.bounds)
                else:
                    change_volume(device, adjustment)
        except Exception as e:
            log.error(e)
            self.show_error(1)

    def _refresh_cached_volume(self):
        """Query PulseAudio once and cache the result."""
        if self.selected_device is None:
            return
        try:
            volumes = get_volumes_from_device(self.device_filter, self.selected_device.pulse_name)
            if volumes:
                self._cached_volume = volumes[0] / 100.0
        except Exception:
            pass

    def _update_display(self):
        """Refresh icon and info label using cached state."""
        self.display_icon()
        self.display_device_info()

    def on_tick(self):
        super().on_tick()
        icon_changed = False
        with self._lock:
            if self._scroll_ticks_remaining > 0:
                self._scroll_ticks_remaining -= 1
                if self._scroll_ticks_remaining == 0:
                    self._scroll_direction = None
                    icon_changed = True

        # Throttled periodic refresh (~2x/sec)
        self._vu_tick_counter += 1
        if self._vu_tick_counter >= self.VU_REFRESH_INTERVAL:
            self._vu_tick_counter = 0
            self._refresh_cached_volume()
            self._update_display()
        elif icon_changed:
            self.display_icon()

    def on_volume_adjust_change(self, widget, value, old):
        self.adjust = value
        self.display_device_info()
        self.set_current_icon()

    def on_volume_bound_change(self, widget, value, old):
        self.bounds = value

    ########### UI STUFF ###########

    def set_current_icon(self):
        if self._is_muted:
            self._current_icon = self.get_icon(Icons.MUTED)
            self._icon_name = Icons.MUTED
        elif self.adjust >= 0:
            self._current_icon = self.get_icon(Icons.VOLUME_UP)
            self._icon_name = Icons.VOLUME_UP
        else:
            self._current_icon = self.get_icon(Icons.VOLUME_DOWN)
            self._icon_name = Icons.VOLUME_DOWN

        self.display_icon()

    def display_icon(self):
        with self._lock:
            direction = self._scroll_direction

        # Pick icon: muted > scroll direction > idle (volume-based waves)
        if self._is_muted:
            icon_asset = self.get_icon(Icons.MUTED)
        elif direction == "up":
            icon_asset = self.get_icon(Icons.VOLUME_UP)
        elif direction == "down":
            icon_asset = self.get_icon(Icons.VOLUME_DOWN)
        elif self._cached_volume > 0.66:
            icon_asset = self.get_icon(Icons.AUDIO_HIGH)
        elif self._cached_volume > 0.33:
            icon_asset = self.get_icon(Icons.AUDIO_MEDIUM)
        else:
            icon_asset = self.get_icon(Icons.AUDIO_LOW)

        if not icon_asset:
            return

        _, rendered = icon_asset.get_values()
        if not rendered:
            return

        peak = self._peak_monitor.peak if self._peak_monitor.available else 0.0
        composite = self._render_overlays(rendered, self._cached_volume, peak)
        self.set_media(image=composite)

    def _render_overlays(self, icon_image: Image.Image, volume: float, peak: float) -> Image.Image:
        """Render the icon with a horizontal volume bar and vertical VU bar."""
        w, h = icon_image.size

        draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))  # temp for measurements
        vol = min(1.0, max(0.0, volume))
        pk = min(1.0, max(0.0, peak))

        pad = max(4, min(w, h) // 18)

        # Pre-calculate overlay regions so we can center the icon in remaining space
        bar_height = max(6, h // 12)
        vu_bar_width = max(4, w // 16)

        # Available area for the icon: left of VU bar, above volume bar
        icon_area_right = w - pad - vu_bar_width - pad  # stop before VU bar
        icon_area_bottom = h - bar_height - pad - pad   # stop before vol bar

        # Shrink icon to fit ~65% of the available area
        icon_scale = 0.65
        icon_w = int(icon_area_right * icon_scale)
        icon_h = int(icon_area_bottom * icon_scale)
        scaled = icon_image.resize((icon_w, icon_h), Image.LANCZOS)

        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        # Center icon within the area left of VU and above vol bar
        ix = (icon_area_right - icon_w) // 2
        iy = (icon_area_bottom - icon_h) // 2
        img.paste(scaled, (ix, iy), scaled)

        del draw
        draw = ImageDraw.Draw(img)

        # --- Horizontal volume bar at bottom (shows volume setting) ---
        bar_y = h - bar_height - pad

        # Track background
        draw.rounded_rectangle(
            [(pad, bar_y), (w - pad, bar_y + bar_height)],
            radius=bar_height // 2,
            fill=(60, 60, 60, 200)
        )

        # Bar fill
        bar_width = w - 2 * pad
        fill_width = int(bar_width * vol)
        if fill_width > 0:
            if self._is_muted:
                bar_color = (180, 40, 40, 230)
            else:
                r = min(255, int(vol * 2 * 255))
                g = min(255, int((1 - vol * 0.5) * 255))
                bar_color = (r, g, 50, 230)

            draw.rounded_rectangle(
                [(pad, bar_y), (pad + fill_width, bar_y + bar_height)],
                radius=bar_height // 2,
                fill=bar_color
            )

        # --- Vertical VU bar on the right (shows actual audio output level) ---
        vu_x = w - pad - vu_bar_width
        vu_bottom = bar_y - pad * 2  # extra gap above horizontal bar
        vu_top = vu_bottom - (vu_bottom - pad) * 2 // 3  # ~2/3 height
        vu_height = vu_bottom - vu_top

        # Track background
        draw.rounded_rectangle(
            [(vu_x, vu_top), (vu_x + vu_bar_width, vu_bottom)],
            radius=vu_bar_width // 2,
            fill=(60, 60, 60, 200)
        )

        # Fill proportional to peak audio level
        fill_h = int(vu_height * pk)
        if fill_h > 0:
            if self._is_muted:
                vu_color = (180, 40, 40, 230)
            elif pk <= 0.5:
                vu_color = (50, 200, 50, 220)
            elif pk <= 0.8:
                vu_color = (220, 200, 30, 220)
            else:
                vu_color = (220, 50, 30, 220)

            draw.rounded_rectangle(
                [(vu_x, vu_bottom - fill_h), (vu_x + vu_bar_width, vu_bottom)],
                radius=vu_bar_width // 2,
                fill=vu_color
            )

        del draw
        return img

    async def on_pulse_device_change(self, *args, **kwargs):
        await super().on_pulse_device_change(*args, **kwargs)
        if self.selected_device is not None:
            try:
                device = get_device(self.device_filter, self.selected_device.pulse_name)
                if device is not None:
                    self._is_muted = bool(device.mute)
                    self._cached_volume = device.volume.value_flat
                    self._update_display()
            except Exception:
                pass

    def display_volume(self):
        if not self.device_filter or not self.selected_device:
            return "N/A"
        vol_pct = int(self._cached_volume * 100)
        return str(vol_pct)

    def display_adjustment(self):
        if self._is_muted:
            return "Muted"
        return f"{"+" if self.adjust > 0 else ""}{self.adjust}"
