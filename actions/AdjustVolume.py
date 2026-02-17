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
    SCROLL_ICON_TICKS = 4

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.icon_keys = [Icons.VOLUME_UP, Icons.VOLUME_DOWN, Icons.MUTED, Icons.UNMUTED]

        self.adjust: int = 1
        self.bounds = 100

        self._scroll_direction = None  # "up", "down", or None
        self._scroll_ticks_remaining = 0
        self._is_muted = False
        self._lock = threading.Lock()

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
        self.display_icon()
        self.display_device_info()

    def event_adjust_volume_negative(self, event):
        with self._lock:
            self._scroll_direction = "down"
            self._scroll_ticks_remaining = self.SCROLL_ICON_TICKS
        self.adjust_volume(-1)
        self.display_icon()
        self.display_device_info()

    def event_toggle_mute(self, event):
        if self.selected_device is None:
            self.show_error(1)
            return

        try:
            device = get_device(self.device_filter, self.selected_device.pulse_name)
            self._is_muted = not device.mute
            mute(device, self._is_muted)
            self.display_icon()
            self.display_device_info()
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

    def on_tick(self):
        super().on_tick()
        with self._lock:
            if self._scroll_ticks_remaining > 0:
                self._scroll_ticks_remaining -= 1
                if self._scroll_ticks_remaining == 0:
                    self._scroll_direction = None
                    self.display_icon()

        # Sync mute state from PulseAudio
        if self.selected_device is not None:
            try:
                device = get_device(self.device_filter, self.selected_device.pulse_name)
                if device is not None:
                    self._is_muted = bool(device.mute)
            except Exception:
                pass

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
        # Pick the right icon based on scroll direction and mute state
        with self._lock:
            direction = self._scroll_direction

        if self._is_muted:
            icon_asset = self.get_icon(Icons.MUTED)
        elif direction == "up":
            icon_asset = self.get_icon(Icons.VOLUME_UP)
        elif direction == "down":
            icon_asset = self.get_icon(Icons.VOLUME_DOWN)
        else:
            # Default: based on adjustment sign
            if self.adjust >= 0:
                icon_asset = self.get_icon(Icons.VOLUME_UP)
            else:
                icon_asset = self.get_icon(Icons.VOLUME_DOWN)

        if not icon_asset:
            return

        _, rendered = icon_asset.get_values()
        if not rendered:
            return

        # Get current volume for the bar
        volume_frac = 0.0
        if self.selected_device is not None:
            try:
                volumes = get_volumes_from_device(self.device_filter, self.selected_device.pulse_name)
                if volumes:
                    volume_frac = volumes[0] / 100.0
            except Exception:
                pass

        # Composite the icon with a volume bar
        composite = self._render_with_volume_bar(rendered, volume_frac)
        self.set_media(image=composite)

    def _render_with_volume_bar(self, icon_image: Image.Image, volume: float) -> Image.Image:
        """Overlay a volume bar onto the bottom of the icon image."""
        img = icon_image.copy().convert("RGBA")
        w, h = img.size
        draw = ImageDraw.Draw(img)

        bar_height = max(4, h // 20)
        bar_margin = max(4, w // 12)
        bar_y = h - bar_height - 2

        # Track background
        draw.rounded_rectangle(
            [(bar_margin, bar_y), (w - bar_margin, bar_y + bar_height)],
            radius=bar_height // 2,
            fill=(60, 60, 60, 200)
        )

        # Bar fill
        bar_width = w - 2 * bar_margin
        fill_width = int(bar_width * min(1.0, max(0.0, volume)))
        if fill_width > 0:
            if self._is_muted:
                bar_color = (180, 40, 40, 230)
            else:
                r = min(255, int(volume * 2 * 255))
                g = min(255, int((1 - volume * 0.5) * 255))
                bar_color = (r, g, 50, 230)

            draw.rounded_rectangle(
                [(bar_margin, bar_y), (bar_margin + fill_width, bar_y + bar_height)],
                radius=bar_height // 2,
                fill=bar_color
            )

        del draw
        return img

    async def on_pulse_device_change(self, *args, **kwargs):
        await super().on_pulse_device_change(*args, **kwargs)
        # Sync mute state on external changes
        if self.selected_device is not None:
            try:
                device = get_device(self.device_filter, self.selected_device.pulse_name)
                if device is not None:
                    self._is_muted = bool(device.mute)
                    self.display_icon()
            except Exception:
                pass

    def display_adjustment(self):
        if self._is_muted:
            return "Muted"
        return f"{"+" if self.adjust > 0 else ""}{self.adjust}"
