from enum import StrEnum

class Icons(StrEnum):
    MAIN = "main-icon"
    MUTED = "mute"
    UNMUTED = "audio"
    VOLUME_UP = "vol-up"
    VOLUME_DOWN = "vol-down"
    AUDIO_OFF = "audio-off"
    AUDIO_LOW = "audio-low"
    AUDIO_MEDIUM_LOW = "audio-medium-low"
    AUDIO_MEDIUM = "audio-medium"
    AUDIO_HIGH = "audio-high"
    AUDIO_MAX = "audio-max"
    SPEAKER_DEFAULT = "speaker-default"
    HEADPHONE_DEFAULT = "headphone-default"
    NONE_DEFAULT = "none-default"

class Colors(StrEnum):
    VOLUME_OK = "volume-ok"
    VOLUME_WARNING = "volume-warning"