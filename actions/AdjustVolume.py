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


class AdjustVolume(AudioCore):
    # Ticks before +/- icon reverts to plain speaker
    SCROLL_ICON_TICKS = 15
    # Refresh VU bars every N ticks (~100ms each → 5 = ~2x/sec)
    VU_REFRESH_INTERVAL = 5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.icon_keys = [Icons.VOLUME_UP, Icons.VOLUME_DOWN, Icons.MUTED, Icons.UNMUTED]

        self.adjust: int = 1
        self.bounds = 100

        self._scroll_direction = None  # "up", "down", or None
        self._scroll_ticks_remaining = 0
        self._is_muted = False
        self._cached_volume = 0.0  # 0.0 - 1.0+
        self._lock = threading.Lock()
        self._vu_tick_counter = 0

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

        # Throttled periodic VU refresh (~2x/sec)
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

        # Pick icon: muted > scroll direction > idle (plain speaker)
        if self._is_muted:
            icon_asset = self.get_icon(Icons.MUTED)
        elif direction == "up":
            icon_asset = self.get_icon(Icons.VOLUME_UP)
        elif direction == "down":
            icon_asset = self.get_icon(Icons.VOLUME_DOWN)
        else:
            # Idle: plain speaker icon
            icon_asset = self.get_icon(Icons.UNMUTED)

        if not icon_asset:
            return

        _, rendered = icon_asset.get_values()
        if not rendered:
            return

        composite = self._render_overlays(rendered, self._cached_volume)
        self.set_media(image=composite)

    def _render_overlays(self, icon_image: Image.Image, volume: float) -> Image.Image:
        """Render the icon with a horizontal volume bar and vertical VU bars."""
        img = icon_image.copy().convert("RGBA")
        w, h = img.size
        draw = ImageDraw.Draw(img)

        vol = min(1.0, max(0.0, volume))

        # --- Horizontal volume bar at bottom ---
        bar_height = max(6, h // 12)
        bar_margin = max(4, w // 14)
        bar_y = h - bar_height - 2

        # Track background
        draw.rounded_rectangle(
            [(bar_margin, bar_y), (w - bar_margin, bar_y + bar_height)],
            radius=bar_height // 2,
            fill=(60, 60, 60, 200)
        )

        # Bar fill
        bar_width = w - 2 * bar_margin
        fill_width = int(bar_width * vol)
        if fill_width > 0:
            if self._is_muted:
                bar_color = (180, 40, 40, 230)
            else:
                r = min(255, int(vol * 2 * 255))
                g = min(255, int((1 - vol * 0.5) * 255))
                bar_color = (r, g, 50, 230)

            draw.rounded_rectangle(
                [(bar_margin, bar_y), (bar_margin + fill_width, bar_y + bar_height)],
                radius=bar_height // 2,
                fill=bar_color
            )

        # --- Vertical VU bars on the right side ---
        num_bars = 5
        vu_bar_width = max(3, w // 25)
        vu_gap = max(1, vu_bar_width // 3)
        vu_right_margin = bar_margin
        vu_total_width = num_bars * vu_bar_width + (num_bars - 1) * vu_gap
        vu_x_start = w - vu_right_margin - vu_total_width

        vu_top = max(4, h // 8)
        vu_bottom = bar_y - 4
        vu_height = vu_bottom - vu_top

        for i in range(num_bars):
            # Each bar represents a volume threshold
            threshold = (i + 1) / num_bars
            bar_x = vu_x_start + i * (vu_bar_width + vu_gap)

            if self._is_muted:
                # All bars dark when muted
                fill = (60, 40, 40, 150)
                filled_h = vu_height
            elif vol >= threshold:
                # Lit bar - height proportional to its threshold
                filled_h = vu_height
                if threshold <= 0.5:
                    fill = (50, 200, 50, 220)   # Green
                elif threshold <= 0.8:
                    fill = (220, 200, 30, 220)   # Yellow
                else:
                    fill = (220, 50, 30, 220)    # Red
            else:
                # Dim unlit bar
                fill = (60, 60, 60, 100)
                filled_h = vu_height

            draw.rounded_rectangle(
                [(bar_x, vu_top + vu_height - filled_h),
                 (bar_x + vu_bar_width, vu_bottom)],
                radius=1,
                fill=fill
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
